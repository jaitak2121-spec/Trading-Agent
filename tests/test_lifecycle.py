"""Tests for order lifecycle behavior and cumulative-to-delta fill calculations."""

from __future__ import annotations

import unittest
from decimal import Decimal

from trading.core.clock import ManualClock
from trading.core.errors import SafetyViolation
from trading.core.money import USD, Currency, Price, Quantity
from trading.core.orders import Order, OrderIntent, OrderSide, OrderState
from trading.ports.broker import AckOutcome, BrokerAck

from .harness import build_rig


def make_intent(quantity: str = "1.0", asset: str = "BTC") -> OrderIntent:
    return OrderIntent(
        strategy_id="momentum-v1",
        signal_id="sig-1",
        symbol="BTCUSD",
        side=OrderSide.BUY,
        quantity=Quantity(quantity, asset),
    )


class TestOrderApplyFillDelta(unittest.TestCase):
    def setUp(self) -> None:
        self.clock = ManualClock()
        self.intent = make_intent("1.0", "BTC")
        self.order = Order(self.intent, clock=self.clock)
        # Advance the order from DRAFT to PENDING_NEW to allow fills
        self.order.transition_to(OrderState.PENDING_NEW, reason="submit simulated")

    def test_initial_flat_delta_applies_correctly(self) -> None:
        # Venue reports a cumulative fill of 0.4 BTC at 50,000 USD
        cumulative_qty = Quantity("0.40000000", "BTC")
        cumulative_notional = Decimal("20000")  # 0.4 * 50,000

        state, delta_qty, delta_price = self.order.apply_fill_delta(
            cumulative_qty,
            cumulative_notional,
            USD,
            reason="first fill sync",
        )

        self.assertEqual(state, OrderState.PARTIALLY_FILLED)
        self.assertEqual(self.order.filled_quantity, Quantity("0.40000000", "BTC"))
        self.assertEqual(self.order.average_fill_price, Price("50000", USD))
        self.assertEqual(self.order.remaining_quantity, Quantity("0.60000000", "BTC"))

    def test_incremental_delta_applies_correctly(self) -> None:
        # 1. Apply initial fill
        self.order.apply_fill(Quantity("0.4", "BTC"), Price("50000", USD))
        self.assertEqual(self.order.filled_quantity, Quantity("0.4", "BTC"))

        # 2. Venue reports cumulative 0.6 BTC at cumulative notional 31,000
        # (initial 0.4 BTC at 50,000 = 20,000, new 0.2 BTC at 55,000 = 11,000, total = 31,000)
        cumulative_qty = Quantity("0.6", "BTC")
        cumulative_notional = Decimal("31000")

        state, delta_qty, delta_price = self.order.apply_fill_delta(
            cumulative_qty,
            cumulative_notional,
            USD,
            reason="second fill sync",
        )

        self.assertEqual(state, OrderState.PARTIALLY_FILLED)
        self.assertEqual(self.order.filled_quantity, Quantity("0.6", "BTC"))
        # Average price = 31,000 / 0.6 = 51,666.66666667
        self.assertEqual(
            self.order.average_fill_price,
            Price("51666.666666666667", USD),
        )

    def test_exact_completion_fill_applies_correctly(self) -> None:
        self.order.apply_fill(Quantity("0.6", "BTC"), Price("50000", USD))

        # Venue reports cumulative 1.0 BTC at 50,000 (total notional = 50,000)
        cumulative_qty = Quantity("1.0", "BTC")
        cumulative_notional = Decimal("50000")

        state, delta_qty, delta_price = self.order.apply_fill_delta(
            cumulative_qty,
            cumulative_notional,
            USD,
        )

        self.assertEqual(state, OrderState.FILLED)
        self.assertEqual(self.order.filled_quantity, Quantity("1.0", "BTC"))
        self.assertEqual(self.order.remaining_quantity, Quantity("0.0", "BTC"))

    def test_zero_delta_is_idempotent_no_op(self) -> None:
        self.order.apply_fill(Quantity("0.5", "BTC"), Price("50000", USD))
        initial_state = self.order.state

        # Venue reports exactly what we have booked
        state, delta_qty, delta_price = self.order.apply_fill_delta(
            Quantity("0.5", "BTC"),
            Decimal("25000"),
            USD,
        )

        self.assertEqual(state, initial_state)
        self.assertEqual(self.order.filled_quantity, Quantity("0.5", "BTC"))
        self.assertTrue(delta_qty.is_zero)

    def test_regressed_cumulative_quantity_rejected(self) -> None:
        self.order.apply_fill(Quantity("0.5", "BTC"), Price("50000", USD))

        # Venue reports less than we have booked
        with self.assertRaises(SafetyViolation) as ctx:
            self.order.apply_fill_delta(
                Quantity("0.4", "BTC"),
                Decimal("20000"),
                USD,
            )
        self.assertIn("regressed fill", str(ctx.exception))

    def test_overfill_rejected(self) -> None:
        # Venue reports cumulative fill exceeding ordered amount (1.0 BTC)
        with self.assertRaises(SafetyViolation) as ctx:
            self.order.apply_fill_delta(
                Quantity("1.1", "BTC"),
                Decimal("55000"),
                USD,
            )
        self.assertIn("overfill", str(ctx.exception))

    def test_asset_mismatch_rejected(self) -> None:
        # Venue reports some other asset
        with self.assertRaises(SafetyViolation) as ctx:
            self.order.apply_fill_delta(
                Quantity("0.5", "ETH"),
                Decimal("25000"),
                USD,
            )
        self.assertIn("asset", str(ctx.exception))

    def test_currency_mismatch_rejected(self) -> None:
        self.order.apply_fill(Quantity("0.5", "BTC"), Price("50000", USD))

        # Venue reports different currency
        INR = Currency("INR", 2)
        with self.assertRaises(SafetyViolation) as ctx:
            self.order.apply_fill_delta(
                Quantity("0.8", "BTC"),
                Decimal("40000"),
                INR,
            )
        self.assertIn("currency mismatch", str(ctx.exception))


