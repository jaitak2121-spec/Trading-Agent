"""Tests for position sizing.

Sizing is where a rounding mistake turns into an oversized position, so these
tests lean on two post-conditions rather than on worked examples alone:

* the priced notional never exceeds the per-order ceiling or available equity;
* the loss at the stop never exceeds the risk budget.

Both are swept over a grid of awkward prices, equities, and lot steps.
"""

from __future__ import annotations

import unittest
from decimal import Decimal

from trading.core.config import RiskConfig
from trading.core.errors import PrecisionError
from trading.core.money import INR, USD, Money, Price, Quantity
from trading.core.orders import OrderSide
from trading.core.sizing import (
    DEFAULT_LOT_STEP,
    PositionSizer,
    SizingConstraint,
    SizingResult,
)


class SizingFixture(unittest.TestCase):
    def setUp(self) -> None:
        self.config = RiskConfig()  # order<=100, fraction=0.005
        self.sizer = PositionSizer(self.config)

    def size(self, **kwargs) -> SizingResult:
        params = dict(
            equity=Money("10000.00", USD),
            entry=Price("50000", USD),
            side=OrderSide.BUY,
            asset="BTC",
        )
        params.update(kwargs)
        # Default the stop to 2% below entry so overriding `entry` alone stays
        # valid; tests that care about stop distance pass one explicitly.
        if "stop" not in params and isinstance(params["entry"], Price):
            entry_amount = params["entry"].amount
            params["stop"] = Price(entry_amount * Decimal("0.98"), USD)
        return self.sizer.size_for_stop(**params)


class TestWorkedExamples(SizingFixture):
    def test_the_order_ceiling_usually_binds_first(self):
        # budget 50 / 1000 per unit = 0.05 BTC by risk;
        # 100 ceiling / 50000 = 0.002 BTC by notional. The smaller wins.
        result = self.size()
        self.assertEqual(result.quantity, Quantity("0.002", "BTC"))
        self.assertEqual(result.notional, Money("100.00", USD))
        self.assertEqual(
            result.binding_constraint, SizingConstraint.ORDER_NOTIONAL_CAP
        )

    def test_risk_fraction_binds_when_the_stop_is_far_away(self):
        # A 40000-wide stop: budget 50 / 40000 = 0.00125 BTC, below the
        # 0.002 BTC the notional ceiling would allow.
        result = self.size(stop=Price("10000", USD))
        self.assertEqual(result.quantity, Quantity("0.00125", "BTC"))
        self.assertEqual(result.binding_constraint, SizingConstraint.RISK_FRACTION)
        self.assertLessEqual(result.max_loss_at_stop, result.risk_budget)

    def test_risk_budget_is_the_configured_fraction_of_equity(self):
        result = self.size(equity=Money("20000.00", USD))
        self.assertEqual(result.risk_budget, Money("100.00", USD))

    def test_short_side_sizes_the_same_way(self):
        result = self.size(side=OrderSide.SELL, stop=Price("51000", USD))
        self.assertEqual(result.quantity, Quantity("0.002", "BTC"))

    def test_risk_per_unit_is_the_stop_distance(self):
        result = self.size(entry=Price("50000", USD), stop=Price("49500", USD))
        self.assertEqual(result.risk_per_unit, Decimal("500"))

    def test_result_renders_readably(self):
        self.assertIn("bound by", str(self.size()))
        self.assertIn("no position", str(self.size(equity=Money("0.00", USD))))


