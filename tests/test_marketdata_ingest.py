"""Tests for the market-data ingestion driver.

A ``ScriptedSource`` yields programmed batches (and, where a batch is an
exception, raises it), so the ingestor is driven deterministically. The tests
pin the behaviours the milestone exists for: a mixed batch applies the good and
counts the bad without crashing; duplicates and out-of-order messages are
discarded; a forward gap is reported but the newer quote still lands; a
disconnect marks data unavailable (and never raises); a reconnect resumes and
reports the gap across the outage. The final test proves the point end to end:
a feed outage makes an order refuse at the risk gate, through the real paper
gateway.
"""

from __future__ import annotations

import unittest
from typing import Sequence

from trading.adapters.marketdata import (
    FeedDisconnected,
    FeedState,
    MarketDataIngestor,
    MarketDataSource,
    QuoteNormalizer,
    RawMessage,
)
from trading.adapters.memory.quote_feed import InMemoryQuoteFeed
from trading.core.clock import ManualClock

SYMBOL = "BTCUSD"


def quote_msg(seq=None, *, bid="49990", ask="50010", symbol=SYMBOL, **extra) -> dict:
    message = {"symbol": symbol, "bid": bid, "ask": ask}
    if seq is not None:
        message["sequence"] = seq
    message.update(extra)
    return message


class ScriptedSource(MarketDataSource):
    """Yields each programmed batch in turn; a batch that is an Exception raises."""

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


def build_ingestor(script, *, feed=None, clock=None):
    clock = clock or ManualClock()
    feed = feed or InMemoryQuoteFeed(clock=clock, source="test")
    ingestor = MarketDataIngestor(
        source=ScriptedSource(script),
        normalizer=QuoteNormalizer(clock=clock),
        feed=feed,
        clock=clock,
    )
    return ingestor, feed, clock


class IngestTest(unittest.TestCase):
    def test_a_good_batch_is_applied(self) -> None:
        ingestor, feed, _ = build_ingestor(
            [[quote_msg(1), quote_msg(2, symbol="ETHUSD", bid="2990", ask="3010")]]
        )
        report = ingestor.poll_once()
        self.assertEqual(sorted(report.accepted), ["BTCUSD", "ETHUSD"])
        self.assertEqual(report.state, "connected")
        self.assertEqual(report.rejected, ())
        self.assertIsNotNone(feed.quote("BTCUSD"))
        self.assertIsNotNone(feed.quote("ETHUSD"))

    def test_a_mixed_batch_applies_the_good_and_counts_the_bad(self) -> None:
        ingestor, feed, _ = build_ingestor(
            [[quote_msg(1), {"symbol": "BAD", "bid": 1.5, "ask": "2"}, quote_msg(2, symbol="ETHUSD", bid="2990", ask="3010")]]
        )
        report = ingestor.poll_once()
        self.assertEqual(sorted(report.accepted), ["BTCUSD", "ETHUSD"])
        self.assertEqual(len(report.rejected), 1)
        self.assertIn("INVARIANT 8", report.rejected[0].reason)
        self.assertIsNone(feed.quote("BAD"))

    def test_malformed_messages_never_raise(self) -> None:
        ingestor, _, _ = build_ingestor([[{"garbage": True}, "not a dict", 42]])
        report = ingestor.poll_once()  # must not raise
        self.assertEqual(report.accepted, ())
        self.assertEqual(len(report.rejected), 3)

    def test_a_duplicate_sequence_is_discarded(self) -> None:
        ingestor, _, _ = build_ingestor([[quote_msg(1)], [quote_msg(1)]])
        ingestor.poll_once()
        report = ingestor.poll_once()
        self.assertEqual(report.duplicates, 1)
        self.assertEqual(report.accepted, ())

    def test_an_out_of_order_message_is_discarded(self) -> None:
        ingestor, _, _ = build_ingestor([[quote_msg(5)], [quote_msg(3)]])
        ingestor.poll_once()
        report = ingestor.poll_once()
        self.assertEqual(report.out_of_order, 1)
        self.assertEqual(report.accepted, ())

    def test_a_forward_gap_is_reported_but_the_newer_quote_is_applied(self) -> None:
        ingestor, feed, _ = build_ingestor(
            [[quote_msg(1)], [quote_msg(5, bid="49995", ask="50005")]]
        )
        ingestor.poll_once()
        report = ingestor.poll_once()
        self.assertEqual(len(report.gaps), 1)
        self.assertEqual(report.gaps[0].expected, 2)
        self.assertEqual(report.gaps[0].got, 5)
        self.assertEqual(report.accepted, ("BTCUSD",))

    def test_a_disconnect_marks_symbols_unavailable_and_does_not_raise(self) -> None:
        ingestor, feed, _ = build_ingestor([[quote_msg(1)], FeedDisconnected("link dropped")])
        ingestor.poll_once()
        self.assertIsNotNone(feed.quote(SYMBOL))
        report = ingestor.poll_once()  # the source raises; the ingestor absorbs it
        self.assertEqual(report.state, "interrupted")
        self.assertIn(SYMBOL, report.dropped)
        self.assertIsNone(feed.quote(SYMBOL), "a known outage drops the quote at once")
        self.assertEqual(ingestor.state, FeedState.INTERRUPTED)

    def test_a_reconnect_resumes_and_reports_the_gap_across_the_outage(self) -> None:
        ingestor, feed, _ = build_ingestor(
            [[quote_msg(1)], FeedDisconnected("x"), [quote_msg(4, bid="49995", ask="50005")]]
        )
        ingestor.poll_once()
        ingestor.poll_once()  # disconnect
        report = ingestor.poll_once()  # reconnect
        self.assertEqual(report.state, "connected")
        self.assertEqual(len(report.gaps), 1)
        self.assertEqual((report.gaps[0].expected, report.gaps[0].got), (2, 4))
        self.assertIsNotNone(feed.quote(SYMBOL))

    def test_an_unclassified_source_error_is_treated_as_a_disconnect(self) -> None:
        ingestor, _, _ = build_ingestor([[quote_msg(1)], RuntimeError("boom")])
        ingestor.poll_once()
        report = ingestor.poll_once()
        self.assertEqual(report.state, "interrupted")
        self.assertIn("RuntimeError", report.error or "")

    def test_identical_scripts_produce_identical_reports(self) -> None:
        script = [[quote_msg(1)], [quote_msg(2, bid="49995", ask="50005")]]
        first, _, _ = build_ingestor([list(batch) for batch in script])
        second, _, _ = build_ingestor([list(batch) for batch in script])
        reports_a = [first.poll_once().as_details() for _ in script]
        reports_b = [second.poll_once().as_details() for _ in script]
        self.assertEqual(reports_a, reports_b)

    def test_the_observation_surface_reflects_the_last_poll(self) -> None:
        ingestor, _, _ = build_ingestor([[quote_msg(1)]])
        report = ingestor.poll_once()
        self.assertEqual(ingestor.passes, 1)
        self.assertIs(ingestor.last_report, report)
        self.assertEqual(ingestor.state, FeedState.CONNECTED)
        self.assertIn(SYMBOL, ingestor.last_message_at)

    def test_a_quiet_poll_after_a_disconnect_is_recovering_until_data_returns(self) -> None:
        ingestor, feed, _ = build_ingestor(
            [[quote_msg(1)], FeedDisconnected("x"), [], [quote_msg(2, bid="49995", ask="50005")]]
        )
        ingestor.poll_once()  # connected
        ingestor.poll_once()  # interrupted
        recovering = ingestor.poll_once()  # link answered, but no data yet
        self.assertEqual(recovering.state, "recovering")
        connected = ingestor.poll_once()  # data returns
        self.assertEqual(connected.state, "connected")
        self.assertIsNotNone(feed.quote(SYMBOL))


