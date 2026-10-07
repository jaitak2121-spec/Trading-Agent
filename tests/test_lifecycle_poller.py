"""Tests for the periodic order poller -- the schedule behind ``sync_order``.

``sync_order`` could always carry a later fill into the portfolio, and the paper
venue could always produce one; what neither could do was *notice*. This is the
component that closes that gap, and almost everything worth asserting about it
is a negative: that it did not acquire a lifecycle opinion of its own along the
way.

So the tests are organised around what it must not do. It must not select an
order the store does not call open -- which is how a terminal order is never
fetched and an UNKNOWN one is never touched, without this module naming a state.
It must not decide who may reconcile; a bad identity has to come back from the
gateway as ``UnauthorizedAction``, and it has to abort the sweep rather than be
swallowed, because ``authorize`` refuses without auditing and a swallowed one
would leave a poller sweeping nothing forever in total silence. And it must not
turn a failure into a safe state: a venue read that raises leaves the order
exactly as open as it was, not UNKNOWN and not finished, and lands in a bucket
kept separate from the gateway's ordinary refusals.

``TestUnknownOrdersStayBlocked`` deliberately feeds the poller an order the real
store would never have offered it. Every other test proves the selection is
right; that one proves the gateway would refuse even if it were wrong.

The threading tests use a bounded wall-clock wait, which is the one place in this
suite that does. A ``ManualClock`` cannot wake a sleeping thread, so liveness has
no deterministic substitute -- but ``stop()`` joins, so every assertion *after* a
stop is exact rather than timed.
"""

from __future__ import annotations

import threading
import time
import unittest

from trading.adapters.lifecycle import (
    DEFAULT_INTERVAL_SECONDS,
    LifecyclePoller,
    PollNote,
    PollReport,
)
from trading.adapters.memory import InMemoryQuoteFeed
from trading.adapters.paper import PaperBroker
from trading.core.authz import ExecutionToken, Principal, Role
from trading.core.clock import ManualClock
from trading.core.errors import (
    ConfigurationError,
    SafetyViolation,
    UnauthorizedAction,
)
from trading.core.money import USD, Price, Quantity
from trading.core.orders import Order, OrderIntent, OrderSide, OrderState, OrderType
from trading.ports.broker import (
    AckOutcome,
    BrokerAck,
    BrokerPort,
    BrokerPositionSnapshot,
)

from .harness import ASSET, DEFAULT_QUANTITY, SYMBOL, build_rig

#: A SYSTEM identity, which is the role that exists so an unattended poller can
#: reconcile. The rig does not build one, because nothing before this needed it.
POLLER = Principal("poller-1", Role.SYSTEM)

BID = Price("49990", USD)
ASK = Price("50010", USD)


