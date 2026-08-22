"""Tests for duplicate-order prevention and position reconciliation.

Covers:

* INVARIANT 12 -- duplicate order submission is prevented, or moved into an
  UNKNOWN/reconciliation state.
* INVARIANT 5 -- an UNKNOWN order state prevents new orders until reconciled.
* INVARIANT 6 -- position mismatch prevents new live orders.
"""

from __future__ import annotations

import datetime as dt
import threading
import unittest
from decimal import Decimal

from trading.core.audit import AuditLog, InMemoryAuditSink
from trading.core.authz import Principal, Role
from trading.core.clock import ManualClock
from trading.core.dedupe import IdempotencyRegistry, ReservationState
from trading.core.errors import (
    DuplicateOrderRejected,
    PositionMismatch,
    SafetyViolation,
    UnauthorizedAction,
    UnknownOrderStateBlocked,
)
from trading.core.money import Currency, Price, Quantity
from trading.core.orders import Order, OrderIntent, OrderSide, OrderState, OrderStore
from trading.core.reconciliation import (
    PositionLedger,
    ReconciliationGate,
)

USD = Currency("USD", 2)
OPERATOR = Principal("alice", Role.OPERATOR)
STRATEGY = Principal("momentum-v1", Role.STRATEGY)
GATEWAY = Principal("gateway-1", Role.EXECUTION_GATEWAY)
SYSTEM = Principal("reconciler", Role.SYSTEM)


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


class RegistryFixture(unittest.TestCase):
    def setUp(self):
        self.clock = ManualClock()
        self.sink = InMemoryAuditSink()
        self.audit = AuditLog(self.sink, clock=self.clock)
        self.registry = IdempotencyRegistry(self.audit, clock=self.clock)
        self.key = intent().idempotency_key