class TestApplyFetchedStateRefusals(unittest.TestCase):
    """Every defensive refusal of a venue's answer, and what it leaves behind.

    These exercise ``ExecutionGateway._apply_fetched_state`` directly because
    ``resolve_unknown`` -- its only caller today -- cannot reach most of these
    branches: it returns early on ``UNCERTAIN`` and refuses an order that is not
    already UNKNOWN. The refusals exist for the lifecycle-sync entry point that
    a later change adds, and they are tested now rather than after the fact.

    Two properties are asserted for every refusal: the refusal is **audited**
    (INVARIANT 13), and the order is **left untouched**.
    """

    def _accepted_order(self, rig, broker_order_id: str = "broker-1"):
        """Submit an order and leave it resting at the venue."""
        rig.broker.script(
            BrokerAck(outcome=AckOutcome.ACCEPTED, broker_order_id=broker_order_id)
        )
        result = rig.submit()
        self.assertEqual(result.outcome, "executed")
        self.assertEqual(result.order.state, OrderState.ACCEPTED)
        return result.order

    def _assert_audited_refusal(self, rig, order, fragment: str) -> None:
        refusals = [
            record
            for record in rig.sink.records
            if record.action == "gateway.venue_state_refused"
        ]
        self.assertEqual(len(refusals), 1, "expected exactly one refusal record")
        record = refusals[0]
        self.assertEqual(record.outcome, "refused")
        self.assertEqual(record.category, "reconciliation")
        self.assertEqual(record.details["order_id"], order.order_id)
        self.assertIn(fragment, record.details["reason"])

    def test_broker_order_id_mismatch_is_refused_and_audited(self) -> None:
        rig = build_rig()
        order = self._accepted_order(rig, "broker-1")

        with self.assertRaises(SafetyViolation) as ctx:
            rig.gateway._apply_fetched_state(
                order,
                BrokerAck(outcome=AckOutcome.ACCEPTED, broker_order_id="broker-2"),
                via_reconciliation=False,
            )

        self.assertIn("broker_order_id mismatch", str(ctx.exception))
        self._assert_audited_refusal(rig, order, "broker_order_id mismatch")
        # The stored identifier is the one we already had, not the venue's.
        self.assertEqual(order.broker_order_id, "broker-1")
        self.assertEqual(order.state, OrderState.ACCEPTED)

    def test_uncertain_answer_is_refused_and_audited(self) -> None:
        rig = build_rig()
        order = self._accepted_order(rig)

        with self.assertRaises(SafetyViolation) as ctx:
            rig.gateway._apply_fetched_state(
                order,
                BrokerAck(outcome=AckOutcome.UNCERTAIN),
                via_reconciliation=False,
            )

        self.assertIn("UNCERTAIN", str(ctx.exception))
        self._assert_audited_refusal(rig, order, "UNCERTAIN")
        self.assertEqual(order.state, OrderState.ACCEPTED)

    def test_filled_ack_cannot_omit_quantity_or_price(self) -> None:
        """The port type refuses it at construction, so the gateway need not.

        ``_apply_fetched_state`` asserts rather than re-checking, on the same
        reasoning as ``_settle``: a second check here would be a second answer
        to a question ``BrokerAck`` has already settled.
        """
        with self.assertRaises(ValueError):
            BrokerAck(
                outcome=AckOutcome.FILLED,
                filled_quantity=Quantity("0.001", "BTC"),
                fill_price=None,
            )
        with self.assertRaises(ValueError):
            BrokerAck(
                outcome=AckOutcome.FILLED,
                filled_quantity=None,
                fill_price=Price("50000", USD),
            )

    def test_fill_against_terminal_order_is_refused_and_audited(self) -> None:
        rig = build_rig()
        rig.broker.script(
            BrokerAck(
                outcome=AckOutcome.FILLED,
                broker_order_id="broker-9",
                filled_quantity=Quantity("0.001", "BTC"),
                fill_price=Price("50000", USD),
            )
        )
        order = rig.submit().order
        self.assertEqual(order.state, OrderState.FILLED)
        self.assertTrue(order.state.is_terminal)
        booked = rig.positions.position("BTCUSD", asset="BTC")

        with self.assertRaises(SafetyViolation) as ctx:
            rig.gateway._apply_fetched_state(
                order,
                BrokerAck(
                    outcome=AckOutcome.FILLED,
                    broker_order_id="broker-9",
                    filled_quantity=Quantity("0.002", "BTC"),
                    fill_price=Price("50000", USD),
                ),
                via_reconciliation=False,
            )

        self.assertIn("terminal order", str(ctx.exception))
        self._assert_audited_refusal(rig, order, "terminal order")
        # Nothing reached the portfolio.
        self.assertEqual(rig.positions.position("BTCUSD", asset="BTC"), booked)

    def test_overfill_from_venue_is_audited_at_the_gateway(self) -> None:
        """Order's own refusal must still leave an audit record behind."""
        rig = build_rig()
        order = self._accepted_order(rig)
        booked = rig.positions.position("BTCUSD", asset="BTC")

        with self.assertRaises(SafetyViolation) as ctx:
            rig.gateway._apply_fetched_state(
                order,
                BrokerAck(
                    outcome=AckOutcome.FILLED,
                    broker_order_id="broker-1",
                    # The order is for 0.001 BTC.
                    filled_quantity=Quantity("0.005", "BTC"),
                    fill_price=Price("50000", USD),
                ),
                via_reconciliation=False,
            )

        self.assertIn("overfill", str(ctx.exception))
        self._assert_audited_refusal(rig, order, "overfill")
        self.assertEqual(order.state, OrderState.ACCEPTED)
        self.assertEqual(rig.positions.position("BTCUSD", asset="BTC"), booked)

    def test_refused_answer_does_not_attach_the_broker_order_id(self) -> None:
        """A refused response must not leave the venue's identifier behind."""
        rig = build_rig()
        rig.broker.script(BrokerAck(outcome=AckOutcome.ACCEPTED))
        order = rig.submit().order
        self.assertIsNone(order.broker_order_id)

        with self.assertRaises(SafetyViolation):
            rig.gateway._apply_fetched_state(
                order,
                BrokerAck(
                    outcome=AckOutcome.FILLED,
                    broker_order_id="broker-late",
                    filled_quantity=Quantity("0.005", "BTC"),  # overfill
                    fill_price=Price("50000", USD),
                ),
                via_reconciliation=False,
            )

        self.assertIsNone(order.broker_order_id)

    def test_unknown_is_not_terminal_so_a_fill_may_resolve_it(self) -> None:
        """The terminal-order guard must not block the UNKNOWN recovery path."""
        rig = build_rig()
        order = self._accepted_order(rig)
        order.mark_unknown(reason="simulated")

        self.assertFalse(order.state.is_terminal)

        rig.gateway._apply_fetched_state(
            order,
            BrokerAck(
                outcome=AckOutcome.FILLED,
                broker_order_id="broker-1",
                filled_quantity=Quantity("0.001", "BTC"),
                fill_price=Price("50000", USD),
            ),
            via_reconciliation=True,
        )

        self.assertEqual(order.state, OrderState.FILLED)
        self.assertEqual(order.filled_quantity, Quantity("0.001", "BTC"))
        self.assertNotIn("gateway.venue_state_refused", rig.actions())

    def test_a_venue_unwinding_a_booked_fill_is_refused_and_audited(self) -> None:
        """An illegal transition is a refusal like any other, and is audited.

        A venue that calls an order merely resting when we have already booked
        fills against it has contradicted the fills. The state machine has no
        PARTIALLY_FILLED -> ACCEPTED edge, and that refusal must reach the
        audit trail rather than surfacing as a bare exception.
        """
        rig = build_rig()
        rig.broker.script(
            BrokerAck(
                outcome=AckOutcome.FILLED,
                broker_order_id="broker-1",
                filled_quantity=Quantity("0.001", "BTC"),
                fill_price=Price("50000", USD),
            )
        )
        order = rig.submit(quantity="0.002").order
        self.assertEqual(order.state, OrderState.PARTIALLY_FILLED)
        booked = rig.positions.position("BTCUSD", asset="BTC")

        with self.assertRaises(SafetyViolation) as ctx:
            rig.gateway._apply_fetched_state(
                order,
                BrokerAck(outcome=AckOutcome.ACCEPTED, broker_order_id="broker-1"),
                via_reconciliation=False,
            )

        self.assertIn("not a valid transition", str(ctx.exception))
        self._assert_audited_refusal(rig, order, "not a valid transition")
        # The fill we already booked is still booked.
        self.assertEqual(order.state, OrderState.PARTIALLY_FILLED)
        self.assertEqual(rig.positions.position("BTCUSD", asset="BTC"), booked)
