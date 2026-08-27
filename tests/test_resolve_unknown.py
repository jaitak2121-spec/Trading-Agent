"""Tests for resolve_unknown() with prior fills and delta-based reconciliation."""

from __future__ import annotations

import unittest

from trading.core.errors import SafetyViolation
from trading.core.money import USD, Money, Price, Quantity
from trading.core.orders import Order, OrderState
from trading.ports.broker import AckOutcome, BrokerAck

from .harness import build_rig


class StubBroker:
    """Test stub that returns a predetermined ack from fetch_order_state."""

    def __init__(self, fetch_ack: BrokerAck):
        self.fetch_ack = fetch_ack

    def fetch_order_state(self, order: Order) -> BrokerAck:
        return self.fetch_ack


class TestResolveUnknownWithPriorFills(unittest.TestCase):
    """Verify resolve_unknown() uses delta logic and prevents double-booking."""

    def test_partially_filled_then_unknown_then_more_fills_no_double_booking(self) -> None:
        """PARTIALLY_FILLED → UNKNOWN → resolved with more fills should not double-book."""
        rig = build_rig()

        # 1. Submit and get a partial fill (0.0005 BTC at 50,000)
        ack1 = BrokerAck(
            outcome=AckOutcome.FILLED,
            broker_order_id="broker-123",
            filled_quantity=Quantity("0.0005", "BTC"),
            fill_price=Price("50000", USD),
        )
        rig.broker.script(ack1)
        result = rig.submit()
        self.assertEqual(result.outcome, "executed")
        order = result.order
        self.assertEqual(order.state, OrderState.PARTIALLY_FILLED)
        self.assertEqual(order.filled_quantity, Quantity("0.0005", "BTC"))
        initial_position = rig.positions.position("BTCUSD", asset="BTC")

        # 2. Manually mark order as UNKNOWN (simulating a later lifecycle sync issue)
        order.mark_unknown(reason="simulated sync uncertainty")
        self.assertTrue(order.is_unknown)

        # 3. Replace broker with stub that reports cumulative 0.0008 BTC filled
        # (initial 0.0005 at 50,000 = 25, new 0.0003 at 53,333.33 = 16, total = 41)
        ack_resolved = BrokerAck(
            outcome=AckOutcome.FILLED,
            broker_order_id="broker-123",
            filled_quantity=Quantity("0.0008", "BTC"),
            fill_price=Price("51250", USD),  # Average price
        )
        rig.gateway._broker = StubBroker(ack_resolved)

        # 4. Resolve the UNKNOWN order
        final_ack = rig.gateway.resolve_unknown(order, operator=rig.operator_id)

        # 5. Verify: order should show cumulative 0.0008, not 0.0013 (double-booked)
        self.assertEqual(order.state, OrderState.PARTIALLY_FILLED)
        self.assertEqual(order.filled_quantity, Quantity("0.0008", "BTC"))
        # Position should have initial + delta (0.0003), not + 0.0008
        self.assertEqual(
            rig.positions.position("BTCUSD", asset="BTC"),
            initial_position + Quantity("0.0003", "BTC"),
        )

    def test_partially_filled_then_unknown_then_fully_filled(self) -> None:
        """PARTIALLY_FILLED → UNKNOWN → fully filled should complete correctly."""
        rig = build_rig()

        # 1. Submit and get a partial fill (0.0005 BTC)
        ack1 = BrokerAck(
            outcome=AckOutcome.FILLED,
            broker_order_id="broker-456",
            filled_quantity=Quantity("0.0005", "BTC"),
            fill_price=Price("50000", USD),
        )
        rig.broker.script(ack1)
        result = rig.submit(quantity="0.001")
        order = result.order
        self.assertEqual(order.filled_quantity, Quantity("0.0005", "BTC"))

        # 2. Mark UNKNOWN
        order.mark_unknown(reason="simulated issue")

        # 3. Replace broker with stub reporting fully filled
        ack_resolved = BrokerAck(
            outcome=AckOutcome.FILLED,
            broker_order_id="broker-456",
            filled_quantity=Quantity("0.001", "BTC"),
            fill_price=Price("50000", USD),
        )
        rig.gateway._broker = StubBroker(ack_resolved)

        # 4. Resolve
        rig.gateway.resolve_unknown(order, operator=rig.operator_id)

        # 5. Should be FILLED with cumulative 0.001
        self.assertEqual(order.state, OrderState.FILLED)
        self.assertEqual(order.filled_quantity, Quantity("0.001", "BTC"))
        self.assertEqual(order.remaining_quantity, Quantity("0.0", "BTC"))

    def test_partially_filled_then_unknown_then_rejected(self) -> None:
        """PARTIALLY_FILLED → UNKNOWN → venue says rejected (no record)."""
        rig = build_rig()

        # 1. Submit and get a partial fill
        ack1 = BrokerAck(
            outcome=AckOutcome.FILLED,
            broker_order_id="broker-789",
            filled_quantity=Quantity("0.0005", "BTC"),
            fill_price=Price("50000", USD),
        )
        rig.broker.script(ack1)
        result = rig.submit()
        order = result.order
        initial_filled = order.filled_quantity

        # 2. Mark UNKNOWN
        order.mark_unknown(reason="simulated issue")

        # 3. Replace broker with stub reporting no record
        ack_resolved = BrokerAck(outcome=AckOutcome.REJECTED, message="no such order")
        rig.gateway._broker = StubBroker(ack_resolved)

        # 4. Resolve
        rig.gateway.resolve_unknown(order, operator=rig.operator_id)

        # 5. Order should be REJECTED but retain the prior fill quantity
        self.assertEqual(order.state, OrderState.REJECTED)
        self.assertEqual(order.filled_quantity, initial_filled)

    def test_zero_delta_on_resolve_is_idempotent(self) -> None:
        """Resolving with exactly the same cumulative state is a no-op."""
        rig = build_rig()

        # 1. Submit and fill
        ack1 = BrokerAck(
            outcome=AckOutcome.FILLED,
            broker_order_id="broker-101",
            filled_quantity=Quantity("0.0005", "BTC"),
            fill_price=Price("50000", USD),
        )
        rig.broker.script(ack1)
        result = rig.submit()
        order = result.order

        # 2. Mark UNKNOWN
        order.mark_unknown(reason="simulated")

        # 3. Replace broker with stub reporting same cumulative state
        ack_resolved = BrokerAck(
            outcome=AckOutcome.FILLED,
            broker_order_id="broker-101",
            filled_quantity=Quantity("0.0005", "BTC"),
            fill_price=Price("50000", USD),
        )
        rig.gateway._broker = StubBroker(ack_resolved)

        # 4. Resolve
        initial_portfolio = rig.portfolio.position("BTC").quantity
        rig.gateway.resolve_unknown(order, operator=rig.operator_id)

        # 5. Portfolio should be unchanged (zero delta)
        self.assertEqual(rig.portfolio.position("BTC").quantity, initial_portfolio)
        self.assertEqual(order.filled_quantity, Quantity("0.0005", "BTC"))

    def test_resolve_unknown_not_in_unknown_state_raises(self) -> None:
        """Cannot resolve an order that isn't UNKNOWN."""
        rig = build_rig()

        rig.broker.script(BrokerAck(outcome=AckOutcome.ACCEPTED, broker_order_id="b-1"))
        result = rig.submit()
        order = result.order
        self.assertEqual(order.state, OrderState.ACCEPTED)

        # Try to resolve non-UNKNOWN order
        with self.assertRaises(SafetyViolation) as ctx:
            rig.gateway.resolve_unknown(order, operator=rig.operator_id)
        self.assertIn("not UNKNOWN", str(ctx.exception))

    def test_resolve_unknown_uncertain_response_leaves_unknown(self) -> None:
        """UNCERTAIN from fetch_order_state should leave order UNKNOWN."""
        rig = build_rig()

        rig.broker.script(BrokerAck(outcome=AckOutcome.UNCERTAIN))
        result = rig.submit()
        order = result.order
        self.assertEqual(order.state, OrderState.UNKNOWN)

        # Replace broker with stub still returning uncertain
        ack_uncertain = BrokerAck(outcome=AckOutcome.UNCERTAIN, message="still uncertain")
        rig.gateway._broker = StubBroker(ack_uncertain)
        ack = rig.gateway.resolve_unknown(order, operator=rig.operator_id)

        # Order should still be UNKNOWN
        self.assertTrue(order.is_unknown)
        self.assertEqual(ack.outcome, AckOutcome.UNCERTAIN)

    def test_resolve_updates_portfolio_correctly_with_prior_fills(self) -> None:
        """Portfolio should reflect only the delta, not re-apply prior fills."""
        rig = build_rig()

        # 1. Submit and partial fill
        ack1 = BrokerAck(
            outcome=AckOutcome.FILLED,
            broker_order_id="b-999",
            filled_quantity=Quantity("0.001", "BTC"),
            fill_price=Price("50000", USD),
        )
        rig.broker.script(ack1)
        result = rig.submit(quantity="0.002")
        order = result.order

        initial_position = rig.positions.position("BTCUSD", asset="BTC")
        initial_cash = rig.portfolio.cash

        # 2. Mark UNKNOWN
        order.mark_unknown(reason="test")

        # 3. Replace broker with stub reporting cumulative 0.0015 filled (delta = 0.0005)
        ack_resolved = BrokerAck(
            outcome=AckOutcome.FILLED,
            broker_order_id="b-999",
            filled_quantity=Quantity("0.0015", "BTC"),
            fill_price=Price("50000", USD),  # Average price
        )
        rig.gateway._broker = StubBroker(ack_resolved)

        # 4. Resolve
        rig.gateway.resolve_unknown(order, operator=rig.operator_id)

        # 5. Position should have increased by delta only (0.0005), not full 0.0015
        expected_position = initial_position + Quantity("0.0005", "BTC")
        self.assertEqual(rig.positions.position("BTCUSD", asset="BTC"), expected_position)

        # Cash should decrease by delta cost only (0.0005 * 50,000 = 25 USD)
        expected_cash = initial_cash - Money("25", USD)
        self.assertEqual(rig.portfolio.cash, expected_cash)


if __name__ == "__main__":
    unittest.main()