class TestDuplicatePrevention(RegistryFixture):
    """INVARIANT 12."""

    def test_first_reservation_succeeds(self):
        reservation = self.registry.reserve(self.key, "ORD-1")
        self.assertIs(reservation.state, ReservationState.RESERVED)
        self.assertTrue(self.registry.is_claimed(self.key))

    def test_second_reservation_of_the_same_key_is_rejected(self):
        self.registry.reserve(self.key, "ORD-1")
        with self.assertRaises(DuplicateOrderRejected) as ctx:
            self.registry.reserve(self.key, "ORD-2")
        self.assertIn("INVARIANT 12", str(ctx.exception))
        self.assertIn("ORD-1", str(ctx.exception))

    def test_duplicate_is_rejected_in_every_reservation_state(self):
        states = {
            "reserved": lambda: None,
            "submitted": lambda: self.registry.mark_submitted(self.key),
            "settled": lambda: self.registry.mark_settled(self.key),
            "unknown": lambda: self.registry.mark_unknown(self.key),
        }
        for name, advance in states.items():
            with self.subTest(state=name):
                fresh = IdempotencyRegistry(self.audit, clock=self.clock)
                self.registry = fresh
                fresh.reserve(self.key, "ORD-1")
                advance()
                with self.assertRaises(DuplicateOrderRejected):
                    fresh.reserve(self.key, "ORD-2")

    def test_different_keys_coexist(self):
        self.registry.reserve(intent(signal_id="sig-1").idempotency_key, "ORD-1")
        self.registry.reserve(intent(signal_id="sig-2").idempotency_key, "ORD-2")
        self.assertEqual(len(self.registry), 2)

    def test_rejection_is_audited(self):
        self.registry.reserve(self.key, "ORD-1")
        with self.assertRaises(DuplicateOrderRejected):
            self.registry.reserve(self.key, "ORD-2")
        records = self.sink.find("duplicate_order_rejected")
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0].details["existing_order_id"], "ORD-1")
        self.assertEqual(records[0].details["rejected_order_id"], "ORD-2")

    def test_reservation_is_audited(self):
        self.registry.reserve(self.key, "ORD-1")
        self.assertEqual(len(self.sink.find("idempotency_key_reserved")), 1)

    def test_empty_arguments_rejected(self):
        with self.assertRaises(ValueError):
            self.registry.reserve("", "ORD-1")
        with self.assertRaises(ValueError):
            self.registry.reserve(self.key, "")
        with self.assertRaises(ValueError):
            self.registry.reserve("   ", "ORD-1")

    def test_concurrent_reservation_yields_exactly_one_winner(self):
        winners: list[str] = []
        losers: list[str] = []
        barrier = threading.Barrier(16)

        def attempt(n: int):
            barrier.wait()
            try:
                self.registry.reserve(self.key, f"ORD-{n}")
                winners.append(f"ORD-{n}")
            except DuplicateOrderRejected:
                losers.append(f"ORD-{n}")

        threads = [threading.Thread(target=attempt, args=(n,)) for n in range(16)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(len(winners), 1)
        self.assertEqual(len(losers), 15)


class TestReservationLifecycle(RegistryFixture):
    def test_release_before_submission_frees_the_key(self):
        self.registry.reserve(self.key, "ORD-1")
        self.registry.release_unsent(self.key, reason="risk check rejected it")
        self.assertFalse(self.registry.is_claimed(self.key))
        # The key can now be legitimately reused, because nothing was sent.
        self.registry.reserve(self.key, "ORD-2")

    def test_release_after_submission_is_refused(self):
        # The whole point: once a request may have reached the venue, freeing
        # the key would permit a duplicate.
        self.registry.reserve(self.key, "ORD-1")
        self.registry.mark_submitted(self.key)
        with self.assertRaises(SafetyViolation) as ctx:
            self.registry.release_unsent(self.key, reason="retrying")
        self.assertIn("INVARIANT 12", str(ctx.exception))
        self.assertTrue(self.registry.is_claimed(self.key))

    def test_release_when_unknown_is_refused(self):
        self.registry.reserve(self.key, "ORD-1")
        self.registry.mark_submitted(self.key)
        self.registry.mark_unknown(self.key, note="timeout")
        with self.assertRaises(SafetyViolation):
            self.registry.release_unsent(self.key, reason="give up")

    def test_release_when_settled_is_refused(self):
        self.registry.reserve(self.key, "ORD-1")
        self.registry.mark_settled(self.key)
        with self.assertRaises(SafetyViolation):
            self.registry.release_unsent(self.key, reason="reuse")

    def test_refused_release_is_audited(self):
        self.registry.reserve(self.key, "ORD-1")
        self.registry.mark_submitted(self.key)
        with self.assertRaises(SafetyViolation):
            self.registry.release_unsent(self.key, reason="retrying")
        self.assertEqual(len(self.sink.find("idempotency_release_refused")), 1)

    def test_release_of_unknown_key_raises(self):
        with self.assertRaises(KeyError):
            self.registry.release_unsent("nope", reason="x")

    def test_settled_is_terminal(self):
        self.registry.reserve(self.key, "ORD-1")
        self.registry.mark_settled(self.key)
        with self.assertRaises(SafetyViolation):
            self.registry.mark_unknown(self.key)
        with self.assertRaises(SafetyViolation):
            self.registry.mark_submitted(self.key)

    def test_unknown_cannot_be_settled_by_the_ordinary_path(self):
        self.registry.reserve(self.key, "ORD-1")
        self.registry.mark_unknown(self.key, note="timeout")
        with self.assertRaises(SafetyViolation) as ctx:
            self.registry.mark_settled(self.key)
        self.assertIn("not a valid reservation transition", str(ctx.exception))

    def test_unknown_is_resolved_only_through_reconciliation(self):
        self.registry.reserve(self.key, "ORD-1")
        self.registry.mark_unknown(self.key, note="timeout")
        self.assertTrue(self.registry.has_unknown())
        resolved = self.registry.resolve_unknown(
            self.key, resolution="venue confirms it never arrived"
        )
        self.assertIs(resolved.state, ReservationState.SETTLED)
        self.assertFalse(self.registry.has_unknown())
        self.assertEqual(len(self.sink.find("idempotency_key_reconciled")), 1)

    def test_resolving_a_non_unknown_reservation_is_refused(self):
        self.registry.reserve(self.key, "ORD-1")
        with self.assertRaises(SafetyViolation):
            self.registry.resolve_unknown(self.key, resolution="nothing wrong")

    def test_resolution_requires_a_reason(self):
        self.registry.reserve(self.key, "ORD-1")
        self.registry.mark_unknown(self.key)
        with self.assertRaises(ValueError):
            self.registry.resolve_unknown(self.key, resolution="")

    def test_resolved_key_stays_claimed(self):
        # Reconciliation establishes the truth; it does not license a retry
        # under the same key.
        self.registry.reserve(self.key, "ORD-1")
        self.registry.mark_unknown(self.key)
        self.registry.resolve_unknown(self.key, resolution="never arrived")
        with self.assertRaises(DuplicateOrderRejected):
            self.registry.reserve(self.key, "ORD-2")

    def test_repeating_the_current_state_is_a_noop(self):
        self.registry.reserve(self.key, "ORD-1")
        self.registry.mark_submitted(self.key)
        again = self.registry.mark_submitted(self.key)
        self.assertIs(again.state, ReservationState.SUBMITTED)

    def test_reserved_can_settle_directly(self):
        # A local rejection that we choose to record rather than free.
        self.registry.reserve(self.key, "ORD-1")
        self.registry.mark_settled(self.key, note="rejected locally")
        self.assertIs(self.registry.get(self.key).state, ReservationState.SETTLED)

    def test_lifecycle_events_are_audited(self):
        self.registry.reserve(self.key, "ORD-1")
        self.registry.mark_submitted(self.key)
        self.registry.mark_unknown(self.key, note="timeout")
        self.assertEqual(len(self.sink.find("idempotency_key_submitted")), 1)
        self.assertEqual(len(self.sink.find("idempotency_key_unknown")), 1)

    def test_in_flight_reporting(self):
        a = intent(signal_id="a").idempotency_key
        b = intent(signal_id="b").idempotency_key
        c = intent(signal_id="c").idempotency_key
        self.registry.reserve(a, "ORD-a")
        self.registry.reserve(b, "ORD-b")
        self.registry.reserve(c, "ORD-c")
        self.registry.mark_submitted(b)
        self.registry.mark_settled(c)
        in_flight = {r.order_id for r in self.registry.in_flight()}
        self.assertEqual(in_flight, {"ORD-a", "ORD-b"})

    def test_unknown_reporting(self):
        a = intent(signal_id="a").idempotency_key
        b = intent(signal_id="b").idempotency_key
        self.registry.reserve(a, "ORD-a")
        self.registry.reserve(b, "ORD-b")
        self.registry.mark_unknown(b, note="timeout")
        self.assertEqual([r.order_id for r in self.registry.unknown_reservations()], ["ORD-b"])

    def test_get_returns_none_for_unknown_key(self):
        self.assertIsNone(self.registry.get("nope"))

    def test_advancing_an_unclaimed_key_raises(self):
        with self.assertRaises(KeyError):
            self.registry.mark_submitted("nope")


class TestPositionLedger(unittest.TestCase):
    def setUp(self):
        self.ledger = PositionLedger()

    def test_buy_increases_and_sell_decreases(self):
        self.ledger.apply_fill("BTCUSD", OrderSide.BUY, Quantity("1", "BTC"))
        self.assertEqual(self.ledger.position("BTCUSD"), Quantity("1", "BTC"))
        self.ledger.apply_fill("BTCUSD", OrderSide.SELL, Quantity("0.4", "BTC"))
        self.assertEqual(self.ledger.position("BTCUSD"), Quantity("0.6", "BTC"))

    def test_position_can_go_short(self):
        self.ledger.apply_fill("BTCUSD", OrderSide.SELL, Quantity("1", "BTC"))
        self.assertEqual(self.ledger.position("BTCUSD"), Quantity("-1", "BTC"))

    def test_unknown_symbol_reads_as_zero(self):
        self.assertTrue(self.ledger.position("ETHUSD", asset="ETH").is_zero)

    def test_arithmetic_is_exact(self):
        for _ in range(10):
            self.ledger.apply_fill("BTCUSD", OrderSide.BUY, Quantity("0.1", "BTC"))
        self.assertEqual(self.ledger.position("BTCUSD"), Quantity("1", "BTC"))
        self.assertEqual(self.ledger.position("BTCUSD").amount, Decimal("1.0"))

    def test_type_and_value_checks(self):
        with self.assertRaises(TypeError):
            self.ledger.apply_fill("BTCUSD", "buy", Quantity("1", "BTC"))
        with self.assertRaises(TypeError):
            self.ledger.apply_fill("BTCUSD", OrderSide.BUY, Decimal("1"))
        with self.assertRaises(ValueError):
            self.ledger.apply_fill("BTCUSD", OrderSide.BUY, Quantity("0", "BTC"))
        with self.assertRaises(ValueError):
            self.ledger.apply_fill("BTCUSD", OrderSide.BUY, Quantity("-1", "BTC"))
        with self.assertRaises(TypeError):
            self.ledger.set_position("BTCUSD", Decimal("1"))

    def test_snapshot_is_a_copy(self):
        self.ledger.apply_fill("BTCUSD", OrderSide.BUY, Quantity("1", "BTC"))
        snap = self.ledger.snapshot()
        snap["BTCUSD"] = Quantity("999", "BTC")
        self.assertEqual(self.ledger.position("BTCUSD"), Quantity("1", "BTC"))

    def test_symbols_and_len(self):
        self.ledger.apply_fill("ETHUSD", OrderSide.BUY, Quantity("1", "ETH"))
        self.ledger.apply_fill("BTCUSD", OrderSide.BUY, Quantity("1", "BTC"))
        self.assertEqual(self.ledger.symbols(), ["BTCUSD", "ETHUSD"])
        self.assertEqual(len(self.ledger), 2)

    def test_concurrent_fills_are_exact(self):
        barrier = threading.Barrier(8)

        def add():
            barrier.wait()
            for _ in range(25):
                self.ledger.apply_fill("BTCUSD", OrderSide.BUY, Quantity("0.01", "BTC"))

        threads = [threading.Thread(target=add) for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(self.ledger.position("BTCUSD"), Quantity("2", "BTC"))


class GateFixture(unittest.TestCase):
    def setUp(self):
        self.clock = ManualClock()
        self.sink = InMemoryAuditSink()
        self.audit = AuditLog(self.sink, clock=self.clock)
        self.ledger = PositionLedger()
        self.store = OrderStore()
        self.gate = ReconciliationGate(
            self.ledger,
            self.store,
            self.audit,
            clock=self.clock,
            max_staleness_seconds=300.0,
        )

    def add_unknown_order(self, signal_id: str = "sig-x") -> Order:
        order = self.store.add(Order(intent(signal_id=signal_id), clock=self.clock))
        order.transition_to(OrderState.PENDING_NEW, reason="submitted")
        order.mark_unknown(reason="submission timed out")
        return order


class TestUnknownOrdersBlockNewOrders(GateFixture):
    """INVARIANT 5, at the gate."""

    def test_clean_state_passes(self):
        self.gate.require_clean(live=False)

    def test_unknown_order_blocks(self):
        self.add_unknown_order()
        with self.assertRaises(UnknownOrderStateBlocked) as ctx:
            self.gate.require_clean(live=False)
        self.assertIn("INVARIANT 5", str(ctx.exception))

    def test_unknown_order_blocks_live_too(self):
        self.gate.reconcile({})
        self.add_unknown_order()
        with self.assertRaises(UnknownOrderStateBlocked):
            self.gate.require_clean(live=True)

    def test_unknown_check_runs_before_the_mismatch_check(self):
        # Both are wrong; the report must name the more fundamental problem.
        self.ledger.apply_fill("BTCUSD", OrderSide.BUY, Quantity("1", "BTC"))
        self.gate.reconcile({"BTCUSD": Quantity("0.5", "BTC")})
        self.add_unknown_order()
        with self.assertRaises(UnknownOrderStateBlocked):
            self.gate.require_clean(live=False)

    def test_block_is_audited(self):
        self.add_unknown_order()
        with self.assertRaises(UnknownOrderStateBlocked):
            self.gate.require_clean(live=False)
        records = self.sink.find("blocked_by_unknown_orders")
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0].details["unknown_order_count"], 1)

    def test_resolving_the_unknown_order_unblocks(self):
        order = self.add_unknown_order()
        with self.assertRaises(UnknownOrderStateBlocked):
            self.gate.require_clean(live=False)
        order.transition_to(
            OrderState.REJECTED,
            reason="venue confirms it never existed",
            via_reconciliation=True,
        )
        self.gate.require_clean(live=False)

    def test_many_unknown_orders_are_summarised(self):
        for n in range(8):
            self.add_unknown_order(signal_id=f"sig-{n}")
        with self.assertRaises(UnknownOrderStateBlocked) as ctx:
            self.gate.require_clean(live=False)
        self.assertIn("8 order(s)", str(ctx.exception))
        self.assertIn("+3 more", str(ctx.exception))


