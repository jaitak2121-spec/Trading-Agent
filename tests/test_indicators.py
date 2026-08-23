"""Tests for the indicator functions.

The point of these is not that the arithmetic is right -- a mean is a mean -- but
that the failure modes are the safe ones:

* Insufficient data returns ``None``, never a shorter-window answer wearing the
  requested window's name. A strategy handed a "20-period mean" computed from 12
  bars would size a real position off it.
* Every result is a ``Decimal`` or a ``Price``. Never a ``float`` (INVARIANT 8).
* Bad input raises instead of producing a plausible number. Unordered bars and
  mixed currencies are the two ways a caller silently gets nonsense.
* :func:`true_range` accounts for gaps. The plain high-low range understates a
  gap open, and a stop placed off it would be far too tight -- so the gap terms
  are tested with an actual gap rather than assumed.

Expected values are computed by hand in the test, not by reimplementing the
function, so a test failure means the answer changed rather than that two copies
of the same bug disagree.
"""

from __future__ import annotations

import unittest
from datetime import datetime, timedelta
from decimal import Decimal

from trading.core.clock import UTC
from trading.core.marketdata import Candle
from trading.core.money import USD, USDT, Price, Quantity
from trading.strategy.indicators import (
    atr,
    ema,
    highest_high,
    lowest_low,
    rsi,
    sma,
    true_range,
)

SYMBOL = "BTCUSD"
T0 = datetime(2026, 1, 1, 0, 0, tzinfo=UTC)
INTERVAL = 60


def candle(
    index: int,
    close: object,
    *,
    high: object | None = None,
    low: object | None = None,
    open_: object | None = None,
    currency=USD,
    symbol: str = SYMBOL,
) -> Candle:
    """One bar at slot ``index``, defaulting to a symmetric range around ``close``.

    The default half-range is 10, narrowing for closes small enough that 10
    either side would put the low at or below zero -- prices are strictly
    positive, so a bar around a close of 1 cannot span 20.
    """
    value = Decimal(str(close))
    offset = Decimal(10) if value > Decimal(20) else value / 2
    return Candle(
        symbol=symbol,
        open_time=T0 + timedelta(seconds=INTERVAL * index),
        interval_seconds=INTERVAL,
        open=Price(Decimal(str(open_)) if open_ is not None else value, currency),
        high=Price(
            Decimal(str(high)) if high is not None else value + offset, currency
        ),
        low=Price(Decimal(str(low)) if low is not None else value - offset, currency),
        close=Price(value, currency),
        volume=Quantity("1", "BTC"),
        source="test",
    )


def series(closes: list[object], **kwargs: object) -> list[Candle]:
    return [candle(i, c, **kwargs) for i, c in enumerate(closes)]  # type: ignore[arg-type]


class TestTooLittleDataReturnsNone(unittest.TestCase):
    """The most important property here: no partial answers.

    Every windowed indicator has to be able to say "I cannot answer that yet",
    and it has to say it distinguishably from a number. A 20-period mean of 12
    bars is not a 20-period mean.
    """

    def test_sma_needs_period_bars(self) -> None:
        bars = series([100] * 4)
        self.assertIsNone(sma(bars, 5))
        self.assertIsNotNone(sma(bars, 4))

    def test_ema_needs_period_bars(self) -> None:
        bars = series([100] * 4)
        self.assertIsNone(ema(bars, 5))
        self.assertIsNotNone(ema(bars, 4))

    def test_atr_needs_one_more_bar_than_its_period(self) -> None:
        # The first true range needs a prior close to measure the gap against,
        # so period bars is one short.
        bars = series([100] * 5)
        self.assertIsNone(atr(bars, 5))
        self.assertIsNotNone(atr(bars, 4))

    def test_rsi_needs_one_more_bar_than_its_period(self) -> None:
        bars = series([100, 101, 102, 103, 104])
        self.assertIsNone(rsi(bars, 5))
        self.assertIsNotNone(rsi(bars, 4))

    def test_extremes_need_period_bars(self) -> None:
        bars = series([100] * 3)
        self.assertIsNone(highest_high(bars, 4))
        self.assertIsNone(lowest_low(bars, 4))

    def test_no_bars_at_all_is_not_an_error(self) -> None:
        # A strategy's first invocation has no history. That is an ordinary
        # state, not a bug, so it must not raise.
        self.assertIsNone(sma([], 5))
        self.assertIsNone(atr([], 5))
        self.assertIsNone(rsi([], 5))
        self.assertIsNone(ema([], 5))


