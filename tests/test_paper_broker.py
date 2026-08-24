"""Tests for the paper-trading venue.

Two questions, and they are different questions.

**Does it fill honestly?** A paper venue exists to produce a track record, and a
track record is worthless if the fills are better than a real venue would have
given. So the tests pin the side of the book (a buy lifts the ask), the direction
of slippage (always against the order), the treatment of a missing or frozen
feed (refuse, never guess), and the existence of partial fills (a book has a
size, and an order larger than it does not magically clear).

**Does routing through it weaken anything?** The paper broker sits behind the
same :class:`~trading.core.gateway.ExecutionGateway` as the simulator, and
``TestThroughTheGateway`` is the part that matters: every gate that refused
before still refuses, the venue is never touched when one does, and a venue
rejection comes back as a refusal at the execution gate rather than as a
successful order. A paper adapter that quietly became its own execution path
would pass every test above this line.

One property is asserted by absence: :class:`PaperBroker` has no ``script`` and
no ``raise_on_next``, and never returns ``UNCERTAIN``. The hostile cases belong
to ``SimulatedBroker``, and keeping them out of here is what makes a paper fill
mean something.
"""

from __future__ import annotations

import unittest
from decimal import Decimal

from tests.harness import ASSET, DEFAULT_PRICE, DEFAULT_QUANTITY, SYMBOL, build_rig
from trading.adapters.memory import InMemoryQuoteFeed
from trading.adapters.paper import PaperBroker, PaperReject
from trading.core.authz import Principal, Role, mint_execution_token
from trading.core.clock import ManualClock
from trading.core.config import RiskConfig
from trading.core.errors import SafetyViolation, UnauthorizedAction
from trading.core.gateway import ExecutionGate
from trading.core.marketdata import StalenessPolicy
from trading.core.modes import TradingMode
from trading.core.money import INR, USD, Money, Price, Quantity
from trading.core.orders import Order, OrderIntent, OrderSide, OrderState, OrderType
from trading.ports.broker import AckOutcome

BID = Price("49990", USD)
ASK = Price("50010", USD)


class PaperCase(unittest.TestCase):
    """A paper venue, a feed with a live quote in it, and a way to place orders."""

    slippage_bps = 0
    depth = None

    def setUp(self):
        self.clock = ManualClock()
        self.gateway_id = Principal("gateway-1", Role.EXECUTION_GATEWAY)
        self.feed = InMemoryQuoteFeed(clock=self.clock, source="paper-test")
        self.broker = PaperBroker(
            clock=self.clock,
            quotes=self.feed,
            slippage_bps=self.slippage_bps,
            depth=self.depth,
        )
        self.publish()
        self._counter = 0

    def publish(self, bid: Price = BID, ask: Price = ASK, **kwargs):
        return self.feed.publish(SYMBOL, bid, ask, **kwargs)

    def order(
        self,
        *,
        side: OrderSide = OrderSide.BUY,
        quantity: Quantity = DEFAULT_QUANTITY,
        order_type: OrderType = OrderType.MARKET,
        limit_price: Price | None = None,
        symbol: str = SYMBOL,
    ) -> Order:
        self._counter += 1
        intent = OrderIntent(
            strategy_id="strat-1",
            signal_id=f"sig-{self._counter}",
            symbol=symbol,
            side=side,
            order_type=order_type,
            quantity=quantity,
            limit_price=limit_price,
        )
        return Order(intent, clock=self.clock)

    def token(self, order: Order, *, ttl_seconds: int = 30):
        return mint_execution_token(
            self.gateway_id,
            order_id=order.order_id,
            idempotency_key=order.idempotency_key,
            clock=self.clock,
            ttl_seconds=ttl_seconds,
        )

    def place(self, order: Order | None = None, **kwargs):
        order = order or self.order(**kwargs)
        return order, self.broker.place_order(order, token=self.token(order))

    def ack(self, order: Order | None = None, **kwargs):
        return self.place(order, **kwargs)[1]


