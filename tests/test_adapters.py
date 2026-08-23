"""Tests for the in-memory adapters.

These are the only :mod:`trading.ports` implementations that ship in Stage 1, and
the safety tests lean on them heavily: every end-to-end claim about UNKNOWN
orders, duplicate suppression, and position mismatch is ultimately a claim about
how :class:`SimulatedBroker` behaved. So the simulator's own contract needs
pinning. If it silently stopped modelling a landed-but-unacknowledged order, a
whole family of gateway tests would keep passing while proving nothing.

Two properties matter most:

1. **The token is consumed before anything else happens** -- including before a
   scripted failure fires. The simulator models a venue that has already taken
   the bytes, so a retry must not find a reusable token (INVARIANT 3, 12).
2. **What we learn is decoupled from what happened.** ``lands_at_venue`` is the
   whole reason the UNKNOWN path is testable, and ``fetch_order_state`` must
   reveal a landed order rather than repeating our uncertainty (INVARIANT 5).
"""

from __future__ import annotations

import dataclasses
import unittest
from decimal import Decimal

from trading.adapters.memory import BrokerFailure, ScriptedAck, SimulatedBroker
from trading.adapters.memory import StaticMarketData
from trading.core.authz import Principal, Role, mint_execution_token
from trading.core.clock import ManualClock
from trading.core.errors import SafetyViolation, UnauthorizedAction
from trading.core.money import USD, Price, Quantity
from trading.core.orders import Order, OrderIntent, OrderSide, OrderType
from trading.ports.broker import AckOutcome, BrokerAck

SYMBOL = "BTCUSD"
ASSET = "BTC"
PRICE = Price("50000", USD)
QUANTITY = Quantity("0.001", ASSET)


