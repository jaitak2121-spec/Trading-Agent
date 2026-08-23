"""Tests for normalized market data and the staleness rule.

The behaviour that matters here is narrow and it is all about one failure: a feed
that keeps answering after it has stopped being true. A dead websocket does not
raise, does not return ``None``, and does not go quiet -- it hands back the last
number it saw, forever. Every other market-data failure is loud and therefore
easy; this one is silent and sizes a real position off a price that no longer
exists.

So the load-bearing test in this module is
``TestAFrozenFeedRefusesAtTheGateway``: it freezes the feed and asserts the
gateway refuses at the risk gate. That is the whole point of routing staleness
through *absence* rather than adding a staleness gate -- the refusal comes from a
control Stage 1 already proved, and no gate had to change to get it.

INVARIANT 8 is also exercised at this new boundary: a ``float`` cannot enter
through a quote feed any more than through a constructor.
"""

from __future__ import annotations

import datetime as _dt
import unittest
from decimal import Decimal

from tests.harness import ASSET, DEFAULT_PRICE, SYMBOL, build_rig
from trading.adapters.memory import InMemoryQuoteFeed
from trading.core.clock import UTC, ManualClock
from trading.core.errors import StaleMarketData
from trading.core.gateway import ExecutionGate
from trading.core.marketdata import (
    DEFAULT_MAX_AGE_SECONDS,
    Candle,
    Freshness,
    FreshMarkPrices,
    MarketSnapshot,
    Quote,
    StalenessPolicy,
)
from trading.core.money import USD, USDT, Price, Quantity
from trading.core.orders import OrderSide
from trading.ports.market_data import MarketDataPort

T0 = _dt.datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC)
BID = Price("49999.50", USD)
ASK = Price("50000.50", USD)


def quote(**overrides) -> Quote:
    fields = {
        "symbol": SYMBOL,
        "bid": BID,
        "ask": ASK,
        "as_of": T0,
        "source": "test",
    }
    fields.update(overrides)
    return Quote(**fields)


class TestQuoteRejectsBadData(unittest.TestCase):
    """A malformed quote is refused at construction, not carried downstream.

    Every one of these is a real venue behaviour, and each would produce a
    plausible-looking but wrong position size if it were accepted.
    """

    def test_a_float_price_is_rejected(self):
        """INVARIANT 8 at the market-data boundary."""
        with self.assertRaises(TypeError):
            quote(bid=49999.5)

    def test_a_crossed_quote_is_rejected(self):
        """Bid above ask means the feed is broken, not that money is free."""
        with self.assertRaises(ValueError) as caught:
            quote(bid=Price("50001", USD), ask=Price("50000", USD))
        self.assertIn("crossed", str(caught.exception))

    def test_a_locked_quote_is_accepted(self):
        """Bid == ask is legal: thin books lock, and it is not a data error."""
        locked = quote(bid=Price("50000", USD), ask=Price("50000", USD))
        self.assertEqual(locked.spread, Decimal("0"))

    def test_bid_and_ask_must_share_a_currency(self):
        with self.assertRaises(ValueError):
            quote(ask=Price("50000.50", USDT))

    def test_a_naive_timestamp_is_rejected(self):
        """A timestamp with no zone cannot be aged without guessing a zone."""
        with self.assertRaises(ValueError) as caught:
            quote(as_of=_dt.datetime(2026, 1, 1, 12, 0, 0))
        self.assertIn("timezone-aware", str(caught.exception))

    def test_a_non_datetime_timestamp_is_rejected(self):
        with self.assertRaises(TypeError):
            quote(as_of=1767268800)

    def test_a_non_utc_timestamp_is_normalized_to_utc(self):
        """Accepted, but converted -- so comparisons are never zone-dependent."""
        offset = _dt.timezone(_dt.timedelta(hours=5, minutes=30))
        converted = quote(as_of=T0.astimezone(offset))
        self.assertEqual(converted.as_of, T0)
        self.assertIs(converted.as_of.tzinfo, UTC)

    def test_an_empty_symbol_is_rejected(self):
        with self.assertRaises(ValueError):
            quote(symbol="   ")

    def test_an_empty_source_is_rejected(self):
        """Provenance is not optional: an unattributable quote is unauditable."""
        with self.assertRaises(ValueError):
            quote(source="")

    def test_a_negative_sequence_is_rejected(self):
        with self.assertRaises(ValueError):
            quote(sequence=-1)

    def test_a_bool_sequence_is_rejected(self):
        with self.assertRaises(TypeError):
            quote(sequence=True)

    def test_a_quote_is_immutable(self):
        with self.assertRaises(Exception):
            quote().bid = ASK