def until(predicate, timeout: float = 5.0) -> bool:
    """Wait for ``predicate`` to hold, bounded. For thread liveness only."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.001)
    return predicate()


class PollVenue(BrokerPort):
    """Every placement rests; what a later poll finds is scripted per order.

    The simulator cannot express this -- its answer to ``fetch_order_state`` is
    derived from what it was told at placement, so it can never report a fill
    that happened afterwards, which is the whole situation a poller exists for.
    ``fetch_calls`` is how the selection tests prove the venue was never asked.
    """

    def __init__(self) -> None:
        self._seq = 0
        #: Every order id asked about, in order.
        self.fetch_calls: list[str] = []
        #: order_id -> the ack a poll should find.
        self.answers: dict[str, BrokerAck] = {}
        #: order_ids whose read blows up instead of answering.
        self.raise_for: set[str] = set()
        #: order_ids the venue no longer holds, because a cancel landed.
        self.canceled: set[str] = set()
        #: Set on every fetch, so a thread test can wait on a sweep happening.
        self.fetched = threading.Event()

    def place_order(self, order: Order, *, token: ExecutionToken) -> BrokerAck:
        self._seq += 1
        return BrokerAck(AckOutcome.ACCEPTED, broker_order_id=f"v-{self._seq}")

    def cancel_order(self, order: Order) -> BrokerAck:
        self.canceled.add(order.order_id)
        return BrokerAck(
            AckOutcome.ACCEPTED, broker_order_id=order.broker_order_id
        )

    def fetch_order_state(self, order: Order) -> BrokerAck:
        self.fetch_calls.append(order.order_id)
        self.fetched.set()
        if order.order_id in self.raise_for:
            raise ConnectionResetError("venue read failed")
        if order.order_id in self.canceled:
            return BrokerAck(
                AckOutcome.REJECTED, message="venue no longer holds this order"
            )
        return self.answers.get(
            order.order_id,
            BrokerAck(AckOutcome.ACCEPTED, broker_order_id=order.broker_order_id),
        )

    def fetch_positions(self) -> BrokerPositionSnapshot:
        return BrokerPositionSnapshot()


class OneOrderStore:
    """A store that offers exactly what it is told to, however wrong.

    Only for proving that the gateway refuses an order the real
    ``open_orders()`` would never have produced.
    """

    def __init__(self, *orders: Order) -> None:
        self._orders = list(orders)

    def open_orders(self) -> list[Order]:
        return list(self._orders)


class PollerCase(unittest.TestCase):
    """A rig whose venue rests every order, with a SYSTEM poller over it."""

    identity = POLLER

    def setUp(self) -> None:
        self.venue = PollVenue()
        self.rig = build_rig(broker=self.venue)
        self.poller = self.build_poller()

    def build_poller(self, **kwargs) -> LifecyclePoller:
        kwargs.setdefault("gateway", self.rig.gateway)
        kwargs.setdefault("orders", self.rig.orders)
        kwargs.setdefault("identity", self.identity)
        return LifecyclePoller(**kwargs)

    def rest(self, **kwargs) -> Order:
        """One order, resting at the venue."""
        result = self.rig.submit(**kwargs)
        self.assertTrue(result.is_executed, result.reason)
        self.assertIs(result.order.state, OrderState.ACCEPTED)
        return result.order

    def fill_for(self, order: Order, quantity: str = "0.001") -> BrokerAck:
        """The cumulative snapshot a poll would find if the order had filled.

        Reuses the venue's own ``broker_order_id``: a different one is a
        contradiction ``sync_order`` refuses, which is its own test below.
        """
        return BrokerAck(
            outcome=AckOutcome.FILLED,
            broker_order_id=order.broker_order_id,
            filled_quantity=Quantity(quantity, ASSET),
            fill_price=Price("50000", USD),
        )


# -- the sweep reaches the gateway, and only the gateway ---------------------


class TestTheSweepReachesTheGateway(PollerCase):
    """The poller's entire job: offer open orders to ``sync_order``."""

    def test_a_resting_order_is_offered_to_the_gateway(self) -> None:
        order = self.rest()
        report = self.poller.poll_once()
        self.assertEqual(report.synced, (order.order_id,))
        self.assertEqual(self.venue.fetch_calls, [order.order_id])

    def test_a_fill_discovered_on_a_poll_reaches_the_portfolio(self) -> None:
        order = self.rest()
        self.venue.answers[order.order_id] = self.fill_for(order)

        self.poller.poll_once()

        self.assertIs(order.state, OrderState.FILLED)
        self.assertEqual(
            self.rig.positions.position(SYMBOL, asset=ASSET),
            Quantity("0.001", ASSET),
        )

    def test_a_partial_fill_discovered_on_a_poll_reaches_the_portfolio(self) -> None:
        order = self.rest()
        self.venue.answers[order.order_id] = self.fill_for(order, "0.0004")

        self.poller.poll_once()

        self.assertIs(order.state, OrderState.PARTIALLY_FILLED)
        self.assertEqual(
            self.rig.positions.position(SYMBOL, asset=ASSET),
            Quantity("0.0004", ASSET),
        )

    def test_every_open_order_is_offered(self) -> None:
        orders = [self.rest() for _ in range(3)]
        report = self.poller.poll_once()
        self.assertEqual(set(report.synced), {o.order_id for o in orders})
        self.assertEqual(report.considered, 3)

    def test_a_sweep_with_nothing_open_asks_the_venue_nothing(self) -> None:
        report = self.poller.poll_once()
        self.assertEqual(report.considered, 0)
        self.assertEqual(self.venue.fetch_calls, [])

    def test_an_order_that_rests_stays_resting(self) -> None:
        order = self.rest()
        self.poller.poll_once()
        self.assertIs(order.state, OrderState.ACCEPTED)
        self.assertEqual(self.rig.positions.snapshot(), {})

    def test_a_second_sweep_books_nothing_more(self) -> None:
        """The property that makes a poller safe to run as often as it likes."""
        order = self.rest()
        self.venue.answers[order.order_id] = self.fill_for(order)
        self.poller.poll_once()
        booked = self.rig.positions.position(SYMBOL, asset=ASSET)
        cash = self.rig.portfolio.cash
        realized = self.rig.portfolio.position(SYMBOL, asset=ASSET).realized_pnl

        second = self.poller.poll_once()

        # FILLED is terminal, so the order is not even a candidate any more.
        self.assertEqual(second.considered, 0)
        self.assertEqual(self.rig.positions.position(SYMBOL, asset=ASSET), booked)
        self.assertEqual(self.rig.portfolio.cash, cash)
        self.assertEqual(
            self.rig.portfolio.position(SYMBOL, asset=ASSET).realized_pnl, realized
        )

    def test_repeating_a_sweep_over_a_still_open_order_books_nothing_twice(self) -> None:
        order = self.rest()
        self.venue.answers[order.order_id] = self.fill_for(order, "0.0004")
        self.poller.poll_once()
        booked = self.rig.positions.position(SYMBOL, asset=ASSET)

        self.poller.poll_once()

        self.assertIs(order.state, OrderState.PARTIALLY_FILLED)
        self.assertEqual(self.rig.positions.position(SYMBOL, asset=ASSET), booked)