class TestConstruction(PaperCase):
    """Every knob is validated at wiring time, not at the first order."""

    def test_it_implements_the_port(self):
        from trading.ports.broker import BrokerPort

        self.assertIsInstance(self.broker, BrokerPort)

    def test_a_non_clock_is_refused(self):
        with self.assertRaises(TypeError):
            PaperBroker(clock=object(), quotes=self.feed)

    def test_a_non_feed_is_refused(self):
        with self.assertRaises(TypeError) as ctx:
            PaperBroker(clock=self.clock, quotes=object())
        self.assertIn("QuoteFeedPort", str(ctx.exception))

    def test_a_market_data_port_is_not_a_quote_feed(self):
        """A ``MarketDataPort`` cannot say how old its price is, so it will not do."""
        from trading.adapters.memory import StaticMarketData

        with self.assertRaises(TypeError):
            PaperBroker(clock=self.clock, quotes=StaticMarketData({SYMBOL: ASK}))

    def test_a_non_policy_staleness_is_refused(self):
        with self.assertRaises(TypeError):
            PaperBroker(clock=self.clock, quotes=self.feed, staleness=5.0)

    def test_a_float_slippage_is_refused(self):
        with self.assertRaises(TypeError):
            PaperBroker(clock=self.clock, quotes=self.feed, slippage_bps=1.5)

    def test_a_bool_slippage_is_refused(self):
        with self.assertRaises(TypeError):
            PaperBroker(clock=self.clock, quotes=self.feed, slippage_bps=True)

    def test_negative_slippage_is_refused(self):
        """Negative slippage is a paper account that profits from crossing."""
        with self.assertRaises(ValueError):
            PaperBroker(clock=self.clock, quotes=self.feed, slippage_bps=-1)

    def test_slippage_of_a_whole_book_is_refused(self):
        """At 100% a sell price reaches zero, and a Price may not be zero."""
        with self.assertRaises(ValueError) as ctx:
            PaperBroker(clock=self.clock, quotes=self.feed, slippage_bps=10_000)
        self.assertIn("10000", str(ctx.exception).replace("_", ""))

    def test_a_float_depth_is_refused(self):
        with self.assertRaises(TypeError) as ctx:
            PaperBroker(clock=self.clock, quotes=self.feed, depth={SYMBOL: 0.5})
        self.assertIn("INVARIANT 8", str(ctx.exception))

    def test_a_negative_depth_is_refused(self):
        with self.assertRaises(ValueError):
            PaperBroker(
                clock=self.clock,
                quotes=self.feed,
                depth={SYMBOL: Quantity("-1", ASSET)},
            )

    def test_an_empty_id_prefix_is_refused(self):
        for bad in ("", "   "):
            with self.subTest(prefix=repr(bad)):
                with self.assertRaises(ValueError):
                    PaperBroker(clock=self.clock, quotes=self.feed, id_prefix=bad)

    def test_set_depth_validates_too(self):
        """Otherwise the setter would be a way around the constructor."""
        with self.assertRaises(TypeError):
            self.broker.set_depth(SYMBOL, 0.5)
        with self.assertRaises(ValueError):
            self.broker.set_depth(SYMBOL, Quantity("-1", ASSET))

    def test_it_starts_with_no_history(self):
        self.assertEqual(self.broker.placement_count, 0)
        self.assertEqual(self.broker.placements, [])
        self.assertEqual(self.broker.duplicate_keys, frozenset())
        self.assertEqual(self.broker.resting_keys, frozenset())
        self.assertEqual(self.broker.fetch_positions().positions, {})

    def test_the_default_staleness_policy_is_the_shared_one(self):
        self.assertEqual(self.broker.staleness, StalenessPolicy())


class TestNormalFills(PaperCase):
    """A market order crosses the spread, in the direction that costs money."""

    def test_a_buy_lifts_the_ask(self):
        ack = self.ack()
        self.assertIs(ack.outcome, AckOutcome.FILLED)
        self.assertEqual(ack.fill_price, ASK)
        self.assertEqual(ack.filled_quantity, DEFAULT_QUANTITY)

    def test_a_sell_hits_the_bid(self):
        ack = self.ack(side=OrderSide.SELL)
        self.assertIs(ack.outcome, AckOutcome.FILLED)
        self.assertEqual(ack.fill_price, BID)

    def test_neither_side_fills_at_the_mid(self):
        """Half the spread on every round trip is the commonest paper flattery."""
        mid = self.feed.quote(SYMBOL).mid
        for side in (OrderSide.BUY, OrderSide.SELL):
            with self.subTest(side=side.value):
                self.assertNotEqual(self.ack(side=side).fill_price, mid)

    def test_a_full_fill_carries_no_partial_message(self):
        self.assertEqual(self.ack().message, "")

    def test_broker_ids_are_sequential_and_prefixed(self):
        ids = [self.ack().broker_order_id for _ in range(3)]
        self.assertEqual(ids, ["PAPER-000001", "PAPER-000002", "PAPER-000003"])

    def test_the_prefix_is_configurable(self):
        broker = PaperBroker(clock=self.clock, quotes=self.feed, id_prefix="SANDBOX")
        order = self.order()
        ack = broker.place_order(order, token=self.token(order))
        self.assertEqual(ack.broker_order_id, "SANDBOX-000001")

    def test_a_buy_fill_moves_the_venue_position_long(self):
        self.place()
        self.assertEqual(self.broker.fetch_positions().positions[SYMBOL], DEFAULT_QUANTITY)

    def test_a_sell_fill_moves_it_short(self):
        self.place(side=OrderSide.SELL)
        self.assertEqual(
            self.broker.fetch_positions().positions[SYMBOL].amount,
            -DEFAULT_QUANTITY.amount,
        )

    def test_fills_accumulate(self):
        self.place()
        self.place()
        self.assertEqual(
            self.broker.fetch_positions().positions[SYMBOL].amount,
            DEFAULT_QUANTITY.amount * 2,
        )

    def test_a_buy_then_an_equal_sell_nets_flat(self):
        self.place()
        self.place(side=OrderSide.SELL)
        self.assertEqual(
            self.broker.fetch_positions().positions[SYMBOL].amount, Decimal(0)
        )

    def test_the_position_snapshot_is_a_copy(self):
        self.place()
        dict(self.broker.fetch_positions().positions).clear()
        self.assertIn(SYMBOL, self.broker.fetch_positions().positions)

    def test_the_quote_is_read_at_placement_not_at_construction(self):
        """A venue that cached the opening print would fill at yesterday's price."""
        moved = self.publish(Price("60000", USD), Price("60010", USD))
        self.assertIsNotNone(moved)
        self.assertEqual(self.ack().fill_price, Price("60010", USD))

    def test_it_is_deterministic(self):
        """Same feed, same order, same config -- byte-identical acks, every run."""
        acks = []
        for _ in range(2):
            clock = ManualClock()
            feed = InMemoryQuoteFeed(clock=clock, source="paper-test")
            feed.publish(SYMBOL, BID, ASK)
            broker = PaperBroker(clock=clock, quotes=feed, slippage_bps=25)
            intent = OrderIntent(
                strategy_id="strat-1",
                signal_id="sig-fixed",
                symbol=SYMBOL,
                side=OrderSide.BUY,
                quantity=DEFAULT_QUANTITY,
            )
            order = Order(intent, clock=clock, order_id="ORD-fixed")
            token = mint_execution_token(
                self.gateway_id,
                order_id=order.order_id,
                idempotency_key=order.idempotency_key,
                clock=clock,
            )
            acks.append(broker.place_order(order, token=token))
        self.assertEqual(acks[0], acks[1])


