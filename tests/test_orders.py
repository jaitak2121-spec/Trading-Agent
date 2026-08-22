"""Tests for order intents, the order state machine, and the order store.

Covers INVARIANT 5 at the order level: an UNKNOWN order can only be left
through reconciliation.
"""

from __future__ import annotations

import threading
import unittest
from decimal import Decimal

from trading.core.clock import ManualClock
from trading.core.errors import (
    InvalidOrderTransition,
    ReconciliationRequired,
    SafetyViolation,
)
from trading.core.money import Currency, Money, Price, Quantity
from trading.core.orders import (
    ORDER_TRANSITIONS,
    TERMINAL_STATES,
    Order,
    OrderIntent,
    OrderSide,
    OrderState,
    OrderStore,
    OrderType,
)

USD = Currency("USD", 2)


def intent(**overrides) -> OrderIntent:
    params = {
        "strategy_id": "momentum-v1",
        "signal_id": "sig-1",
        "symbol": "BTCUSD",
        "side": OrderSide.BUY,
        "quantity": Quantity("0.5", "BTC"),
    }
    params.update(overrides)
    return OrderIntent(**params)


class TestOrderIntentIsInert(unittest.TestCase):
    """INVARIANT 3 support: an intent carries no ability to act."""

    def test_intent_has_no_execution_surface(self):
        i = intent()
        public = [name for name in dir(i) if not name.startswith("_")]
        for forbidden in ("execute", "submit", "send", "broker", "client", "place", "cancel"):
            with self.subTest(attribute=forbidden):
                offenders = [name for name in public if forbidden in name.lower()]
                self.assertEqual(
                    offenders,
                    [],
                    f"OrderIntent exposes {offenders} matching {forbidden!r}",
                )

    def test_intent_public_surface_is_data_only(self):
        i = intent()
        public = {name for name in dir(i) if not name.startswith("_")}
        self.assertEqual(
            public,
            {
                "strategy_id",
                "signal_id",
                "symbol",
                "side",
                "quantity",
                "order_type",
                "limit_price",
                "idempotency_key",
                "as_details",
            },
        )

    def test_intent_is_frozen(self):
        i = intent()
        with self.assertRaises(Exception):
            i.symbol = "ETHUSD"  # type: ignore[misc]

    def test_intent_is_hashable(self):
        self.assertEqual(len({intent(), intent()}), 1)


class TestOrderIntentValidation(unittest.TestCase):
    def test_empty_identifiers_rejected(self):
        for field in ("strategy_id", "signal_id", "symbol"):
            with self.subTest(field=field):
                with self.assertRaises(ValueError):
                    intent(**{field: ""})
                with self.assertRaises(ValueError):
                    intent(**{field: "   "})

    def test_non_enum_side_rejected(self):
        with self.assertRaises(TypeError):
            intent(side="buy")

    def test_non_enum_order_type_rejected(self):
        with self.assertRaises(TypeError):
            intent(order_type="market")

    def test_quantity_must_be_a_quantity(self):
        with self.assertRaises(TypeError):
            intent(quantity=Decimal("0.5"))

    def test_zero_quantity_rejected(self):
        with self.assertRaises(ValueError):
            intent(quantity=Quantity("0", "BTC"))

    def test_negative_quantity_rejected(self):
        # Direction belongs to side, never to the sign of the quantity.
        with self.assertRaises(ValueError) as ctx:
            intent(quantity=Quantity("-0.5", "BTC"))
        self.assertIn("direction is carried by side", str(ctx.exception))

    def test_limit_order_requires_a_price(self):
        with self.assertRaises(ValueError):
            intent(order_type=OrderType.LIMIT)

    def test_limit_price_must_be_a_price(self):
        with self.assertRaises(TypeError):
            intent(order_type=OrderType.LIMIT, limit_price=Decimal("30000"))

    def test_market_order_must_not_carry_a_limit_price(self):
        with self.assertRaises(ValueError):
            intent(order_type=OrderType.MARKET, limit_price=Price("30000", USD))

    def test_valid_limit_order(self):
        i = intent(order_type=OrderType.LIMIT, limit_price=Price("30000", USD))
        self.assertEqual(i.limit_price, Price("30000", USD))

    def test_side_sign(self):
        self.assertEqual(OrderSide.BUY.sign, 1)
        self.assertEqual(OrderSide.SELL.sign, -1)


