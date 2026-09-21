"""Tests for ``ExecutionGateway.sync_order`` -- the route a later fill takes home.

``place_order`` reports what happened at the moment of placement. An order that
rests at the venue and fills afterwards has no other way into the portfolio, so
before this existed a resting order stayed ACCEPTED forever and its fill was
invisible to the position ledger, the cost basis, and the daily-loss limit.

The venue answers with a *cumulative* snapshot and only the delta is booked,
which is what makes a sync safe to repeat: the second one costs nothing. That
property is asserted against position, cash **and** realized P&L, because a
double-booking that moved only one of the three would be the worst kind.

The refusals are the other half. A venue answer that contradicts what we have
already booked leaves the order untouched and lands in the audit trail; an order
that should never be polled -- UNKNOWN, or already finished -- is refused before
the venue is asked at all, which is what ``fetch_calls == 0`` is checking.
"""

from __future__ import annotations

import threading
import unittest

from trading.core.authz import Principal, Role
from trading.core.config import RiskConfig
from trading.core.errors import SafetyViolation, UnauthorizedAction
from trading.core.money import USD, Currency, Money, Price, Quantity
from trading.core.orders import Order, OrderState
from trading.ports.broker import AckOutcome, BrokerAck

from .harness import build_rig

RESTING = BrokerAck(AckOutcome.ACCEPTED, broker_order_id="b-1")
NO_RECORD = BrokerAck(AckOutcome.REJECTED, message="venue has no record of this order")


def filled(quantity: str, price: str = "50000") -> BrokerAck:
    """A cumulative fill snapshot, as ``fetch_order_state`` reports one."""
    return BrokerAck(
        outcome=AckOutcome.FILLED,
        broker_order_id="b-1",
        filled_quantity=Quantity(quantity, "BTC"),
        fill_price=Price(price, USD),
    )


class SyncVenue:
    """A venue with one scripted answer to ``fetch_order_state``.

    The simulator cannot express this: its answer is derived from what it was
    told during ``place_order``, so it can never report a fill that happened
    later -- which is the entire situation under test. ``fetch_calls`` is how
    the refusal tests prove the venue was never even asked.
    """

    def __init__(self, fetch_ack: BrokerAck) -> None:
        self.fetch_ack = fetch_ack
        self.fetch_calls = 0
        #: Every call in order, so audit-vs-effect ordering can be checked.
        self.calls: list[str] = []

    def fetch_order_state(self, order: Order) -> BrokerAck:
        self.fetch_calls += 1
        self.calls.append("fetch_order_state")
        return self.fetch_ack


class SyncFixture(unittest.TestCase):
    """A rig whose order is resting at the venue, behind a scriptable stub."""

    def resting_order(self, rig, quantity: str = "0.001") -> Order:
        rig.broker.script(RESTING)
        result = rig.submit(quantity=quantity)
        self.assertEqual(result.order.state, OrderState.ACCEPTED)
        return result.order

    def partially_filled_order(self, rig, quantity: str = "0.002") -> Order:
        """An order with 0.001 BTC at 50 000 already on the books."""
        rig.broker.script(filled("0.001"))
        result = rig.submit(quantity=quantity)
        self.assertEqual(result.order.state, OrderState.PARTIALLY_FILLED)
        return result.order

    def venue(self, rig, fetch_ack: BrokerAck) -> SyncVenue:
        stub = SyncVenue(fetch_ack)
        rig.gateway._broker = stub
        return stub

    def records(self, rig, action: str) -> list:
        return [r for r in rig.sink.records if r.action == action]

    def refusals(self, rig) -> list:
        return self.records(rig, "gateway.venue_state_refused")


