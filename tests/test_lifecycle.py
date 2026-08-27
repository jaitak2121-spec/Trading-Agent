"""Tests for order lifecycle behavior and cumulative-to-delta fill calculations."""

from __future__ import annotations

import unittest
from decimal import Decimal

from trading.core.clock import ManualClock
from trading.core.errors import SafetyViolation
from trading.core.money import USD, Currency, Price, Quantity
from trading.core.orders import Order, OrderIntent, OrderSide, OrderState


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