class TestIdempotencyKeyDerivation(unittest.TestCase):
    """INVARIANT 12 support: the key must be a function of the content."""

    def test_same_content_same_key(self):
        self.assertEqual(intent().idempotency_key, intent().idempotency_key)

    def test_key_is_stable_across_instances_and_calls(self):
        i = intent()
        self.assertEqual(i.idempotency_key, i.idempotency_key)

    def test_key_changes_with_every_meaningful_field(self):
        base = intent().idempotency_key
        variations = {
            "strategy_id": {"strategy_id": "mean-reversion"},
            "signal_id": {"signal_id": "sig-2"},
            "symbol": {"symbol": "ETHUSD"},
            "side": {"side": OrderSide.SELL},
            "quantity": {"quantity": Quantity("0.6", "BTC")},
            "asset": {"quantity": Quantity("0.5", "XBT")},
            "order_type": {
                "order_type": OrderType.LIMIT,
                "limit_price": Price("30000", USD),
            },
        }
        for name, override in variations.items():
            with self.subTest(field=name):
                self.assertNotEqual(base, intent(**override).idempotency_key)

    def test_limit_price_and_currency_affect_the_key(self):
        eur = Currency("EUR", 2)
        a = intent(order_type=OrderType.LIMIT, limit_price=Price("30000", USD))
        b = intent(order_type=OrderType.LIMIT, limit_price=Price("30001", USD))
        c = intent(order_type=OrderType.LIMIT, limit_price=Price("30000", eur))
        self.assertEqual(len({a.idempotency_key, b.idempotency_key, c.idempotency_key}), 3)

    def test_key_is_a_sha256_hexdigest(self):
        key = intent().idempotency_key
        self.assertEqual(len(key), 64)
        int(key, 16)  # must parse as hex

    def test_quantity_scale_does_not_change_identity(self):
        # 0.5 and 0.50 are the same number but different Decimal strings. If the
        # key derivation used str() they would be different orders, and a retry
        # that formatted its quantity differently would slip past dedupe.
        a = intent(quantity=Quantity("0.5", "BTC"))
        b = intent(quantity=Quantity("0.50", "BTC"))
        self.assertEqual(a.quantity, b.quantity)
        self.assertEqual(a.idempotency_key, b.idempotency_key)

    def test_price_scale_does_not_change_identity(self):
        a = intent(order_type=OrderType.LIMIT, limit_price=Price("30000", USD))
        b = intent(order_type=OrderType.LIMIT, limit_price=Price("30000.00", USD))
        self.assertEqual(a.idempotency_key, b.idempotency_key)

    def test_exponent_notation_does_not_change_identity(self):
        a = intent(quantity=Quantity(Decimal("1E+2"), "BTC"))
        b = intent(quantity=Quantity(Decimal("100"), "BTC"))
        self.assertEqual(a.idempotency_key, b.idempotency_key)


class OrderFixture(unittest.TestCase):
    def setUp(self):
        self.clock = ManualClock()

    def order(self, **overrides) -> Order:
        return Order(intent(**overrides), clock=self.clock)

    def accepted(self, **overrides) -> Order:
        o = self.order(**overrides)
        o.transition_to(OrderState.PENDING_NEW, reason="submitted")
        o.transition_to(OrderState.ACCEPTED, reason="ack")
        return o


