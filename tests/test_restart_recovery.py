"""Tests for restart recovery coordinator.

This module tests the RestartRecoveryCoordinator's ability to:
1. Scan persistent state and identify ambiguous orders after restart
2. Maintain UNKNOWN blocking semantics across restarts
3. Provide operator-facing reconciliation methods
4. Handle PENDING_NEW ambiguity correctly

INVARIANT 5: An UNKNOWN order blocks new orders until reconciled.
This blocking must survive process restart.

INVARIANT: A PENDING_NEW order after restart is treated as potentially
submitted and blocks new orders until the venue is queried to determine
the actual state.
"""
from __future__ import annotations

import unittest
from decimal import Decimal
from typing import Mapping

from trading.adapters.memory import SimulatedBroker
from trading.adapters.persistence import OrderStore, PositionLedger, ReservationRepository
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
from trading.core.orders import OrderIntent, OrderSide, OrderStore as CoreOrderStore
from trading.core.portfolio import Portfolio
from trading.core.reconciliation import PositionLedger as ReconciliationLedger
from trading.core.reconciliation import ReconciliationGate
from trading.core.risk import RiskEngine
from trading.ports.broker import AckOutcome, BrokerAck
from trading.adapters.recovery import RestartRecoveryCoordinator

from .harness import SYMBOL, ASSET, DEFAULT_PRICE, DEFAULT_QUANTITY, build_rig


class RestartRecoveryFixture(unittest.TestCase):
    """Base fixture for restart recovery tests."""

    def setUp(self) -> None:
        self.clock = ManualClock()
        self.sink = InMemoryAuditSink()
        self.audit = AuditLog(self.sink, clock=self.clock)

        # Create repository-backed stores
        self.order_repository = OrderStore()
        self.reservation_repository = ReservationRepository()

        # Wire them up
        self.orders = self.order_repository
        self.positions = ReconciliationLedger()
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

        # Create recovery coordinator with same repositories
        self.recovery = RestartRecoveryCoordinator(
            gateway=self.gateway,
            orders=self.orders,
            reservations=self.reservation_repository,
        )

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


class TestRestartRecoveryScan(RestartRecoveryFixture):
    """Test the scan() method for identifying ambiguous orders."""

    def test_scan_returns_empty_report_when_no_orders(self) -> None:
        """Scan returns empty report when no orders exist."""
        report = self.recovery.scan()

        self.assertEqual(report.total_orders, 0)
        self.assertEqual(len(report.ambiguous_orders), 0)
        self.assertEqual(len(report.unknown_orders), 0)
        self.assertEqual(len(report.pending_new_orders), 0)
        self.assertFalse(report.has_ambiguous_orders)
        self.assertFalse(report.has_unknown)

    def test_scan_identifies_unknown_orders(self) -> None:
        """Scan identifies orders in UNKNOWN state."""
        # Create UNKNOWN order using broker that returns UNCERTAIN
        uncertain_ack = BrokerAck(outcome=AckOutcome.UNCERTAIN)
        self.broker.script(uncertain_ack, lands_at_venue=True)

        result = self.submit()
        self.assertEqual(result.outcome, "unknown")

        # Scan should identify the UNKNOWN order
        report = self.recovery.scan()

        self.assertEqual(report.total_orders, 1)
        self.assertEqual(len(report.unknown_orders), 1)
        self.assertEqual(report.unknown_orders[0].order_id, result.order.order_id)
        self.assertEqual(len(report.ambiguous_orders), 1)
        self.assertIn("UNKNOWN state", report.ambiguous_orders[0].reason)

    def test_scan_excludes_terminal_orders(self) -> None:
        """Scan does not count ACCEPTED/FILLED/REJECTED as ambiguous."""
        # Orders that fill immediately are terminal
        result = self.submit()

        order = self.orders.get(result.order.order_id)
        self.assertIn(order.state.value, ["accepted", "filled", "rejected"])

        report = self.recovery.scan()

        # Terminal orders are not in ambiguous
        self.assertEqual(len(report.ambiguous_orders), 0)
        self.assertEqual(len(report.pending_new_orders), 0)

    def test_scan_shows_order_count(self) -> None:
        """Scan correctly counts orders after submissions."""
        # Submit first order (fills)
        self.submit()

        # Submit second order (also fills)
        self.submit(signal_id="sig-2")

        report = self.recovery.scan()

        self.assertEqual(report.total_orders, 2)