class TestQuoteArithmetic(unittest.TestCase):
    def test_the_mid_sits_between_the_two_sides(self):
        self.assertEqual(quote().mid, Price("50000.00", USD))

    def test_the_mid_invents_no_precision(self):
        """A mid of two 2-dp prices stays at 2 dp rather than growing digits."""
        mid = quote(bid=Price("100.01", USD), ask=Price("100.02", USD)).mid
        self.assertEqual(mid.amount.as_tuple().exponent, -2)

    def test_the_spread_is_a_bare_decimal_not_a_price(self):
        """A difference is not a thing you can trade at, so it is not a Price."""
        self.assertIsInstance(quote().spread, Decimal)
        self.assertEqual(quote().spread, Decimal("1.00"))

    def test_the_spread_in_basis_points(self):
        self.assertEqual(quote().spread_bps.quantize(Decimal("0.01")), Decimal("0.20"))

    def test_a_buy_crosses_the_ask(self):
        """Using the mid would understate cost, which flatters every risk check."""
        self.assertEqual(quote().price_for(OrderSide.BUY), ASK)

    def test_a_sell_crosses_the_bid(self):
        self.assertEqual(quote().price_for(OrderSide.SELL), BID)

    def test_an_unrecognised_side_is_rejected(self):
        with self.assertRaises(ValueError):
            quote().price_for("sideways")

    def test_age_is_positive_for_a_past_quote(self):
        self.assertEqual(quote().age_seconds(T0 + _dt.timedelta(seconds=3)), 3.0)

    def test_age_is_negative_for_a_future_quote(self):
        self.assertEqual(quote().age_seconds(T0 - _dt.timedelta(seconds=3)), -3.0)

    def test_audit_details_are_strings_and_carry_provenance(self):
        details = quote().as_details()
        self.assertEqual(details["source"], "test")
        self.assertEqual(details["as_of"], T0.isoformat())
        self.assertIsInstance(details["bid"], str)