class TestOrderStateMachine(OrderFixture):
    def test_starts_in_draft(self):
        self.assertIs(self.order().state, OrderState.DRAFT)

    def test_happy_path(self):
        o = self.order()
        o.transition_to(OrderState.PENDING_NEW, reason="submitted")
        o.transition_to(OrderState.ACCEPTED, reason="ack")
        o.transition_to(OrderState.FILLED, reason="filled")
        self.assertIs(o.state, OrderState.FILLED)

    def test_draft_cannot_jump_to_filled(self):
        o = self.order()
        with self.assertRaises(InvalidOrderTransition) as ctx:
            o.transition_to(OrderState.FILLED, reason="wishful thinking")
        self.assertIn("not a valid transition", str(ctx.exception))

    def test_terminal_states_are_terminal(self):
        for terminal in TERMINAL_STATES:
            with self.subTest(state=terminal):
                self.assertEqual(ORDER_TRANSITIONS[terminal], frozenset())
                self.assertTrue(terminal.is_terminal)

    def test_filled_order_cannot_move(self):
        o = self.accepted()
        o.transition_to(OrderState.FILLED, reason="filled")
        for target in OrderState:
            with self.subTest(target=target):
                with self.assertRaises(InvalidOrderTransition):
                    o.transition_to(target, reason="attempt")

    def test_canceled_order_cannot_be_resurrected(self):
        o = self.accepted()
        o.transition_to(OrderState.CANCELED, reason="operator cancel")
        with self.assertRaises(InvalidOrderTransition):
            o.transition_to(OrderState.ACCEPTED, reason="undo")

    def test_transition_table_covers_every_state(self):
        for state in OrderState:
            self.assertIn(state, ORDER_TRANSITIONS)

    def test_no_transition_targets_draft(self):
        # DRAFT is an entry state only; nothing may return to it.
        for source, targets in ORDER_TRANSITIONS.items():
            with self.subTest(source=source):
                self.assertNotIn(OrderState.DRAFT, targets)

    def test_target_must_be_an_order_state(self):
        with self.assertRaises(TypeError):
            self.order().transition_to("filled", reason="x")

    def test_history_records_each_transition(self):
        o = self.order()
        o.transition_to(OrderState.PENDING_NEW, reason="submitted")
        o.transition_to(OrderState.ACCEPTED, reason="ack")
        history = o.history
        self.assertEqual(len(history), 2)
        self.assertIs(history[0].from_state, OrderState.DRAFT)
        self.assertIs(history[0].to_state, OrderState.PENDING_NEW)
        self.assertEqual(history[1].reason, "ack")

    def test_history_is_a_copy(self):
        o = self.order()
        o.transition_to(OrderState.PENDING_NEW, reason="submitted")
        snapshot = o.history
        list(snapshot).clear()
        self.assertEqual(len(o.history), 1)

    def test_rejected_transitions_are_not_recorded(self):
        o = self.order()
        with self.assertRaises(InvalidOrderTransition):
            o.transition_to(OrderState.FILLED, reason="nope")
        self.assertEqual(o.history, [])
        self.assertIs(o.state, OrderState.DRAFT)

    def test_is_open_flags(self):
        o = self.order()
        self.assertFalse(o.is_open)  # DRAFT is not open exposure
        o.transition_to(OrderState.PENDING_NEW, reason="submitted")
        self.assertTrue(o.is_open)
        o.transition_to(OrderState.REJECTED, reason="venue reject")
        self.assertFalse(o.is_open)