class TestSlippage(PaperCase):
    """Fifty basis points, applied against the order in both directions."""

    slippage_bps = 50

    def test_a_buy_pays_more_than_the_ask(self):
        ack = self.ack()
        self.assertEqual(ack.fill_price, Price("50260.05", USD))
        self.assertGreater(ack.fill_price.amount, ASK.amount)

    def test_a_sell_receives_less_than_the_bid(self):
        ack = self.ack(side=OrderSide.SELL)
        self.assertEqual(ack.fill_price, Price("49740.05", USD))
        self.assertLess(ack.fill_price.amount, BID.amount)

    def test_zero_slippage_leaves_the_book_price_exactly_alone(self):
        """No rounding pass at all, so a whole-number ask stays a whole number."""
        broker = PaperBroker(clock=self.clock, quotes=self.feed, slippage_bps=0)
        order = self.order()
        ack = broker.place_order(order, token=self.token(order))
        self.assertEqual(ack.fill_price, ASK)

    def test_the_reported_slippage_is_the_configured_one(self):
        self.assertEqual(self.broker.slippage_bps, 50)


class TestLimitOrders(PaperCase):
    """A limit order fills only if the book reaches it, and rests if it does not."""

    def limit(self, price: str, *, side: OrderSide = OrderSide.BUY):
        return self.order(
            side=side,
            order_type=OrderType.LIMIT,
            limit_price=Price(price, USD),
        )

    def test_a_buy_limit_above_the_ask_fills_at_the_ask(self):
        """The venue fills at the book, not at the limit -- the limit is a ceiling."""
        ack = self.ack(self.limit("50100"))
        self.assertIs(ack.outcome, AckOutcome.FILLED)
        self.assertEqual(ack.fill_price, ASK)

    def test_a_buy_limit_exactly_at_the_ask_crosses(self):
        """The boundary a strict comparison would drop."""
        self.assertIs(self.ack(self.limit("50010")).outcome, AckOutcome.FILLED)

    def test_a_buy_limit_below_the_ask_rests(self):
        ack = self.ack(self.limit("49000"))
        self.assertIs(ack.outcome, AckOutcome.ACCEPTED)
        self.assertIsNone(ack.filled_quantity)
        self.assertIn("resting", ack.message)

    def test_a_sell_limit_below_the_bid_fills_at_the_bid(self):
        ack = self.ack(self.limit("49900", side=OrderSide.SELL))
        self.assertIs(ack.outcome, AckOutcome.FILLED)
        self.assertEqual(ack.fill_price, BID)

    def test_a_sell_limit_exactly_at_the_bid_crosses(self):
        ack = self.ack(self.limit("49990", side=OrderSide.SELL))
        self.assertIs(ack.outcome, AckOutcome.FILLED)

    def test_a_sell_limit_above_the_bid_rests(self):
        ack = self.ack(self.limit("51000", side=OrderSide.SELL))
        self.assertIs(ack.outcome, AckOutcome.ACCEPTED)

    def test_a_resting_order_moves_no_position(self):
        self.place(self.limit("49000"))
        self.assertEqual(self.broker.fetch_positions().positions, {})

    def test_a_resting_order_is_reported_by_a_later_query(self):
        """Which is what makes it discoverable rather than lost."""
        order, _ = self.place(self.limit("49000"))
        found = self.broker.fetch_order_state(order)
        self.assertIs(found.outcome, AckOutcome.ACCEPTED)
        self.assertIn(order.idempotency_key, self.broker.resting_keys)

    def test_a_resting_order_does_not_fill_itself_later(self):
        """Stage 2F rests it and stops there; driving it to a fill is 2G's job."""
        order, _ = self.place(self.limit("49000"))
        self.publish(Price("48000", USD), Price("48010", USD))
        self.assertIs(
            self.broker.fetch_order_state(order).outcome, AckOutcome.ACCEPTED
        )
        self.assertEqual(self.broker.fetch_positions().positions, {})

    def test_slippage_can_push_a_marginal_limit_out_of_reach(self):
        """The realistic outcome: the price moved away before we got there."""
        broker = PaperBroker(clock=self.clock, quotes=self.feed, slippage_bps=50)
        order = self.limit("50010")
        ack = broker.place_order(order, token=self.token(order))
        self.assertIs(ack.outcome, AckOutcome.ACCEPTED)
        self.assertIn("resting", ack.message)

    def test_a_limit_in_the_wrong_currency_is_rejected(self):
        order = self.order(
            order_type=OrderType.LIMIT, limit_price=Price("50100", INR)
        )
        ack = self.broker.place_order(order, token=self.token(order))
        self.assertIs(ack.outcome, AckOutcome.REJECTED)
        self.assertIn(PaperReject.CURRENCY_MISMATCH, ack.message)
        self.assertIn("INR", ack.message)