class TestSyncApplies(SyncFixture):
    """What the venue says, the order becomes."""

    def test_a_venue_still_reporting_the_order_resting_changes_nothing(self) -> None:
        rig = build_rig()
        order = self.resting_order(rig)
        booked = rig.positions.position("BTCUSD", asset="BTC")
        self.venue(rig, RESTING)

        ack = rig.gateway.sync_order(order, operator=rig.operator_id)

        self.assertEqual(ack.outcome, AckOutcome.ACCEPTED)
        self.assertEqual(order.state, OrderState.ACCEPTED)
        self.assertTrue(order.is_open)
        self.assertEqual(rig.positions.position("BTCUSD", asset="BTC"), booked)
        self.assertEqual(self.refusals(rig), [])

    def test_a_fill_discovered_later_reaches_the_portfolio(self) -> None:
        """The defect this entry point exists to fix.

        Before this, a resting order that filled at the venue had no route into
        the position ledger at all, so the daily-loss limit was judging a
        position the system did not know it held.
        """
        rig = build_rig()
        order = self.resting_order(rig)
        booked = rig.positions.position("BTCUSD", asset="BTC")
        cash = rig.portfolio.cash
        self.venue(rig, filled("0.001"))

        rig.gateway.sync_order(order, operator=rig.operator_id)

        self.assertEqual(order.state, OrderState.FILLED)
        self.assertEqual(order.filled_quantity, Quantity("0.001", "BTC"))
        self.assertEqual(
            rig.positions.position("BTCUSD", asset="BTC"),
            booked + Quantity("0.001", "BTC"),
        )
        # 0.001 BTC at 50 000 USD is 50 USD of cash, spent.
        self.assertEqual(rig.portfolio.cash, cash - Money("50", USD))

    def test_a_partial_fill_discovered_later_is_booked_and_leaves_it_open(self) -> None:
        rig = build_rig()
        order = self.resting_order(rig, quantity="0.002")
        booked = rig.positions.position("BTCUSD", asset="BTC")
        self.venue(rig, filled("0.001"))

        rig.gateway.sync_order(order, operator=rig.operator_id)

        self.assertEqual(order.state, OrderState.PARTIALLY_FILLED)
        self.assertEqual(order.remaining_quantity, Quantity("0.001", "BTC"))
        self.assertTrue(order.is_open)
        self.assertEqual(
            rig.positions.position("BTCUSD", asset="BTC"),
            booked + Quantity("0.001", "BTC"),
        )

    def test_a_completing_fill_releases_the_open_order_budget(self) -> None:
        """A finished order must stop consuming a slot it no longer needs."""
        rig = build_rig(
            default_outcome=AckOutcome.ACCEPTED, risk=RiskConfig(max_open_orders=1)
        )
        first = rig.submit().order
        self.assertTrue(first.is_open)
        self.assertEqual(rig.submit().outcome, "refused")

        rig.gateway._broker = SyncVenue(
            BrokerAck(
                outcome=AckOutcome.FILLED,
                filled_quantity=Quantity("0.001", "BTC"),
                fill_price=Price("50000", USD),
            )
        )
        rig.gateway.sync_order(first, operator=rig.operator_id)

        self.assertEqual(first.state, OrderState.FILLED)
        self.assertEqual(len(rig.orders.open_orders()), 0)

    def test_a_venue_with_no_record_of_a_resting_order_rejects_it(self) -> None:
        """No ``after_cancel`` suppression here -- nobody withdrew this order.

        That distinction belongs to ``cancel``, which knows it just asked the
        venue to forget the order. A routine poll knows no such thing, so "no
        record" is read literally.
        """
        rig = build_rig()
        order = self.resting_order(rig)
        self.venue(rig, NO_RECORD)

        rig.gateway.sync_order(order, operator=rig.operator_id)

        self.assertEqual(order.state, OrderState.REJECTED)
        self.assertFalse(order.is_open)

    def test_the_system_role_may_sync_because_a_poller_runs_unattended(self) -> None:
        """``RECONCILE``, not ``CANCEL_ORDER``: this asks rather than tells."""
        rig = build_rig()
        order = self.resting_order(rig)
        self.venue(rig, filled("0.001"))
        poller = Principal("poller-1", Role.SYSTEM)

        rig.gateway.sync_order(order, operator=poller)

        self.assertEqual(order.state, OrderState.FILLED)