class TestTheGatewayDoesTheAuditing(PollerCase):
    """INVARIANT 13 stays where it was: the poller writes no records itself."""

    def test_a_sweep_audits_through_the_gateway(self) -> None:
        self.rest()
        self.poller.poll_once()
        actions = self.rig.actions()
        self.assertIn("gateway.sync_requested", actions)
        self.assertIn("gateway.sync_completed", actions)

    def test_the_poller_writes_no_audit_records_of_its_own(self) -> None:
        self.rest()
        before = len(self.rig.sink.records)
        self.poller.poll_once()
        added = [r.action for r in self.rig.sink.records[before:]]
        self.assertTrue(added, "the sweep should have produced gateway records")
        self.assertTrue(
            all(action.startswith("gateway.") for action in added),
            f"the poller audited on its own: {added}",
        )

    def test_a_refused_order_is_audited_by_the_gateway_not_the_poller(self) -> None:
        order = self.rest()
        self.venue.answers[order.order_id] = BrokerAck(
            AckOutcome.UNCERTAIN, broker_order_id=order.broker_order_id
        )
        before = len(self.rig.sink.records)

        self.poller.poll_once()

        added = [r.action for r in self.rig.sink.records[before:]]
        self.assertIn("gateway.venue_state_refused", added)
        self.assertTrue(all(action.startswith("gateway.") for action in added))


# -- selection: terminal and UNKNOWN orders are never candidates -------------


class TestTerminalOrdersAreNotFetched(PollerCase):
    """``open_orders()`` already excludes them, so the venue is never asked."""

    def test_a_filled_order_is_not_fetched_again(self) -> None:
        order = self.rest()
        self.venue.answers[order.order_id] = self.fill_for(order)
        self.poller.poll_once()
        self.assertIs(order.state, OrderState.FILLED)
        calls = len(self.venue.fetch_calls)

        report = self.poller.poll_once()

        self.assertEqual(len(self.venue.fetch_calls), calls)
        self.assertEqual(report.considered, 0)

    def test_a_canceled_order_is_not_fetched_again(self) -> None:
        order = self.rest()
        self.rig.gateway.cancel(order, operator=self.rig.operator_id)
        self.assertIs(order.state, OrderState.CANCELED)
        calls = len(self.venue.fetch_calls)

        report = self.poller.poll_once()

        self.assertEqual(len(self.venue.fetch_calls), calls)
        self.assertEqual(report.considered, 0)

    def test_a_terminal_order_does_not_stop_the_sweep(self) -> None:
        done = self.rest()
        self.venue.answers[done.order_id] = self.fill_for(done)
        self.poller.poll_once()
        still_open = self.rest()

        report = self.poller.poll_once()

        self.assertEqual(report.synced, (still_open.order_id,))