class TestRejections(PaperCase):
    """Every refusal names its reason, and none of them invent a price."""

    def test_no_quote_is_a_rejection_not_a_guess(self):
        self.feed.go_dark(SYMBOL)
        ack = self.ack()
        self.assertIs(ack.outcome, AckOutcome.REJECTED)
        self.assertIn(PaperReject.NO_QUOTE, ack.message)

    def test_an_unknown_symbol_is_a_rejection(self):
        ack = self.ack(symbol="ETHUSD", quantity=Quantity("1", "ETH"))
        self.assertIs(ack.outcome, AckOutcome.REJECTED)
        self.assertIn(PaperReject.NO_QUOTE, ack.message)

    def test_a_stale_quote_is_a_rejection(self):
        """A frozen feed must not produce fills; that is a fictional track record."""
        self.clock.advance(6)
        ack = self.ack()
        self.assertIs(ack.outcome, AckOutcome.REJECTED)
        self.assertIn(PaperReject.STALE_QUOTE, ack.message)
        self.assertIn("6.000s old", ack.message)

    def test_a_quote_inside_the_age_limit_still_fills(self):
        self.clock.advance(4)
        self.assertIs(self.ack().outcome, AckOutcome.FILLED)

    def test_the_staleness_policy_is_configurable(self):
        broker = PaperBroker(
            clock=self.clock,
            quotes=self.feed,
            staleness=StalenessPolicy(max_age_seconds=1.0),
        )
        self.clock.advance(2)
        order = self.order()
        ack = broker.place_order(order, token=self.token(order))
        self.assertIs(ack.outcome, AckOutcome.REJECTED)
        self.assertIn(PaperReject.STALE_QUOTE, ack.message)

    def test_a_venue_clock_running_ahead_is_not_evidence_of_freshness(self):
        self.feed.set_clock_skew(30)
        self.publish()
        ack = self.ack()
        self.assertIs(ack.outcome, AckOutcome.REJECTED)
        self.assertIn(PaperReject.STALE_QUOTE, ack.message)

    def test_an_empty_book_rejects_a_market_order(self):
        self.broker.set_depth(SYMBOL, Quantity.zero(ASSET))
        ack = self.ack()
        self.assertIs(ack.outcome, AckOutcome.REJECTED)
        self.assertIn(PaperReject.NO_LIQUIDITY, ack.message)

    def test_an_empty_book_rests_a_limit_order(self):
        """A limit order has somewhere to wait; a market order does not."""
        self.broker.set_depth(SYMBOL, Quantity.zero(ASSET))
        order = self.order(
            order_type=OrderType.LIMIT, limit_price=Price("50100", USD)
        )
        ack = self.broker.place_order(order, token=self.token(order))
        self.assertIs(ack.outcome, AckOutcome.ACCEPTED)

    def test_depth_in_the_wrong_asset_is_rejected_rather_than_compared(self):
        """Comparing BTC against ETH would raise, and the gateway reads a raise
        as an unknown outcome -- which is a system-wide halt over a typo."""
        self.broker.set_depth(SYMBOL, Quantity("5", "ETH"))
        ack = self.ack()
        self.assertIs(ack.outcome, AckOutcome.REJECTED)
        self.assertIn(PaperReject.CURRENCY_MISMATCH, ack.message)

    def test_a_rejection_carries_no_broker_id_quantity_or_price(self):
        self.feed.go_dark(SYMBOL)
        ack = self.ack()
        self.assertIsNone(ack.broker_order_id)
        self.assertIsNone(ack.filled_quantity)
        self.assertIsNone(ack.fill_price)

    def test_a_rejection_leaves_nothing_to_reconcile(self):
        self.feed.go_dark(SYMBOL)
        order, _ = self.place()
        self.assertEqual(self.broker.resting_keys, frozenset())
        self.assertEqual(self.broker.fetch_positions().positions, {})
        self.assertIs(
            self.broker.fetch_order_state(order).outcome, AckOutcome.REJECTED
        )

    def test_a_rejection_still_counts_as_a_placement(self):
        """The token was spent, so the attempt happened."""
        self.feed.go_dark(SYMBOL)
        self.place()
        self.assertEqual(self.broker.placement_count, 1)