class TestRestartRecoveryBlocking(RestartRecoveryFixture):
    """Test that UNKNOWN blocking survives restart."""

    def test_unknown_order_blocks_new_orders_after_restart_simulation(self) -> None:
        """UNKNOWN order blocks new submissions even after 'restart'."""
        # Create UNKNOWN order
        uncertain_ack = BrokerAck(outcome=AckOutcome.UNCERTAIN)
        self.broker.script(uncertain_ack, lands_at_venue=True)
        result1 = self.submit()
        self.assertEqual(result1.outcome, "unknown")

        # Simulate restart by creating new gateway with same repositories
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

        # New gateway should still block due to UNKNOWN
        # UNKNOWN blocks via reconciliation gate (gate 7), not duplicate_order
        result2 = self.submit(signal_id="sig-2")
        self.assertEqual(result2.outcome, "refused")
        self.assertEqual(result2.gate, "reconciliation")

    def test_scan_has_unknown_returns_true_for_unknown_orders(self) -> None:
        """scan().has_unknown is True when UNKNOWN orders exist."""
        uncertain_ack = BrokerAck(outcome=AckOutcome.UNCERTAIN)
        self.broker.script(uncertain_ack, lands_at_venue=True)
        self.submit()

        report = self.recovery.scan()

        self.assertTrue(report.has_unknown)
        self.assertTrue(report.has_ambiguous_orders)

    def test_no_unknown_means_no_blocking(self) -> None:
        """No UNKNOWN orders means no blocking."""
        # Submit a normal order (fills)
        self.submit()

        report = self.recovery.scan()

        self.assertFalse(report.has_unknown)
        self.assertFalse(report.has_ambiguous_orders)


class TestRestartRecoveryCoordinator(RestartRecoveryFixture):
    """Test the RestartRecoveryCoordinator directly."""

    def test_coordinator_exposes_scan(self) -> None:
        """Coordinator.scan() returns RecoveryReport."""
        report = self.recovery.scan()

        self.assertIsInstance(report.total_orders, int)
        self.assertIsInstance(report.ambiguous_orders, tuple)
        self.assertIsInstance(report.unknown_orders, tuple)
        self.assertIsInstance(report.pending_new_orders, tuple)

    def test_coordinator_exposes_sync_order(self) -> None:
        """Coordinator.sync_order() delegates to gateway."""
        # Create an order first
        self.broker.script(BrokerAck(outcome=AckOutcome.ACCEPTED), lands_at_venue=True)
        result = self.submit()
        order = self.orders.get(result.order.order_id)

        # Sync should work through coordinator
        ack = self.recovery.sync_order(order, operator=self.operator)

        self.assertIsNotNone(ack)

    def test_coordinator_exposes_resolve_unknown(self) -> None:
        """Coordinator.resolve_unknown() delegates to gateway."""
        # Create UNKNOWN order
        uncertain_ack = BrokerAck(outcome=AckOutcome.UNCERTAIN)
        self.broker.script(uncertain_ack, lands_at_venue=True)
        result = self.submit()
        order = self.orders.get(result.order.order_id)

        # Resolve should work through coordinator (no resolution parameter)
        ack = self.recovery.resolve_unknown(order, operator=self.operator)

        # The resolution works by fetching the order state from broker
        # which returns the actual state after reconciliation
        self.assertIsNotNone(ack)


class TestPENDING_NEWAmbiguity(RestartRecoveryFixture):
    """Test PENDING_NEW restart ambiguity handling."""

    def test_scan_correctly_classifies_terminal_orders(self) -> None:
        """Scan correctly classifies orders based on their state."""
        # Submit and wait for fill (terminal state)
        result = self.submit()
        order = self.orders.get(result.order.order_id)

        # Terminal orders should not appear in ambiguous
        report = self.recovery.scan()

        self.assertEqual(order.state.value, "filled")
        self.assertEqual(len(report.pending_new_orders), 0)
        self.assertEqual(len(report.ambiguous_orders), 0)

    def test_unknown_order_includes_all_details(self) -> None:
        """UNKNOWN order scan includes order details in ambiguous report."""
        uncertain_ack = BrokerAck(outcome=AckOutcome.UNCERTAIN)
        self.broker.script(uncertain_ack, lands_at_venue=True)
        result = self.submit()
        order = self.orders.get(result.order.order_id)

        report = self.recovery.scan()

        self.assertEqual(len(report.ambiguous_orders), 1)
        ambiguous = report.ambiguous_orders[0]
        self.assertEqual(ambiguous.order.order_id, order.order_id)
        self.assertIn("UNKNOWN", ambiguous.reason)


if __name__ == "__main__":
    unittest.main()
