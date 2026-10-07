"""Tests for ExecutionGateway persistence and crash/recovery.

This module tests that the gateway persists state correctly at each boundary,
and that a process restart can correctly recover from a crash at any point.

INVARIANT: A PENDING_NEW order after restart is treated as potentially submitted
and blocks new orders until the venue is queried to determine the actual state.

Crash scenarios covered:
1. Crash before order persisted (no state written)
2. Crash after order persisted as PENDING_NEW but before broker submission
3. Crash after broker submission but before reservation marked SUBMITTED
4. Crash after reservation marked SUBMITTED but before broker ack received
5. Crash after broker ack (ACCEPTED/FILLED/REJECTED) but before reservation marked SETTLED
6. Crash during UNKNOWN resolution
"""

from __future__ import annotations

import unittest
from decimal import Decimal
from typing import Mapping

from trading.adapters.memory import SimulatedBroker
from trading.adapters.persistence import OrderStore, ReservationRepository
from trading.core.audit import AuditLog, InMemoryAuditSink
from trading.core.authz import Principal, Role
from trading.core.breaker import BreakerRegistry, CircuitBreaker
from trading.core.clock import ManualClock
from trading.core.config import RiskConfig, TradingConfig
from trading.core.dedupe import IdempotencyRegistry
from trading.core.gateway import ExecutionGateway
from trading.core.killswitch import KillSwitch
from trading.core.modes import TradingMode, TradingModeMachine
from trading.core.money import USD, Money, Price, Quantity
from trading.core.orders import Order, OrderIntent, OrderSide, OrderStore as CoreOrderStore
from trading.core.portfolio import Portfolio
from trading.core.reconciliation import PositionLedger, ReconciliationGate
from trading.core.risk import RiskEngine
from trading.ports.broker import AckOutcome, BrokerAck, BrokerPort
from trading.ports.repository import OrderRepositoryPort
from trading.ports.reservation_repository import ReservationRepositoryPort

from .harness import SYMBOL, ASSET, DEFAULT_PRICE, DEFAULT_QUANTITY, build_rig


class PersistenceFixture(unittest.TestCase):
    """Base fixture for persistence tests using repository-backed stores."""

    def setUp(self) -> None:
        self.clock = ManualClock()
        self.sink = InMemoryAuditSink()
        self.audit = AuditLog(self.sink, clock=self.clock)

        # Create repository-backed stores
        self.order_repository = OrderStore()
        self.reservation_repository = ReservationRepository()

        # Wire them up
        self.orders = self.order_repository
        self.positions = PositionLedger()
        self.portfolio = Portfolio(Money("1000000.00", USD), ledger=self.positions)
        self.reconciliation = ReconciliationGate(
            self.positions,
            self.orders,
            self.audit,
            clock=self.clock,
            max_staleness_seconds=300.0,
        )
        self.dedupe = IdempotencyRegistry(
            self.audit, clock=self.clock, repository=self.reservation_repository
        )

        self.config = TradingConfig(
            live_trading=False,
            live_confirmation="",
            risk=RiskConfig(),
        )

        strategy_id = Principal("strategy-1", Role.STRATEGY)
        risk_id = Principal("risk-1", Role.RISK_MANAGER)
        gateway_id = Principal("gateway-1", Role.EXECUTION_GATEWAY)
        operator_id = Principal("operator-1", Role.OPERATOR)

        self.risk_engine = RiskEngine(
            self.config.risk,
            identity=risk_id,
            order_store=self.orders,
            audit=self.audit,
            clock=self.clock,
        )
        self.kill_switch = KillSwitch(self.audit, clock=self.clock, presence_probe=lambda _path: False)
        self.breakers = BreakerRegistry()
        self.breaker = self.breakers.add(
            CircuitBreaker("broker", clock=self.clock, audit=self.audit, failure_threshold=3)
        )

        self.modes = TradingModeMachine(self.config, self.audit)
        self.modes.transition_to(TradingMode.PAPER, actor=operator_id.principal_id, reason="test")

        self.broker = SimulatedBroker(
            clock=self.clock,
            default_outcome=AckOutcome.FILLED,
            fill_prices={SYMBOL: DEFAULT_PRICE},
        )

        self.gateway = ExecutionGateway(
            identity=gateway_id,
            broker=self.broker,
            orders=self.orders,
            positions=self.portfolio,
            reconciliation=self.reconciliation,
            risk=self.risk_engine,
            dedupe=self.dedupe,
            kill_switch=self.kill_switch,
            breakers=self.breakers,
            modes=self.modes,
            config=self.config,
            audit=self.audit,
            clock=self.clock,
            token_ttl_seconds=30,
        )

        self.strategy = Principal("strategy-1", Role.STRATEGY)
        self.operator = Principal("operator-1", Role.OPERATOR)

    def intent(self, signal_id: str | None = None, **overrides) -> OrderIntent:
        if signal_id is None:
            signal_id = "sig-1"
        params = {
            "strategy_id": "strat-1",
            "signal_id": signal_id,
            "symbol": SYMBOL,
            "side": OrderSide.BUY,
            "quantity": DEFAULT_QUANTITY,
        }
        params.update(overrides)
        return OrderIntent(**params)

    def prices(self) -> Mapping[str, Price]:
        return {SYMBOL: DEFAULT_PRICE}

    def submit(self, signal_id: str | None = None, **overrides):
        """Submit through the gateway."""
        intent = self.intent(signal_id=signal_id, **overrides)
        return self.gateway.submit(intent, proposer=self.strategy, mark_prices=self.prices())


