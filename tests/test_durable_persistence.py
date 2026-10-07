"""Tests for durable, crash-safe persistence -- reservations and positions.

Two layers are exercised. The unit tests prove the adapters round-trip through a
real file, fail loud on a corrupt or unrecognised file rather than starting
empty, and leave no torn temp file behind. The end-to-end test is the one that
matters: it wires the durable adapters into a full gateway stack, drives an order
to UNKNOWN, then **rebuilds the entire stack from the same files with a fresh,
empty order store** -- a real process restart, not a shared-object rebuild -- and
proves the safety state survived the crossing:

* INVARIANT 12 -- the claimed key still refuses a duplicate of the same intent.
* INVARIANT 5  -- the persisted UNKNOWN reservation still blocks *any* new order,
  even though the order object behind it did not survive. This is the gateway's
  reservation-level UNKNOWN backstop doing its job; without durable orders it is
  the only thing that can carry the block across the boundary.

The last part shows the system is not then wedged: an operator clears the UNKNOWN
at the reservation layer (no Order object required) and submissions flow again.
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from typing import Mapping

from trading.adapters.memory import SimulatedBroker
from trading.adapters.persistence import (
    DurablePositionLedger,
    DurableReservationRepository,
    OrderStore,
    PersistenceError,
)
from trading.core.audit import AuditLog, InMemoryAuditSink
from trading.core.authz import Principal, Role
from trading.core.breaker import BreakerRegistry, CircuitBreaker
from trading.core.clock import ManualClock
from trading.core.config import RiskConfig, TradingConfig
from trading.core.dedupe import (
    IdempotencyRegistry,
    Reservation,
    ReservationState,
)
from trading.core.gateway import ExecutionGateway
from trading.core.killswitch import KillSwitch
from trading.core.modes import TradingMode, TradingModeMachine
from trading.core.money import USD, Money, Price, Quantity
from trading.core.orders import OrderIntent, OrderSide, OrderType
from trading.core.portfolio import Portfolio
from trading.core.reconciliation import ReconciliationGate
from trading.core.risk import RiskEngine
from trading.ports.broker import AckOutcome, BrokerAck
from trading.ports.repository import PositionRepositoryPort
from trading.ports.reservation_repository import ReservationRepositoryPort

from .harness import ASSET, DEFAULT_PRICE, DEFAULT_QUANTITY, SYMBOL


def _tmpdir() -> Path:
    return Path(tempfile.mkdtemp())


# -- unit: reservations --------------------------------------------------------


class TestDurableReservationRepository(unittest.TestCase):
    def setUp(self) -> None:
        self.path = _tmpdir() / "reservations.json"

    def reservation(self, key: str, state: ReservationState) -> Reservation:
        return Reservation(
            key=key, order_id=f"ord-{key}", state=state,
            reserved_at="t0", updated_at="t1",
        )

    def test_it_satisfies_the_port(self) -> None:
        repo = DurableReservationRepository(self.path)
        self.assertIsInstance(repo, ReservationRepositoryPort)

    def test_a_reservation_survives_a_reload(self) -> None:
        repo = DurableReservationRepository(self.path)
        repo.add(self.reservation("k1", ReservationState.RESERVED))
        # A brand-new instance reads the file -- the simulated restart.
        reloaded = DurableReservationRepository(self.path)
        self.assertEqual(reloaded.get("k1").state, ReservationState.RESERVED)

    def test_an_update_survives_a_reload(self) -> None:
        repo = DurableReservationRepository(self.path)
        repo.add(self.reservation("k1", ReservationState.RESERVED))
        repo.update(self.reservation("k1", ReservationState.SUBMITTED))
        reloaded = DurableReservationRepository(self.path)
        self.assertEqual(reloaded.get("k1").state, ReservationState.SUBMITTED)

    def test_an_unknown_reservation_and_has_unknown_survive(self) -> None:
        """The INVARIANT 5 cornerstone: the block must come back after a restart."""
        repo = DurableReservationRepository(self.path)
        repo.add(self.reservation("k1", ReservationState.UNKNOWN))
        reloaded = DurableReservationRepository(self.path)
        self.assertTrue(reloaded.has_unknown())
        self.assertEqual(len(reloaded.unknown_reservations()), 1)

    def test_a_delete_survives_a_reload(self) -> None:
        repo = DurableReservationRepository(self.path)
        repo.add(self.reservation("k1", ReservationState.RESERVED))
        repo.delete("k1")
        reloaded = DurableReservationRepository(self.path)
        self.assertIsNone(reloaded.get("k1"))

    def test_a_missing_file_is_a_fresh_empty_start(self) -> None:
        repo = DurableReservationRepository(self.path)
        self.assertEqual(len(repo), 0)
        self.assertFalse(repo.has_unknown())

    def test_a_corrupt_file_fails_loud(self) -> None:
        self.path.write_text("{ not valid json", encoding="utf-8")
        with self.assertRaises(PersistenceError):
            DurableReservationRepository(self.path)

    def test_an_unrecognised_schema_fails_loud(self) -> None:
        self.path.write_text('{"version": 999, "reservations": []}', encoding="utf-8")
        with self.assertRaises(PersistenceError):
            DurableReservationRepository(self.path)

    def test_a_malformed_entry_fails_loud(self) -> None:
        self.path.write_text(
            '{"version": 1, "reservations": [{"key": "k1"}]}', encoding="utf-8"
        )
        with self.assertRaises(PersistenceError):
            DurableReservationRepository(self.path)

    def test_a_non_list_reservations_field_fails_loud(self) -> None:
        self.path.write_text(
            '{"version": 1, "reservations": {"not": "a list"}}', encoding="utf-8"
        )
        with self.assertRaises(PersistenceError):
            DurableReservationRepository(self.path)

    def test_a_non_object_reservation_entry_fails_loud(self) -> None:
        self.path.write_text(
            '{"version": 1, "reservations": ["just a string"]}', encoding="utf-8"
        )
        with self.assertRaises(PersistenceError):
            DurableReservationRepository(self.path)

    def test_an_unknown_state_value_fails_loud(self) -> None:
        self.path.write_text(
            '{"version": 1, "reservations": [{"key": "k1", "order_id": "o1", '
            '"state": "levitating", "reserved_at": "t", "updated_at": "t"}]}',
            encoding="utf-8",
        )
        with self.assertRaises(PersistenceError):
            DurableReservationRepository(self.path)

    def test_no_torn_temp_file_is_left_behind(self) -> None:
        repo = DurableReservationRepository(self.path)
        repo.add(self.reservation("k1", ReservationState.RESERVED))
        leftovers = list(self.path.parent.glob("*.tmp"))
        self.assertEqual(leftovers, [])


# -- unit: positions -----------------------------------------------------------


class TestDurablePositionLedger(unittest.TestCase):
    def setUp(self) -> None:
        self.path = _tmpdir() / "positions.json"

    def test_it_satisfies_the_port(self) -> None:
        ledger = DurablePositionLedger(self.path)
        self.assertIsInstance(ledger, PositionRepositoryPort)

    def test_a_fill_survives_a_reload(self) -> None:
        ledger = DurablePositionLedger(self.path)
        ledger.apply_fill(SYMBOL, OrderSide.BUY, Quantity("0.5", ASSET))
        reloaded = DurablePositionLedger(self.path)
        self.assertEqual(reloaded.position(SYMBOL, asset=ASSET), Quantity("0.5", ASSET))

    def test_set_position_survives_a_reload(self) -> None:
        ledger = DurablePositionLedger(self.path)
        ledger.set_position(SYMBOL, Quantity("-1.25", ASSET))
        reloaded = DurablePositionLedger(self.path)
        self.assertEqual(
            reloaded.position(SYMBOL, asset=ASSET).amount, Quantity("-1.25", ASSET).amount
        )

    def test_accumulated_fills_survive_exactly(self) -> None:
        ledger = DurablePositionLedger(self.path)
        ledger.apply_fill(SYMBOL, OrderSide.BUY, Quantity("0.3", ASSET))
        ledger.apply_fill(SYMBOL, OrderSide.BUY, Quantity("0.4", ASSET))
        ledger.apply_fill(SYMBOL, OrderSide.SELL, Quantity("0.1", ASSET))
        reloaded = DurablePositionLedger(self.path)
        self.assertEqual(
            reloaded.position(SYMBOL, asset=ASSET), Quantity("0.6", ASSET)
        )

    def test_a_corrupt_file_fails_loud(self) -> None:
        self.path.write_text("not json at all", encoding="utf-8")
        with self.assertRaises(PersistenceError):
            DurablePositionLedger(self.path)

    def test_a_non_object_positions_field_fails_loud(self) -> None:
        self.path.write_text(
            '{"version": 1, "positions": ["not an object"]}', encoding="utf-8"
        )
        with self.assertRaises(PersistenceError):
            DurablePositionLedger(self.path)

    def test_a_non_object_position_entry_fails_loud(self) -> None:
        self.path.write_text(
            '{"version": 1, "positions": {"BTCUSD": "not an object"}}', encoding="utf-8"
        )
        with self.assertRaises(PersistenceError):
            DurablePositionLedger(self.path)

    def test_a_malformed_position_amount_fails_loud(self) -> None:
        self.path.write_text(
            '{"version": 1, "positions": {"BTCUSD": {"amount": "xyz", "asset": "BTC"}}}',
            encoding="utf-8",
        )
        with self.assertRaises(PersistenceError):
            DurablePositionLedger(self.path)

    def test_a_position_entry_missing_fields_fails_loud(self) -> None:
        self.path.write_text(
            '{"version": 1, "positions": {"BTCUSD": {"asset": "BTC"}}}', encoding="utf-8"
        )
        with self.assertRaises(PersistenceError):
            DurablePositionLedger(self.path)

    def test_a_missing_file_is_a_fresh_empty_start(self) -> None:
        ledger = DurablePositionLedger(self.path)
        self.assertEqual(ledger.symbols(), [])


# -- end to end: a real on-disk restart ----------------------------------------


class DurableStackFixture(unittest.TestCase):
    """Builds a full gateway stack around a given set of repositories.

    The order store is always a fresh in-memory one, because orders are not
    durable (see durable.py). The reservation and position stores are handed in,
    so a test can rebuild the stack around the *same files* to simulate a crash
    and restart.
    """

    def setUp(self) -> None:
        self.dir = _tmpdir()
        self.rpath = self.dir / "reservations.json"
        self.ppath = self.dir / "positions.json"
        self.clock = ManualClock()
        self.strategy_id = Principal("strategy-1", Role.STRATEGY)
        self.risk_id = Principal("risk-1", Role.RISK_MANAGER)
        self.gateway_id = Principal("gateway-1", Role.EXECUTION_GATEWAY)
        self.operator_id = Principal("operator-1", Role.OPERATOR)

    def build_stack(self, *, broker: SimulatedBroker | None = None):
        """Construct a stack reading reservations/positions from disk."""
        sink = InMemoryAuditSink()
        audit = AuditLog(sink, clock=self.clock)
        orders = OrderStore()  # fresh: orders are not durable
        ledger = DurablePositionLedger(self.ppath)
        reservations = DurableReservationRepository(self.rpath)
        portfolio = Portfolio(Money("1000000.00", USD), ledger=ledger)
        reconciliation = ReconciliationGate(
            ledger, orders, audit, clock=self.clock, max_staleness_seconds=300.0
        )
        dedupe = IdempotencyRegistry(audit, clock=self.clock, repository=reservations)
        config = TradingConfig(live_trading=False, live_confirmation="", risk=RiskConfig())
        risk = RiskEngine(
            config.risk, identity=self.risk_id, order_store=orders,
            audit=audit, clock=self.clock,
        )
        kill_switch = KillSwitch(audit, clock=self.clock, presence_probe=lambda _p: False)
        breakers = BreakerRegistry()
        breakers.add(CircuitBreaker("broker", clock=self.clock, audit=audit, failure_threshold=3))
        modes = TradingModeMachine(config, audit)
        modes.transition_to(TradingMode.PAPER, actor=self.operator_id.principal_id, reason="t")
        if broker is None:
            broker = SimulatedBroker(
                clock=self.clock, default_outcome=AckOutcome.FILLED,
                fill_prices={SYMBOL: DEFAULT_PRICE},
            )
        gateway = ExecutionGateway(
            identity=self.gateway_id, broker=broker, orders=orders, positions=portfolio,
            reconciliation=reconciliation, risk=risk, dedupe=dedupe,
            kill_switch=kill_switch, breakers=breakers, modes=modes, config=config,
            audit=audit, clock=self.clock, token_ttl_seconds=30,
        )
        self.orders, self.ledger, self.reservations = orders, ledger, reservations
        self.dedupe, self.gateway, self.broker = dedupe, gateway, broker
        return gateway

    def intent(self, signal_id: str, side: OrderSide = OrderSide.BUY) -> OrderIntent:
        return OrderIntent(
            strategy_id="strat-1", signal_id=signal_id, symbol=SYMBOL,
            side=side, order_type=OrderType.MARKET, quantity=DEFAULT_QUANTITY,
        )

    def prices(self) -> Mapping[str, Price]:
        return {SYMBOL: DEFAULT_PRICE}

    def submit(self, signal_id: str, side: OrderSide = OrderSide.BUY):
        return self.gateway.submit(
            self.intent(signal_id, side), proposer=self.strategy_id,
            mark_prices=self.prices(),
        )


class TestDurableRestart(DurableStackFixture):
    def test_invariants_survive_a_real_on_disk_restart(self) -> None:
        # --- first process ---
        self.build_stack()
        # A filled order moves the durable position ledger.
        filled = self.submit("sig-fill")
        self.assertEqual(filled.outcome, "executed")

        # An order that lands behind an UNCERTAIN ack goes UNKNOWN, persisting an
        # UNKNOWN reservation to disk.
        self.broker.script(BrokerAck(AckOutcome.UNCERTAIN), lands_at_venue=True)
        unknown = self.submit("sig-unknown")
        self.assertEqual(unknown.outcome, "unknown")
        unknown_key = unknown.order.idempotency_key

        # --- restart: new stack, same files, FRESH (empty) order store ---
        self.build_stack()
        self.assertEqual(len(self.orders.all_orders()), 0, "orders are not durable")

        # Position survived the real on-disk round trip (INVARIANT 6 baseline).
        self.assertEqual(
            self.ledger.position(SYMBOL, asset=ASSET), DEFAULT_QUANTITY
        )

        # The UNKNOWN reservation survived (INVARIANT 5 state is on disk).
        self.assertTrue(self.reservations.has_unknown())

        # A brand-new, unrelated order is blocked at the reconciliation gate --
        # the reservation-level UNKNOWN backstop carrying the block across the
        # restart even though no UNKNOWN order object exists any more.
        blocked = self.submit("sig-new-after-restart")
        self.assertEqual(blocked.outcome, "refused")
        self.assertEqual(blocked.gate, "reconciliation")

        # Re-submitting the *same* intent that went UNKNOWN is refused earlier,
        # at the duplicate gate -- the claimed key survived too (INVARIANT 12).
        dup = self.submit("sig-unknown")
        self.assertEqual(dup.outcome, "refused")
        self.assertEqual(dup.gate, "duplicate_order")
        self.assertEqual(self.broker.duplicate_keys, frozenset())

    def test_operator_can_clear_the_block_at_the_reservation_layer(self) -> None:
        """Not wedged: the UNKNOWN clears at the key level, no Order needed."""
        self.build_stack()
        self.broker.script(BrokerAck(AckOutcome.UNCERTAIN), lands_at_venue=True)
        self.submit("sig-unknown")

        self.build_stack()  # restart
        self.assertTrue(self.reservations.has_unknown())
        # The operator reads the surviving UNKNOWN reservation and resolves it by
        # key -- the recovery path that does not depend on a durable Order.
        stuck = self.reservations.unknown_reservations()[0]
        self.dedupe.resolve_unknown(stuck.key, resolution="reconciled by hand after restart")
        self.assertFalse(self.reservations.has_unknown())

        # Submissions flow again.
        ok = self.submit("sig-after-clear")
        self.assertEqual(ok.outcome, "executed")

    def test_the_resolution_is_itself_durable(self) -> None:
        """Clearing the UNKNOWN must also survive a subsequent restart."""
        self.build_stack()
        self.broker.script(BrokerAck(AckOutcome.UNCERTAIN), lands_at_venue=True)
        self.submit("sig-unknown")

        self.build_stack()  # restart 1
        stuck = self.reservations.unknown_reservations()[0]
        self.dedupe.resolve_unknown(stuck.key, resolution="cleared")

        self.build_stack()  # restart 2
        self.assertFalse(self.reservations.has_unknown())
        ok = self.submit("sig-fresh")
        self.assertEqual(ok.outcome, "executed")


if __name__ == "__main__":
    unittest.main()