class TestPositionMismatch(GateFixture):
    """INVARIANT 6."""

    def test_matching_positions_are_clean(self):
        self.ledger.apply_fill("BTCUSD", OrderSide.BUY, Quantity("1", "BTC"))
        report = self.gate.reconcile({"BTCUSD": Quantity("1", "BTC")})
        self.assertTrue(report.is_clean)
        self.assertFalse(self.gate.has_mismatch)

    def test_scale_differences_are_not_a_mismatch(self):
        self.ledger.apply_fill("BTCUSD", OrderSide.BUY, Quantity("1", "BTC"))
        report = self.gate.reconcile({"BTCUSD": Quantity("1.00000000", "BTC")})
        self.assertTrue(report.is_clean)

    def test_mismatch_is_detected(self):
        self.ledger.apply_fill("BTCUSD", OrderSide.BUY, Quantity("1", "BTC"))
        report = self.gate.reconcile({"BTCUSD": Quantity("0.9", "BTC")})
        self.assertFalse(report.is_clean)
        self.assertEqual(len(report.discrepancies), 1)
        self.assertEqual(report.discrepancies[0].difference, Quantity("0.1", "BTC"))

    def test_mismatch_blocks_new_orders(self):
        self.ledger.apply_fill("BTCUSD", OrderSide.BUY, Quantity("1", "BTC"))
        self.gate.reconcile({"BTCUSD": Quantity("0.9", "BTC")})
        with self.assertRaises(PositionMismatch) as ctx:
            self.gate.require_clean(live=True)
        self.assertIn("INVARIANT 6", str(ctx.exception))

    def test_position_we_do_not_know_about_is_a_mismatch(self):
        # The venue holds something we have no record of: the worst case.
        report = self.gate.reconcile({"BTCUSD": Quantity("0.25", "BTC")})
        self.assertFalse(report.is_clean)
        self.assertTrue(report.discrepancies[0].local.is_zero)

    def test_position_the_venue_does_not_have_is_a_mismatch(self):
        self.ledger.apply_fill("BTCUSD", OrderSide.BUY, Quantity("1", "BTC"))
        report = self.gate.reconcile({})
        self.assertFalse(report.is_clean)
        self.assertTrue(report.discrepancies[0].remote.is_zero)

    def test_mismatch_latches_even_if_the_next_snapshot_agrees(self):
        self.ledger.apply_fill("BTCUSD", OrderSide.BUY, Quantity("1", "BTC"))
        self.gate.reconcile({"BTCUSD": Quantity("0.9", "BTC")})
        self.assertTrue(self.gate.has_mismatch)
        # A later agreeing snapshot must not silently unblock trading.
        self.gate.reconcile({"BTCUSD": Quantity("1", "BTC")})
        self.assertTrue(self.gate.has_mismatch)
        with self.assertRaises(PositionMismatch):
            self.gate.require_clean(live=False)

    def test_mismatch_is_audited(self):
        self.ledger.apply_fill("BTCUSD", OrderSide.BUY, Quantity("1", "BTC"))
        self.gate.reconcile({"BTCUSD": Quantity("0.9", "BTC")})
        records = self.sink.find("position_mismatch_detected")
        self.assertEqual(len(records), 1)
        self.assertFalse(records[0].details["clean"])

    def test_clean_reconciliation_is_audited(self):
        self.gate.reconcile({})
        self.assertEqual(len(self.sink.find("reconciliation_clean")), 1)

    def test_tolerance_absorbs_small_differences(self):
        gate = ReconciliationGate(
            self.ledger,
            self.store,
            self.audit,
            clock=self.clock,
            tolerances={"BTCUSD": Decimal("0.00000001")},
        )
        self.ledger.apply_fill("BTCUSD", OrderSide.BUY, Quantity("1", "BTC"))
        self.assertTrue(gate.reconcile({"BTCUSD": Quantity("0.99999999", "BTC")}).is_clean)
        self.assertFalse(gate.reconcile({"BTCUSD": Quantity("0.99999998", "BTC")}).is_clean)

    def test_tolerance_rejects_float_and_negative(self):
        with self.assertRaises(TypeError):
            ReconciliationGate(
                self.ledger, self.store, self.audit,
                clock=self.clock, tolerances={"BTCUSD": 0.001},
            )
        with self.assertRaises(ValueError):
            ReconciliationGate(
                self.ledger, self.store, self.audit,
                clock=self.clock, tolerances={"BTCUSD": Decimal("-1")},
            )

    def test_broker_positions_must_be_quantities(self):
        with self.assertRaises(TypeError):
            self.gate.reconcile({"BTCUSD": Decimal("1")})

    def test_asset_disagreement_is_a_hard_error(self):
        self.ledger.apply_fill("BTCUSD", OrderSide.BUY, Quantity("1", "BTC"))
        with self.assertRaises(SafetyViolation) as ctx:
            self.gate.reconcile({"BTCUSD": Quantity("1", "XBT")})
        self.assertIn("cannot compare", str(ctx.exception))

    def test_staleness_must_be_positive(self):
        with self.assertRaises(ValueError):
            ReconciliationGate(
                self.ledger, self.store, self.audit,
                clock=self.clock, max_staleness_seconds=0,
            )

    def test_discrepancy_renders_readably(self):
        self.ledger.apply_fill("BTCUSD", OrderSide.BUY, Quantity("1", "BTC"))
        report = self.gate.reconcile({"BTCUSD": Quantity("0.9", "BTC")})
        text = str(report.discrepancies[0])
        self.assertIn("local=1", text)
        self.assertIn("remote=0.9", text)