class TestNeverRoundsUp(SizingFixture):
    def test_size_is_floored_to_the_lot_step(self):
        result = self.size(entry=Price("33333", USD), lot_step="0.001")
        # 100 / 33333 = 0.00300003...; floored to 0.003 at a 0.001 step.
        self.assertEqual(result.quantity, Quantity("0.003", "BTC"))

    def test_a_coarse_lot_step_can_floor_to_zero(self):
        result = self.size(lot_step="1")
        self.assertFalse(result.is_tradeable)
        self.assertEqual(result.quantity, Quantity.zero("BTC"))

    def test_lot_step_is_reported_as_the_binding_constraint(self):
        result = self.size(entry=Price("33333", USD), lot_step="0.001")
        self.assertEqual(result.binding_constraint, SizingConstraint.LOT_STEP)

    def test_excess_precision_never_reaches_quantity(self):
        # 100 / 3 = 33.333... would blow Quantity's 8-dp limit if not truncated.
        result = self.size(entry=Price("3", USD), stop=Price("1", USD))
        self.assertLessEqual(-result.quantity.amount.as_tuple().exponent, 8)

    def test_result_quantity_is_always_a_multiple_of_the_step(self):
        for step in ("0.00000001", "0.0001", "0.001", "0.01", "0.1"):
            with self.subTest(step=step):
                result = self.size(entry=Price("37777.77", USD), lot_step=step)
                if result.is_tradeable:
                    self.assertEqual(
                        result.quantity.amount % Decimal(step), Decimal(0)
                    )


class TestPostConditionsHold(SizingFixture):
    """The two properties that make an oversized position impossible."""

    GRID_PRICES = [
        "0.00000001",
        "0.5",
        "1",
        "3",
        "7.77",
        "99.999999",
        "33333.33",
        "50000",
        "100000.001",
        "1234567.89",
    ]
    GRID_EQUITIES = ["0.01", "1.00", "99.99", "10000.00", "1000000.00"]
    GRID_STEPS = ["0.00000001", "0.0001", "0.001", "0.01", "0.1", "1"]

    def test_notional_never_exceeds_the_ceiling_or_equity(self):
        ceiling = self.config.max_order_notional
        for price in self.GRID_PRICES:
            for equity in self.GRID_EQUITIES:
                for step in self.GRID_STEPS:
                    with self.subTest(price=price, equity=equity, step=step):
                        entry = Price(price, USD)
                        stop_amount = entry.amount / 2
                        result = self.size(
                            equity=Money(equity, USD),
                            entry=entry,
                            stop=Price(stop_amount, USD),
                            lot_step=step,
                        )
                        if not result.is_tradeable:
                            continue
                        self.assertLessEqual(result.notional, ceiling)
                        self.assertLessEqual(result.notional, Money(equity, USD))

    def test_loss_at_the_stop_never_exceeds_the_risk_budget(self):
        for price in self.GRID_PRICES:
            for equity in self.GRID_EQUITIES:
                for step in self.GRID_STEPS:
                    with self.subTest(price=price, equity=equity, step=step):
                        entry = Price(price, USD)
                        result = self.size(
                            equity=Money(equity, USD),
                            entry=entry,
                            stop=Price(entry.amount / 2, USD),
                            lot_step=step,
                        )
                        if not result.is_tradeable:
                            continue
                        self.assertLessEqual(
                            result.max_loss_at_stop, result.risk_budget
                        )

    def test_a_tight_stop_does_not_produce_an_oversized_notional(self):
        """The dangerous case: a near-zero stop distance implies a huge size."""
        result = self.size(entry=Price("50000", USD), stop=Price("49999.99", USD))
        self.assertLessEqual(result.notional, self.config.max_order_notional)

    def test_notional_rounding_up_cannot_push_past_the_ceiling(self):
        # Prices chosen so quantity * price lands just above a cent boundary,
        # where notional's ROUND_UP could otherwise cross the ceiling.
        for price in ("100000.001", "33333.333333", "7.777777", "0.000000015"):
            with self.subTest(price=price):
                result = self.size(entry=Price(price, USD), stop=Price("0.000000001", USD))
                if result.is_tradeable:
                    self.assertLessEqual(
                        result.notional, self.config.max_order_notional
                    )


