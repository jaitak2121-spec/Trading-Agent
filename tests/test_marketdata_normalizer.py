"""Tests for the market-data quote normalizer.

The normalizer is the boundary that turns an untrusted raw message into a
validated :class:`~trading.core.marketdata.Quote`. These tests pin what it
accepts, what it refuses (and that a refusal is a `MalformedMessage`, never a
crash), and the timestamp rule: a venue stamp is honored, an absent one falls
back to receipt time, and a present one is never overridden with "now".
"""

from __future__ import annotations

import datetime as dt
import unittest

from trading.adapters.marketdata import MalformedMessage, QuoteNormalizer
from trading.core.clock import UTC, ManualClock
from trading.core.marketdata import Quote
from trading.core.money import USD, Price


class QuoteNormalizerTest(unittest.TestCase):
    def setUp(self) -> None:
        self.clock = ManualClock(dt.datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC))
        self.norm = QuoteNormalizer(clock=self.clock)

    def test_a_well_formed_message_becomes_a_quote(self) -> None:
        quote = self.norm.normalize(
            {
                "symbol": "BTCUSD",
                "bid": "49990",
                "ask": "50010",
                "timestamp": "2026-01-01T12:00:00Z",
                "sequence": 7,
            }
        )
        self.assertIsInstance(quote, Quote)
        self.assertEqual(quote.symbol, "BTCUSD")
        self.assertEqual(quote.bid, Price("49990", USD))
        self.assertEqual(quote.ask, Price("50010", USD))
        self.assertEqual(quote.sequence, 7)
        self.assertEqual(quote.as_of, dt.datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC))

    def test_a_float_price_is_refused(self) -> None:
        # INVARIANT 8: a float cannot represent money exactly.
        with self.assertRaises(MalformedMessage):
            self.norm.normalize({"symbol": "BTCUSD", "bid": 49990.0, "ask": "50010"})

    def test_a_missing_price_is_refused(self) -> None:
        with self.assertRaises(MalformedMessage):
            self.norm.normalize({"symbol": "BTCUSD", "ask": "50010"})

    def test_a_non_numeric_price_is_refused(self) -> None:
        with self.assertRaises(MalformedMessage):
            self.norm.normalize({"symbol": "BTCUSD", "bid": "abc", "ask": "50010"})

    def test_a_crossed_book_is_refused(self) -> None:
        with self.assertRaises(MalformedMessage):
            self.norm.normalize({"symbol": "BTCUSD", "bid": "50010", "ask": "49990"})

    def test_a_missing_symbol_is_refused(self) -> None:
        with self.assertRaises(MalformedMessage):
            self.norm.normalize({"bid": "1", "ask": "2"})

    def test_a_non_mapping_is_refused(self) -> None:
        with self.assertRaises(MalformedMessage):
            self.norm.normalize(["not", "a", "mapping"])  # type: ignore[arg-type]

    def test_a_bad_sequence_is_refused(self) -> None:
        with self.assertRaises(MalformedMessage):
            self.norm.normalize({"symbol": "BTCUSD", "bid": "1", "ask": "2", "sequence": -1})

    def test_epoch_milliseconds_timestamp(self) -> None:
        ts = dt.datetime(2026, 1, 1, tzinfo=UTC)
        quote = self.norm.normalize(
            {"symbol": "BTCUSD", "bid": "1", "ask": "2", "timestamp": int(ts.timestamp() * 1000)}
        )
        self.assertEqual(quote.as_of, ts)

    def test_epoch_seconds_timestamp(self) -> None:
        ts = dt.datetime(2026, 1, 1, tzinfo=UTC)
        quote = self.norm.normalize(
            {"symbol": "BTCUSD", "bid": "1", "ask": "2", "timestamp": int(ts.timestamp())}
        )
        self.assertEqual(quote.as_of, ts)

    def test_an_absent_timestamp_uses_receipt_time(self) -> None:
        quote = self.norm.normalize({"symbol": "BTCUSD", "bid": "1", "ask": "2"})
        self.assertEqual(quote.as_of, self.clock.now())

    def test_a_present_timestamp_is_not_overridden_with_now(self) -> None:
        quote = self.norm.normalize(
            {"symbol": "BTCUSD", "bid": "1", "ask": "2", "timestamp": "2020-01-01T00:00:00+00:00"}
        )
        self.assertEqual(quote.as_of, dt.datetime(2020, 1, 1, tzinfo=UTC))
        self.assertNotEqual(quote.as_of, self.clock.now())

    def test_a_naive_iso_timestamp_is_assumed_utc(self) -> None:
        quote = self.norm.normalize(
            {"symbol": "BTCUSD", "bid": "1", "ask": "2", "timestamp": "2026-01-01T00:00:00"}
        )
        self.assertEqual(quote.as_of, dt.datetime(2026, 1, 1, tzinfo=UTC))

    def test_an_unparseable_timestamp_is_refused(self) -> None:
        with self.assertRaises(MalformedMessage):
            self.norm.normalize(
                {"symbol": "BTCUSD", "bid": "1", "ask": "2", "timestamp": "not-a-time"}
            )

    def test_configurable_field_names(self) -> None:
        norm = QuoteNormalizer(clock=self.clock, bid_field="b", ask_field="a", symbol_field="s")
        quote = norm.normalize({"s": "ETHUSD", "b": "2990", "a": "3010"})
        self.assertEqual(quote.symbol, "ETHUSD")
        self.assertEqual(quote.bid, Price("2990", USD))

    def test_a_bool_timestamp_is_refused(self) -> None:
        with self.assertRaises(MalformedMessage):
            self.norm.normalize({"symbol": "BTCUSD", "bid": "1", "ask": "2", "timestamp": True})

    def test_a_wrong_type_timestamp_is_refused(self) -> None:
        with self.assertRaises(MalformedMessage):
            self.norm.normalize(
                {"symbol": "BTCUSD", "bid": "1", "ask": "2", "timestamp": ["nope"]}
            )

    def test_an_epoch_in_a_string_is_parsed(self) -> None:
        ts = dt.datetime(2026, 1, 1, tzinfo=UTC)
        quote = self.norm.normalize(
            {"symbol": "BTCUSD", "bid": "1", "ask": "2", "timestamp": str(int(ts.timestamp()))}
        )
        self.assertEqual(quote.as_of, ts)

    def test_an_out_of_range_epoch_is_refused(self) -> None:
        with self.assertRaises(MalformedMessage):
            self.norm.normalize(
                {"symbol": "BTCUSD", "bid": "1", "ask": "2", "timestamp": 10 ** 20}
            )

    def test_a_float_epoch_timestamp_is_parsed(self) -> None:
        # Some feeds send a fractional epoch; a float is fine for a *timestamp*
        # (it is not money, so INVARIANT 8 does not apply here).
        ts = dt.datetime(2026, 1, 1, tzinfo=UTC)
        quote = self.norm.normalize(
            {"symbol": "BTCUSD", "bid": "1", "ask": "2", "timestamp": float(ts.timestamp())}
        )
        self.assertEqual(quote.as_of, ts)


if __name__ == "__main__":
    unittest.main()