class TestNeverCheckingIsAlsoAMismatch(GateFixture):
    """The obvious way to defeat INVARIANT 6 is never to reconcile."""

    def test_live_orders_refused_before_any_reconciliation(self):
        with self.assertRaises(PositionMismatch) as ctx:
            self.gate.require_clean(live=True)
        self.assertIn("has ever run", str(ctx.exception))
        self.assertEqual(len(self.sink.find("blocked_by_missing_reconciliation")), 1)

    def test_paper_orders_are_not_blocked_by_missing_reconciliation(self):
        self.gate.require_clean(live=False)

    def test_live_orders_allowed_after_a_clean_reconciliation(self):
        self.gate.reconcile({})
        self.gate.require_clean(live=True)

    def test_live_orders_refused_once_reconciliation_goes_stale(self):
        self.gate.reconcile({})
        self.clock.advance(300)
        self.gate.require_clean(live=True)  # exactly at the limit is still fine
        self.clock.advance(1)
        with self.assertRaises(PositionMismatch) as ctx:
            self.gate.require_clean(live=True)
        self.assertIn("older than", str(ctx.exception))
        self.assertEqual(len(self.sink.find("blocked_by_stale_reconciliation")), 1)

    def test_re_reconciling_refreshes_freshness(self):
        self.gate.reconcile({})
        self.clock.advance(1000)
        with self.assertRaises(PositionMismatch):
            self.gate.require_clean(live=True)
        self.gate.reconcile({})
        self.gate.require_clean(live=True)

    def test_a_mismatching_snapshot_does_not_count_as_reconciled(self):
        self.gate.reconcile({})  # clean baseline
        self.clock.advance(1000)
        self.ledger.apply_fill("BTCUSD", OrderSide.BUY, Quantity("1", "BTC"))
        self.gate.reconcile({"BTCUSD": Quantity("0.5", "BTC")})  # mismatch
        # Freshness must not have been refreshed by a failed check.
        self.assertGreater(self.gate.seconds_since_clean(), 300)

    def test_staleness_uses_the_monotonic_clock(self):
        self.gate.reconcile({})
        # A wall-clock jump forwards must not age out the reconciliation, and a
        # jump backwards must not make a stale one look fresh.
        self.clock.set_wall_clock(self.clock.now() + dt.timedelta(hours=2))
        self.gate.require_clean(live=True)
        self.clock.advance(301)
        self.clock.set_wall_clock(self.clock.now() - dt.timedelta(hours=2))
        with self.assertRaises(PositionMismatch):
            self.gate.require_clean(live=True)

    def test_seconds_since_clean_is_none_before_any_check(self):
        self.assertIsNone(self.gate.seconds_since_clean())