class BrokerCase(unittest.TestCase):
    """A simulator plus the machinery needed to place a legitimate order."""

    def setUp(self):
        self.clock = ManualClock()
        self.gateway_id = Principal("gateway-1", Role.EXECUTION_GATEWAY)
        self.broker = SimulatedBroker(
            clock=self.clock,
            default_outcome=AckOutcome.FILLED,
            fill_prices={SYMBOL: PRICE},
        )
        self._counter = 0

    def order(self, *, side: OrderSide = OrderSide.BUY, quantity=QUANTITY) -> Order:
        self._counter += 1
        intent = OrderIntent(
            strategy_id="strat-1",
            signal_id=f"sig-{self._counter}",
            symbol=SYMBOL,
            side=side,
            order_type=OrderType.MARKET,
            quantity=quantity,
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

    def place(self, order: Order | None = None, **kwargs) -> BrokerAck:
        order = order or self.order()
        return self.broker.place_order(order, token=self.token(order, **kwargs))


class TestConstruction(BrokerCase):
    def test_a_non_ack_default_outcome_is_refused(self):
        with self.assertRaises(TypeError):
            SimulatedBroker(clock=self.clock, default_outcome="filled")

    def test_a_float_fill_price_is_refused(self):
        """INVARIANT 8 at the venue boundary, not just inside the core."""
        with self.assertRaises(TypeError) as ctx:
            SimulatedBroker(clock=self.clock, fill_prices={SYMBOL: 50000.0})
        self.assertIn(SYMBOL, str(ctx.exception))

    def test_the_default_default_is_accepted_not_filled(self):
        """A venue that fills by default would hide the accepted-then-fill path."""
        broker = SimulatedBroker(clock=self.clock)
        ack = broker.place_order(
            (order := self.order()), token=self.token(order)
        )
        self.assertIs(ack.outcome, AckOutcome.ACCEPTED)

    def test_it_starts_with_no_history(self):
        self.assertEqual(self.broker.placement_count, 0)
        self.assertEqual(self.broker.attempts, [])
        self.assertEqual(self.broker.duplicate_keys, frozenset())
        self.assertEqual(self.broker.resting_keys, frozenset())


class TestTokenIsConsumedFirst(BrokerCase):
    """INVARIANT 3: no placement happens without gateway-minted authority."""

    def test_a_placement_without_a_token_is_a_type_error(self):
        order = self.order()
        with self.assertRaises(TypeError):
            self.broker.place_order(order)

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
        """The refusal must happen before any state changes."""
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

    def test_the_token_is_spent_even_when_the_placement_raises(self):
        """A socket dying after the bytes left must not hand back a usable token.

        This is the property that makes a blind retry impossible: the caller has
        no authority left, so it has to go through the gateway again, where the
        idempotency registry is waiting.
        """
        order = self.order()
        token = self.token(order)
        self.broker.raise_on_next()
        with self.assertRaises(BrokerFailure):
            self.broker.place_order(order, token=token)
        self.assertTrue(token.is_consumed)
        with self.assertRaises(SafetyViolation):
            self.broker.place_order(order, token=token)

    def test_a_failed_placement_still_counts_as_an_attempt(self):
        """Because the venue may well have it. Silence is not absence."""
        order = self.order()
        self.broker.raise_on_next()
        with self.assertRaises(BrokerFailure):
            self.broker.place_order(order, token=self.token(order))
        self.assertEqual(self.broker.times_seen(order.idempotency_key), 1)


class TestDuplicateEvidence(BrokerCase):
    """INVARIANT 12, evidenced by the venue rather than asserted of the gateway."""

    def test_distinct_orders_produce_no_duplicates(self):
        for _ in range(3):
            self.place()
        self.assertEqual(self.broker.duplicate_keys, frozenset())
        self.assertEqual(self.broker.placement_count, 3)

    def test_the_same_key_twice_is_reported(self):
        """Two tokens for one key is exactly what the gateway must never do.

        Minted deliberately here so the detector itself is proven to work -- an
        always-empty ``duplicate_keys`` would make every gateway test vacuous.
        """
        order = self.order()
        self.broker.place_order(order, token=self.token(order))
        self.broker.place_order(order, token=self.token(order))
        self.assertEqual(self.broker.duplicate_keys, frozenset({order.idempotency_key}))
        self.assertEqual(self.broker.times_seen(order.idempotency_key), 2)

    def test_an_unseen_key_has_been_seen_zero_times(self):
        self.assertEqual(self.broker.times_seen("nope"), 0)

    def test_attempts_records_order_id_and_key_in_order(self):
        first, second = self.order(), self.order()
        self.broker.place_order(first, token=self.token(first))
        self.broker.place_order(second, token=self.token(second))
        self.assertEqual(
            self.broker.attempts,
            [
                (first.order_id, first.idempotency_key),
                (second.order_id, second.idempotency_key),
            ],
        )

    def test_attempts_is_a_copy(self):
        self.place()
        snapshot = self.broker.attempts
        snapshot.clear()
        self.assertEqual(self.broker.placement_count, 1)


class TestScriptedAcks(BrokerCase):
    def test_a_scripted_ack_is_returned_instead_of_the_default(self):
        self.broker.script(BrokerAck(AckOutcome.REJECTED, message="no"))
        ack = self.place()
        self.assertIs(ack.outcome, AckOutcome.REJECTED)
        self.assertEqual(ack.message, "no")

    def test_scripts_are_consumed_in_order(self):
        self.broker.script(BrokerAck(AckOutcome.REJECTED, message="first"))
        self.broker.script(BrokerAck(AckOutcome.UNCERTAIN, message="second"))
        self.assertEqual(self.place().message, "first")
        self.assertEqual(self.place().message, "second")

    def test_the_default_resumes_once_the_script_is_exhausted(self):
        self.broker.script(BrokerAck(AckOutcome.REJECTED))
        self.place()
        self.assertIs(self.place().outcome, AckOutcome.FILLED)

    def test_script_refuses_a_non_ack(self):
        with self.assertRaises(TypeError):
            self.broker.script("filled")

    def test_a_scripted_ack_takes_precedence_over_a_queued_failure(self):
        """Both were requested; the script is the more specific instruction."""
        self.broker.script(BrokerAck(AckOutcome.ACCEPTED))
        self.broker.raise_on_next()
        self.assertIs(self.place().outcome, AckOutcome.ACCEPTED)

    def test_a_queued_failure_fires_only_once(self):
        self.broker.raise_on_next()
        with self.assertRaises(BrokerFailure):
            self.place()
        self.assertIs(self.place().outcome, AckOutcome.FILLED)

    def test_the_failure_message_is_carried(self):
        self.broker.raise_on_next("socket closed")
        with self.assertRaises(BrokerFailure) as ctx:
            self.place()
        self.assertIn("socket closed", str(ctx.exception))

    def test_a_scripted_ack_is_immutable_once_queued(self):
        scripted = ScriptedAck(BrokerAck(AckOutcome.ACCEPTED))
        with self.assertRaises(dataclasses.FrozenInstanceError):
            scripted.lands_at_venue = True

    def test_a_filled_ack_must_carry_a_quantity_and_a_price(self):
        """Otherwise a fill could be recorded with no idea what it cost."""
        with self.assertRaises(ValueError):
            BrokerAck(AckOutcome.FILLED)


class TestWhatWeLearnVersusWhatHappened(BrokerCase):
    """The simulator's reason for existing: INVARIANT 5's dangerous case."""

    def test_an_uncertain_ack_that_did_not_land_leaves_no_order(self):
        self.broker.script(BrokerAck(AckOutcome.UNCERTAIN), lands_at_venue=False)
        order = self.order()
        self.broker.place_order(order, token=self.token(order))
        self.assertEqual(self.broker.resting_keys, frozenset())
        self.assertIs(
            self.broker.fetch_order_state(order).outcome, AckOutcome.REJECTED
        )

    def test_an_uncertain_ack_that_landed_leaves_a_live_order(self):
        self.broker.script(BrokerAck(AckOutcome.UNCERTAIN), lands_at_venue=True)
        order = self.order()
        ack = self.broker.place_order(order, token=self.token(order))
        self.assertIs(ack.outcome, AckOutcome.UNCERTAIN, "we still learn nothing")
        self.assertIn(order.idempotency_key, self.broker.resting_keys)

    def test_fetching_a_landed_order_reveals_it_rather_than_repeating_doubt(self):
        """Otherwise reconciliation could never exit UNKNOWN."""
        self.broker.script(BrokerAck(AckOutcome.UNCERTAIN), lands_at_venue=True)
        order = self.order()
        self.broker.place_order(order, token=self.token(order))
        resolved = self.broker.fetch_order_state(order)
        self.assertIs(resolved.outcome, AckOutcome.ACCEPTED)
        self.assertTrue(resolved.broker_order_id)

    def test_a_venue_with_no_record_reports_rejected(self):
        order = self.order()
        ack = self.broker.fetch_order_state(order)
        self.assertIs(ack.outcome, AckOutcome.REJECTED)
        self.assertIn("no record", ack.message)

    def test_a_rejected_ack_leaves_nothing_resting(self):
        self.broker.script(BrokerAck(AckOutcome.REJECTED))
        order = self.order()
        self.broker.place_order(order, token=self.token(order))
        self.assertEqual(self.broker.resting_keys, frozenset())

    def test_a_transport_failure_leaves_nothing_resting_but_is_still_ambiguous(self):
        """The simulator's default for a raise is "did not land".

        A test that needs the other reading scripts UNCERTAIN with
        ``lands_at_venue=True``; the gateway cannot tell the two apart, which is
        the whole point.
        """
        order = self.order()
        self.broker.raise_on_next()
        with self.assertRaises(BrokerFailure):
            self.broker.place_order(order, token=self.token(order))
        self.assertEqual(self.broker.resting_keys, frozenset())

    def test_an_accepted_order_rests_at_the_venue(self):
        order = self.order()
        self.broker.place_order(order, token=self.token(order))
        self.assertIn(order.idempotency_key, self.broker.resting_keys)

    def test_cancelling_removes_it(self):
        order = self.order()
        self.broker.place_order(order, token=self.token(order))
        ack = self.broker.cancel_order(order)
        self.assertIs(ack.outcome, AckOutcome.ACCEPTED)
        self.assertEqual(self.broker.resting_keys, frozenset())

    def test_cancelling_something_unknown_is_not_an_error(self):
        """Cancel must be idempotent; an operator may retry it."""
        ack = self.broker.cancel_order(self.order())
        self.assertIs(ack.outcome, AckOutcome.ACCEPTED)


class TestFills(BrokerCase):
    def test_a_fill_uses_the_configured_price(self):
        ack = self.place()
        self.assertIs(ack.outcome, AckOutcome.FILLED)
        self.assertEqual(ack.fill_price, PRICE)
        self.assertEqual(ack.filled_quantity, QUANTITY)

    def test_with_no_price_it_accepts_rather_than_inventing_one(self):
        """INVARIANT 8: a made-up fill price would corrupt every downstream sum."""
        broker = SimulatedBroker(clock=self.clock, default_outcome=AckOutcome.FILLED)
        order = self.order()
        ack = broker.place_order(order, token=self.token(order))
        self.assertIs(ack.outcome, AckOutcome.ACCEPTED)
        self.assertIsNone(ack.fill_price)
        self.assertIn("no simulated fill price", ack.message)

    def test_a_fill_price_can_be_set_after_construction(self):
        broker = SimulatedBroker(clock=self.clock, default_outcome=AckOutcome.FILLED)
        broker.set_fill_price(SYMBOL, PRICE)
        order = self.order()
        self.assertIs(
            broker.place_order(order, token=self.token(order)).outcome,
            AckOutcome.FILLED,
        )

    def test_set_fill_price_refuses_a_float(self):
        with self.assertRaises(TypeError):
            self.broker.set_fill_price(SYMBOL, 50000.0)

    def test_broker_order_ids_are_unique_and_sequential(self):
        ids = [self.place().broker_order_id for _ in range(3)]
        self.assertEqual(ids, ["SIM-000001", "SIM-000002", "SIM-000003"])
        self.assertEqual(len(set(ids)), 3)

    def test_a_rejection_carries_no_broker_order_id(self):
        broker = SimulatedBroker(clock=self.clock, default_outcome=AckOutcome.REJECTED)
        order = self.order()
        ack = broker.place_order(order, token=self.token(order))
        self.assertIsNone(ack.broker_order_id)

    def test_an_uncertain_default_carries_no_broker_order_id(self):
        broker = SimulatedBroker(clock=self.clock, default_outcome=AckOutcome.UNCERTAIN)
        order = self.order()
        ack = broker.place_order(order, token=self.token(order))
        self.assertIs(ack.outcome, AckOutcome.UNCERTAIN)
        self.assertIsNone(ack.broker_order_id)


class TestVenuePositions(BrokerCase):
    """INVARIANT 6 needs a venue that can actually disagree with us."""

    def test_a_buy_fill_moves_the_venue_position_long(self):
        self.place()
        snapshot = self.broker.fetch_positions()
        self.assertEqual(snapshot.positions[SYMBOL], QUANTITY)

    def test_a_sell_fill_moves_it_short(self):
        self.place(self.order(side=OrderSide.SELL))
        snapshot = self.broker.fetch_positions()
        self.assertEqual(snapshot.positions[SYMBOL].amount, -QUANTITY.amount)

    def test_fills_accumulate(self):
        self.place()
        self.place()
        snapshot = self.broker.fetch_positions()
        self.assertEqual(snapshot.positions[SYMBOL].amount, QUANTITY.amount * 2)

    def test_a_buy_then_an_equal_sell_nets_flat(self):
        self.place()
        self.place(self.order(side=OrderSide.SELL))
        snapshot = self.broker.fetch_positions()
        self.assertEqual(snapshot.positions[SYMBOL].amount, Decimal(0))

    def test_a_position_can_be_forced_to_create_a_mismatch(self):
        forced = Quantity("5", ASSET)
        self.broker.set_venue_position(SYMBOL, forced)
        self.assertEqual(self.broker.fetch_positions().positions[SYMBOL], forced)

    def test_set_venue_position_refuses_a_float(self):
        with self.assertRaises(TypeError):
            self.broker.set_venue_position(SYMBOL, 5.0)

    def test_the_snapshot_is_a_copy(self):
        self.broker.set_venue_position(SYMBOL, Quantity("5", ASSET))
        snapshot = self.broker.fetch_positions()
        dict(snapshot.positions).clear()
        self.assertIn(SYMBOL, self.broker.fetch_positions().positions)

    def test_an_unfilled_order_does_not_move_the_position(self):
        broker = SimulatedBroker(clock=self.clock, default_outcome=AckOutcome.ACCEPTED)
        order = self.order()
        broker.place_order(order, token=self.token(order))
        self.assertEqual(broker.fetch_positions().positions, {})


class TestStaticMarketData(unittest.TestCase):
    """The price boundary. A float must not get through it (INVARIANT 8)."""

    def setUp(self):
        self.feed = StaticMarketData({SYMBOL: PRICE})

    def test_a_known_price_is_returned(self):
        self.assertEqual(self.feed.mark_price(SYMBOL), PRICE)

    def test_an_unknown_symbol_is_none(self):
        self.assertIsNone(self.feed.mark_price("ETHUSD"))

    def test_a_price_can_be_set(self):
        new = Price("60000", USD)
        self.feed.set_price(SYMBOL, new)
        self.assertEqual(self.feed.mark_price(SYMBOL), new)

    def test_a_float_price_is_refused_with_the_invariant_named(self):
        with self.assertRaises(TypeError) as ctx:
            self.feed.set_price(SYMBOL, 50000.0)
        self.assertIn("INVARIANT 8", str(ctx.exception))

    def test_a_decimal_is_not_a_price_either(self):
        """A bare Decimal has no currency, so it cannot be a price."""
        with self.assertRaises(TypeError):
            self.feed.set_price(SYMBOL, Decimal("50000"))

    def test_an_int_is_refused(self):
        with self.assertRaises(TypeError):
            self.feed.set_price(SYMBOL, 50000)

    def test_a_none_price_is_refused(self):
        with self.assertRaises(TypeError):
            self.feed.set_price(SYMBOL, None)

    def test_an_empty_symbol_is_refused(self):
        for bad in ("", "   "):
            with self.subTest(symbol=repr(bad)):
                with self.assertRaises(ValueError):
                    self.feed.set_price(bad, PRICE)

    def test_a_non_string_symbol_is_refused(self):
        with self.assertRaises(ValueError):
            self.feed.set_price(None, PRICE)

    def test_construction_validates_too(self):
        """Otherwise the constructor would be a way around ``set_price``."""
        with self.assertRaises(TypeError):
            StaticMarketData({SYMBOL: 50000.0})

    def test_going_dark_drops_the_price(self):
        self.feed.go_dark(SYMBOL)
        self.assertIsNone(self.feed.mark_price(SYMBOL))

    def test_going_dark_on_an_unknown_symbol_is_harmless(self):
        self.feed.go_dark("ETHUSD")
        self.assertEqual(self.feed.symbols(), [SYMBOL])

    def test_symbols_are_sorted(self):
        self.feed.set_price("ETHUSD", Price("3000", USD))
        self.feed.set_price("ADAUSD", Price("1", USD))
        self.assertEqual(self.feed.symbols(), ["ADAUSD", "BTCUSD", "ETHUSD"])

    def test_an_empty_feed_has_no_symbols(self):
        self.assertEqual(StaticMarketData().symbols(), [])

    def test_a_batch_read_omits_a_dark_symbol(self):
        self.feed.set_price("ETHUSD", Price("3000", USD))
        self.feed.go_dark("ETHUSD")
        self.assertEqual(set(self.feed.mark_prices([SYMBOL, "ETHUSD"])), {SYMBOL})

    def test_no_network_or_credential_is_reachable(self):
        """The Stage 1 constraint, checked on the module rather than assumed."""
        import trading.adapters.memory.market_data as mod

        for name in ("socket", "requests", "httpx", "urllib", "os"):
            self.assertNotIn(name, vars(mod))


if __name__ == "__main__":
    unittest.main()