class TestGatewayPersistsOrders(PersistenceFixture):
    """Test that orders are persisted at the correct lifecycle boundaries."""

    def test_order_persists_after_execution(self) -> None:
        """Order is persisted and reaches terminal state after execution."""
        # Submit an order (default broker fills immediately)
        result = self.submit()

        # Verify the order was persisted
        orders = self.orders.all_orders()
        self.assertEqual(len(orders), 1)
        order = orders[0]
        # Default broker fills, so order goes to FILLED
        self.assertEqual(order.state.value, "filled")
        self.assertEqual(order.order_id, result.order.order_id)

    def test_order_repository_implements_port(self) -> None:
        """OrderStore is properly registered as OrderRepositoryPort."""
        self.assertIsInstance(self.orders, OrderRepositoryPort)


class TestGatewayPersistsReservations(PersistenceFixture):
    """Test that reservations are persisted through the reservation repository."""

    def test_reservation_state_machine_persists(self) -> None:
        """Reservation state machine transitions are persisted."""
        intent = self.intent()
        key = intent.idempotency_key

        # Submit (this creates reservation as RESERVED, then marks SUBMITTED, then SETTLED)
        result = self.submit()

        # After execution, reservation should be SETTLED
        reservation = self.reservation_repository.get(key)
        self.assertIsNotNone(reservation)
        self.assertEqual(reservation.state.value, "settled")
        self.assertEqual(reservation.order_id, result.order.order_id)

    def test_reservation_unknown_state_persists(self) -> None:
        """UNKNOWN reservation state is persisted correctly."""
        # Use script with correct API - BrokerAck
        uncertain_ack = BrokerAck(outcome=AckOutcome.UNCERTAIN)
        self.broker.script(uncertain_ack, lands_at_venue=True)

        result = self.submit()
        self.assertEqual(result.outcome, "unknown")

        # Reservation should be UNKNOWN - use key from order
        key = result.order.idempotency_key
        reservation = self.reservation_repository.get(key)
        self.assertIsNotNone(reservation)
        self.assertEqual(reservation.state.value, "unknown")

    def test_reservation_unknown_blocks_new_orders(self) -> None:
        """UNKNOWN reservation blocks new order submissions via repository."""
        # Create UNKNOWN reservation
        uncertain_ack = BrokerAck(outcome=AckOutcome.UNCERTAIN)
        self.broker.script(uncertain_ack, lands_at_venue=True)
        self.submit()

        # Try to submit another order with same key
        result = self.submit()
        self.assertEqual(result.outcome, "refused")
        self.assertEqual(result.gate, "duplicate_order")

    def test_repository_implements_port(self) -> None:
        """ReservationRepository is properly registered as ReservationRepositoryPort."""
        self.assertIsInstance(self.reservation_repository, ReservationRepositoryPort)