class TestStalenessPolicy(unittest.TestCase):
    """The boundary cases, because this is where a wrong sign costs money."""

    def setUp(self):
        self.policy = StalenessPolicy(max_age_seconds=5.0)

    def assess(self, seconds: float, **overrides) -> Freshness:
        return self.policy.assess(
            quote(**overrides), now=T0 + _dt.timedelta(seconds=seconds)
        )

    def test_a_current_quote_is_fresh(self):
        self.assertIs(self.assess(0), Freshness.FRESH)

    def test_a_quote_exactly_at_the_limit_is_still_fresh(self):
        """The limit is a maximum age, not an exclusive bound."""
        self.assertIs(self.assess(5.0), Freshness.FRESH)

    def test_a_quote_one_tick_past_the_limit_is_stale(self):
        self.assertIs(self.assess(5.001), Freshness.STALE)

    def test_an_absent_quote_is_missing_not_stale(self):
        """Distinguished because they mean different things operationally."""
        self.assertIs(self.policy.assess(None, now=T0), Freshness.MISSING)

    def test_only_fresh_is_usable(self):
        self.assertTrue(Freshness.FRESH.is_usable)
        self.assertFalse(Freshness.STALE.is_usable)
        self.assertFalse(Freshness.MISSING.is_usable)

    def test_slight_clock_skew_ahead_is_tolerated(self):
        """Sub-second host skew is normal and must not cause spurious refusals."""
        self.assertIs(self.assess(-1.0), Freshness.FRESH)

    def test_a_quote_far_in_the_future_is_stale_not_maximally_fresh(self):
        """The critical sign case.

        If a future timestamp counted as fresh, a venue with a fast clock could
        keep a frozen feed looking alive indefinitely -- age would only get more
        negative as our clock advanced.
        """
        self.assertIs(self.assess(-600.0), Freshness.STALE)

    def test_require_fresh_returns_the_quote_when_it_is_fresh(self):
        fresh = quote()
        self.assertIs(self.policy.require_fresh(fresh, symbol=SYMBOL, now=T0), fresh)

    def test_require_fresh_raises_a_safety_violation_when_stale(self):
        with self.assertRaises(StaleMarketData) as caught:
            self.policy.require_fresh(
                quote(), symbol=SYMBOL, now=T0 + _dt.timedelta(seconds=60)
            )
        message = str(caught.exception)
        self.assertIn(SYMBOL, message)
        self.assertIn("60.000s", message)

    def test_require_fresh_distinguishes_missing_from_stale(self):
        with self.assertRaises(StaleMarketData) as caught:
            self.policy.require_fresh(None, symbol="ETHUSD", now=T0)
        self.assertIn("no quote available", str(caught.exception))

    def test_a_negative_limit_is_rejected(self):
        with self.assertRaises(ValueError):
            StalenessPolicy(max_age_seconds=-1)

    def test_a_non_numeric_limit_is_rejected(self):
        with self.assertRaises(TypeError):
            StalenessPolicy(max_age_seconds="5")

    def test_a_bool_limit_is_rejected(self):
        with self.assertRaises(TypeError):
            StalenessPolicy(max_age_seconds=True)

    def test_a_zero_limit_means_only_this_instant(self):
        """Legal, and useful for a caller that wants the strictest possible rule."""
        strict = StalenessPolicy(max_age_seconds=0)
        self.assertIs(strict.assess(quote(), now=T0), Freshness.FRESH)
        self.assertIs(
            strict.assess(quote(), now=T0 + _dt.timedelta(milliseconds=1)),
            Freshness.STALE,
        )

    def test_the_default_limit_is_short(self):
        """Documented as a value, so a change to it is a visible change."""
        self.assertEqual(StalenessPolicy().max_age_seconds, DEFAULT_MAX_AGE_SECONDS)
        self.assertLessEqual(DEFAULT_MAX_AGE_SECONDS, 5.0)