class FailClosedIntegrationTest(unittest.TestCase):
    def test_a_feed_outage_refuses_an_order_through_the_real_gateway(self) -> None:
        from trading.app import build_paper_stack
        from trading.core.marketdata import FreshMarkPrices
        from trading.core.money import Quantity
        from trading.core.orders import OrderIntent, OrderSide

        stack = build_paper_stack()  # default limits: 0.001 BTC ~ $50 < $100 ceiling
        ingestor = MarketDataIngestor(
            source=ScriptedSource([[quote_msg(1)], FeedDisconnected("outage")]),
            normalizer=QuoteNormalizer(clock=stack.clock),
            feed=stack.feed,  # ingest straight into the stack's quote feed
            clock=stack.clock,
        )
        marks = FreshMarkPrices(stack.feed, clock=stack.clock, policy=stack.staleness)

        # Connected: a quote arrives, the mark is available, the order executes.
        ingestor.poll_once()
        available = marks.mark_prices([SYMBOL])
        self.assertIn(SYMBOL, available)
        executed = stack.gateway.submit(
            OrderIntent(
                strategy_id="strategy-1", signal_id="s1", symbol=SYMBOL,
                side=OrderSide.BUY, quantity=Quantity("0.001", "BTC"),
            ),
            proposer=stack.strategy_id,
            mark_prices=available,
        )
        self.assertEqual(executed.outcome, "executed", executed.reason)

        # Disconnect: the symbol is dropped, the mark is gone, the SAME order is
        # refused -- a feed outage fails closed at the risk gate, no retry.
        ingestor.poll_once()
        after_outage = marks.mark_prices([SYMBOL])
        self.assertNotIn(SYMBOL, after_outage)
        refused = stack.gateway.submit(
            OrderIntent(
                strategy_id="strategy-1", signal_id="s2", symbol=SYMBOL,
                side=OrderSide.BUY, quantity=Quantity("0.001", "BTC"),
            ),
            proposer=stack.strategy_id,
            mark_prices=after_outage,
        )
        self.assertEqual(refused.outcome, "refused")
        self.assertEqual(refused.gate, "risk")


if __name__ == "__main__":
    unittest.main()
