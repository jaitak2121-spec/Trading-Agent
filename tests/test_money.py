"""Tests for the Decimal money layer.

Covers INVARIANT 8: financial calculations use Decimal, never float.
"""

from __future__ import annotations

import unittest
from decimal import Decimal

from trading.core.errors import CurrencyMismatch, PrecisionError
from trading.core.money import (
    BTC,
    INR,
    ROUND_DOWN,
    ROUND_HALF_EVEN,
    ROUND_UP,
    USD,
    USDT,
    Currency,
    Money,
    Price,
    Quantity,
    canonical_decimal_text,
    to_decimal,
    total,
)


class TestFloatRejection(unittest.TestCase):
    """INVARIANT 8: no code path accepts a binary float."""

    def test_to_decimal_rejects_float(self):
        with self.assertRaises(TypeError) as ctx:
            to_decimal(0.1)
        self.assertIn("float is forbidden", str(ctx.exception))

    def test_to_decimal_rejects_bool(self):
        with self.assertRaises(TypeError):
            to_decimal(True)

    def test_money_constructor_rejects_float(self):
        with self.assertRaises(TypeError):
            Money(1.5, USD)

    def test_money_rounded_rejects_float(self):
        with self.assertRaises(TypeError):
            Money.rounded(1.5, USD)

    def test_quantity_rejects_float(self):
        with self.assertRaises(TypeError):
            Quantity(0.5, "BTC")

    def test_price_rejects_float(self):
        with self.assertRaises(TypeError):
            Price(100.25, USD)

    def test_times_rejects_float(self):
        m = Money("10.00", USD)
        with self.assertRaises(TypeError):
            m.times(1.5)

    def test_divided_by_rejects_float(self):
        m = Money("10.00", USD)
        with self.assertRaises(TypeError):
            m.divided_by(2.0)

    def test_floor_to_step_rejects_float(self):
        q = Quantity("1.23456789", "BTC")
        with self.assertRaises(TypeError):
            q.floor_to_step(0.001)

    def test_allocate_rejects_float_weights(self):
        m = Money("10.00", USD)
        with self.assertRaises(TypeError):
            m.allocate([1.0, 2.0])

    def test_nan_rejected(self):
        with self.assertRaises(TypeError):
            to_decimal(Decimal("NaN"))
        with self.assertRaises(TypeError):
            to_decimal("NaN")

    def test_infinity_rejected(self):
        with self.assertRaises(TypeError):
            to_decimal(Decimal("Infinity"))
        with self.assertRaises(TypeError):
            to_decimal("-Infinity")

    def test_garbage_string_rejected(self):
        with self.assertRaises(TypeError):
            to_decimal("not-a-number")

    def test_unsupported_type_rejected(self):
        with self.assertRaises(TypeError):
            to_decimal([1])


class TestDecimalExactness(unittest.TestCase):
    def test_tenths_sum_exactly(self):
        """The canonical float failure: 0.1 + 0.2 != 0.3."""
        a = Money("0.10", USD)
        b = Money("0.20", USD)
        self.assertEqual(a + b, Money("0.30", USD))

    def test_repeated_addition_does_not_drift(self):
        acc = Money.zero(USD)
        for _ in range(1000):
            acc = acc + Money("0.01", USD)
        self.assertEqual(acc, Money("10.00", USD))

    def test_eight_decimal_crypto_precision_preserved(self):
        q = Quantity("0.00000001", "BTC")
        acc = Quantity.zero("BTC")
        for _ in range(100):
            acc = acc + q
        self.assertEqual(acc, Quantity("0.00000100", "BTC"))


class TestPrecisionStrictness(unittest.TestCase):
    def test_excess_precision_raises_rather_than_rounding(self):
        with self.assertRaises(PrecisionError) as ctx:
            Money("1.234", USD)
        self.assertIn("Refusing to round silently", str(ctx.exception))

    def test_currency_precision_respected(self):
        self.assertEqual(Money("1.23", USD).amount, Decimal("1.23"))
        self.assertEqual(Money("1.12345678", USDT).amount, Decimal("1.12345678"))
        with self.assertRaises(PrecisionError):
            Money("1.123456789", USDT)

    def test_explicit_rounding_permitted(self):
        self.assertEqual(Money.rounded("1.234", USD).amount, Decimal("1.23"))
        self.assertEqual(
            Money.rounded("1.235", USD, rounding=ROUND_UP).amount, Decimal("1.24")
        )
        self.assertEqual(
            Money.rounded("1.239", USD, rounding=ROUND_DOWN).amount, Decimal("1.23")
        )

    def test_half_even_is_the_default_rounding(self):
        self.assertEqual(Money.rounded("1.005", USD).amount, Decimal("1.00"))
        self.assertEqual(Money.rounded("1.015", USD).amount, Decimal("1.02"))

    def test_quantity_max_scale_enforced(self):
        with self.assertRaises(PrecisionError):
            Quantity("1.123456789", "BTC")
        self.assertEqual(Quantity("1.12345678", "BTC").amount, Decimal("1.12345678"))

    def test_price_allows_finer_scale_than_cash(self):
        p = Price("0.000012345678", USDT)
        self.assertEqual(p.amount, Decimal("0.000012345678"))
        with self.assertRaises(PrecisionError):
            Price("0.0000123456789", USDT)


