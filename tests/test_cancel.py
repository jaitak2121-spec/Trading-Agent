"""Tests for ``ExecutionGateway.cancel`` -- the cancel/fill race, and its outcomes.

A cancel acknowledgement is not an outcome. Between the moment we ask a venue to
cancel and the moment it answers, the order may have filled. So ``cancel`` asks
the venue for its authoritative state afterwards and applies *that*, and these
tests are organised around who wins that race:

* the cancel wins        -> CANCELED, nothing booked
* a partial fill lands   -> the fill is booked, the remainder is CANCELED
* the fill wins outright -> FILLED, and never CANCELED

The recurring assertions are that a fill is booked **exactly once** (the delta is
computed against what is already on the books, so a re-read changes nothing), and
that an order which should never have been cancellable never reached the venue at
all -- ``StubVenue.cancel_calls`` staying at zero is the whole point of the
UNKNOWN and terminal guards.
"""

from __future__ import annotations

import unittest

from trading.core.config import RiskConfig
from trading.core.errors import SafetyViolation, UnauthorizedAction
from trading.core.money import USD, Price, Quantity
from trading.core.orders import Order, OrderState
from trading.ports.broker import AckOutcome, BrokerAck

from .harness import build_rig

RESTING = BrokerAck(AckOutcome.ACCEPTED, broker_order_id="b-1")
NO_RECORD = BrokerAck(AckOutcome.REJECTED, message="venue has no record of this order")
CANCEL_OK = BrokerAck(AckOutcome.ACCEPTED, broker_order_id="b-1", message="canceled")


def filled(quantity: str, price: str = "50000") -> BrokerAck:
    return BrokerAck(
        outcome=AckOutcome.FILLED,
        broker_order_id="b-1",
        filled_quantity=Quantity(quantity, "BTC"),
        fill_price=Price(price, USD),
    )


class StubVenue:
    """A venue whose two cancel-path answers are set independently.

    The simulator cannot express the races this needs: its ``cancel_order``
    always succeeds and always leaves ``fetch_order_state`` reporting no record,
    which is exactly one of the six cases below. Here the acknowledgement and the
    authoritative state are separate knobs, so "the venue says it cancelled but
    the order is still working" is expressible and can be asserted against.
    """

    def __init__(self, cancel_ack: BrokerAck, fetch_ack: BrokerAck) -> None:
        self.cancel_ack = cancel_ack
        self.fetch_ack = fetch_ack
        self.cancel_calls = 0
        self.fetch_calls = 0
        #: Every call in order, so audit-vs-effect ordering can be checked.
        self.calls: list[str] = []

    def cancel_order(self, order: Order) -> BrokerAck:
        self.cancel_calls += 1
        self.calls.append("cancel_order")
        return self.cancel_ack

    def fetch_order_state(self, order: Order) -> BrokerAck:
        self.fetch_calls += 1
        self.calls.append("fetch_order_state")
        return self.fetch_ack


class CancelFixture(unittest.TestCase):
    """A rig whose order is resting at the venue, behind a scriptable stub."""

    def resting_order(self, rig, quantity: str = "0.001") -> Order:
        rig.broker.script(RESTING)
        result = rig.submit(quantity=quantity)
        self.assertEqual(result.order.state, OrderState.ACCEPTED)
        return result.order

    def venue(self, rig, cancel_ack: BrokerAck, fetch_ack: BrokerAck) -> StubVenue:
        stub = StubVenue(cancel_ack, fetch_ack)
        rig.gateway._broker = stub
        return stub

    def records(self, rig, action: str) -> list:
        return [r for r in rig.sink.records if r.action == action]