class TestCandleValidation(unittest.TestCase):
    """An inconsistent bar produces stops that could never have been hit."""

    def candle(self, **overrides) -> Candle:
        fields = {
            "symbol": SYMBOL,
            "open_time": T0,
            "interval_seconds": 60,
            "open": Price("100", USD),
            "high": Price("110", USD),
            "low": Price("90", USD),
            "close": Price("105", USD),
            "volume": Quantity("1.5", ASSET),
        }
        fields.update(overrides)
        return Candle(**fields)

    def test_a_well_formed_bar_is_accepted(self):
        bar = self.candle()
        self.assertTrue(bar.is_up)
        self.assertEqual(bar.range, Decimal("20"))

    def test_low_above_high_is_rejected(self):
        with self.assertRaises(ValueError):
            self.candle(low=Price("120", USD))

    def test_a_close_outside_the_range_is_rejected(self):
        with self.assertRaises(ValueError) as caught:
            self.candle(close=Price("111", USD))
        self.assertIn("close", str(caught.exception))

    def test_an_open_outside_the_range_is_rejected(self):
        with self.assertRaises(ValueError):
            self.candle(open=Price("89", USD))

    def test_mixed_currencies_are_rejected(self):
        with self.assertRaises(ValueError):
            self.candle(close=Price("105", USDT))

    def test_a_float_price_is_rejected(self):
        with self.assertRaises(TypeError):
            self.candle(close=105.0)

    def test_volume_must_be_a_quantity(self):
        with self.assertRaises(TypeError):
            self.candle(volume="1.5")

    def test_negative_volume_is_rejected(self):
        """``Quantity`` permits negatives (a short position); a bar cannot have one."""
        with self.assertRaises(ValueError):
            self.candle(volume=Quantity("-1", ASSET))

    def test_an_empty_symbol_is_rejected(self):
        with self.assertRaises(ValueError):
            self.candle(symbol="")

    def test_a_non_positive_interval_is_rejected(self):
        with self.assertRaises(ValueError):
            self.candle(interval_seconds=0)

    def test_a_bool_interval_is_rejected(self):
        with self.assertRaises(TypeError):
            self.candle(interval_seconds=True)

    def test_the_bar_is_incomplete_before_its_close_time(self):
        """A strategy reading an in-progress bar as closed is a live/backtest split."""
        bar = self.candle()
        self.assertEqual(bar.close_time, T0 + _dt.timedelta(seconds=60))
        self.assertFalse(bar.is_complete(T0 + _dt.timedelta(seconds=59)))

    def test_the_bar_is_complete_exactly_at_its_close_time(self):
        self.assertTrue(self.candle().is_complete(T0 + _dt.timedelta(seconds=60)))


class TestMarketSnapshot(unittest.TestCase):
    """One instant across several symbols, so a decision sees one market."""

    def snapshot(self, ages=(0, 0)) -> MarketSnapshot:
        btc, eth = ages
        return MarketSnapshot(
            as_of=T0,
            quotes={
                SYMBOL: quote(as_of=T0 - _dt.timedelta(seconds=btc)),
                "ETHUSD": quote(
                    symbol="ETHUSD",
                    bid=Price("3000", USD),
                    ask=Price("3001", USD),
                    as_of=T0 - _dt.timedelta(seconds=eth),
                ),
            },
        )

    def test_a_quote_filed_under_the_wrong_key_is_rejected(self):
        """A mis-keyed quote would price one symbol off another's book."""
        with self.assertRaises(ValueError):
            MarketSnapshot(as_of=T0, quotes={"ETHUSD": quote()})

    def test_a_non_quote_value_is_rejected(self):
        with self.assertRaises(TypeError):
            MarketSnapshot(as_of=T0, quotes={SYMBOL: DEFAULT_PRICE})

    def test_the_snapshot_copies_its_mapping(self):
        """So a later mutation of the caller's dict cannot rewrite history."""
        source = {SYMBOL: quote()}
        snap = MarketSnapshot(as_of=T0, quotes=source)
        source["ETHUSD"] = quote(symbol="ETHUSD")
        self.assertEqual(snap.symbols, [SYMBOL])

    def test_stale_and_fresh_symbols_are_partitioned(self):
        policy = StalenessPolicy(max_age_seconds=5.0)
        snap = self.snapshot(ages=(0, 600))
        self.assertEqual(snap.fresh_symbols(policy), [SYMBOL])
        self.assertEqual(snap.stale_symbols(policy), ["ETHUSD"])

    def test_mark_prices_omit_the_stale_symbol_entirely(self):
        """Omission, not a warning: the risk engine refuses on a gap."""
        prices = self.snapshot(ages=(0, 600)).mark_prices(
            StalenessPolicy(max_age_seconds=5.0)
        )
        self.assertEqual(set(prices), {SYMBOL})

    def test_an_unknown_symbol_reads_as_none(self):
        self.assertIsNone(self.snapshot().quote("SOLUSD"))


