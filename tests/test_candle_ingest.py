"""Tests for the candle/bar ingestion driver.

A scripted source yields programmed batches (or raises, for a disconnect), so the
ingestor is driven deterministically. The tests pin: a normal stream applied and
readable from the store; a mixed batch applying the good and counting the bad
without crashing; duplicates and out-of-order bars discarded; a gap reported with
the missing bars **not** fabricated; an interval change rejected; a disconnect
interrupting without raising and **retaining** the real history; a reconnect
resuming and reporting the gap across the outage; deterministic replay. A final
test proves the validated, ingested candles feed the existing indicator layer.
"""

from __future__ import annotations

import datetime as dt
import unittest
from typing import Sequence

from trading.adapters.marketdata import (
    CandleIngestor,
    CandleNormalizer,
    FeedDisconnected,
    FeedState,
    InMemoryCandleStore,
    MarketDataSource,
    RawMessage,
)
from trading.core.clock import UTC, ManualClock
from trading.strategy.indicators import atr, sma

SYMBOL = "BTCUSD"
BASE = dt.datetime(2026, 1, 1, tzinfo=UTC)


def at(index: int) -> str:
    return (BASE + dt.timedelta(seconds=60 * index)).isoformat()


def bar(index, *, o="100", h="102", low="99", c="101", v="5", symbol=SYMBOL, interval=None) -> dict:
    message = {
        "symbol": symbol,
        "open_time": at(index),
        "open": o,
        "high": h,
        "low": low,
        "close": c,
        "volume": v,
    }
    if interval is not None:
        message["interval_seconds"] = interval
    return message


class ScriptedSource(MarketDataSource):
    def __init__(self, script: Sequence[object]) -> None:
        self._script = list(script)
        self._index = 0

    def poll(self) -> Sequence[RawMessage]:
        if self._index >= len(self._script):
            return []
        item = self._script[self._index]
        self._index += 1
        if isinstance(item, Exception):
            raise item
        return item  # type: ignore[return-value]


def build(script):
    clock = ManualClock(BASE)
    store = InMemoryCandleStore()
    ingestor = CandleIngestor(
        source=ScriptedSource(script),
        normalizer=CandleNormalizer(clock=clock, volume_asset="BTC", default_interval_seconds=60),
        store=store,
        clock=clock,
    )
    return ingestor, store