class TestCapsAndBudgets(SizingFixture):
    def test_equity_caps_size_so_there_is_no_leverage(self):
        tiny = self.sizer.size_for_stop(
            equity=Money("10.00", USD),
            entry=Price("50000", USD),
            stop=Price("25000", USD),
            side=OrderSide.BUY,
            asset="BTC",
        )
        if tiny.is_tradeable:
            self.assertLessEqual(tiny.notional, Money("10.00", USD))

    def test_zero_equity_means_no_position(self):
        result = self.size(equity=Money("0.00", USD))
        self.assertFalse(result.is_tradeable)
        self.assertEqual(result.binding_constraint, SizingConstraint.EQUITY_CAP)
        self.assertIn("nothing to risk", result.reason)

    def test_negative_equity_means_no_position(self):
        result = self.size(equity=Money("-500.00", USD))
        self.assertFalse(result.is_tradeable)

    def test_remaining_loss_budget_caps_the_risk_budget(self):
        result = self.size(
            stop=Price("10000", USD),
            remaining_loss_budget=Money("10.00", USD),
        )
        self.assertEqual(result.risk_budget, Money("10.00", USD))
        self.assertEqual(
            result.binding_constraint, SizingConstraint.LOSS_BUDGET_CAP
        )
        self.assertLessEqual(result.max_loss_at_stop, Money("10.00", USD))

    def test_an_exhausted_loss_budget_means_no_position(self):
        for remaining in ("0.00", "-25.00"):
            with self.subTest(remaining=remaining):
                result = self.size(remaining_loss_budget=Money(remaining, USD))
                self.assertFalse(result.is_tradeable)
                self.assertEqual(
                    result.binding_constraint, SizingConstraint.LOSS_BUDGET_CAP
                )
                self.assertIn("exhausted", result.reason)

    def test_a_generous_loss_budget_does_not_raise_the_risk_fraction(self):
        result = self.size(
            stop=Price("10000", USD),
            remaining_loss_budget=Money("100000.00", USD),
        )
        self.assertEqual(result.risk_budget, Money("50.00", USD))
        self.assertEqual(result.binding_constraint, SizingConstraint.RISK_FRACTION)

    def test_a_budget_that_rounds_to_nothing_means_no_position(self):
        result = self.size(equity=Money("0.01", USD), stop=Price("10000", USD))
        self.assertFalse(result.is_tradeable)


class TestStopValidation(SizingFixture):
    def test_a_long_stop_above_entry_is_rejected(self):
        with self.assertRaises(ValueError) as ctx:
            self.size(entry=Price("100", USD), stop=Price("110", USD))
        self.assertIn("must be below entry", str(ctx.exception))

    def test_a_short_stop_below_entry_is_rejected(self):
        with self.assertRaises(ValueError) as ctx:
            self.size(
                side=OrderSide.SELL, entry=Price("100", USD), stop=Price("90", USD)
            )
        self.assertIn("must be above entry", str(ctx.exception))

    def test_a_stop_equal_to_entry_is_rejected(self):
        """Otherwise risk-per-unit is zero and the size is unbounded."""
        for side in (OrderSide.BUY, OrderSide.SELL):
            with self.subTest(side=side):
                with self.assertRaises(ValueError):
                    self.size(
                        side=side, entry=Price("100", USD), stop=Price("100", USD)
                    )

    def test_side_must_be_an_order_side(self):
        with self.assertRaises(TypeError):
            self.size(side="buy")

    def test_asset_must_be_a_non_empty_string(self):
        for bad in ("", "   "):
            with self.subTest(asset=bad):
                with self.assertRaises(ValueError):
                    self.size(asset=bad)


class TestTypeAndCurrencySafety(SizingFixture):
    def test_floats_are_rejected_everywhere(self):
        # Each call passes every other argument validly, so the TypeError can
        # only come from the float under test.
        valid = dict(
            equity=Money("10000.00", USD),
            entry=Price("50000", USD),
            stop=Price("49000", USD),
            side=OrderSide.BUY,
            asset="BTC",
        )
        for field, bad in (
            ("equity", 10000.0),
            ("entry", 50000.0),
            ("stop", 49000.0),
            ("lot_step", 0.001),
        ):
            with self.subTest(field=field):
                with self.assertRaises(TypeError):
                    self.sizer.size_for_stop(**{**valid, field: bad})

    def test_wrong_currency_is_rejected(self):
        with self.assertRaises(ValueError):
            self.size(equity=Money("10000.00", INR))
        with self.assertRaises(ValueError):
            self.size(entry=Price("50000", INR))
        with self.assertRaises(ValueError):
            self.size(remaining_loss_budget=Money("10.00", INR))

    def test_non_positive_lot_step_is_rejected(self):
        for bad in ("0", "-0.001"):
            with self.subTest(step=bad):
                with self.assertRaises(ValueError):
                    self.size(lot_step=bad)

    def test_sizer_requires_a_risk_config(self):
        with self.assertRaises(TypeError):
            PositionSizer("not a config")

    def test_default_lot_step_matches_quantity_precision(self):
        # A step finer than Quantity allows would raise on construction.
        result = self.size(lot_step=DEFAULT_LOT_STEP)
        self.assertTrue(result.is_tradeable)
        with self.assertRaises(PrecisionError):
            Quantity("0.000000001", "BTC")