class TestUnknownOrderState(OrderFixture):
    """INVARIANT 5, at the order level."""

    def test_pending_new_can_become_unknown(self):
        o = self.order()
        o.transition_to(OrderState.PENDING_NEW, reason="submitted")
        o.mark_unknown(reason="submission timed out")
        self.assertIs(o.state, OrderState.UNKNOWN)
        self.assertTrue(o.is_unknown)

    def test_unknown_order_counts_as_open(self):
        # It may hold real exposure at the venue, so it is not "closed".
        o = self.order()
        o.transition_to(OrderState.PENDING_NEW, reason="submitted")
        o.mark_unknown(reason="timeout")
        self.assertFalse(o.state.is_terminal)

    def test_unknown_cannot_be_left_without_reconciliation(self):
        o = self.order()
        o.transition_to(OrderState.PENDING_NEW, reason="submitted")
        o.mark_unknown(reason="timeout")
        for target in (
            OrderState.ACCEPTED,
            OrderState.FILLED,
            OrderState.REJECTED,
            OrderState.CANCELED,
            OrderState.EXPIRED,
            OrderState.PARTIALLY_FILLED,
        ):
            with self.subTest(target=target):
                with self.assertRaises(ReconciliationRequired) as ctx:
                    o.transition_to(target, reason="assume it failed")
                self.assertIn("INVARIANT 5", str(ctx.exception))
        self.assertIs(o.state, OrderState.UNKNOWN)

    def test_unknown_exits_only_via_reconciliation(self):
        o = self.order()
        o.transition_to(OrderState.PENDING_NEW, reason="submitted")
        o.mark_unknown(reason="timeout")
        o.transition_to(
            OrderState.REJECTED,
            reason="venue confirms it never existed",
            via_reconciliation=True,
        )
        self.assertIs(o.state, OrderState.REJECTED)

    def test_reconciliation_flag_is_recorded_in_history(self):
        o = self.order()
        o.transition_to(OrderState.PENDING_NEW, reason="submitted")
        o.mark_unknown(reason="timeout")
        o.transition_to(
            OrderState.FILLED, reason="venue shows a fill", via_reconciliation=True
        )
        last = o.history[-1]
        self.assertTrue(last.via_reconciliation)
        self.assertIs(last.from_state, OrderState.UNKNOWN)

    def test_mark_unknown_is_idempotent(self):
        o = self.order()
        o.transition_to(OrderState.PENDING_NEW, reason="submitted")
        o.mark_unknown(reason="first")
        o.mark_unknown(reason="second")
        self.assertEqual(len(o.history), 2)  # PENDING_NEW + UNKNOWN only

    def test_terminal_order_cannot_become_unknown(self):
        # Once the venue told us the outcome, ambiguity is not a valid regression.
        o = self.accepted()
        o.transition_to(OrderState.FILLED, reason="filled")
        with self.assertRaises(InvalidOrderTransition):
            o.mark_unknown(reason="second thoughts")

    def test_reconciliation_flag_does_not_bypass_the_transition_table(self):
        # via_reconciliation lets you *leave* UNKNOWN; it is not a master key.
        o = self.order()
        with self.assertRaises(InvalidOrderTransition):
            o.transition_to(
                OrderState.FILLED, reason="forced", via_reconciliation=True
            )

    def test_via_reconciliation_cannot_revive_a_terminal_order(self):
        o = self.accepted()
        o.transition_to(OrderState.CANCELED, reason="cancel")
        with self.assertRaises(InvalidOrderTransition):
            o.transition_to(
                OrderState.ACCEPTED, reason="forced", via_reconciliation=True
            )