class TestSyncBooksExactlyOnce(SyncFixture):
    """Cumulative snapshot in, delta out."""

    def test_re_syncing_an_unchanged_fill_costs_nothing(self) -> None:
        """The property that makes a poller safe to run as often as it likes.

        A still-open order is the case that matters: a finished one is refused
        before the venue is asked, so this is the only way the same cumulative
        snapshot can actually reach the delta arithmetic twice.
        """
        rig = build_rig()
        order = self.resting_order(rig, quantity="0.002")
        self.venue(rig, filled("0.001"))
        rig.gateway.sync_order(order, operator=rig.operator_id)
        self.assertEqual(order.state, OrderState.PARTIALLY_FILLED)

        booked = rig.positions.position("BTCUSD", asset="BTC")
        cash = rig.portfolio.cash
        realized = rig.risk.pnl.realized

        rig.gateway.sync_order(order, operator=rig.operator_id)

        self.assertEqual(order.state, OrderState.PARTIALLY_FILLED)
        self.assertEqual(order.filled_quantity, Quantity("0.001", "BTC"))
        self.assertEqual(rig.positions.position("BTCUSD", asset="BTC"), booked)
        self.assertEqual(rig.portfolio.cash, cash)
        self.assertEqual(rig.risk.pnl.realized, realized)

    def test_a_growing_cumulative_fill_books_only_the_difference(self) -> None:
        rig = build_rig()
        order = self.partially_filled_order(rig, quantity="0.002")
        booked = rig.positions.position("BTCUSD", asset="BTC")
        cash = rig.portfolio.cash

        # Cumulative 0.002 BTC for 110 USD: the 0.001 at 50 000 we already have
        # plus 0.001 at 60 000. The venue reports the *average*, 55 000.
        self.venue(rig, filled("0.002", price="55000"))
        rig.gateway.sync_order(order, operator=rig.operator_id)

        self.assertEqual(order.state, OrderState.FILLED)
        self.assertEqual(order.average_fill_price, Price("55000", USD))
        self.assertEqual(
            rig.positions.position("BTCUSD", asset="BTC"),
            booked + Quantity("0.001", "BTC"),
        )
        # 60 USD, the real cost of the new lot -- not 55, which is the average
        # and would misstate the cost basis the daily-loss limit reads.
        self.assertEqual(rig.portfolio.cash, cash - Money("60", USD))

    def test_concurrent_syncs_of_one_fill_book_it_once(self) -> None:
        """Four threads, one fill. The gateway lock is what makes this true."""
        rig = build_rig()
        order = self.resting_order(rig)
        booked = rig.positions.position("BTCUSD", asset="BTC")
        cash = rig.portfolio.cash
        self.venue(rig, filled("0.001"))

        start = threading.Barrier(4)
        failures: list[BaseException] = []

        def run() -> None:
            start.wait(timeout=5)
            try:
                rig.gateway.sync_order(order, operator=rig.operator_id)
            except SafetyViolation:
                # Whichever threads lose the race find a FILLED order and are
                # refused. That is the correct answer, not a failure.
                pass
            except BaseException as exc:  # pragma: no cover - a real defect
                failures.append(exc)

        threads = [threading.Thread(target=run) for _ in range(4)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=10)
            self.assertFalse(thread.is_alive(), "a sync thread never finished")

        self.assertEqual(failures, [])
        self.assertEqual(order.filled_quantity, Quantity("0.001", "BTC"))
        self.assertEqual(
            rig.positions.position("BTCUSD", asset="BTC"),
            booked + Quantity("0.001", "BTC"),
        )
        self.assertEqual(rig.portfolio.cash, cash - Money("50", USD))
        rig.audit.verify()