class TestPartialFills(PaperCase):
    """The outcome later stages need and nothing in this repository produced."""

    depth = {SYMBOL: Quantity("0.0004", ASSET)}

    def test_an_order_larger_than_the_book_fills_partially(self):
        ack = self.ack()
        self.assertIs(ack.outcome, AckOutcome.FILLED)
        self.assertEqual(ack.filled_quantity, Quantity("0.0004", ASSET))

    def test_the_message_names_both_quantities(self):
        ack = self.ack()
        self.assertIn("partial fill", ack.message)
        self.assertIn("0.0004", ack.message)
        self.assertIn("0.001", ack.message)

    def test_only_the_filled_quantity_moves_the_venue_position(self):
        self.place()
        self.assertEqual(
            self.broker.fetch_positions().positions[SYMBOL],
            Quantity("0.0004", ASSET),
        )

    def test_an_order_inside_the_book_fills_whole(self):
        ack = self.ack(quantity=Quantity("0.0002", ASSET))
        self.assertEqual(ack.filled_quantity, Quantity("0.0002", ASSET))
        self.assertEqual(ack.message, "")

    def test_an_order_exactly_the_size_of_the_book_fills_whole(self):
        ack = self.ack(quantity=Quantity("0.0004", ASSET))
        self.assertEqual(ack.filled_quantity, Quantity("0.0004", ASSET))
        self.assertEqual(ack.message, "")

    def test_depth_is_per_placement_and_does_not_deplete(self):
        """Documented on purpose: this is not a market-impact model."""
        first, second = self.ack(), self.ack()
        self.assertEqual(first.filled_quantity, second.filled_quantity)

    def test_a_symbol_with_no_depth_setting_is_unbounded(self):
        ack = self.ack(symbol="ETHUSD", quantity=Quantity("1000", "ETH"))
        # No quote for ETHUSD, so this rejects for that reason rather than depth.
        self.assertIn(PaperReject.NO_QUOTE, ack.message)
        self.feed.publish("ETHUSD", Price("2990", USD), Price("3010", USD))
        ack = self.ack(symbol="ETHUSD", quantity=Quantity("1000", "ETH"))
        self.assertEqual(ack.filled_quantity, Quantity("1000", "ETH"))


class TestTokenIsRequired(PaperCase):
    """INVARIANT 3 at the venue boundary: no gateway authority, no placement."""

    def test_a_placement_without_a_token_is_a_type_error(self):
        with self.assertRaises(TypeError):
            self.broker.place_order(self.order())

    def test_a_strategy_cannot_mint_a_token_to_use_here(self):
        strategy = Principal("strategy-1", Role.STRATEGY)
        order = self.order()
        with self.assertRaises(UnauthorizedAction):
            mint_execution_token(
                strategy,
                order_id=order.order_id,
                idempotency_key=order.idempotency_key,
                clock=self.clock,
            )

    def test_a_token_cannot_be_replayed(self):
        order = self.order()
        token = self.token(order)
        self.broker.place_order(order, token=token)
        with self.assertRaises(SafetyViolation):
            self.broker.place_order(order, token=token)

    def test_a_replayed_token_does_not_reach_the_venue_twice(self):
        order = self.order()
        token = self.token(order)
        self.broker.place_order(order, token=token)
        with self.assertRaises(SafetyViolation):
            self.broker.place_order(order, token=token)
        self.assertEqual(self.broker.times_seen(order.idempotency_key), 1)
        self.assertEqual(self.broker.placement_count, 1)

    def test_a_token_for_a_different_order_is_refused(self):
        mine, theirs = self.order(), self.order()
        with self.assertRaises(SafetyViolation):
            self.broker.place_order(mine, token=self.token(theirs))
        self.assertEqual(self.broker.placement_count, 0)

    def test_an_expired_token_is_refused(self):
        order = self.order()
        token = self.token(order, ttl_seconds=10)
        self.clock.advance(11)
        with self.assertRaises(SafetyViolation):
            self.broker.place_order(order, token=token)
        self.assertEqual(self.broker.placement_count, 0)

    def test_a_refused_token_moves_no_position(self):
        mine, theirs = self.order(), self.order()
        with self.assertRaises(SafetyViolation):
            self.broker.place_order(mine, token=self.token(theirs))
        self.assertEqual(self.broker.fetch_positions().positions, {})


