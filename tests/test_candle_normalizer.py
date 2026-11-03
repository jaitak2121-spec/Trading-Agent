"""Tests for the candle/bar normalizer.

Mirrors the quote normalizer's tests on the bar side: what it accepts, what it
refuses (always as a `MalformedMessage`, never a crash), OHLC coherence delegated
to `Candle`, and the same timestamp rule for `open_time` (venue stamp honored,
absent falls back to receipt time).
"""

from __future__ import annotations

import datetime as dt
import unittest

from trading.adapters.marketdata import CandleNormalizer, MalformedMessage
from trading.core.clock import UTC, ManualClock
from trading.core.marketdata import Candle
from trading.core.money import USD, Price, Quantity


def bar(**overrides) -> dict:
    message = {
        "symbol": "BTCUSD",
        "open_time": "2026-01-01T00:00:00Z",
        "interval_seconds": 60,
        "open": "100",
        "high": "101",
        "low": "99",
        "close": "100.5",
        "volume": "5",
    }
    message.update(overrides)
    return message


class CandleNormalizerTest(unittest.TestCase):
    def setUp(self) -> None:
        self.clock = ManualClock(dt.datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC))
        self.norm = CandleNormalizer(clock=self.clock, volume_asset="BTC")

    def test_a_well_formed_bar_becomes_a_candle(self) -> None:
        candle = self.norm.normalize(bar())
        self.assertIsInstance(candle, Candle)
        self.assertEqual(candle.symbol, "BTCUSD")
        self.assertEqual(candle.interval_seconds, 60)
        self.assertEqual(candle.open, Price("100", USD))
        self.assertEqual(candle.high, Price("101", USD))
        self.assertEqual(candle.low, Price("99", USD))
        self.assertEqual(candle.close, Price("100.5", USD))
        self.assertEqual(candle.volume, Quantity("5", "BTC"))
        self.assertEqual(candle.open_time, dt.datetime(2026, 1, 1, tzinfo=UTC))

    def test_a_float_price_is_refused(self) -> None:
        with self.assertRaises(MalformedMessage):
            self.norm.normalize(bar(close=100.5))

    def test_a_missing_field_is_refused(self) -> None:
        message = bar()
        del message["high"]
        with self.assertRaises(MalformedMessage):
            self.norm.normalize(message)

    def test_low_above_high_is_refused(self) -> None:
        with self.assertRaises(MalformedMessage):
            self.norm.normalize(bar(low="200"))

    def test_close_outside_the_range_is_refused(self) -> None:
        with self.assertRaises(MalformedMessage):
            self.norm.normalize(bar(close="500"))

    def test_a_negative_volume_is_refused(self) -> None:
        with self.assertRaises(MalformedMessage):
            self.norm.normalize(bar(volume="-1"))

    def test_a_non_numeric_price_is_refused(self) -> None:
        with self.assertRaises(MalformedMessage):
            self.norm.normalize(bar(open="abc"))

    def test_a_non_mapping_is_refused(self) -> None:
        with self.assertRaises(MalformedMessage):
            self.norm.normalize(["not", "a", "mapping"])  # type: ignore[arg-type]

    def test_an_epoch_open_time_is_parsed(self) -> None:
        ts = dt.datetime(2026, 1, 1, tzinfo=UTC)
        candle = self.norm.normalize(bar(open_time=int(ts.timestamp() * 1000)))
        self.assertEqual(candle.open_time, ts)

    def test_an_absent_open_time_uses_receipt_time(self) -> None:
        message = bar()
        del message["open_time"]
        candle = self.norm.normalize(message)
        self.assertEqual(candle.open_time, self.clock.now())

    def test_an_unparseable_open_time_is_refused(self) -> None:
        with self.assertRaises(MalformedMessage):
            self.norm.normalize(bar(open_time="not-a-time"))

    def test_interval_from_the_message_is_used(self) -> None:
        candle = self.norm.normalize(bar(interval_seconds=300))
        self.assertEqual(candle.interval_seconds, 300)

    def test_a_default_interval_is_used_when_absent(self) -> None:
        norm = CandleNormalizer(clock=self.clock, volume_asset="BTC", default_interval_seconds=60)
        message = bar()
        del message["interval_seconds"]
        candle = norm.normalize(message)
        self.assertEqual(candle.interval_seconds, 60)

    def test_a_missing_interval_with_no_default_is_refused(self) -> None:
        message = bar()
        del message["interval_seconds"]
        with self.assertRaises(MalformedMessage):
            self.norm.normalize(message)

    def test_a_non_positive_interval_is_refused(self) -> None:
        with self.assertRaises(MalformedMessage):
            self.norm.normalize(bar(interval_seconds=0))

    def test_interval_as_a_numeric_string_is_used(self) -> None:
        candle = self.norm.normalize(bar(interval_seconds="300"))
        self.assertEqual(candle.interval_seconds, 300)

    def test_interval_as_a_non_numeric_string_is_refused(self) -> None:
        with self.assertRaises(MalformedMessage):
            self.norm.normalize(bar(interval_seconds="abc"))

    def test_interval_of_the_wrong_type_is_refused(self) -> None:
        with self.assertRaises(MalformedMessage):
            self.norm.normalize(bar(interval_seconds=[60]))

    def test_the_constructor_rejects_a_bad_default_interval(self) -> None:
        with self.assertRaises(ValueError):
            CandleNormalizer(clock=self.clock, default_interval_seconds=0)

    def test_decimal_valued_ohlcv_is_accepted(self) -> None:
        # A JSON decoder configured with parse_float=Decimal hands over Decimals:
        # the canonical float-free way to ingest money, so it must be accepted.
        from decimal import Decimal

        candle = self.norm.normalize(
            bar(open=Decimal("100"), high=Decimal("101"), low=Decimal("99"),
                close=Decimal("100.5"), volume=Decimal("5"))
        )
        self.assertEqual(candle.close, Price("100.5", USD))

    def test_int_valued_ohlcv_is_accepted(self) -> None:
        candle = self.norm.normalize(bar(open=100, high=102, low=99, close=101, volume=5))
        self.assertEqual(candle.high, Price("102", USD))
        self.assertEqual(candle.volume, Quantity("5", "BTC"))

    def test_a_bool_interval_is_refused(self) -> None:
        # bool is an int subclass; True must not be read as a 1-second interval.
        with self.assertRaises(MalformedMessage):
            self.norm.normalize(bar(interval_seconds=True))


if __name__ == "__main__":
    unittest.main()