class TestClearingAMismatch(GateFixture):
    def setUp(self):
        super().setUp()
        self.ledger.apply_fill("BTCUSD", OrderSide.BUY, Quantity("1", "BTC"))
        self.gate.reconcile({"BTCUSD": Quantity("0.9", "BTC")})
        self.assertTrue(self.gate.has_mismatch)

    def test_operator_can_clear_when_positions_actually_agree(self):
        self.gate.clear_mismatch(
            OPERATOR,
            reason="late fill arrived",
            broker_positions={"BTCUSD": Quantity("1", "BTC")},
        )
        self.assertFalse(self.gate.has_mismatch)
        self.gate.require_clean(live=True)

    def test_clearing_is_refused_while_the_snapshot_still_disagrees(self):
        with self.assertRaises(PositionMismatch) as ctx:
            self.gate.clear_mismatch(
                OPERATOR,
                reason="trust me",
                broker_positions={"BTCUSD": Quantity("0.9", "BTC")},
            )
        self.assertIn("still disagrees", str(ctx.exception))
        self.assertTrue(self.gate.has_mismatch)
        self.assertEqual(len(self.sink.find("mismatch_clear_refused")), 1)

    def test_strategy_and_gateway_cannot_clear(self):
        for principal in (STRATEGY, GATEWAY):
            with self.subTest(principal=principal):
                with self.assertRaises(UnauthorizedAction):
                    self.gate.clear_mismatch(
                        principal,
                        reason="attempt",
                        broker_positions={"BTCUSD": Quantity("1", "BTC")},
                    )
        self.assertTrue(self.gate.has_mismatch)

    def test_clearing_requires_a_reason(self):
        with self.assertRaises(ValueError):
            self.gate.clear_mismatch(
                OPERATOR, reason="", broker_positions={"BTCUSD": Quantity("1", "BTC")}
            )

    def test_clearing_is_audited(self):
        self.gate.clear_mismatch(
            OPERATOR,
            reason="late fill arrived",
            broker_positions={"BTCUSD": Quantity("1", "BTC")},
        )
        records = self.sink.find("position_mismatch_cleared")
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0].details["reason"], "late fill arrived")

    def test_adopting_venue_positions_then_clearing(self):
        self.gate.adopt_broker_positions(
            OPERATOR,
            reason="venue is authoritative after an outage",
            broker_positions={"BTCUSD": Quantity("0.9", "BTC")},
        )
        self.assertEqual(self.ledger.position("BTCUSD"), Quantity("0.9", "BTC"))
        # Adopting alone does not unblock; the latch still stands.
        self.assertTrue(self.gate.has_mismatch)
        self.gate.clear_mismatch(
            OPERATOR,
            reason="adopted venue view",
            broker_positions={"BTCUSD": Quantity("0.9", "BTC")},
        )
        self.assertFalse(self.gate.has_mismatch)

    def test_adopting_zeroes_symbols_the_venue_does_not_report(self):
        self.gate.adopt_broker_positions(
            OPERATOR, reason="venue outage recovery", broker_positions={}
        )
        self.assertTrue(self.ledger.position("BTCUSD").is_zero)

    def test_adopting_requires_authority_and_a_reason(self):
        with self.assertRaises(UnauthorizedAction):
            self.gate.adopt_broker_positions(
                STRATEGY, reason="attempt", broker_positions={}
            )
        with self.assertRaises(ValueError):
            self.gate.adopt_broker_positions(OPERATOR, reason="", broker_positions={})

    def test_adopting_is_audited_with_before_and_after(self):
        self.gate.adopt_broker_positions(
            OPERATOR,
            reason="venue is authoritative",
            broker_positions={"BTCUSD": Quantity("0.9", "BTC")},
        )
        records = self.sink.find("broker_positions_adopted")
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0].details["before"], {"BTCUSD": "1"})
        self.assertEqual(records[0].details["after"], {"BTCUSD": "0.9"})

    def test_automated_reconciler_may_also_clear(self):
        # Role.SYSTEM holds RECONCILE so an unattended reconciler can clear a
        # mismatch -- but only by passing the same re-verification.
        self.gate.clear_mismatch(
            SYSTEM,
            reason="automated reconciliation",
            broker_positions={"BTCUSD": Quantity("1", "BTC")},
        )
        self.assertFalse(self.gate.has_mismatch)


if __name__ == "__main__":
    unittest.main()