class TestFills(OrderFixture):
    def test_partial_fill(self):
        o = self.accepted()
        o.apply_fill(Quantity("0.2", "BTC"), Price("30000", USD))
        self.assertIs(o.state, OrderState.PARTIALLY_FILLED)
        self.assertEqual(o.filled_quantity, Quantity("0.2", "BTC"))
        self.assertEqual(o.remaining_quantity, Quantity("0.3", "BTC"))

    def test_completing_fill_moves_to_filled(self):
        o = self.accepted()
        o.apply_fill(Quantity("0.2", "BTC"), Price("30000", USD))
        o.apply_fill(Quantity("0.3", "BTC"), Price("31000", USD))
        self.assertIs(o.state, OrderState.FILLED)
        self.assertTrue(o.remaining_quantity.is_zero)

    def test_weighted_average_price(self):
        o = self.accepted()
        o.apply_fill(Quantity("0.2", "BTC"), Price("30000", USD))
        o.apply_fill(Quantity("0.3", "BTC"), Price("31000", USD))
        # (0.2*30000 + 0.3*31000) / 0.5 = 30600
        self.assertEqual(o.average_fill_price.amount, Decimal("30600.000000000000"))

    def test_filled_notional_is_the_exact_sum_of_the_parts(self):
        o = self.accepted(quantity=Quantity("0.3", "BTC"))
        o.apply_fill(Quantity("0.1", "BTC"), Price("30000.01", USD))
        o.apply_fill(Quantity("0.1", "BTC"), Price("30000.02", USD))
        o.apply_fill(Quantity("0.1", "BTC"), Price("30000.03", USD))
        # 3000.001 + 3000.002 + 3000.003 = 9000.006 -> 9000.01 at cent precision
        self.assertEqual(o.filled_notional(), Money("9000.01", USD))

    def test_no_rounding_drift_over_many_partial_fills(self):
        o = self.accepted(quantity=Quantity("1.00000000", "BTC"))
        for _ in range(100):
            o.apply_fill(Quantity("0.01", "BTC"), Price("33333.333333333333", USD))
        self.assertIs(o.state, OrderState.FILLED)
        # 100 * 0.01 * 33333.333333333333 = 33333.3333333333330000
        self.assertEqual(o.filled_notional(), Money("33333.33", USD))

    def test_overfill_refused(self):
        o = self.accepted()
        o.apply_fill(Quantity("0.4", "BTC"), Price("30000", USD))
        with self.assertRaises(SafetyViolation) as ctx:
            o.apply_fill(Quantity("0.2", "BTC"), Price("30000", USD))
        self.assertIn("overfill refused", str(ctx.exception))
        self.assertEqual(o.filled_quantity, Quantity("0.4", "BTC"))

    def test_exact_fill_is_not_an_overfill(self):
        o = self.accepted()
        o.apply_fill(Quantity("0.5", "BTC"), Price("30000", USD))
        self.assertIs(o.state, OrderState.FILLED)

    def test_mismatched_asset_refused(self):
        o = self.accepted()
        with self.assertRaises(SafetyViolation) as ctx:
            o.apply_fill(Quantity("0.1", "ETH"), Price("30000", USD))
        self.assertIn("does not match order asset", str(ctx.exception))

    def test_mismatched_currency_refused(self):
        o = self.accepted()
        o.apply_fill(Quantity("0.1", "BTC"), Price("30000", USD))
        with self.assertRaises(SafetyViolation) as ctx:
            o.apply_fill(Quantity("0.1", "BTC"), Price("28000", Currency("EUR", 2)))
        self.assertIn("does not match earlier fills", str(ctx.exception))

    def test_zero_and_negative_fills_refused(self):
        o = self.accepted()
        with self.assertRaises(ValueError):
            o.apply_fill(Quantity("0", "BTC"), Price("30000", USD))
        with self.assertRaises(ValueError):
            o.apply_fill(Quantity("-0.1", "BTC"), Price("30000", USD))

    def test_fill_type_checks(self):
        o = self.accepted()
        with self.assertRaises(TypeError):
            o.apply_fill(Decimal("0.1"), Price("30000", USD))
        with self.assertRaises(TypeError):
            o.apply_fill(Quantity("0.1", "BTC"), Decimal("30000"))

    def test_no_fill_means_no_notional(self):
        o = self.accepted()
        self.assertIsNone(o.filled_notional())
        self.assertIsNone(o.average_fill_price)

    def test_fill_on_unknown_order_requires_reconciliation(self):
        o = self.accepted()
        o.mark_unknown(reason="lost the connection")
        with self.assertRaises(ReconciliationRequired):
            o.apply_fill(Quantity("0.1", "BTC"), Price("30000", USD))

    def test_reconciled_fill_on_unknown_order_is_allowed(self):
        o = self.accepted()
        o.mark_unknown(reason="lost the connection")
        o.apply_fill(
            Quantity("0.5", "BTC"),
            Price("30000", USD),
            reason="venue reported a fill",
            via_reconciliation=True,
        )
        self.assertIs(o.state, OrderState.FILLED)

    def test_failed_fill_does_not_mutate_quantities(self):
        o = self.accepted()
        o.apply_fill(Quantity("0.4", "BTC"), Price("30000", USD))
        before = o.filled_quantity
        notional_before = o.filled_notional()
        with self.assertRaises(SafetyViolation):
            o.apply_fill(Quantity("0.9", "BTC"), Price("30000", USD))
        self.assertEqual(o.filled_quantity, before)
        self.assertEqual(o.filled_notional(), notional_before)


class TestBrokerOrderId(OrderFixture):
    def test_attach_then_read(self):
        o = self.order()
        o.attach_broker_order_id("VENUE-123")
        self.assertEqual(o.broker_order_id, "VENUE-123")

    def test_reattaching_the_same_id_is_fine(self):
        o = self.order()
        o.attach_broker_order_id("VENUE-123")
        o.attach_broker_order_id("VENUE-123")

    def test_rebinding_to_a_different_id_refused(self):
        # Two venue ids for one local order means we have lost track of one.
        o = self.order()
        o.attach_broker_order_id("VENUE-123")
        with self.assertRaises(SafetyViolation) as ctx:
            o.attach_broker_order_id("VENUE-456")
        self.assertIn("refusing to rebind", str(ctx.exception))
        self.assertEqual(o.broker_order_id, "VENUE-123")