class TestSizeForNotional(SizingFixture):
    def test_spends_at_most_the_target(self):
        result = self.sizer.size_for_notional(
            target_notional=Money("50.00", USD),
            entry=Price("50000", USD),
            asset="BTC",
        )
        self.assertEqual(result.quantity, Quantity("0.001", "BTC"))
        self.assertLessEqual(result.notional, Money("50.00", USD))

    def test_target_above_the_ceiling_is_clamped(self):
        result = self.sizer.size_for_notional(
            target_notional=Money("100000.00", USD),
            entry=Price("50000", USD),
            asset="BTC",
        )
        self.assertLessEqual(result.notional, self.config.max_order_notional)
        self.assertEqual(
            result.binding_constraint, SizingConstraint.ORDER_NOTIONAL_CAP
        )

    def test_non_positive_target_means_no_position(self):
        for amount in ("0.00", "-10.00"):
            with self.subTest(amount=amount):
                result = self.sizer.size_for_notional(
                    target_notional=Money(amount, USD),
                    entry=Price("50000", USD),
                    asset="BTC",
                )
                self.assertFalse(result.is_tradeable)

    def test_a_target_too_small_for_one_lot_means_no_position(self):
        result = self.sizer.size_for_notional(
            target_notional=Money("0.01", USD),
            entry=Price("50000", USD),
            asset="BTC",
            lot_step="0.001",
        )
        self.assertFalse(result.is_tradeable)
        self.assertIn("does not buy one lot step", result.reason)

    def test_reports_the_whole_position_as_the_worst_case(self):
        """With no stop, the honest maximum loss is everything."""
        result = self.sizer.size_for_notional(
            target_notional=Money("50.00", USD),
            entry=Price("50000", USD),
            asset="BTC",
        )
        self.assertEqual(result.max_loss_at_stop, result.notional)

    def test_notional_never_exceeds_the_target_across_a_grid(self):
        for price in TestPostConditionsHold.GRID_PRICES:
            for step in TestPostConditionsHold.GRID_STEPS:
                with self.subTest(price=price, step=step):
                    target = Money("75.00", USD)
                    result = self.sizer.size_for_notional(
                        target_notional=target,
                        entry=Price(price, USD),
                        asset="BTC",
                        lot_step=step,
                    )
                    if result.is_tradeable:
                        self.assertLessEqual(result.notional, target)

    def test_floats_and_wrong_currency_are_rejected(self):
        with self.assertRaises(TypeError):
            self.sizer.size_for_notional(
                target_notional=50.0, entry=Price("1", USD), asset="BTC"
            )
        with self.assertRaises(ValueError):
            self.sizer.size_for_notional(
                target_notional=Money("50.00", INR), entry=Price("1", USD), asset="BTC"
            )


class TestSizingGrantsNoPermission(SizingFixture):
    """Sizing proposes; risk decides (INVARIANT 4)."""

    def test_result_has_no_execution_surface(self):
        result = self.size()
        public = {name for name in dir(result) if not name.startswith("_")}
        self.assertEqual(
            public,
            {
                "quantity",
                "notional",
                "risk_per_unit",
                "risk_budget",
                "max_loss_at_stop",
                "binding_constraint",
                "reason",
                "is_tradeable",
                "as_details",
            },
        )

    def test_result_is_immutable(self):
        result = self.size()
        with self.assertRaises(Exception):
            result.quantity = Quantity("999", "BTC")

    def test_zero_result_is_not_tradeable_and_says_why(self):
        result = self.size(lot_step="1")
        self.assertFalse(result.is_tradeable)
        self.assertIsNotNone(result.reason)
        self.assertFalse(result.as_details()["tradeable"])


if __name__ == "__main__":
    unittest.main()