class TestInputIsValidatedNotTrusted(unittest.TestCase):
    """Bad bars raise. A plausible-looking wrong number is the worse outcome."""

    def test_unordered_bars_are_refused(self) -> None:
        bars = series([100, 101, 102])
        shuffled = [bars[2], bars[0], bars[1]]
        with self.assertRaises(ValueError) as caught:
            sma(shuffled, 3)
        self.assertIn("chronological", str(caught.exception))

    def test_mixed_currencies_are_refused(self) -> None:
        bars = series([100, 101]) + [candle(2, 102, currency=USDT)]
        with self.assertRaises(ValueError) as caught:
            sma(bars, 3)
        self.assertIn("mix currencies", str(caught.exception))

    def test_non_candles_are_refused(self) -> None:
        with self.assertRaises(TypeError):
            sma([candle(0, 100), "not a candle"], 2)  # type: ignore[list-item]

    def test_a_non_integer_period_is_refused(self) -> None:
        bars = series([100] * 5)
        with self.assertRaises(TypeError):
            sma(bars, "3")  # type: ignore[arg-type]
        with self.assertRaises(TypeError):
            sma(bars, 3.0)  # type: ignore[arg-type]

    def test_a_boolean_period_is_refused(self) -> None:
        # True == 1 in Python, so a bool would silently become a 1-period mean.
        with self.assertRaises(TypeError):
            sma(series([100] * 5), True)  # type: ignore[arg-type]

    def test_a_non_positive_period_is_refused(self) -> None:
        bars = series([100] * 5)
        for bad in (0, -1):
            with self.subTest(period=bad), self.assertRaises(ValueError):
                sma(bars, bad)

    def test_validation_runs_before_the_length_check(self) -> None:
        # Otherwise a caller with too little data never learns their bars are
        # unusable, and finds out later when they finally have enough.
        with self.assertRaises(ValueError):
            sma(series([100, 101])[::-1], 10)


class TestSimpleMovingAverage(unittest.TestCase):
    def test_it_averages_only_the_last_period_closes(self) -> None:
        bars = series([1, 2, 3, 100, 200, 300])
        # (100 + 200 + 300) / 3 -- the early bars must not leak in.
        self.assertEqual(sma(bars, 3), Price("200", USD))

    def test_the_result_is_a_price_in_the_bars_currency(self) -> None:
        result = sma(series([10, 20], currency=USDT), 2)
        assert result is not None
        self.assertIsInstance(result, Price)
        self.assertEqual(result.currency, USDT)

    def test_a_non_terminating_division_is_rounded_not_rejected(self) -> None:
        # 100/3 has no finite decimal expansion. Price refuses more than twelve
        # decimal places, so the mean has to be rounded somewhere -- and it must
        # be here, deliberately, rather than as an exception the caller sees.
        result = sma(series([1, 1, 98]), 3)
        assert result is not None
        self.assertEqual(result.amount, Decimal("33.333333333333"))

    def test_a_whole_number_mean_keeps_its_scale(self) -> None:
        # Not cosmetic: this value is stringified into signal evidence and audit
        # records, and "50000" is readable where "50000.000000000000" is not.
        result = sma(series([100, 200]), 2)
        assert result is not None
        self.assertEqual(str(result.amount), "150")