class CandleIngestTest(unittest.TestCase):
    def test_a_normal_stream_is_applied(self) -> None:
        ingestor, store = build([[bar(0), bar(1), bar(2)]])
        report = ingestor.poll_once()
        self.assertEqual(report.state, "connected")
        self.assertEqual(report.accepted, (SYMBOL, SYMBOL, SYMBOL))
        self.assertEqual(report.rejected, ())
        self.assertEqual(report.gaps, ())
        self.assertEqual(len(store.candles(SYMBOL)), 3)

    def test_a_mixed_batch_applies_the_good_and_counts_the_bad(self) -> None:
        # The middle message is not a usable bar (no symbol); it is rejected
        # without disturbing the open_time grid of the two good bars.
        ingestor, store = build([[bar(0), {"open": "1"}, bar(1)]])
        report = ingestor.poll_once()
        self.assertEqual(len(report.rejected), 1)
        self.assertEqual(report.gaps, ())
        self.assertEqual(len(store.candles(SYMBOL)), 2)

    def test_malformed_messages_never_raise(self) -> None:
        ingestor, store = build([[{"garbage": True}, "not a dict", 42]])
        report = ingestor.poll_once()  # must not raise
        self.assertEqual(len(report.rejected), 3)
        self.assertEqual(len(store.candles(SYMBOL)), 0)

    def test_a_duplicate_open_time_is_discarded(self) -> None:
        ingestor, store = build([[bar(0)], [bar(0)]])
        ingestor.poll_once()
        report = ingestor.poll_once()
        self.assertEqual(report.duplicates, 1)
        self.assertEqual(len(store.candles(SYMBOL)), 1)

    def test_an_out_of_order_bar_is_discarded(self) -> None:
        ingestor, store = build([[bar(2)], [bar(1)]])
        ingestor.poll_once()
        report = ingestor.poll_once()
        self.assertEqual(report.out_of_order, 1)
        self.assertEqual(len(store.candles(SYMBOL)), 1)

    def test_a_gap_is_reported_but_the_newer_bar_is_applied(self) -> None:
        ingestor, store = build([[bar(0)], [bar(3)]])  # t0 then t0+180 => 2 missing
        ingestor.poll_once()
        report = ingestor.poll_once()
        self.assertEqual(len(report.gaps), 1)
        gap = report.gaps[0]
        self.assertEqual(gap.missing, 2)
        self.assertEqual(gap.expected, at(1))
        self.assertEqual(gap.got, at(3))
        self.assertEqual(len(store.candles(SYMBOL)), 2, "missing bars are not fabricated")

    def test_an_interval_change_is_rejected(self) -> None:
        ingestor, store = build([[bar(0, interval=60)], [bar(1, interval=30)]])
        ingestor.poll_once()
        report = ingestor.poll_once()
        self.assertEqual(len(report.rejected), 1)
        self.assertIn("interval", report.rejected[0].reason)
        self.assertEqual(len(store.candles(SYMBOL)), 1)

    def test_a_disconnect_interrupts_without_raising_and_keeps_history(self) -> None:
        ingestor, store = build([[bar(0), bar(1)], FeedDisconnected("drop")])
        ingestor.poll_once()
        self.assertEqual(len(store.candles(SYMBOL)), 2)
        report = ingestor.poll_once()  # the source raises; the ingestor absorbs it
        self.assertEqual(report.state, "interrupted")
        self.assertEqual(ingestor.state, FeedState.INTERRUPTED)
        self.assertEqual(len(store.candles(SYMBOL)), 2, "real history is retained, not dropped")

    def test_a_reconnect_resumes_and_reports_the_gap_across_the_outage(self) -> None:
        ingestor, store = build([[bar(0)], FeedDisconnected("x"), [bar(3)]])
        ingestor.poll_once()
        ingestor.poll_once()  # disconnect
        report = ingestor.poll_once()  # reconnect at t0+180
        self.assertEqual(report.state, "connected")
        self.assertEqual(len(report.gaps), 1)
        self.assertEqual(report.gaps[0].missing, 2)
        self.assertEqual(len(store.candles(SYMBOL)), 2)

    def test_identical_scripts_produce_identical_reports(self) -> None:
        script = [[bar(0)], [bar(1)]]
        first, _ = build([list(batch) for batch in script])
        second, _ = build([list(batch) for batch in script])
        reports_a = [first.poll_once().as_details() for _ in script]
        reports_b = [second.poll_once().as_details() for _ in script]
        self.assertEqual(reports_a, reports_b)

    def test_the_observation_surface_reflects_the_last_poll(self) -> None:
        ingestor, _ = build([[bar(0)]])
        report = ingestor.poll_once()
        self.assertEqual(ingestor.passes, 1)
        self.assertIs(ingestor.last_report, report)
        self.assertEqual(ingestor.state, FeedState.CONNECTED)

    def test_an_unclassified_source_error_is_treated_as_a_disconnect(self) -> None:
        ingestor, store = build([[bar(0)], RuntimeError("boom")])
        ingestor.poll_once()
        report = ingestor.poll_once()
        self.assertEqual(report.state, "interrupted")
        self.assertIn("RuntimeError", report.error or "")
        self.assertEqual(len(store.candles(SYMBOL)), 1, "history retained")

    def test_healthy_is_true_only_for_a_clean_poll(self) -> None:
        ingestor, _ = build([[bar(0)], [bar(3)], [{"open": "1"}], FeedDisconnected("x")])
        self.assertTrue(ingestor.poll_once().healthy, "clean poll")
        self.assertFalse(ingestor.poll_once().healthy, "a gap is not healthy")
        self.assertFalse(ingestor.poll_once().healthy, "a rejection is not healthy")
        self.assertFalse(ingestor.poll_once().healthy, "an interruption is not healthy")

    def test_a_gap_serializes_into_the_report_details(self) -> None:
        import json

        ingestor, _ = build([[bar(0)], [bar(3)]])
        ingestor.poll_once()
        details = ingestor.poll_once().as_details()
        self.assertEqual(
            details["gaps"],
            [{"symbol": SYMBOL, "expected": at(1), "got": at(3), "missing": 2}],
        )
        json.dumps(details)  # audit-safe: plain JSON types only


class CandleStoreTest(unittest.TestCase):
    def _candle(self, index: int):
        norm = CandleNormalizer(clock=ManualClock(BASE), volume_asset="BTC", default_interval_seconds=60)
        return norm.normalize(bar(index))

    def test_store_query_surface(self) -> None:
        store = InMemoryCandleStore()
        first, second = self._candle(0), self._candle(1)
        store.append(first)
        store.append(second)
        self.assertEqual(store.symbols(), [SYMBOL])
        self.assertEqual(store.last(SYMBOL), second)
        self.assertEqual(store.interval(SYMBOL), 60)
        self.assertEqual(len(store), 2)
        self.assertIsNone(store.last("NOPE"))
        self.assertIsNone(store.interval("NOPE"))

    def test_append_rejects_a_non_candle(self) -> None:
        store = InMemoryCandleStore()
        with self.assertRaises(TypeError):
            store.append("not a candle")  # type: ignore[arg-type]


class IndicatorConsumptionTest(unittest.TestCase):
    def test_ingested_candles_feed_the_existing_indicators(self) -> None:
        # Validated, ingested candles are directly consumable by the existing
        # pure indicator functions -- no execution, no new strategy.
        bars = [
            bar(i, o=str(100 + i), h=str(102 + i), low=str(99 + i), c=str(101 + i))
            for i in range(5)
        ]
        ingestor, store = build([bars])
        ingestor.poll_once()
        candles = store.candles(SYMBOL)
        self.assertEqual(len(candles), 5)
        self.assertIsNotNone(sma(candles, 3))
        self.assertIsNotNone(atr(candles, 3))


if __name__ == "__main__":
    unittest.main()