class TestTheQuoteFeedAdapter(unittest.TestCase):
    def setUp(self):
        self.clock = ManualClock(start=T0)
        self.feed = InMemoryQuoteFeed(clock=self.clock)

    def test_a_published_quote_is_stamped_from_the_injected_clock(self):
        published = self.feed.publish(SYMBOL, BID, ASK)
        self.assertEqual(published.as_of, T0)

    def test_a_float_cannot_enter_through_the_feed(self):
        """INVARIANT 8 at the adapter boundary, not only in the core."""
        with self.assertRaises(TypeError):
            self.feed.publish(SYMBOL, 49999.5, ASK)

    def test_an_unknown_symbol_reads_as_none(self):
        self.assertIsNone(self.feed.quote("ETHUSD"))

    def test_a_late_arriving_older_tick_is_discarded(self):
        """Out-of-order delivery must not move the book backwards."""
        self.feed.publish(SYMBOL, BID, ASK, sequence=10)
        self.clock.advance(1)
        rejected = self.feed.publish(SYMBOL, Price("1", USD), Price("2", USD), sequence=9)
        self.assertIsNone(rejected)
        self.assertEqual(self.feed.quote(SYMBOL).bid, BID)
        self.assertEqual(self.feed.rejected_out_of_order, 1)

    def test_a_repeated_sequence_is_discarded(self):
        """At-least-once delivery means the same tick can arrive twice."""
        self.feed.publish(SYMBOL, BID, ASK, sequence=10)
        self.assertIsNone(self.feed.publish(SYMBOL, BID, ASK, sequence=10))

    def test_a_newer_sequence_is_applied(self):
        self.feed.publish(SYMBOL, BID, ASK, sequence=10)
        self.assertIsNotNone(self.feed.publish(SYMBOL, BID, ASK, sequence=11))

    def test_going_dark_removes_the_symbol(self):
        self.feed.publish(SYMBOL, BID, ASK)
        self.feed.go_dark(SYMBOL)
        self.assertIsNone(self.feed.quote(SYMBOL))
        self.assertEqual(list(self.feed.symbols()), [])

    def test_total_feed_loss_removes_everything(self):
        self.feed.publish(SYMBOL, BID, ASK)
        self.feed.publish("ETHUSD", Price("3000", USD), Price("3001", USD))
        self.feed.go_dark_entirely()
        self.assertEqual(list(self.feed.symbols()), [])

    def test_freezing_an_unknown_symbol_is_an_error(self):
        """A test that thinks it froze a feed but did not is worse than useless."""
        with self.assertRaises(KeyError):
            self.feed.freeze(SYMBOL)

    def test_a_frozen_symbol_is_reported(self):
        self.feed.publish(SYMBOL, BID, ASK)
        self.feed.freeze(SYMBOL)
        self.assertEqual(self.feed.frozen_symbols, [SYMBOL])

    def test_republishing_unfreezes(self):
        self.feed.publish(SYMBOL, BID, ASK)
        self.feed.freeze(SYMBOL)
        self.feed.publish(SYMBOL, BID, ASK)
        self.assertEqual(self.feed.frozen_symbols, [])

    def test_clock_skew_stamps_quotes_in_the_future(self):
        self.feed.set_clock_skew(600)
        self.assertEqual(
            self.feed.publish(SYMBOL, BID, ASK).as_of,
            T0 + _dt.timedelta(seconds=600),
        )

    def test_a_non_numeric_skew_is_rejected(self):
        with self.assertRaises(TypeError):
            self.feed.set_clock_skew("600")

    def test_the_batch_read_omits_unknown_symbols(self):
        self.feed.publish(SYMBOL, BID, ASK)
        self.assertEqual(set(self.feed.quotes([SYMBOL, "ETHUSD"])), {SYMBOL})

    def test_the_feed_requires_a_clock(self):
        """Ambient time would make staleness untestable and non-deterministic."""
        with self.assertRaises(TypeError):
            InMemoryQuoteFeed(clock=None)

    def test_the_feed_requires_a_source_name(self):
        """Every quote it stamps carries this, and provenance is not optional."""
        with self.assertRaises(ValueError):
            InMemoryQuoteFeed(clock=self.clock, source=" ")

    def test_a_one_sided_source_can_be_widened_into_a_book(self):
        widened = self.feed.publish_last(SYMBOL, Price("50000", USD), spread_bps=4)
        self.assertEqual(widened.mid, Price("50000", USD))
        self.assertEqual(
            widened.spread_bps.quantize(Decimal("0.01")), Decimal("4.00")
        )

    def test_widening_rejects_a_float(self):
        with self.assertRaises(TypeError):
            self.feed.publish_last(SYMBOL, 50000.0)