class TestCancelWins(CancelFixture):
    def test_clean_cancel_with_no_fill_reaches_canceled(self) -> None:
        rig = build_rig()
        order = self.resting_order(rig)
        booked = rig.positions.position("BTCUSD", asset="BTC")

        ack = rig.gateway.cancel(order, operator=rig.operator_id)

        self.assertEqual(ack.outcome, AckOutcome.ACCEPTED)
        self.assertEqual(order.state, OrderState.CANCELED)
        self.assertFalse(order.is_open)
        # Nothing was filled, so nothing moved.
        self.assertEqual(rig.positions.position("BTCUSD", asset="BTC"), booked)

    def test_no_record_after_a_cancel_is_not_a_rejection(self) -> None:
        """The distinction ``after_cancel`` exists to preserve.

        "The venue has no record" normally means the order never existed. Right
        after a cancel it means the venue had the order and we withdrew it.
        """
        rig = build_rig()
        order = self.resting_order(rig)
        self.venue(rig, CANCEL_OK, NO_RECORD)

        rig.gateway.cancel(order, operator=rig.operator_id)

        self.assertEqual(order.state, OrderState.CANCELED)
        self.assertNotEqual(order.state, OrderState.REJECTED)

    def test_cancellation_releases_the_open_order_budget(self) -> None:
        """The defect this change exists to fix.

        A cancelled order used to stay ``is_open`` forever, holding a slot in
        ``max_open_orders`` that no amount of waiting would give back.
        """
        rig = build_rig(
            default_outcome=AckOutcome.ACCEPTED, risk=RiskConfig(max_open_orders=1)
        )
        first = rig.submit().order
        self.assertTrue(first.is_open)

        # The budget is spent: a second order cannot get through.
        refused = rig.submit()
        self.assertEqual(refused.outcome, "refused")
        self.assertIn("open", refused.reason.lower())

        rig.gateway.cancel(first, operator=rig.operator_id)
        self.assertEqual(first.state, OrderState.CANCELED)
        self.assertEqual(len(rig.orders.open_orders()), 0)

        # And now the slot is genuinely free.
        self.assertEqual(rig.submit().outcome, "executed")


class TestFillWinsTheRace(CancelFixture):
    def test_a_fill_that_beat_the_cancel_is_booked_and_wins(self) -> None:
        rig = build_rig()
        order = self.resting_order(rig)
        booked = rig.positions.position("BTCUSD", asset="BTC")
        # The venue refuses the cancel because the order already filled, and
        # its authoritative state says so.
        self.venue(
            rig,
            BrokerAck(AckOutcome.REJECTED, message="already filled"),
            filled("0.001"),
        )

        ack = rig.gateway.cancel(order, operator=rig.operator_id)

        self.assertEqual(ack.outcome, AckOutcome.REJECTED)
        self.assertEqual(order.state, OrderState.FILLED)
        self.assertNotEqual(order.state, OrderState.CANCELED)
        self.assertEqual(
            rig.positions.position("BTCUSD", asset="BTC"),
            booked + Quantity("0.001", "BTC"),
        )

    def test_partial_fill_is_booked_and_the_remainder_is_canceled(self) -> None:
        rig = build_rig()
        order = self.resting_order(rig, quantity="0.002")
        booked = rig.positions.position("BTCUSD", asset="BTC")
        # "canceled the unfilled remainder" -- the cancel succeeded, but half
        # the order had already traded.
        self.venue(
            rig,
            BrokerAck(AckOutcome.ACCEPTED, message="canceled the unfilled remainder"),
            filled("0.001"),
        )

        rig.gateway.cancel(order, operator=rig.operator_id)

        # The fill is real and booked; the remainder will never fill.
        self.assertEqual(order.filled_quantity, Quantity("0.001", "BTC"))
        self.assertEqual(order.state, OrderState.CANCELED)
        self.assertEqual(
            rig.positions.position("BTCUSD", asset="BTC"),
            booked + Quantity("0.001", "BTC"),
        )

    def test_an_already_booked_fill_produces_no_second_delta(self) -> None:
        """Cumulative snapshot in, delta out. A re-read must cost nothing."""
        rig = build_rig()
        # Submit and take a partial fill through the normal path first.
        rig.broker.script(filled("0.001"))
        order = rig.submit(quantity="0.002").order
        self.assertEqual(order.state, OrderState.PARTIALLY_FILLED)

        booked = rig.positions.position("BTCUSD", asset="BTC")
        cash = rig.portfolio.cash
        realized = rig.risk.pnl.realized

        # The venue reports the same cumulative fill it already gave us.
        self.venue(rig, CANCEL_OK, filled("0.001"))
        rig.gateway.cancel(order, operator=rig.operator_id)

        self.assertEqual(order.filled_quantity, Quantity("0.001", "BTC"))
        self.assertEqual(rig.positions.position("BTCUSD", asset="BTC"), booked)
        self.assertEqual(rig.portfolio.cash, cash)
        self.assertEqual(rig.risk.pnl.realized, realized)
        self.assertEqual(order.state, OrderState.CANCELED)

    def test_an_order_still_working_at_the_venue_is_not_declared_canceled(self) -> None:
        """The acknowledgement does not outrank the authoritative state.

        A venue that says "cancelled" but still reports the order working has
        contradicted itself. Retiring the order here would hide something that
        can still fill, so the local state stays honest instead.
        """
        rig = build_rig()
        order = self.resting_order(rig)
        self.venue(rig, CANCEL_OK, RESTING)

        rig.gateway.cancel(order, operator=rig.operator_id)

        self.assertEqual(order.state, OrderState.ACCEPTED)
        self.assertTrue(order.is_open)