class TestSyncRefusesBeforeAsking(SyncFixture):
    """Orders that must never be polled. ``fetch_calls`` stays at zero."""

    def test_an_unknown_order_is_refused_and_points_at_resolve_unknown(self) -> None:
        rig = build_rig()
        order = self.resting_order(rig)
        order.mark_unknown(reason="simulated")
        stub = self.venue(rig, filled("0.001"))

        with self.assertRaises(SafetyViolation) as ctx:
            rig.gateway.sync_order(order, operator=rig.operator_id)

        self.assertIn("UNKNOWN", str(ctx.exception))
        self.assertIn("resolve", str(ctx.exception))
        self.assertEqual(stub.fetch_calls, 0)
        self.assertTrue(order.is_unknown)

    def test_a_terminal_order_is_refused(self) -> None:
        rig = build_rig()  # the default rig fills immediately
        order = rig.submit().order
        self.assertTrue(order.state.is_terminal)
        stub = self.venue(rig, filled("0.001"))

        with self.assertRaises(SafetyViolation) as ctx:
            rig.gateway.sync_order(order, operator=rig.operator_id)

        self.assertIn("already filled", str(ctx.exception))
        self.assertEqual(stub.fetch_calls, 0)
        self.assertEqual(order.state, OrderState.FILLED)

    def test_a_canceled_order_is_refused(self) -> None:
        rig = build_rig()
        order = self.resting_order(rig)
        rig.gateway.cancel(order, operator=rig.operator_id)
        self.assertEqual(order.state, OrderState.CANCELED)
        stub = self.venue(rig, filled("0.001"))

        with self.assertRaises(SafetyViolation) as ctx:
            rig.gateway.sync_order(order, operator=rig.operator_id)

        self.assertIn("already canceled", str(ctx.exception))
        self.assertEqual(stub.fetch_calls, 0)

    def test_a_strategy_may_not_sync(self) -> None:
        rig = build_rig()
        order = self.resting_order(rig)
        stub = self.venue(rig, filled("0.001"))

        with self.assertRaises(UnauthorizedAction):
            rig.gateway.sync_order(order, operator=rig.strategy_id)

        self.assertEqual(stub.fetch_calls, 0)
        self.assertEqual(order.state, OrderState.ACCEPTED)
        # Authorization refuses before anything is recorded about this sync.
        self.assertEqual(self.records(rig, "gateway.sync_requested"), [])

    def test_the_gateway_identity_may_not_sync_either(self) -> None:
        """Execution and reconciliation are different authorities (INVARIANT 3)."""
        rig = build_rig()
        order = self.resting_order(rig)
        stub = self.venue(rig, filled("0.001"))

        with self.assertRaises(UnauthorizedAction):
            rig.gateway.sync_order(order, operator=rig.gateway_id)

        self.assertEqual(stub.fetch_calls, 0)