class TestInvalidOrders(PaperCase):
    """A malformed order never reaches a venue, because it cannot be built."""

    def test_a_zero_quantity_order_cannot_exist(self):
        with self.assertRaises(ValueError):
            self.order(quantity=Quantity.zero(ASSET))
        self.assertEqual(self.broker.placement_count, 0)

    def test_a_negative_quantity_order_cannot_exist(self):
        with self.assertRaises(ValueError):
            self.order(quantity=Quantity("-1", ASSET))

    def test_a_limit_order_without_a_price_cannot_exist(self):
        with self.assertRaises(ValueError):
            self.order(order_type=OrderType.LIMIT)

    def test_a_market_order_with_a_limit_price_cannot_exist(self):
        with self.assertRaises(ValueError):
            self.order(order_type=OrderType.MARKET, limit_price=ASK)

    def test_a_float_quantity_cannot_exist(self):
        with self.assertRaises(TypeError):
            Quantity(0.001, ASSET)

    def test_the_venue_saw_none_of_them(self):
        for build in (
            lambda: self.order(quantity=Quantity.zero(ASSET)),
            lambda: self.order(order_type=OrderType.LIMIT),
            lambda: self.order(order_type=OrderType.MARKET, limit_price=ASK),
        ):
            with self.subTest(build=build):
                with self.assertRaises(ValueError):
                    build()
        self.assertEqual(self.broker.placement_count, 0)


class TestItNeverClaimsNotToKnow(PaperCase):
    """A venue in this process is never genuinely in doubt.

    Which is a property worth asserting rather than assuming: an ``UNCERTAIN``
    ack from here would put the whole system into the UNKNOWN state and stop it
    accepting orders, over nothing.
    """

    def test_no_scenario_produces_an_uncertain_ack(self):
        scenarios = {
            "fill": lambda: None,
            "dark": lambda: self.feed.go_dark(SYMBOL),
            "stale": lambda: self.clock.advance(6),
            "no depth": lambda: self.broker.set_depth(SYMBOL, Quantity.zero(ASSET)),
            "wrong depth asset": lambda: self.broker.set_depth(
                SYMBOL, Quantity("1", "ETH")
            ),
        }
        for name, arrange in scenarios.items():
            with self.subTest(scenario=name):
                self.setUp()
                arrange()
                ack = self.ack()
                self.assertIsNot(ack.outcome, AckOutcome.UNCERTAIN)
                self.assertFalse(ack.is_uncertain)

    def test_there_is_no_way_to_script_a_lie(self):
        """The hostile cases live in SimulatedBroker, and stay there."""
        for attribute in ("script", "raise_on_next", "set_venue_position"):
            with self.subTest(attribute=attribute):
                self.assertFalse(hasattr(self.broker, attribute))


class TestQueryingAndCancelling(PaperCase):
    """What the venue will tell an operator afterwards."""

    def test_an_unknown_order_has_no_record(self):
        ack = self.broker.fetch_order_state(self.order())
        self.assertIs(ack.outcome, AckOutcome.REJECTED)
        self.assertIn("no record", ack.message)

    def test_a_filled_order_reports_its_fill(self):
        order, placed = self.place()
        found = self.broker.fetch_order_state(order)
        self.assertEqual(found, placed)
        self.assertEqual(found.fill_price, ASK)

    def test_cancelling_a_resting_order_removes_it(self):
        order = self.order(
            order_type=OrderType.LIMIT, limit_price=Price("49000", USD)
        )
        self.place(order)
        ack = self.broker.cancel_order(order)
        self.assertIs(ack.outcome, AckOutcome.ACCEPTED)
        self.assertEqual(self.broker.resting_keys, frozenset())

    def test_cancelling_something_unknown_is_idempotent(self):
        ack = self.broker.cancel_order(self.order())
        self.assertIs(ack.outcome, AckOutcome.ACCEPTED)
        self.assertIn("idempotent", ack.message)

    def test_cancelling_a_filled_order_is_refused(self):
        """A venue that says "canceled" about a fill is how a position gets lost."""
        order, _ = self.place()
        ack = self.broker.cancel_order(order)
        self.assertIs(ack.outcome, AckOutcome.REJECTED)
        self.assertIn("already filled", ack.message)

    def test_a_cancelled_fill_is_still_reported_afterwards(self):
        order, _ = self.place()
        self.broker.cancel_order(order)
        self.assertIs(self.broker.fetch_order_state(order).outcome, AckOutcome.FILLED)

    def test_cancelling_a_partial_fill_cancels_only_the_remainder(self):
        self.broker.set_depth(SYMBOL, Quantity("0.0004", ASSET))
        order, _ = self.place()
        ack = self.broker.cancel_order(order)
        self.assertIs(ack.outcome, AckOutcome.ACCEPTED)
        self.assertIn("remainder", ack.message)
        # The fill happened, so the record must survive the cancel.
        self.assertIs(self.broker.fetch_order_state(order).outcome, AckOutcome.FILLED)

    def test_the_placement_log_records_order_id_and_key_in_order(self):
        first, _ = self.place()
        second, _ = self.place()
        self.assertEqual(
            self.broker.placements,
            [
                (first.order_id, first.idempotency_key),
                (second.order_id, second.idempotency_key),
            ],
        )

    def test_the_placement_log_is_a_copy(self):
        self.place()
        self.broker.placements.clear()
        self.assertEqual(self.broker.placement_count, 1)

    def test_an_unseen_key_has_been_seen_zero_times(self):
        self.assertEqual(self.broker.times_seen("nope"), 0)

    def test_the_venue_reports_a_duplicate_if_it_ever_sees_one(self):
        """Proving the detector works, so an empty ``duplicate_keys`` means something."""
        order = self.order()
        self.broker.place_order(order, token=self.token(order))
        self.broker.place_order(order, token=self.token(order))
        self.assertEqual(
            self.broker.duplicate_keys, frozenset({order.idempotency_key})
        )