class TestUnknownOrdersStayBlocked(PollerCase):
    """INVARIANT 5. Twice over: never selected, and refused even if it were."""

    def unknown(self) -> Order:
        order = self.rest()
        order.mark_unknown(reason="test: venue answer lost")
        self.assertTrue(order.is_unknown)
        return order

    def test_an_unknown_order_is_never_offered_to_the_gateway(self) -> None:
        self.unknown()
        calls = len(self.venue.fetch_calls)

        report = self.poller.poll_once()

        self.assertEqual(report.considered, 0)
        self.assertEqual(len(self.venue.fetch_calls), calls)

    def test_the_sweep_does_not_resolve_an_unknown_order(self) -> None:
        order = self.unknown()
        self.poller.poll_once()
        self.assertTrue(order.is_unknown)
        self.assertTrue(self.rig.orders.has_unknown_orders())

    def test_an_unknown_order_does_not_stop_the_sweep(self) -> None:
        # Both orders first: an UNKNOWN order blocks every new submission
        # (INVARIANT 5), so the second one could not be placed afterwards.
        open_order = self.rest()
        self.unknown()
        report = self.poller.poll_once()
        self.assertEqual(report.synced, (open_order.order_id,))

    def test_a_mis_selected_unknown_order_is_refused_by_the_gateway(self) -> None:
        """Every other test proves selection is right. This one assumes it is not.

        If the candidate list ever grew an UNKNOWN order, the refusal has to
        come from ``sync_order``, not from a check in the poller -- otherwise
        there would be two places that decide what UNKNOWN means.
        """
        order = self.unknown()
        poller = self.build_poller(orders=OneOrderStore(order))
        calls = len(self.venue.fetch_calls)

        report = poller.poll_once()

        self.assertEqual(report.synced, ())
        self.assertEqual(len(report.refused), 1)
        self.assertIn("UNKNOWN", report.refused[0].reason)
        self.assertEqual(report.refused[0].order_id, order.order_id)
        # Refused before the venue was asked at all.
        self.assertEqual(len(self.venue.fetch_calls), calls)
        self.assertTrue(order.is_unknown)

    def test_a_mis_selected_terminal_order_is_refused_by_the_gateway(self) -> None:
        order = self.rest()
        self.venue.answers[order.order_id] = self.fill_for(order)
        self.poller.poll_once()
        poller = self.build_poller(orders=OneOrderStore(order))
        calls = len(self.venue.fetch_calls)

        report = poller.poll_once()

        self.assertEqual(len(report.refused), 1)
        self.assertEqual(len(self.venue.fetch_calls), calls)


# -- authorization -----------------------------------------------------------


class TestAuthorizationIsPreserved(PollerCase):
    """``Action.RECONCILE``, checked in the gateway and nowhere else."""

    def unprivileged(self) -> LifecyclePoller:
        return self.build_poller(identity=Principal("nosy-1", Role.STRATEGY))

    def test_the_system_role_may_sweep_because_a_poller_runs_unattended(self) -> None:
        order = self.rest()
        report = self.poller.poll_once()
        self.assertEqual(report.synced, (order.order_id,))

    def test_an_operator_may_sweep_by_hand(self) -> None:
        order = self.rest()
        poller = self.build_poller(identity=self.rig.operator_id)
        self.assertEqual(poller.poll_once().synced, (order.order_id,))

    def test_an_identity_without_reconcile_cannot_sweep(self) -> None:
        self.rest()
        with self.assertRaises(UnauthorizedAction):
            self.unprivileged().poll_once()

    def test_the_refusal_happens_before_the_venue_is_asked(self) -> None:
        self.rest()
        with self.assertRaises(UnauthorizedAction):
            self.unprivileged().poll_once()
        self.assertEqual(self.venue.fetch_calls, [])

    def test_an_unauthorized_sweep_aborts_rather_than_continuing(self) -> None:
        """Not swallowed as a per-order refusal: every order would fail alike."""
        for _ in range(3):
            self.rest()
        poller = self.unprivileged()

        with self.assertRaises(UnauthorizedAction):
            poller.poll_once()

        self.assertEqual(self.venue.fetch_calls, [])
        self.assertIsNone(poller.last_report)

    def test_nothing_is_booked_by_an_unauthorized_sweep(self) -> None:
        order = self.rest()
        self.venue.answers[order.order_id] = self.fill_for(order)
        with self.assertRaises(UnauthorizedAction):
            self.unprivileged().poll_once()
        self.assertIs(order.state, OrderState.ACCEPTED)
        self.assertEqual(self.rig.positions.snapshot(), {})

    def test_the_poller_does_not_check_authorization_itself(self) -> None:
        """One place decides who may reconcile, and it is not this one.

        Constructing with an identity that cannot reconcile is allowed; the
        gateway is what refuses. A second copy of the permission rule here
        could drift from the one in ``authz``.
        """
        poller = self.unprivileged()
        self.assertEqual(poller.identity.role, Role.STRATEGY)