class TestSyncRefusesContradictions(SyncFixture):
    """The venue was asked, and its answer cannot be believed.

    Every case leaves the order exactly as it was and puts the reason in the
    audit trail (INVARIANT 13).
    """

    def assert_refused(self, rig, fragment: str) -> None:
        refusals = self.refusals(rig)
        self.assertEqual(len(refusals), 1, "expected exactly one refusal record")
        self.assertEqual(refusals[0].outcome, "refused")
        self.assertIn(fragment, refusals[0].details["reason"])

    def test_an_uncertain_answer_is_refused_and_does_not_mark_unknown(self) -> None:
        """A read that failed is not an order we cannot account for.

        Marking the order UNKNOWN here would latch the whole system through
        ``require_clean``, which would make an unanswered poll indistinguishable
        from a lost order. The order keeps the state we already knew it had.
        """
        rig = build_rig()
        order = self.resting_order(rig)
        self.venue(rig, BrokerAck(AckOutcome.UNCERTAIN))

        with self.assertRaises(SafetyViolation) as ctx:
            rig.gateway.sync_order(order, operator=rig.operator_id)

        self.assertIn("UNCERTAIN", str(ctx.exception))
        self.assert_refused(rig, "UNCERTAIN")
        self.assertEqual(order.state, OrderState.ACCEPTED)
        self.assertFalse(order.is_unknown)
        self.assertEqual(rig.orders.unknown_orders(), [])

    def test_a_broker_order_id_mismatch_is_refused(self) -> None:
        rig = build_rig()
        order = self.resting_order(rig)
        self.venue(rig, BrokerAck(AckOutcome.ACCEPTED, broker_order_id="someone-else"))

        with self.assertRaises(SafetyViolation) as ctx:
            rig.gateway.sync_order(order, operator=rig.operator_id)

        self.assertIn("broker_order_id mismatch", str(ctx.exception))
        self.assert_refused(rig, "broker_order_id mismatch")
        self.assertEqual(order.broker_order_id, "b-1")
        self.assertEqual(order.state, OrderState.ACCEPTED)

    def test_an_overfill_is_refused_and_nothing_is_booked(self) -> None:
        rig = build_rig()
        order = self.resting_order(rig)  # 0.001 BTC ordered
        booked = rig.positions.position("BTCUSD", asset="BTC")
        self.venue(rig, filled("0.005"))

        with self.assertRaises(SafetyViolation) as ctx:
            rig.gateway.sync_order(order, operator=rig.operator_id)

        self.assertIn("overfill", str(ctx.exception))
        self.assert_refused(rig, "overfill")
        self.assertEqual(rig.positions.position("BTCUSD", asset="BTC"), booked)
        self.assertEqual(order.state, OrderState.ACCEPTED)

    def test_a_regressed_cumulative_quantity_is_refused(self) -> None:
        """A venue reporting less than it already gave us has contradicted itself."""
        rig = build_rig()
        order = self.partially_filled_order(rig)
        booked = rig.positions.position("BTCUSD", asset="BTC")
        self.venue(rig, filled("0.0005"))

        with self.assertRaises(SafetyViolation) as ctx:
            rig.gateway.sync_order(order, operator=rig.operator_id)

        self.assertIn("regressed fill", str(ctx.exception))
        self.assert_refused(rig, "regressed fill")
        self.assertEqual(order.filled_quantity, Quantity("0.001", "BTC"))
        self.assertEqual(rig.positions.position("BTCUSD", asset="BTC"), booked)

    def test_a_currency_change_is_refused(self) -> None:
        rig = build_rig()
        order = self.partially_filled_order(rig)
        inr = Currency("INR", 2)
        self.venue(
            rig,
            BrokerAck(
                outcome=AckOutcome.FILLED,
                broker_order_id="b-1",
                filled_quantity=Quantity("0.002", "BTC"),
                fill_price=Price("50000", inr),
            ),
        )

        with self.assertRaises(SafetyViolation) as ctx:
            rig.gateway.sync_order(order, operator=rig.operator_id)

        self.assertIn("currency mismatch", str(ctx.exception))
        self.assert_refused(rig, "currency mismatch")

    def test_a_venue_disowning_a_partially_filled_order_is_refused(self) -> None:
        """"No record" against booked fills would unwind them, so it refuses.

        PARTIALLY_FILLED -> REJECTED is absent from the transition table: a
        venue cannot retroactively claim it never took an order we have already
        traded against.
        """
        rig = build_rig()
        order = self.partially_filled_order(rig)
        booked = rig.positions.position("BTCUSD", asset="BTC")
        self.venue(rig, NO_RECORD)

        with self.assertRaises(SafetyViolation) as ctx:
            rig.gateway.sync_order(order, operator=rig.operator_id)

        self.assertIn("not a valid transition", str(ctx.exception))
        self.assert_refused(rig, "not a valid transition")
        self.assertEqual(order.state, OrderState.PARTIALLY_FILLED)
        self.assertEqual(rig.positions.position("BTCUSD", asset="BTC"), booked)