class TestCancelRefusals(CancelFixture):
    def test_an_unknown_order_is_refused_without_reaching_the_venue(self) -> None:
        rig = build_rig()
        order = self.resting_order(rig)
        order.mark_unknown(reason="simulated")
        stub = self.venue(rig, CANCEL_OK, NO_RECORD)

        with self.assertRaises(SafetyViolation) as ctx:
            rig.gateway.cancel(order, operator=rig.operator_id)

        self.assertIn("UNKNOWN", str(ctx.exception))
        self.assertEqual(stub.cancel_calls, 0)
        self.assertEqual(stub.fetch_calls, 0)
        self.assertTrue(order.is_unknown)

    def test_a_terminal_order_is_refused_without_reaching_the_venue(self) -> None:
        rig = build_rig()  # default rig fills immediately
        order = rig.submit().order
        self.assertTrue(order.state.is_terminal)
        stub = self.venue(rig, CANCEL_OK, NO_RECORD)

        with self.assertRaises(SafetyViolation) as ctx:
            rig.gateway.cancel(order, operator=rig.operator_id)

        self.assertIn("already filled", str(ctx.exception))
        self.assertEqual(stub.cancel_calls, 0)
        self.assertEqual(order.state, OrderState.FILLED)

    def test_a_second_cancel_refuses_cleanly(self) -> None:
        rig = build_rig()
        order = self.resting_order(rig)
        rig.gateway.cancel(order, operator=rig.operator_id)
        self.assertEqual(order.state, OrderState.CANCELED)

        stub = self.venue(rig, CANCEL_OK, NO_RECORD)
        with self.assertRaises(SafetyViolation) as ctx:
            rig.gateway.cancel(order, operator=rig.operator_id)

        self.assertIn("already canceled", str(ctx.exception))
        self.assertEqual(stub.cancel_calls, 0)
        self.assertEqual(order.state, OrderState.CANCELED)

    def test_an_unauthorized_principal_never_reaches_the_venue(self) -> None:
        rig = build_rig()
        order = self.resting_order(rig)
        stub = self.venue(rig, CANCEL_OK, NO_RECORD)

        with self.assertRaises(UnauthorizedAction):
            rig.gateway.cancel(order, operator=rig.strategy_id)

        self.assertEqual(stub.cancel_calls, 0)
        self.assertEqual(order.state, OrderState.ACCEPTED)


    def test_a_failed_cancel_on_a_partial_fill_refuses_rather_than_rewrites(self) -> None:
        """``after_cancel`` is not a blanket amnesty for a "no record" answer.

        The cancel failed, so the suppression does not apply and the no-record
        answer is read literally -- and PARTIALLY_FILLED -> REJECTED is not a
        transition the state machine allows, because it would unwind a booked
        fill. The refusal is audited and the fill survives.
        """
        rig = build_rig()
        rig.broker.script(filled("0.001"))
        order = rig.submit(quantity="0.002").order
        self.assertEqual(order.state, OrderState.PARTIALLY_FILLED)
        booked = rig.positions.position("BTCUSD", asset="BTC")

        self.venue(rig, BrokerAck(AckOutcome.REJECTED, message="cannot cancel"), NO_RECORD)

        with self.assertRaises(SafetyViolation):
            rig.gateway.cancel(order, operator=rig.operator_id)

        refusals = self.records(rig, "gateway.venue_state_refused")
        self.assertEqual(len(refusals), 1)
        self.assertEqual(order.state, OrderState.PARTIALLY_FILLED)
        self.assertEqual(rig.positions.position("BTCUSD", asset="BTC"), booked)