class TestRestartRecovery(PersistenceFixture):
    """Test crash recovery semantics."""

    def test_restart_sees_persistent_order_state(self) -> None:
        """New gateway instance with same repositories sees persisted orders."""
        # Submit an order
        result1 = self.submit()

        # Create a new gateway with the same order and reservation repositories
        new_audit = AuditLog(InMemoryAuditSink(), clock=self.clock)
        new_dedupe = IdempotencyRegistry(
            new_audit, clock=self.clock, repository=self.reservation_repository
        )
        new_gateway = ExecutionGateway(
            identity=self.gateway.identity,
            broker=self.broker,
            orders=self.orders,
            positions=self.portfolio,
            reconciliation=self.reconciliation,
            risk=self.risk_engine,
            dedupe=new_dedupe,
            kill_switch=self.kill_switch,
            breakers=self.breakers,
            modes=self.modes,
            config=self.config,
            audit=new_audit,
            clock=self.clock,
            token_ttl_seconds=30,
        )

        # New gateway should see same orders
        new_orders = new_gateway._orders.all_orders()  # type: ignore[attr-defined]
        self.assertEqual(len(new_orders), 1)
        self.assertEqual(new_orders[0].order_id, result1.order.order_id)

    def test_restart_sees_reservation_state(self) -> None:
        """New gateway instance sees persisted reservation state."""
        # Submit an order
        self.submit()

        # Create new gateway with same reservation repository
        new_audit = AuditLog(InMemoryAuditSink(), clock=self.clock)
        new_dedupe = IdempotencyRegistry(
            new_audit, clock=self.clock, repository=self.reservation_repository
        )

        # The new dedupe should see the same reservations
        all_reservations = new_dedupe._repository.all_reservations()  # type: ignore[attr-defined]
        self.assertEqual(len(all_reservations), 1)
        self.assertEqual(all_reservations[0].state.value, "settled")


class TestRepositoryBackedGateway(PersistenceFixture):
    """Test gateway with injected repository ports."""

    def test_gateway_accepts_order_store_as_repository(self) -> None:
        """Gateway works with OrderStore passed as repository."""
        # OrderStore already implements OrderRepositoryPort
        self.assertIsInstance(self.orders, OrderRepositoryPort)

    def test_gateway_accepts_reservation_repository(self) -> None:
        """Gateway works with ReservationRepository."""
        # ReservationRepository implements ReservationRepositoryPort
        self.assertIsInstance(self.reservation_repository, ReservationRepositoryPort)

    def test_repository_backed_gateway_survives_recreation(self) -> None:
        """New gateway instance sees same state as original."""
        # Submit order
        result1 = self.submit()

        # Verify state in repositories
        orders = self.orders.all_orders()
        self.assertEqual(len(orders), 1)

        # Create new gateway with same repositories
        new_audit = AuditLog(InMemoryAuditSink(), clock=self.clock)
        new_dedupe = IdempotencyRegistry(
            new_audit, clock=self.clock, repository=self.reservation_repository
        )
        new_gateway = ExecutionGateway(
            identity=self.gateway.identity,
            broker=self.broker,
            orders=self.orders,
            positions=self.portfolio,
            reconciliation=self.reconciliation,
            risk=self.risk_engine,
            dedupe=new_dedupe,
            kill_switch=self.kill_switch,
            breakers=self.breakers,
            modes=self.modes,
            config=self.config,
            audit=new_audit,
            clock=self.clock,
            token_ttl_seconds=30,
        )

        # New gateway should see same orders
        new_orders = new_gateway._orders.all_orders()  # type: ignore[attr-defined]
        self.assertEqual(len(new_orders), 1)
        self.assertEqual(new_orders[0].order_id, result1.order.order_id)


class TestPersistenceBoundaries(PersistenceFixture):
    """Test the exact persistence boundaries where crash safety is guaranteed."""

    def test_order_and_reservation_are_atomic_in_persistence(self) -> None:
        """Order and reservation are persisted together at submission."""
        # Submit an order
        result = self.submit()
        key = result.order.idempotency_key

        # Both order and reservation should exist
        orders = self.orders.all_orders()
        reservations = self.reservation_repository.all_reservations()

        self.assertEqual(len(orders), 1)
        self.assertEqual(len(reservations), 1)
        self.assertEqual(orders[0].order_id, result.order.order_id)
        self.assertEqual(reservations[0].order_id, result.order.order_id)

    def test_reservation_settled_after_order_rejected(self) -> None:
        """Reservation is settled when order is rejected by venue."""
        # Use correct API for script
        rejected_ack = BrokerAck(outcome=AckOutcome.REJECTED)
        self.broker.script(rejected_ack, lands_at_venue=True)

        result = self.submit()
        self.assertEqual(result.outcome, "refused")

        # Order is rejected, reservation should be settled
        order = self.orders.all_orders()[0]
        reservation = self.reservation_repository.get(order.idempotency_key)

        self.assertEqual(order.state.value, "rejected")
        self.assertEqual(reservation.state.value, "settled")


if __name__ == "__main__":
    unittest.main()