class TestFreshMarkPricesHidesStaleQuotes(unittest.TestCase):
    """The bridge: a stale quote is reported as an absent price."""

    def setUp(self):
        self.clock = ManualClock(start=T0)
        self.feed = InMemoryQuoteFeed(clock=self.clock)
        self.feed.publish(SYMBOL, BID, ASK)
        self.prices = FreshMarkPrices(
            self.feed, clock=self.clock, policy=StalenessPolicy(max_age_seconds=5.0)
        )

    def test_it_is_a_market_data_port(self):
        """So it can be dropped in wherever Stage 1 expects one."""
        self.assertIsInstance(self.prices, MarketDataPort)

    def test_a_fresh_quote_yields_its_mid(self):
        self.assertEqual(self.prices.mark_price(SYMBOL), Price("50000.00", USD))

    def test_a_stale_quote_yields_none(self):
        """The central conversion: staleness becomes absence."""
        self.clock.advance(600)
        self.assertIsNone(self.prices.mark_price(SYMBOL))

    def test_a_quote_stamped_in_the_future_also_yields_none(self):
        self.feed.set_clock_skew(600)
        self.feed.publish(SYMBOL, BID, ASK)
        self.assertIsNone(self.prices.mark_price(SYMBOL))

    def test_an_unknown_symbol_yields_none(self):
        self.assertIsNone(self.prices.mark_price("ETHUSD"))

    def test_the_batch_read_omits_the_stale_symbol(self):
        """``mark_prices`` is inherited from the port, so confirm it composes."""
        self.feed.publish("ETHUSD", Price("3000", USD), Price("3001", USD))
        self.clock.advance(600)
        self.feed.publish("ETHUSD", Price("3000", USD), Price("3001", USD))
        prices = self.prices.mark_prices([SYMBOL, "ETHUSD"])
        self.assertEqual(set(prices), {"ETHUSD"})

    def test_suppression_is_counted_not_silent(self):
        """A feed outage that produces no signal is an outage nobody notices."""
        self.clock.advance(600)
        self.prices.mark_price(SYMBOL)
        self.assertEqual(self.prices.suppressed_count, 1)
        self.assertEqual(self.prices.last_suppressed, {SYMBOL: Freshness.STALE})

    def test_a_missing_symbol_is_recorded_as_missing_not_stale(self):
        self.prices.mark_price("ETHUSD")
        self.assertEqual(self.prices.last_suppressed, {"ETHUSD": Freshness.MISSING})

    def test_assess_still_tells_the_truth_after_hiding_the_price(self):
        """Advisory output has to *say* the data is stale, not just refuse."""
        self.clock.advance(600)
        self.assertIsNone(self.prices.mark_price(SYMBOL))
        self.assertIs(self.prices.assess(SYMBOL), Freshness.STALE)

    def test_the_snapshot_is_deliberately_unfiltered(self):
        """Monitoring needs the honest view, including the stale entries."""
        self.clock.advance(600)
        snap = self.prices.snapshot([SYMBOL])
        self.assertEqual(snap.symbols, [SYMBOL])
        self.assertEqual(snap.stale_symbols(self.prices.policy), [SYMBOL])

    def test_the_snapshot_is_stamped_at_read_time_not_quote_time(self):
        self.clock.advance(600)
        self.assertEqual(
            self.prices.snapshot([SYMBOL]).as_of, T0 + _dt.timedelta(seconds=600)
        )

    def test_it_refuses_a_feed_that_is_not_a_quote_feed(self):
        with self.assertRaises(TypeError):
            FreshMarkPrices(object(), clock=self.clock)

    def test_it_refuses_an_ambient_clock(self):
        with self.assertRaises(TypeError):
            FreshMarkPrices(self.feed, clock=None)

    def test_the_default_policy_is_the_strict_one(self):
        self.assertEqual(
            FreshMarkPrices(self.feed, clock=self.clock).policy.max_age_seconds,
            DEFAULT_MAX_AGE_SECONDS,
        )