class TestExponentialMovingAverage(unittest.TestCase):
    def test_it_is_seeded_with_the_simple_mean_of_the_first_window(self) -> None:
        # With exactly period bars there is nothing to smooth, so the EMA must
        # equal the SMA. Seeding from the first close instead would not.
        bars = series([10, 20, 30])
        self.assertEqual(ema(bars, 3), sma(bars, 3))

    def test_it_weights_recent_bars_more_heavily_than_the_simple_mean(self) -> None:
        bars = series([100, 100, 100, 100, 200])
        fast, slow = ema(bars, 4), sma(bars, 4)
        assert fast is not None and slow is not None
        self.assertGreater(fast.amount, slow.amount)

    def test_the_smoothing_factor_is_two_over_period_plus_one(self) -> None:
        # Hand-computed: seed = 100, multiplier = 2/3, next close = 200
        # => (200 - 100) * 2/3 + 100 = 166.666...
        bars = series([100, 100, 200])
        result = ema(bars, 2)
        assert result is not None
        self.assertEqual(result.amount, Decimal("166.666666666667"))


class TestTrueRange(unittest.TestCase):
    def test_without_a_previous_bar_it_is_the_high_low_span(self) -> None:
        bar = candle(0, 100, high=110, low=90)
        self.assertEqual(true_range(bar), Decimal(20))

    def test_a_gap_up_is_measured_from_the_previous_close(self) -> None:
        # This is the whole reason true range exists. The bar's own span is 10,
        # but price actually travelled 60 from the prior close, and a stop sized
        # off 10 would be six times too tight.
        previous = candle(0, 100, high=105, low=95)
        current = candle(1, 155, high=160, low=150)
        self.assertEqual(true_range(current, previous), Decimal(60))

    def test_a_gap_down_is_measured_from_the_previous_close(self) -> None:
        previous = candle(0, 100, high=105, low=95)
        current = candle(1, 45, high=50, low=40)
        self.assertEqual(true_range(current, previous), Decimal(60))

    def test_an_inside_bar_falls_back_to_its_own_span(self) -> None:
        previous = candle(0, 100, high=120, low=80)
        current = candle(1, 100, high=105, low=95)
        self.assertEqual(true_range(current, previous), Decimal(10))

    def test_it_refuses_a_non_candle(self) -> None:
        with self.assertRaises(TypeError):
            true_range("nope")  # type: ignore[arg-type]
        with self.assertRaises(TypeError):
            true_range(candle(1, 100), "nope")  # type: ignore[arg-type]

    def test_it_refuses_bars_in_different_currencies(self) -> None:
        with self.assertRaises(ValueError):
            true_range(candle(1, 100), candle(0, 100, currency=USDT))


class TestAverageTrueRange(unittest.TestCase):
    def test_it_is_a_bare_decimal_not_a_price(self) -> None:
        # A range is a distance, not a level. Typing it as a Price would invite
        # someone to compare it against one, which is meaningless.
        result = atr(series([100] * 5), 4)
        self.assertIsInstance(result, Decimal)
        self.assertNotIsInstance(result, Price)

    def test_a_constant_range_averages_to_that_range(self) -> None:
        # Every bar spans 20 with no gaps, so every true range is 20.
        self.assertEqual(atr(series([100] * 6), 5), Decimal(20))

    def test_it_averages_over_the_last_period_ranges_only(self) -> None:
        # First three bars span 20; last two span 2. A 2-period ATR must see
        # only the narrow ones.
        bars = series([100, 100, 100]) + [
            candle(3, 100, high=101, low=99),
            candle(4, 100, high=101, low=99),
        ]
        self.assertEqual(atr(bars, 2), Decimal(2))

    def test_a_flat_market_has_zero_range(self) -> None:
        # The value a strategy must refuse to place a stop against: there is no
        # volatility here, so there is no risk-defined position.
        bars = [candle(i, 100, high=100, low=100, open_=100) for i in range(5)]
        self.assertEqual(atr(bars, 4), Decimal(0))