class TestCancelAuditOrdering(CancelFixture):
    """INVARIANT 13: the decision is recorded before it takes effect."""

    def test_the_intent_is_audited_before_the_request_leaves(self) -> None:
        rig = build_rig()
        order = self.resting_order(rig)
        before = len(rig.sink.records)

        stub = self.venue(rig, CANCEL_OK, NO_RECORD)
        rig.gateway.cancel(order, operator=rig.operator_id)

        actions = [r.action for r in rig.sink.records[before:]]
        self.assertEqual(actions[0], "gateway.cancel_requested")
        self.assertEqual(actions[-1], "gateway.cancel_completed")
        self.assertEqual(stub.calls, ["cancel_order", "fetch_order_state"])

    def test_the_intent_record_predates_the_outcome_it_describes(self) -> None:
        """The first record names the state we cancelled *from*, not the result."""
        rig = build_rig()
        order = self.resting_order(rig)
        rig.gateway.cancel(order, operator=rig.operator_id)

        requested = self.records(rig, "gateway.cancel_requested")[0]
        completed = self.records(rig, "gateway.cancel_completed")[0]
        self.assertEqual(requested.details["state"], OrderState.ACCEPTED.value)
        self.assertEqual(completed.details["state"], OrderState.CANCELED.value)
        self.assertEqual(completed.outcome, "allowed")

    def test_a_refused_cancel_is_audited_and_sends_nothing(self) -> None:
        rig = build_rig()
        order = self.resting_order(rig)
        order.mark_unknown(reason="simulated")

        with self.assertRaises(SafetyViolation):
            rig.gateway.cancel(order, operator=rig.operator_id)

        refusals = self.records(rig, "gateway.cancel_refused")
        self.assertEqual(len(refusals), 1)
        self.assertEqual(refusals[0].outcome, "refused")
        self.assertIn("UNKNOWN", refusals[0].details["reason"])
        # The request never happened, so it was never recorded as happening.
        self.assertEqual(self.records(rig, "gateway.cancel_requested"), [])

    def test_a_failed_cancel_is_recorded_as_refused_not_allowed(self) -> None:
        rig = build_rig()
        order = self.resting_order(rig)
        self.venue(
            rig,
            BrokerAck(AckOutcome.REJECTED, message="already filled"),
            filled("0.001"),
        )

        rig.gateway.cancel(order, operator=rig.operator_id)

        completed = self.records(rig, "gateway.cancel_completed")[0]
        self.assertEqual(completed.outcome, "refused")
        self.assertEqual(completed.details["state"], OrderState.FILLED.value)

    def test_the_audit_chain_still_verifies_across_a_cancel(self) -> None:
        rig = build_rig()
        order = self.resting_order(rig)
        rig.gateway.cancel(order, operator=rig.operator_id)
        rig.audit.verify()


class TestCancelLocking(CancelFixture):
    def test_cancel_holds_the_gateway_lock_while_it_books(self) -> None:
        """Cancel can move the portfolio, so it must serialise against submit."""
        rig = build_rig()
        order = self.resting_order(rig)

        class Watcher(StubVenue):
            def cancel_order(self, inner: Order) -> BrokerAck:
                # The gateway lock is not reentrant, so a held lock cannot be
                # acquired again here. That is the assertion.
                held = not rig.gateway._lock.acquire(blocking=False)
                if not held:
                    rig.gateway._lock.release()
                self.held_during_cancel = held
                return super().cancel_order(inner)

        watcher = Watcher(CANCEL_OK, NO_RECORD)
        rig.gateway._broker = watcher
        rig.gateway.cancel(order, operator=rig.operator_id)

        self.assertTrue(watcher.held_during_cancel)

    def test_the_lock_is_released_when_a_cancel_refuses(self) -> None:
        """A refusal must not strand the lock and wedge every later order."""
        rig = build_rig()
        order = self.resting_order(rig)
        order.mark_unknown(reason="simulated")

        with self.assertRaises(SafetyViolation):
            rig.gateway.cancel(order, operator=rig.operator_id)

        acquired = rig.gateway._lock.acquire(blocking=False)
        self.assertTrue(acquired, "cancel left the gateway lock held")
        rig.gateway._lock.release()

    def test_the_lock_is_released_when_the_venue_answer_is_contradictory(self) -> None:
        rig = build_rig()
        order = self.resting_order(rig)
        # A fetch that reports a different venue identifier is refused deep
        # inside _apply_fetched_state, after the cancel request already went.
        self.venue(
            rig, CANCEL_OK, BrokerAck(AckOutcome.ACCEPTED, broker_order_id="someone-else")
        )

        with self.assertRaises(SafetyViolation) as ctx:
            rig.gateway.cancel(order, operator=rig.operator_id)

        self.assertIn("broker_order_id mismatch", str(ctx.exception))
        acquired = rig.gateway._lock.acquire(blocking=False)
        self.assertTrue(acquired, "cancel left the gateway lock held")
        rig.gateway._lock.release()
        # The order is left exactly as it was.
        self.assertEqual(order.state, OrderState.ACCEPTED)
        self.assertEqual(order.broker_order_id, "b-1")


if __name__ == "__main__":
    unittest.main()