class TestCurrencySafety(unittest.TestCase):
    def test_add_across_currencies_raises(self):
        with self.assertRaises(CurrencyMismatch):
            Money("1.00", USD) + Money("1.00", INR)

    def test_subtract_across_currencies_raises(self):
        with self.assertRaises(CurrencyMismatch):
            Money("1.00", USD) - Money("1.00", INR)

    def test_ordering_across_currencies_raises(self):
        with self.assertRaises(CurrencyMismatch):
            Money("1.00", USD) < Money("2.00", INR)

    def test_equality_across_currencies_is_false_not_an_error(self):
        # Ordering raises, but equality must stay total so Money is hashable
        # and usable as a dict key without surprise exceptions.
        self.assertNotEqual(Money("1.00", USD), Money("1.00", INR))
        self.assertFalse(Money("1.00", USD) == Money("1.00", INR))

    def test_money_is_hashable(self):
        s = {Money("1.00", USD), Money("1.00", USD), Money("2.00", USD)}
        self.assertEqual(len(s), 2)

    def test_quantity_asset_mismatch_raises(self):
        with self.assertRaises(CurrencyMismatch):
            Quantity("1", "BTC") + Quantity("1", "ETH")

    def test_add_non_money_raises_typeerror(self):
        with self.assertRaises(TypeError):
            Money("1.00", USD) + 1

    def test_currency_code_validated(self):
        with self.assertRaises(ValueError):
            Currency("usd", 2)
        with self.assertRaises(ValueError):
            Currency("", 2)
        with self.assertRaises(ValueError):
            Currency("USD", -1)


class TestConservativeRounding(unittest.TestCase):
    def test_notional_rounds_up_by_default(self):
        """Exposure must never be understated by rounding."""
        price = Price("100.005", USD)
        qty = Quantity("1", "BTC")
        self.assertEqual(price.notional(qty), Money("100.01", USD))

    def test_notional_of_short_position_is_negative_but_magnitude_rounds_up(self):
        price = Price("100.005", USD)
        qty = Quantity("-1", "BTC")
        self.assertEqual(price.notional(qty), Money("-100.01", USD))

    def test_floor_to_step_rounds_down(self):
        """A lot-size adjustment must never increase approved exposure."""
        q = Quantity("1.23456789", "BTC")
        self.assertEqual(q.floor_to_step("0.001"), Quantity("1.234", "BTC"))
        self.assertEqual(q.floor_to_step("0.01"), Quantity("1.23", "BTC"))
        self.assertEqual(q.floor_to_step("1"), Quantity("1", "BTC"))

    def test_floor_to_step_can_reach_zero(self):
        q = Quantity("0.0005", "BTC")
        self.assertTrue(q.floor_to_step("0.001").is_zero)

    def test_floor_to_step_rejects_non_positive_step(self):
        q = Quantity("1", "BTC")
        with self.assertRaises(ValueError):
            q.floor_to_step("0")
        with self.assertRaises(ValueError):
            q.floor_to_step("-1")


class TestAllocationConservation(unittest.TestCase):
    def test_split_conserves_total(self):
        m = Money("100.00", USD)
        parts = m.allocate([1, 1, 1])
        self.assertEqual(sum(p.amount for p in parts), Decimal("100.00"))
        self.assertEqual([str(p) for p in parts], ["33.34 USD", "33.33 USD", "33.33 USD"])

    def test_weighted_split_conserves_total(self):
        m = Money("10.00", USD)
        parts = m.allocate([70, 30])
        self.assertEqual(sum(p.amount for p in parts), Decimal("10.00"))
        self.assertEqual(parts[0], Money("7.00", USD))
        self.assertEqual(parts[1], Money("3.00", USD))

    def test_crypto_precision_split_conserves_total(self):
        m = Money("1.00000001", USDT)
        parts = m.allocate([1, 1, 1])
        self.assertEqual(sum(p.amount for p in parts), Decimal("1.00000001"))

    def test_invalid_weights_rejected(self):
        m = Money("10.00", USD)
        with self.assertRaises(ValueError):
            m.allocate([])
        with self.assertRaises(ValueError):
            m.allocate([0, 0])
        with self.assertRaises(ValueError):
            m.allocate([-1, 2])