class TestRelativeStrengthIndex(unittest.TestCase):
    def test_a_monotonic_rise_with_no_losses_reads_one_hundred(self) -> None:
        # Returning None here would conflate "maximally overbought" with "not
        # enough data" -- opposite instructions to a strategy.
        self.assertEqual(rsi(series([1, 2, 3, 4, 5]), 4), Decimal(100))

    def test_a_monotonic_fall_reads_zero(self) -> None:
        self.assertEqual(rsi(series([50, 40, 30, 20, 10]), 4), Decimal(0))

    def test_a_flat_series_reads_fifty(self) -> None:
        # No gains and no losses: the ratio is 0/0, and the neutral reading is
        # the only defensible answer.
        self.assertEqual(rsi(series([100] * 5), 4), Decimal(50))

    def test_equal_gains_and_losses_read_fifty(self) -> None:
        bars = series([100, 110, 100, 110, 100])
        self.assertEqual(rsi(bars, 4), Decimal(50))

    def test_it_stays_within_zero_and_one_hundred(self) -> None:
        for closes in (
            [100, 130, 90, 140, 80, 150],
            [100, 101, 99, 102, 98, 103],
            [100, 100, 100, 200, 100, 100],
        ):
            with self.subTest(closes=closes):
                value = rsi(series(closes), 5)
                assert value is not None
                self.assertGreaterEqual(value, Decimal(0))
                self.assertLessEqual(value, Decimal(100))


class TestExtremes(unittest.TestCase):
    def test_highest_high_scans_the_window_not_the_last_bar(self) -> None:
        bars = series([100, 100, 100]) + [candle(3, 100, high=999), candle(4, 100)]
        self.assertEqual(highest_high(bars, 3), Price("999", USD))

    def test_highest_high_ignores_bars_before_the_window(self) -> None:
        bars = [candle(0, 100, high=999)] + series([100, 100, 100])[1:]
        self.assertEqual(highest_high(bars, 2), Price("110", USD))

    def test_lowest_low_scans_the_window(self) -> None:
        bars = series([100, 100]) + [candle(2, 100, low=1)]
        self.assertEqual(lowest_low(bars, 3), Price("1", USD))

    def test_lowest_low_ignores_bars_before_the_window(self) -> None:
        bars = [candle(0, 100, low=1)] + series([100, 100, 100])[1:]
        self.assertEqual(lowest_low(bars, 2), Price("90", USD))


class TestIndicatorsAreStateless(unittest.TestCase):
    """No indicator may remember anything between calls.

    An indicator that carries state gives a different answer on the second run
    over the same bars, which is how a backtest and a live session disagree for
    reasons nobody can find.
    """

    def test_repeated_calls_over_the_same_bars_agree(self) -> None:
        bars = series([100, 105, 103, 110, 108, 115, 112])
        for name, first, second in (
            ("sma", sma(bars, 3), sma(bars, 3)),
            ("ema", ema(bars, 3), ema(bars, 3)),
            ("atr", atr(bars, 3), atr(bars, 3)),
            ("rsi", rsi(bars, 3), rsi(bars, 3)),
            ("highest", highest_high(bars, 3), highest_high(bars, 3)),
            ("lowest", lowest_low(bars, 3), lowest_low(bars, 3)),
        ):
            with self.subTest(indicator=name):
                self.assertEqual(first, second)

    def test_an_indicator_does_not_mutate_the_bars_it_is_given(self) -> None:
        bars = series([100, 105, 103])
        before = list(bars)
        sma(bars, 2)
        atr(bars, 2)
        rsi(bars, 2)
        self.assertEqual(bars, before)

    def test_a_generator_of_bars_is_accepted_once(self) -> None:
        # _validated materialises its input, so a one-shot iterable does not get
        # silently consumed halfway through a calculation.
        bars = series([100, 200, 300])
        self.assertEqual(sma(iter(bars), 3), Price("200", USD))


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