class TestSyncAuditOrdering(SyncFixture):
    """INVARIANT 13: the decision is in the trail before it takes effect."""

    def test_the_request_is_recorded_before_the_venue_is_asked(self) -> None:
        rig = build_rig()
        order = self.resting_order(rig)
        before = len(rig.sink.records)
        stub = self.venue(rig, filled("0.001"))

        rig.gateway.sync_order(order, operator=rig.operator_id)

        actions = [r.action for r in rig.sink.records[before:]]
        self.assertEqual(actions[0], "gateway.sync_requested")
        self.assertEqual(actions[-1], "gateway.sync_completed")
        self.assertEqual(stub.calls, ["fetch_order_state"])

    def test_the_completion_record_names_both_states(self) -> None:
        rig = build_rig()
        order = self.resting_order(rig)
        self.venue(rig, filled("0.001"))

        rig.gateway.sync_order(order, operator=rig.operator_id)

        completed = self.records(rig, "gateway.sync_completed")[0]
        self.assertEqual(completed.outcome, "allowed")
        self.assertEqual(completed.details["previous_state"], OrderState.ACCEPTED.value)
        self.assertEqual(completed.details["state"], OrderState.FILLED.value)

    def test_a_refused_sync_is_audited_and_asks_nothing(self) -> None:
        rig = build_rig()
        order = self.resting_order(rig)
        order.mark_unknown(reason="simulated")

        with self.assertRaises(SafetyViolation):
            rig.gateway.sync_order(order, operator=rig.operator_id)

        refusals = self.records(rig, "gateway.sync_refused")
        self.assertEqual(len(refusals), 1)
        self.assertEqual(refusals[0].outcome, "refused")
        self.assertEqual(refusals[0].category, "reconciliation")
        self.assertIn("UNKNOWN", refusals[0].details["reason"])
        # It never got as far as asking, so it never recorded that it had.
        self.assertEqual(self.records(rig, "gateway.sync_requested"), [])
        self.assertEqual(self.records(rig, "gateway.sync_completed"), [])

    def test_a_contradicted_sync_records_the_request_but_not_a_completion(self) -> None:
        rig = build_rig()
        order = self.resting_order(rig)
        self.venue(rig, filled("0.005"))  # overfill

        with self.assertRaises(SafetyViolation):
            rig.gateway.sync_order(order, operator=rig.operator_id)

        self.assertEqual(len(self.records(rig, "gateway.sync_requested")), 1)
        self.assertEqual(self.records(rig, "gateway.sync_completed"), [])
        self.assertEqual(len(self.refusals(rig)), 1)

    def test_the_audit_chain_still_verifies_across_a_sync(self) -> None:
        rig = build_rig()
        order = self.resting_order(rig)
        self.venue(rig, filled("0.001"))
        rig.gateway.sync_order(order, operator=rig.operator_id)
        rig.audit.verify()


class TestSyncLocking(SyncFixture):
    def test_sync_holds_the_gateway_lock_while_it_reads_and_books(self) -> None:
        """It can move the portfolio, so it must serialise against submit."""
        rig = build_rig()
        order = self.resting_order(rig)

        class Watcher(SyncVenue):
            def fetch_order_state(self, inner: Order) -> BrokerAck:
                # The gateway lock is not reentrant, so a held lock cannot be
                # acquired again here. That is the assertion.
                held = not rig.gateway._lock.acquire(blocking=False)
                if not held:
                    rig.gateway._lock.release()
                self.held_during_fetch = held
                return super().fetch_order_state(inner)

        watcher = Watcher(filled("0.001"))
        rig.gateway._broker = watcher
        rig.gateway.sync_order(order, operator=rig.operator_id)

        self.assertTrue(watcher.held_during_fetch)

    def test_the_lock_is_released_when_a_sync_refuses_before_asking(self) -> None:
        rig = build_rig()
        order = self.resting_order(rig)
        order.mark_unknown(reason="simulated")

        with self.assertRaises(SafetyViolation):
            rig.gateway.sync_order(order, operator=rig.operator_id)

        self.assert_lock_free(rig)

    def test_the_lock_is_released_when_authorization_refuses(self) -> None:
        rig = build_rig()
        order = self.resting_order(rig)

        with self.assertRaises(UnauthorizedAction):
            rig.gateway.sync_order(order, operator=rig.strategy_id)

        self.assert_lock_free(rig)

    def test_the_lock_is_released_when_the_venue_answer_is_refused(self) -> None:
        rig = build_rig()
        order = self.resting_order(rig)
        self.venue(rig, filled("0.005"))  # overfill

        with self.assertRaises(SafetyViolation):
            rig.gateway.sync_order(order, operator=rig.operator_id)

        self.assert_lock_free(rig)

    def test_a_sync_does_not_deadlock_a_later_submit(self) -> None:
        """The whole point of releasing it: the system keeps working."""
        rig = build_rig(default_outcome=AckOutcome.ACCEPTED)
        order = self.resting_order(rig)
        real_broker = rig.gateway._broker
        self.venue(rig, filled("0.001"))
        rig.gateway.sync_order(order, operator=rig.operator_id)

        rig.gateway._broker = real_broker
        self.assertEqual(rig.submit().outcome, "executed")

    def assert_lock_free(self, rig) -> None:
        acquired = rig.gateway._lock.acquire(blocking=False)
        self.assertTrue(acquired, "the gateway lock was left held")
        rig.gateway._lock.release()


