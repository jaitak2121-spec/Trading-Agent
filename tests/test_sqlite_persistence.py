"""Transactional SQLite persistence across the execution gateway."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from trading.adapters.memory import SimulatedBroker
from trading.adapters.persistence import (
    SQLiteAuditSink,
    SQLiteOrderStore,
    SQLitePortfolio,
    SQLitePositionLedger,
    SQLiteReservationRepository,
    SQLiteRiskEngine,
    SQLiteState,
)
from trading.core.audit import AuditCategory, AuditLog
from trading.core.authz import Principal, Role
from trading.core.breaker import BreakerRegistry, CircuitBreaker
from trading.core.clock import ManualClock
from trading.core.config import RiskConfig, TradingConfig
from trading.core.dedupe import IdempotencyRegistry, ReservationState
from trading.core.gateway import ExecutionGateway
from trading.core.killswitch import KillSwitch
from trading.core.modes import TradingMode, TradingModeMachine
from trading.core.money import USD, Money, Quantity
from trading.core.orders import OrderIntent, OrderSide, OrderState
from trading.core.reconciliation import ReconciliationGate
from trading.core.risk import RiskEngine
from trading.ports.broker import AckOutcome, BrokerAck

from .harness import DEFAULT_PRICE, SYMBOL


class FailingSQLiteAuditSink(SQLiteAuditSink):
    def __init__(self, state: SQLiteState, *, fail_action: str) -> None:
        super().__init__(state)
        self.fail_action = fail_action

    def emit(self, record) -> None:
        if record.action == self.fail_action:
            raise RuntimeError("injected audit failure")
        super().emit(record)


class SQLiteRig:
    def __init__(self, path: Path, *, fail_action: str | None = None) -> None:
        self.path = path
        self.clock = ManualClock()
        self.state = SQLiteState(path)
        self.sink = (
            SQLiteAuditSink(self.state)
            if fail_action is None
            else FailingSQLiteAuditSink(self.state, fail_action=fail_action)
        )
        self.audit = AuditLog(
            self.sink, clock=self.clock, initial_records=self.sink.records
        )
        self.orders = SQLiteOrderStore(self.state, clock=self.clock)
        self.reservations = SQLiteReservationRepository(self.state)
        self.ledger = SQLitePositionLedger(self.state)
        self.portfolio = SQLitePortfolio(
            Money("1000000.00", USD), ledger=self.ledger, state=self.state
        )
        self.reconciliation = ReconciliationGate(
            self.ledger, self.orders, self.audit, clock=self.clock,
            max_staleness_seconds=300.0,
        )
        self.config = TradingConfig(risk=RiskConfig())
        self.strategy = Principal("strategy-1", Role.STRATEGY)
        self.risk_id = Principal("risk-1", Role.RISK_MANAGER)
        self.gateway_id = Principal("gateway-1", Role.EXECUTION_GATEWAY)
        self.operator = Principal("operator-1", Role.OPERATOR)
        self.risk = SQLiteRiskEngine(
            self.state, self.config.risk, identity=self.risk_id, order_store=self.orders,
            audit=self.audit, clock=self.clock,
        )
        self.dedupe = IdempotencyRegistry(
            self.audit, clock=self.clock, repository=self.reservations
        )
        self.kill_switch = KillSwitch(
            self.audit, clock=self.clock, presence_probe=lambda _path: False
        )
        self.breakers = BreakerRegistry()
        self.breakers.add(
            CircuitBreaker("broker", clock=self.clock, audit=self.audit)
        )
        self.modes = TradingModeMachine(self.config, self.audit)
        self.modes.transition_to(
            TradingMode.PAPER, actor=self.operator.principal_id, reason="test"
        )
        self.broker = SimulatedBroker(
            clock=self.clock,
            default_outcome=AckOutcome.FILLED,
            fill_prices={SYMBOL: DEFAULT_PRICE},
        )
        self.gateway = ExecutionGateway(
            identity=self.gateway_id,
            broker=self.broker,
            orders=self.orders,
            positions=self.portfolio,
            reconciliation=self.reconciliation,
            risk=self.risk,
            dedupe=self.dedupe,
            kill_switch=self.kill_switch,
            breakers=self.breakers,
            modes=self.modes,
            config=self.config,
            audit=self.audit,
            clock=self.clock,
            transaction_factory=self.state.transaction,
        )

    def submit(
        self, *, signal_id: str = "signal-1", side: OrderSide = OrderSide.BUY
    ):
        intent = OrderIntent(
            strategy_id="strategy-1", signal_id=signal_id, symbol=SYMBOL,
            side=side, quantity=Quantity("0.001", "BTC"),
        )
        return self.gateway.submit(
            intent, proposer=self.strategy, mark_prices={SYMBOL: DEFAULT_PRICE}
        )


class TestSQLiteTransaction(unittest.TestCase):
    def setUp(self) -> None:
        self.path = Path(tempfile.mkdtemp()) / "trading.sqlite3"

    def test_explicit_transaction_rolls_back_all_writes(self) -> None:
        state = SQLiteState(self.path)
        self.addCleanup(state.close)
        with self.assertRaises(RuntimeError):
            with state.transaction():
                state.connection.execute(
                    "INSERT INTO positions VALUES('BTCUSD', '1', 'BTC')"
                )
                raise RuntimeError("crash")
        with state.read() as connection:
            count = connection.execute("SELECT COUNT(*) FROM positions").fetchone()[0]
        self.assertEqual(count, 0)

    def test_successful_fill_round_trips_every_persisted_component(self) -> None:
        rig = SQLiteRig(self.path)
        result = rig.submit()
        self.assertEqual(result.outcome, "executed")
        order_id = result.order.order_id
        key = result.order.idempotency_key
        expected_cash = rig.portfolio.cash
        expected_audit_count = rig.audit.count
        rig.state.close()

        state = SQLiteState(self.path)
        self.addCleanup(state.close)
        orders = SQLiteOrderStore(state, clock=ManualClock())
        reservations = SQLiteReservationRepository(state)
        ledger = SQLitePositionLedger(state)
        portfolio = SQLitePortfolio(
            Money("1000000.00", USD), ledger=ledger, state=state
        )
        sink = SQLiteAuditSink(state)

        self.assertEqual(orders.get(order_id).state, OrderState.FILLED)
        self.assertEqual(
            reservations.get(key).state, ReservationState.SETTLED
        )
        self.assertEqual(
            ledger.position(SYMBOL, asset="BTC"), Quantity("0.001", "BTC")
        )
        self.assertEqual(portfolio.cash, expected_cash)
        self.assertEqual(
            portfolio.position(SYMBOL, asset="BTC").average_entry_price,
            DEFAULT_PRICE,
        )
        self.assertEqual(len(sink.records), expected_audit_count)
        AuditLog(clock=ManualClock()).verify(sink.records)

    def test_failed_final_audit_rolls_settlement_back_to_ambiguous_checkpoint(self) -> None:
        rig = SQLiteRig(self.path, fail_action="gateway.executed")
        with self.assertRaisesRegex(RuntimeError, "injected audit failure"):
            rig.submit()
        rig.state.close()

        state = SQLiteState(self.path)
        self.addCleanup(state.close)
        orders = SQLiteOrderStore(state, clock=ManualClock())
        reservations = SQLiteReservationRepository(state)
        ledger = SQLitePositionLedger(state)

        restored = orders.all_orders()[0]
        self.assertEqual(restored.state, OrderState.PENDING_NEW)
        self.assertEqual(
            reservations.get(restored.idempotency_key).state,
            ReservationState.SUBMITTED,
        )
        self.assertTrue(ledger.position(SYMBOL, asset="BTC").is_zero)
        self.assertNotIn(
            "gateway.executed",
            [record.action for record in SQLiteAuditSink(state).records],
        )

    def test_unknown_order_and_reservation_commit_together(self) -> None:
        rig = SQLiteRig(self.path)
        rig.broker.script(
            BrokerAck(AckOutcome.UNCERTAIN),
            lands_at_venue=True,
        )
        result = rig.submit()
        rig.state.close()

        state = SQLiteState(self.path)
        self.addCleanup(state.close)
        order = SQLiteOrderStore(state, clock=ManualClock()).get(result.order.order_id)
        reservation = SQLiteReservationRepository(state).get(order.idempotency_key)

        self.assertEqual(order.state, OrderState.UNKNOWN)
        self.assertEqual(reservation.state, ReservationState.UNKNOWN)
        self.assertTrue(SQLitePositionLedger(state).position(SYMBOL, asset="BTC").is_zero)

    def test_cash_basis_and_realized_pnl_survive_a_round_trip(self) -> None:
        rig = SQLiteRig(self.path)
        rig.submit(signal_id="entry", side=OrderSide.BUY)
        rig.submit(signal_id="exit", side=OrderSide.SELL)
        realized = rig.portfolio.realized()
        cash = rig.portfolio.cash
        rig.state.close()

        state = SQLiteState(self.path)
        self.addCleanup(state.close)
        ledger = SQLitePositionLedger(state)
        portfolio = SQLitePortfolio(
            Money("1000000.00", USD), ledger=ledger, state=state
        )

        self.assertTrue(ledger.position(SYMBOL, asset="BTC").is_zero)
        self.assertEqual(portfolio.cash, cash)
        self.assertEqual(portfolio.realized(), realized)

    def test_audit_chain_resumes_after_restart(self) -> None:
        rig = SQLiteRig(self.path)
        before = rig.audit.count
        rig.state.close()

        state = SQLiteState(self.path)
        self.addCleanup(state.close)
        sink = SQLiteAuditSink(state)
        log = AuditLog(sink, clock=ManualClock(), initial_records=sink.records)
        log.record(
            AuditCategory.SYSTEM,
            "restart.record",
        )

        self.assertEqual(log.count, before + 1)
        log.verify(sink.records)

    def test_daily_pnl_and_rate_window_survive_restart(self) -> None:
        rig = SQLiteRig(self.path)
        rig.risk.pnl.record(Money("-25.00", USD))
        rig.risk.record_submission()
        rig.state.close()

        state = SQLiteState(self.path)
        self.addCleanup(state.close)
        sink = SQLiteAuditSink(state)
        audit = AuditLog(sink, clock=rig.clock, initial_records=sink.records)
        orders = SQLiteOrderStore(state, clock=rig.clock)
        restored = SQLiteRiskEngine(
            state,
            rig.config.risk,
            identity=rig.risk_id,
            order_store=orders,
            audit=audit,
            clock=rig.clock,
        )

        self.assertEqual(restored.pnl.realized, Money("-25.00", USD))
        self.assertEqual(restored.submissions_in_window(), 1)


if __name__ == "__main__":
    unittest.main()