# -- failure must not become a safe state ------------------------------------


class TestErrorsDoNotBecomeSafeStates(PollerCase):
    """A read that failed is not an order we can account for, either way."""

    def failing_order(self) -> Order:
        order = self.rest()
        self.venue.raise_for.add(order.order_id)
        return order

    def test_a_venue_read_that_raises_leaves_the_order_open(self) -> None:
        order = self.failing_order()
        self.poller.poll_once()
        self.assertIs(order.state, OrderState.ACCEPTED)
        self.assertFalse(order.state.is_terminal)

    def test_a_venue_read_that_raises_does_not_mark_the_order_unknown(self) -> None:
        order = self.failing_order()
        self.poller.poll_once()
        self.assertFalse(order.is_unknown)
        self.assertFalse(self.rig.orders.has_unknown_orders())

    def test_a_failed_read_is_reported_rather_than_swallowed(self) -> None:
        order = self.failing_order()
        report = self.poller.poll_once()
        self.assertEqual(report.synced, ())
        self.assertEqual(len(report.failed), 1)
        self.assertEqual(report.failed[0].order_id, order.order_id)
        self.assertIn("ConnectionResetError", report.failed[0].reason)

    def test_a_failure_is_kept_apart_from_a_refusal(self) -> None:
        """Collapsing the two would be the silent conversion this must not do."""
        failed = self.failing_order()
        refused = self.rest()
        self.venue.answers[refused.order_id] = BrokerAck(
            AckOutcome.UNCERTAIN, broker_order_id=refused.broker_order_id
        )

        report = self.poller.poll_once()

        self.assertEqual([n.order_id for n in report.failed], [failed.order_id])
        self.assertEqual([n.order_id for n in report.refused], [refused.order_id])

    def test_one_failing_order_does_not_stop_the_sweep(self) -> None:
        self.failing_order()
        healthy = self.rest()
        report = self.poller.poll_once()
        self.assertEqual(report.synced, (healthy.order_id,))

    def test_a_failed_read_is_retried_on_the_next_sweep(self) -> None:
        order = self.failing_order()
        self.poller.poll_once()
        self.venue.raise_for.clear()
        self.venue.answers[order.order_id] = self.fill_for(order)

        report = self.poller.poll_once()

        self.assertEqual(report.synced, (order.order_id,))
        self.assertIs(order.state, OrderState.FILLED)

    def test_an_uncertain_answer_is_refused_and_does_not_latch_unknown(self) -> None:
        order = self.rest()
        self.venue.answers[order.order_id] = BrokerAck(
            AckOutcome.UNCERTAIN, broker_order_id=order.broker_order_id
        )

        report = self.poller.poll_once()

        self.assertEqual(len(report.refused), 1)
        self.assertIs(order.state, OrderState.ACCEPTED)
        self.assertFalse(order.is_unknown)

    def test_a_contradictory_answer_leaves_the_order_untouched(self) -> None:
        order = self.rest()
        self.venue.answers[order.order_id] = BrokerAck(
            outcome=AckOutcome.FILLED,
            broker_order_id="some-other-order",
            filled_quantity=DEFAULT_QUANTITY,
            fill_price=Price("50000", USD),
        )

        report = self.poller.poll_once()

        self.assertEqual(len(report.refused), 1)
        self.assertIs(order.state, OrderState.ACCEPTED)
        self.assertEqual(self.rig.positions.snapshot(), {})

    def test_an_overfilling_answer_leaves_the_order_untouched(self) -> None:
        order = self.rest()
        self.venue.answers[order.order_id] = self.fill_for(order, "0.5")

        report = self.poller.poll_once()

        self.assertEqual(len(report.refused), 1)
        self.assertIs(order.state, OrderState.ACCEPTED)
        self.assertEqual(self.rig.positions.snapshot(), {})


# -- the worker --------------------------------------------------------------