class TestResolveUnknownLocking(SyncFixture):
    """``resolve_unknown`` books fills too, and used to do it unserialised."""

    def unknown_order(self, rig) -> Order:
        order = self.resting_order(rig)
        order.mark_unknown(reason="simulated")
        return order

    def test_resolve_unknown_holds_the_gateway_lock(self) -> None:
        rig = build_rig()
        order = self.unknown_order(rig)

        class Watcher(SyncVenue):
            def fetch_order_state(self, inner: Order) -> BrokerAck:
                held = not rig.gateway._lock.acquire(blocking=False)
                if not held:
                    rig.gateway._lock.release()
                self.held_during_fetch = held
                return super().fetch_order_state(inner)

        watcher = Watcher(filled("0.001"))
        rig.gateway._broker = watcher
        rig.gateway.resolve_unknown(order, operator=rig.operator_id)

        self.assertTrue(watcher.held_during_fetch)
        self.assertEqual(order.state, OrderState.FILLED)

    def test_the_lock_is_released_when_resolve_unknown_refuses(self) -> None:
        rig = build_rig()
        order = self.resting_order(rig)  # not UNKNOWN, so it refuses

        with self.assertRaises(SafetyViolation):
            rig.gateway.resolve_unknown(order, operator=rig.operator_id)

        acquired = rig.gateway._lock.acquire(blocking=False)
        self.assertTrue(acquired, "resolve_unknown left the gateway lock held")
        rig.gateway._lock.release()

    def test_the_lock_is_released_on_an_uncertain_answer(self) -> None:
        """The early-return path, which leaves the state untouched."""
        rig = build_rig()
        order = self.unknown_order(rig)
        self.venue(rig, BrokerAck(AckOutcome.UNCERTAIN))

        ack = rig.gateway.resolve_unknown(order, operator=rig.operator_id)

        self.assertEqual(ack.outcome, AckOutcome.UNCERTAIN)
        self.assertTrue(order.is_unknown)
        acquired = rig.gateway._lock.acquire(blocking=False)
        self.assertTrue(acquired, "resolve_unknown left the gateway lock held")
        rig.gateway._lock.release()

    def test_the_lock_is_released_when_unauthorized(self) -> None:
        rig = build_rig()
        order = self.unknown_order(rig)

        with self.assertRaises(UnauthorizedAction):
            rig.gateway.resolve_unknown(order, operator=rig.strategy_id)

        acquired = rig.gateway._lock.acquire(blocking=False)
        self.assertTrue(acquired, "resolve_unknown left the gateway lock held")
        rig.gateway._lock.release()

    def test_resolve_unknown_does_not_deadlock_a_later_submit(self) -> None:
        rig = build_rig(default_outcome=AckOutcome.ACCEPTED)
        order = self.unknown_order(rig)
        real_broker = rig.gateway._broker
        self.venue(rig, filled("0.001"))
        rig.gateway.resolve_unknown(order, operator=rig.operator_id)

        rig.gateway._broker = real_broker
        self.assertEqual(rig.submit().outcome, "executed")


if __name__ == "__main__":
    unittest.main()