# -- the part that matters --------------------------------------------------


class GatewayCase(unittest.TestCase):
    """A full rig with the paper venue behind the one execution gateway."""

    slippage_bps = 0
    depth = None
    mode = TradingMode.PAPER
    risk = None

    def setUp(self):
        self.clock = ManualClock()
        self.feed = InMemoryQuoteFeed(clock=self.clock, source="paper-test")
        self.feed.publish(SYMBOL, BID, ASK)
        self.paper = PaperBroker(
            clock=self.clock,
            quotes=self.feed,
            slippage_bps=self.slippage_bps,
            depth=self.depth,
        )
        self.rig = build_rig(
            clock=self.clock, broker=self.paper, mode=self.mode, risk=self.risk
        )


class TestThroughTheGateway(GatewayCase):
    """Nothing about the venue changes which gates run, or in what order."""

    def test_a_well_formed_order_executes(self):
        result = self.rig.submit()
        self.assertTrue(result.is_executed, result.reason)
        self.assertIs(result.order.state, OrderState.FILLED)
        self.assertEqual(result.ack.fill_price, ASK)

    def test_the_fill_reaches_the_portfolio_at_the_venue_price(self):
        self.rig.submit()
        self.assertEqual(
            self.rig.positions.snapshot()[SYMBOL], DEFAULT_QUANTITY
        )
        position = self.rig.portfolio.position(SYMBOL)
        self.assertEqual(position.average_entry_price, ASK)

    def test_the_venue_and_the_ledger_agree_afterwards(self):
        """INVARIANT 6: a paper fill must not create a mismatch of its own."""
        self.rig.submit()
        report = self.rig.reconciliation.reconcile(
            self.paper.fetch_positions().positions
        )
        self.assertTrue(report.is_clean, report.as_details())

    def test_the_execution_is_audited(self):
        self.rig.submit()
        self.assertIn("gateway.executed", self.rig.actions())

    def test_a_disabled_mode_never_reaches_the_venue(self):
        rig = build_rig(clock=self.clock, broker=self.paper, mode=TradingMode.DISABLED)
        result = rig.submit()
        self.assertTrue(result.is_refused)
        self.assertEqual(result.gate, ExecutionGate.TRADING_MODE)
        self.assertEqual(self.paper.placement_count, 0)

    def test_the_kill_switch_never_reaches_the_venue(self):
        self.rig.kill_switch.engage(self.rig.operator_id, reason="test")
        result = self.rig.submit()
        self.assertEqual(result.gate, ExecutionGate.KILL_SWITCH)
        self.assertEqual(self.paper.placement_count, 0)

    def test_an_open_breaker_never_reaches_the_venue(self):
        for _ in range(3):
            self.rig.breaker.record_failure(reason="test")
        result = self.rig.submit()
        self.assertEqual(result.gate, ExecutionGate.CIRCUIT_BREAKERS)
        self.assertEqual(self.paper.placement_count, 0)

    def test_a_risk_breach_never_reaches_the_venue(self):
        """The default per-order ceiling is 100 USD; this order is worth 5 000."""
        result = self.rig.submit(quantity=Quantity("0.1", ASSET))
        self.assertTrue(result.is_refused)
        self.assertEqual(result.gate, ExecutionGate.RISK)
        self.assertEqual(self.paper.placement_count, 0)

    def test_an_unauthorised_proposer_never_reaches_the_venue(self):
        result = self.rig.submit(proposer=self.rig.operator_id)
        self.assertEqual(result.gate, ExecutionGate.AUTHORIZATION)
        self.assertEqual(self.paper.placement_count, 0)

    def test_a_duplicate_intent_reaches_the_venue_once(self):
        """INVARIANT 12, evidenced by the venue rather than asserted of the gateway."""
        intent = self.rig.intent()
        first = self.rig.submit(intent)
        second = self.rig.submit(intent)
        self.assertTrue(first.is_executed)
        self.assertTrue(second.is_refused)
        self.assertEqual(second.gate, ExecutionGate.DUPLICATE_ORDER)
        self.assertEqual(self.paper.times_seen(intent.idempotency_key), 1)
        self.assertEqual(self.paper.duplicate_keys, frozenset())

    def test_a_venue_rejection_is_a_refusal_at_the_execution_gate(self):
        self.feed.go_dark(SYMBOL)
        result = self.rig.submit()
        self.assertTrue(result.is_refused)
        self.assertEqual(result.gate, ExecutionGate.EXECUTION)
        self.assertIn(PaperReject.NO_QUOTE, result.reason)
        self.assertIs(result.order.state, OrderState.REJECTED)

    def test_a_rejected_order_leaves_the_portfolio_alone(self):
        self.feed.go_dark(SYMBOL)
        self.rig.submit()
        self.assertEqual(self.rig.positions.snapshot(), {})

    def test_a_rejected_order_does_not_block_the_next_one(self):
        """A rejection is definitive, so it settles rather than latching."""
        self.feed.go_dark(SYMBOL)
        self.rig.submit()
        self.feed.publish(SYMBOL, BID, ASK)
        self.assertTrue(self.rig.submit().is_executed)

    def test_a_stale_feed_refuses_at_the_venue_too(self):
        """Belt and braces: the risk gate reads prices the caller supplied, so the
        venue is the second place a frozen feed is caught rather than the first."""
        self.clock.advance(6)
        result = self.rig.submit()
        self.assertTrue(result.is_refused)
        self.assertIn(PaperReject.STALE_QUOTE, result.reason)

    def test_nothing_ends_up_in_the_unknown_state(self):
        for arrange in (lambda: None, lambda: self.feed.go_dark(SYMBOL)):
            with self.subTest(arrange=arrange):
                arrange()
                self.rig.submit()
        self.assertEqual(self.rig.orders.unknown_orders(), [])
        self.assertFalse(self.rig.reconciliation.has_mismatch)