class TestAFrozenFeedRefusesAtTheGateway(unittest.TestCase):
    """The reason this module exists.

    A frozen feed is indistinguishable from a live one through
    ``MarketDataPort`` -- unless staleness is converted to absence first. These
    tests wire a real gateway to a real quote feed and assert that a feed which
    has stopped advancing causes a refusal at the *risk* gate: no new gate, no
    change to the chain, and the same refusal path a missing price has always
    taken.
    """

    def setUp(self):
        self.rig = build_rig()
        self.feed = InMemoryQuoteFeed(clock=self.rig.clock)
        self.feed.publish(SYMBOL, BID, ASK)
        self.prices = FreshMarkPrices(
            self.feed,
            clock=self.rig.clock,
            policy=StalenessPolicy(max_age_seconds=5.0),
        )

    def marks(self):
        return self.prices.mark_prices([SYMBOL])

    def test_a_live_feed_executes(self):
        """The control: without this, the refusal below proves nothing."""
        result = self.rig.submit(mark_prices=self.marks())
        self.assertTrue(result.is_executed, result.reason)

    def test_a_frozen_feed_is_refused_at_the_risk_gate(self):
        self.feed.freeze(SYMBOL)
        self.rig.clock.advance(600)
        result = self.rig.submit(mark_prices=self.marks())
        self.assertEqual(result.gate, ExecutionGate.RISK)
        self.assertIn("no mark price", result.reason)

    def test_nothing_reaches_the_venue_when_the_feed_is_frozen(self):
        """The refusal has to be *before* the broker, not a rollback after it."""
        self.feed.freeze(SYMBOL)
        self.rig.clock.advance(600)
        self.rig.submit(mark_prices=self.marks())
        self.assertEqual(self.rig.broker.placement_count, 0)

    def test_a_dark_feed_is_refused_the_same_way(self):
        """Honest silence and dishonest staleness converge on one refusal."""
        self.feed.go_dark(SYMBOL)
        result = self.rig.submit(mark_prices=self.marks())
        self.assertEqual(result.gate, ExecutionGate.RISK)

    def test_a_venue_clock_running_ahead_is_refused(self):
        self.feed.set_clock_skew(600)
        self.feed.publish(SYMBOL, BID, ASK)
        result = self.rig.submit(mark_prices=self.marks())
        self.assertEqual(result.gate, ExecutionGate.RISK)

    def test_trading_resumes_once_the_feed_recovers(self):
        """Fail closed, but not latching: market data is not a safety incident.

        Contrast the kill switch and the position-mismatch gate, which latch
        deliberately. A stale tick is an ordinary operational event, so recovery
        must not need an operator -- otherwise every hiccup becomes an outage.
        """
        self.feed.freeze(SYMBOL)
        self.rig.clock.advance(600)
        self.assertFalse(self.rig.submit(mark_prices=self.marks()).is_executed)
        self.feed.publish(SYMBOL, BID, ASK)
        self.assertTrue(self.rig.submit(mark_prices=self.marks()).is_executed)

    def test_the_stale_refusal_is_audited(self):
        """An operator has to be able to see why the order did not go."""
        self.feed.freeze(SYMBOL)
        self.rig.clock.advance(600)
        self.rig.submit(mark_prices=self.marks())
        self.assertIn("risk_refused", self.rig.actions())
        self.rig.audit.verify()


if __name__ == "__main__":
    unittest.main()