class TestArithmeticGuards(unittest.TestCase):
    def test_divide_by_zero_raises(self):
        with self.assertRaises(ZeroDivisionError):
            Money("10.00", USD).divided_by(0)

    def test_ratio_to_zero_raises(self):
        with self.assertRaises(ZeroDivisionError):
            Money("10.00", USD).ratio_to(Money.zero(USD))

    def test_ratio_is_unrounded(self):
        r = Money("10.00", USD).ratio_to(Money("3.00", USD))
        self.assertGreater(r, Decimal("3.3333"))
        self.assertLess(r, Decimal("3.3334"))

    def test_price_must_be_positive(self):
        with self.assertRaises(ValueError):
            Price("0", USD)
        with self.assertRaises(ValueError):
            Price("-1", USD)

    def test_negation_and_abs(self):
        self.assertEqual(-Money("1.00", USD), Money("-1.00", USD))
        self.assertEqual(abs(Money("-1.00", USD)), Money("1.00", USD))
        self.assertEqual(abs(Quantity("-2", "BTC")), Quantity("2", "BTC"))

    def test_predicates(self):
        self.assertTrue(Money.zero(USD).is_zero)
        self.assertTrue(Money("1.00", USD).is_positive)
        self.assertTrue(Money("-1.00", USD).is_negative)

    def test_total_of_empty_is_zero(self):
        self.assertEqual(total([], USD), Money.zero(USD))

    def test_total_sums(self):
        self.assertEqual(
            total([Money("1.00", USD), Money("2.50", USD)], USD), Money("3.50", USD)
        )


class TestRendering(unittest.TestCase):
    def test_str_pads_to_currency_precision(self):
        self.assertEqual(str(Money("1", USD)), "1.00 USD")
        self.assertEqual(str(Money("1", BTC)), "1.00000000 BTC")

    def test_repr_is_unambiguous_and_shows_no_float(self):
        self.assertEqual(repr(Money("1.23", USD)), "Money('1.23', USD)")
        self.assertNotIn(".0000000000000", repr(Money("1.23", USD)))

    def test_large_value_str_does_not_overflow_context(self):
        # Guards against relying on the ambient 28-digit default context.
        big = Money(Decimal("1234567890123456789012345678901.23"), USD)
        self.assertTrue(str(big).endswith(" USD"))


class TestCanonicalDecimalText(unittest.TestCase):
    """Identity derivation must be scale-independent (supports INVARIANT 12)."""

    def test_equal_numbers_produce_equal_text(self):
        pairs = [
            ("0.5", "0.50"),
            ("0.5", "0.500000"),
            ("100", "1E+2"),
            ("100", "100.00"),
            ("0", "0.00"),
            ("0", "-0"),
            ("1", "1.0"),
            ("-2.5", "-2.500"),
        ]
        for left, right in pairs:
            with self.subTest(pair=(left, right)):
                self.assertEqual(Decimal(left), Decimal(right))
                self.assertEqual(
                    canonical_decimal_text(Decimal(left)),
                    canonical_decimal_text(Decimal(right)),
                )

    def test_different_numbers_produce_different_text(self):
        values = ["0.5", "0.51", "5", "50", "0.05", "-0.5"]
        rendered = {canonical_decimal_text(Decimal(v)) for v in values}
        self.assertEqual(len(rendered), len(values))

    def test_str_would_have_disagreed(self):
        # Demonstrates why the helper exists at all.
        self.assertNotEqual(str(Decimal("0.5")), str(Decimal("0.50")))

    def test_no_rounding_even_beyond_context_precision(self):
        # 40 significant digits: more than FINANCIAL_CONTEXT's prec of 34.
        # normalize() would round here; as_tuple() cannot.
        long_value = Decimal("1." + "1" * 39)
        text = canonical_decimal_text(long_value)
        self.assertEqual(text, "1" + "1" * 39 + "E-39")

    def test_float_is_still_rejected(self):
        with self.assertRaises(TypeError):
            canonical_decimal_text(0.5)

    def test_accepts_int_and_str(self):
        self.assertEqual(canonical_decimal_text(100), canonical_decimal_text("100.0"))


if __name__ == "__main__":
    unittest.main()