class TestStartAndStop(PollerCase):
    """Lifecycle only. Behaviour is ``poll_once``, tested above without a thread."""

    def setUp(self) -> None:
        super().setUp()
        self.poller = self.build_poller(interval_seconds=0.001)
        self.addCleanup(self.poller.stop)

    def test_a_fresh_poller_is_not_running(self) -> None:
        self.assertFalse(self.poller.is_running)
        self.assertEqual(self.poller.passes, 0)
        self.assertIsNone(self.poller.last_report)
        self.assertIsNone(self.poller.last_error)

    def test_start_runs_sweeps(self) -> None:
        self.rest()
        self.poller.start()
        self.assertTrue(self.poller.is_running)
        self.assertTrue(self.venue.fetched.wait(5.0), "no sweep happened")
        self.assertTrue(until(lambda: self.poller.last_report is not None))

    def test_the_worker_syncs_a_fill_nobody_asked_about(self) -> None:
        """The whole point: a fill reaches the portfolio unattended."""
        order = self.rest()
        self.venue.answers[order.order_id] = self.fill_for(order)

        self.poller.start()
        self.assertTrue(until(lambda: order.state is OrderState.FILLED))
        self.poller.stop()

        self.assertEqual(
            self.rig.positions.position(SYMBOL, asset=ASSET),
            Quantity("0.001", ASSET),
        )

    def test_stop_ends_the_worker(self) -> None:
        self.poller.start()
        self.poller.stop()
        self.assertFalse(self.poller.is_running)

    def test_stop_joins_so_no_sweep_happens_afterwards(self) -> None:
        """``stop()`` returning means the worker is done, not merely asked."""
        self.rest()
        self.poller.start()
        self.assertTrue(self.venue.fetched.wait(5.0))
        self.poller.stop()

        passes = self.poller.passes
        calls = len(self.venue.fetch_calls)
        self.assertEqual(self.poller.passes, passes)
        self.assertEqual(len(self.venue.fetch_calls), calls)

    def test_stop_without_start_is_a_no_op(self) -> None:
        self.poller.stop()
        self.assertFalse(self.poller.is_running)
        self.assertEqual(self.poller.passes, 0)

    def test_stop_is_idempotent(self) -> None:
        self.poller.start()
        self.poller.stop()
        self.poller.stop()
        self.assertFalse(self.poller.is_running)

    def test_a_poller_can_be_restarted(self) -> None:
        self.rest()
        self.poller.start()
        self.assertTrue(self.venue.fetched.wait(5.0))
        self.poller.stop()
        first = self.poller.passes

        self.venue.fetched.clear()
        self.poller.start()
        self.assertTrue(self.venue.fetched.wait(5.0))
        self.poller.stop()

        self.assertGreater(self.poller.passes, first)

    def test_passes_counts_completed_sweeps(self) -> None:
        self.poller.poll_once()
        self.poller.poll_once()
        self.assertEqual(self.poller.passes, 2)