class TestPartialFillsThroughTheGateway(GatewayCase):
    """The lifecycle state nothing produced before Stage 2F."""

    depth = {SYMBOL: Quantity("0.0004", ASSET)}

    def test_the_order_lands_in_partially_filled(self):
        result = self.rig.submit()
        self.assertTrue(result.is_executed)
        self.assertIs(result.order.state, OrderState.PARTIALLY_FILLED)

    def test_it_is_still_open_with_a_remainder(self):
        result = self.rig.submit()
        self.assertTrue(result.order.is_open)
        self.assertEqual(result.order.filled_quantity, Quantity("0.0004", ASSET))
        self.assertEqual(result.order.remaining_quantity, Quantity("0.0006", ASSET))

    def test_only_the_filled_quantity_reaches_the_portfolio(self):
        self.rig.submit()
        self.assertEqual(
            self.rig.positions.snapshot()[SYMBOL], Quantity("0.0004", ASSET)
        )

    def test_the_venue_and_the_ledger_still_agree(self):
        self.rig.submit()
        report = self.rig.reconciliation.reconcile(
            self.paper.fetch_positions().positions
        )
        self.assertTrue(report.is_clean, report.as_details())

    def test_the_key_is_settled_so_the_remainder_cannot_be_resubmitted(self):
        """Re-sending the same intent is a duplicate, not a top-up."""
        intent = self.rig.intent()
        self.rig.submit(intent)
        second = self.rig.submit(intent)
        self.assertEqual(second.gate, ExecutionGate.DUPLICATE_ORDER)


class TestSlippageThroughTheGateway(GatewayCase):
    """Cost expressed in the fill price is cost the risk ledger can see."""

    slippage_bps = 100
    risk = RiskConfig(max_daily_loss=Money("500.00", USD))

    def test_a_round_trip_at_a_worse_price_realizes_a_loss(self):
        self.rig.submit()
        self.rig.submit(side=OrderSide.SELL)
        self.assertTrue(self.rig.risk.pnl.realized.amount < 0, self.rig.risk.pnl.realized)
        self.assertTrue(self.rig.risk.pnl.realized_loss.amount > 0)

    def test_that_loss_reduces_the_remaining_daily_allowance(self):
        """Stage 2D's wiring, reached through a paper fill rather than a scripted ack."""
        before = self.rig.risk.remaining_loss_budget
        self.rig.submit()
        self.rig.submit(side=OrderSide.SELL)
        after = self.rig.risk.remaining_loss_budget
        self.assertIsNotNone(before)
        self.assertIsNotNone(after)
        self.assertTrue(after.amount < before.amount, f"{before} -> {after}")


class TestNoNetworkAnywhere(unittest.TestCase):
    """The Stage 1 constraint, checked on the module rather than assumed."""

    def test_the_paper_venue_reaches_no_infrastructure(self):
        import trading.adapters.paper.broker as mod

        for name in ("socket", "requests", "httpx", "urllib", "os", "ssl"):
            with self.subTest(name=name):
                self.assertNotIn(name, vars(mod))

    def test_it_holds_no_credential_and_no_endpoint(self):
        import trading.adapters.paper.broker as mod
        import inspect

        source = inspect.getsource(mod)
        for token in ("http://", "https://", "api_key", "secret", "Bearer"):
            with self.subTest(token=token):
                self.assertNotIn(token, source)


if __name__ == "__main__":
    unittest.main()