class TestOrderConcurrency(OrderFixture):
    def test_concurrent_transitions_yield_one_winner(self):
        o = self.order()
        o.transition_to(OrderState.PENDING_NEW, reason="submitted")
        successes: list[OrderState] = []
        failures: list[Exception] = []
        barrier = threading.Barrier(8)

        def attempt():
            barrier.wait()
            try:
                successes.append(o.transition_to(OrderState.FILLED, reason="race"))
            except Exception as exc:  # noqa: BLE001 - recording, not handling
                failures.append(exc)

        threads = [threading.Thread(target=attempt) for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(len(successes), 1)
        self.assertEqual(len(failures), 7)

    def test_concurrent_fills_cannot_overfill(self):
        o = self.accepted(quantity=Quantity("1", "BTC"))
        filled: list[int] = []
        refused: list[int] = []
        barrier = threading.Barrier(8)

        def attempt():
            barrier.wait()
            try:
                o.apply_fill(Quantity("0.2", "BTC"), Price("30000", USD))
                filled.append(1)
            except SafetyViolation:
                refused.append(1)

        threads = [threading.Thread(target=attempt) for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(len(filled), 5)  # 5 * 0.2 == 1
        self.assertEqual(len(refused), 3)
        self.assertEqual(o.filled_quantity, Quantity("1", "BTC"))


class TestOrderStore(OrderFixture):
    def setUp(self):
        super().setUp()
        self.store = OrderStore()

    def test_add_and_get(self):
        o = self.store.add(self.order())
        self.assertIs(self.store.get(o.order_id), o)
        self.assertEqual(len(self.store), 1)

    def test_get_missing_raises(self):
        with self.assertRaises(KeyError):
            self.store.get("ORD-nope")

    def test_duplicate_order_id_refused(self):
        o = self.order()
        self.store.add(o)
        with self.assertRaises(SafetyViolation):
            self.store.add(o)

    def test_lookup_by_idempotency_key(self):
        o = self.store.add(self.order())
        self.assertIs(self.store.find_by_idempotency_key(o.idempotency_key), o)
        self.assertIsNone(self.store.find_by_idempotency_key("no-such-key"))

    def test_order_ids_are_unique(self):
        ids = {self.order().order_id for _ in range(200)}
        self.assertEqual(len(ids), 200)

    def test_explicit_order_id_is_honoured(self):
        o = Order(intent(), clock=self.clock, order_id="ORD-fixed")
        self.assertEqual(o.order_id, "ORD-fixed")

    def test_unknown_orders_reported(self):
        clean = self.store.add(self.order(signal_id="sig-1"))
        broken = self.store.add(self.order(signal_id="sig-2"))
        self.assertFalse(self.store.has_unknown_orders())
        broken.transition_to(OrderState.PENDING_NEW, reason="submitted")
        broken.mark_unknown(reason="timeout")
        self.assertTrue(self.store.has_unknown_orders())
        self.assertEqual(self.store.unknown_orders(), [broken])
        self.assertNotIn(clean, self.store.unknown_orders())

    def test_open_orders_reported(self):
        a = self.store.add(self.order(signal_id="sig-a"))
        b = self.store.add(self.order(signal_id="sig-b"))
        a.transition_to(OrderState.PENDING_NEW, reason="submitted")
        b.transition_to(OrderState.PENDING_NEW, reason="submitted")
        b.transition_to(OrderState.REJECTED, reason="venue reject")
        self.assertEqual(self.store.open_orders(), [a])

    def test_intent_type_checked(self):
        with self.assertRaises(TypeError):
            Order("not an intent", clock=self.clock)  # type: ignore[arg-type]

    def test_as_details_is_json_safe(self):
        import json

        o = self.accepted()
        o.apply_fill(Quantity("0.5", "BTC"), Price("30000", USD))
        rendered = json.dumps(o.as_details())
        self.assertIn("30000", rendered)
        # Nothing may serialise as a float.
        self.assertNotIn("30000.0,", rendered)


if __name__ == "__main__":
    unittest.main()