class TestNoDuplicateWorkers(PollerCase):
    """One owner, or a loud refusal."""

    def setUp(self) -> None:
        super().setUp()
        self.poller = self.build_poller(interval_seconds=0.001)
        self.addCleanup(self.poller.stop)

    def test_starting_twice_raises(self) -> None:
        self.poller.start()
        with self.assertRaises(SafetyViolation) as caught:
            self.poller.start()
        self.assertIn("already running", str(caught.exception))

    def test_a_refused_second_start_leaves_the_first_worker_alone(self) -> None:
        self.rest()
        self.poller.start()
        with self.assertRaises(SafetyViolation):
            self.poller.start()
        self.assertTrue(self.poller.is_running)
        self.assertTrue(self.venue.fetched.wait(5.0))

    def test_one_stop_is_enough_after_a_refused_second_start(self) -> None:
        self.poller.start()
        with self.assertRaises(SafetyViolation):
            self.poller.start()
        self.poller.stop()
        self.assertFalse(self.poller.is_running)

    def test_start_after_stop_is_allowed(self) -> None:
        self.poller.start()
        self.poller.stop()
        self.poller.start()
        self.assertTrue(self.poller.is_running)

    def test_concurrent_starts_produce_exactly_one_worker(self) -> None:
        begin = threading.Barrier(8)
        started: list[bool] = []
        guard = threading.Lock()

        def race() -> None:
            begin.wait(5.0)
            try:
                self.poller.start()
            except SafetyViolation:
                return
            with guard:
                started.append(True)

        threads = [threading.Thread(target=race) for _ in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(5.0)

        self.assertEqual(len(started), 1, "more than one worker was started")


class TestAWorkerThatStopsOnItsOwnSaysSo(PollerCase):
    """A dead poller must be observably dead, not silently dead."""

    def setUp(self) -> None:
        super().setUp()
        self.rest()
        self.poller = self.build_poller(
            identity=Principal("nosy-1", Role.STRATEGY), interval_seconds=0.001
        )
        self.addCleanup(self.poller.stop)

    def test_an_unauthorized_worker_stops_rather_than_spinning(self) -> None:
        self.poller.start()
        self.assertTrue(until(lambda: not self.poller.is_running))

    def test_the_reason_is_recorded(self) -> None:
        self.poller.start()
        self.assertTrue(until(lambda: self.poller.last_error is not None))
        self.assertIsInstance(self.poller.last_error, UnauthorizedAction)

    def test_a_worker_that_stopped_on_its_own_can_still_be_stopped(self) -> None:
        self.poller.start()
        self.assertTrue(until(lambda: not self.poller.is_running))
        self.poller.stop()
        self.assertFalse(self.poller.is_running)

    def test_starting_again_clears_the_previous_error(self) -> None:
        self.poller.start()
        self.assertTrue(until(lambda: self.poller.last_error is not None))
        self.poller.stop()

        working = self.build_poller(interval_seconds=0.001)
        self.addCleanup(working.stop)
        working.start()
        self.assertIsNone(working.last_error)
        working.stop()


class TestAWorkerThatWillNotStop(PollerCase):
    """A hung sweep is reported, not waited on forever and not declared over."""

    def setUp(self) -> None:
        super().setUp()
        self.entered = threading.Event()
        self.release = threading.Event()
        answer = self.venue.fetch_order_state

        def blocking(order: Order) -> BrokerAck:
            self.entered.set()
            self.release.wait(10.0)
            return answer(order)

        self.venue.fetch_order_state = blocking
        self.rest()
        self.poller = self.build_poller(interval_seconds=0.001)
        # LIFO: release the venue first, then stop.
        self.addCleanup(self.poller.stop)
        self.addCleanup(self.release.set)

    def test_stop_reports_a_worker_that_will_not_finish(self) -> None:
        self.poller.start()
        self.assertTrue(self.entered.wait(5.0))
        with self.assertRaises(SafetyViolation) as caught:
            self.poller.stop(timeout_seconds=0.01)
        self.assertIn("did not stop", str(caught.exception))

    def test_a_worker_that_would_not_stop_is_still_running(self) -> None:
        self.poller.start()
        self.assertTrue(self.entered.wait(5.0))
        with self.assertRaises(SafetyViolation):
            self.poller.stop(timeout_seconds=0.01)
        self.assertTrue(self.poller.is_running)

    def test_a_worker_that_would_not_stop_still_blocks_a_second_start(self) -> None:
        self.poller.start()
        self.assertTrue(self.entered.wait(5.0))
        with self.assertRaises(SafetyViolation):
            self.poller.stop(timeout_seconds=0.01)
        with self.assertRaises(SafetyViolation) as caught:
            self.poller.start()
        self.assertIn("already running", str(caught.exception))

    def test_it_stops_cleanly_once_the_sweep_finishes(self) -> None:
        self.poller.start()
        self.assertTrue(self.entered.wait(5.0))
        with self.assertRaises(SafetyViolation):
            self.poller.stop(timeout_seconds=0.01)

        self.release.set()
        self.poller.stop()

        self.assertFalse(self.poller.is_running)


# -- construction and reporting ----------------------------------------------


class TestConstruction(PollerCase):
    def test_the_default_interval_is_positive(self) -> None:
        self.assertGreater(DEFAULT_INTERVAL_SECONDS, 0)
        self.assertEqual(self.poller.interval_seconds, DEFAULT_INTERVAL_SECONDS)

    def test_a_zero_interval_is_a_configuration_error(self) -> None:
        with self.assertRaises(ConfigurationError):
            self.build_poller(interval_seconds=0)

    def test_a_negative_interval_is_a_configuration_error(self) -> None:
        with self.assertRaises(ConfigurationError):
            self.build_poller(interval_seconds=-1)

    def test_a_nan_interval_is_a_configuration_error(self) -> None:
        with self.assertRaises(ConfigurationError):
            self.build_poller(interval_seconds=float("nan"))

    def test_an_integer_interval_is_accepted_as_seconds(self) -> None:
        self.assertEqual(self.build_poller(interval_seconds=2).interval_seconds, 2.0)

    def test_the_repr_names_the_identity_and_the_interval(self) -> None:
        text = repr(self.poller)
        self.assertIn("poller-1", text)
        self.assertIn("running=False", text)


class TestTheReport(PollerCase):
    def test_considered_is_the_sum_of_the_buckets(self) -> None:
        report = PollReport(
            synced=("a",),
            refused=(PollNote("b", "terminal"),),
            failed=(PollNote("c", "boom"),),
        )
        self.assertEqual(report.considered, 3)

    def test_an_empty_report_considers_nothing(self) -> None:
        self.assertEqual(PollReport().considered, 0)

    def test_last_report_is_the_most_recent_sweep(self) -> None:
        order = self.rest()
        first = self.poller.poll_once()
        self.assertIs(self.poller.last_report, first)
        second = self.poller.poll_once()
        self.assertIs(self.poller.last_report, second)
        self.assertEqual(second.synced, (order.order_id,))

    def test_a_note_carries_the_order_it_is_about(self) -> None:
        note = PollNote("ord-1", "because")
        self.assertEqual(note.order_id, "ord-1")
        self.assertEqual(note.reason, "because")


# -- end to end, against the paper venue -------------------------------------


class TestThroughThePaperVenue(unittest.TestCase):
    """No stubs anywhere: a limit rests, the market reaches it, the poller books it.

    This is the case the system could not represent before Stage 2G. The venue
    re-evaluates on a poll and ``sync_order`` carries the answer home -- but
    until this module existed, something had to ask. Note what the timing means:
    the order fills on the *sweep* that follows the market reaching it, not at
    the moment it did, which is why SAFETY.md says a paper fill time is the time
    it was noticed.
    """

    def setUp(self) -> None:
        self.clock = ManualClock()
        self.feed = InMemoryQuoteFeed(clock=self.clock, source="poller-test")
        self.feed.publish(SYMBOL, BID, ASK)
        self.paper = PaperBroker(clock=self.clock, quotes=self.feed)
        self.rig = build_rig(clock=self.clock, broker=self.paper)
        self.poller = LifecyclePoller(
            gateway=self.rig.gateway, orders=self.rig.orders, identity=POLLER
        )

    def rest(self, price: str = "49000") -> Order:
        result = self.rig.submit(
            OrderIntent(
                strategy_id="strat-1",
                signal_id="sig-poll-1",
                symbol=SYMBOL,
                side=OrderSide.BUY,
                order_type=OrderType.LIMIT,
                quantity=DEFAULT_QUANTITY,
                limit_price=Price(price, USD),
            )
        )
        self.assertTrue(result.is_executed, result.reason)
        self.assertIs(result.order.state, OrderState.ACCEPTED)
        return result.order

    def test_a_sweep_before_the_market_arrives_changes_nothing(self) -> None:
        order = self.rest()
        report = self.poller.poll_once()
        self.assertEqual(report.synced, (order.order_id,))
        self.assertIs(order.state, OrderState.ACCEPTED)
        self.assertEqual(self.rig.positions.snapshot(), {})

    def test_the_sweep_after_the_market_arrives_books_the_fill(self) -> None:
        order = self.rest()
        self.poller.poll_once()

        self.feed.publish(SYMBOL, Price("48000", USD), Price("48010", USD))
        self.poller.poll_once()

        self.assertIs(order.state, OrderState.FILLED)
        self.assertEqual(
            self.rig.positions.position(SYMBOL, asset=ASSET), DEFAULT_QUANTITY
        )

    def test_the_fill_is_booked_once_however_many_sweeps_follow(self) -> None:
        order = self.rest()
        self.feed.publish(SYMBOL, Price("48000", USD), Price("48010", USD))
        self.poller.poll_once()
        cash = self.rig.portfolio.cash

        for _ in range(3):
            self.poller.poll_once()

        self.assertIs(order.state, OrderState.FILLED)
        self.assertEqual(
            self.rig.positions.position(SYMBOL, asset=ASSET), DEFAULT_QUANTITY
        )
        self.assertEqual(self.rig.portfolio.cash, cash)

    def test_a_stale_quote_leaves_the_resting_order_alone(self) -> None:
        """A gap in the feed must not disown a live order."""
        order = self.rest()
        self.clock.advance(3600)

        report = self.poller.poll_once()

        self.assertEqual(report.synced, (order.order_id,))
        self.assertIs(order.state, OrderState.ACCEPTED)
        self.assertFalse(order.is_unknown)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
