"""Tests for the sandbox broker boundary -- translation, without a network.

What is being tested here is the adapter's *judgement*, and almost all of it is
judgement about what to refuse. A venue answer that is well formed and says
"open" is easy; the interesting cases are the ones where the answer is missing,
contradictory, unreadable, or never arrives. Each of those must become
``UNCERTAIN`` -- which the gateway turns into an UNKNOWN order and a system-wide
block -- rather than a guess, because a guess here is what creates a second
order at the venue or retires one that is still live.

So the transport in these tests is a scripted stand-in that can be made to time
out, disconnect, throttle, and lie. ``TestNoAutomaticRetry`` is the one that
matters most: having seen every failure mode, it asserts the transport was
called exactly once for each placement, because INVARIANT 12's whole point is
that an ambiguous submission must not produce a second copy.

``TestThroughTheGateway`` closes the loop: the same gates that refused before
still refuse, and an uncertain sandbox answer lands as UNKNOWN through the real
``ExecutionGateway`` rather than as a fabricated outcome.
"""

from __future__ import annotations

import unittest
from decimal import Decimal

from tests.harness import ASSET, DEFAULT_QUANTITY, SYMBOL, build_rig
from trading.adapters.sandbox import (
    SandboxBroker,
    SandboxRequest,
    SandboxResponse,
    SandboxTransport,
    TransportDisconnected,
    TransportMalformed,
    TransportRateLimited,
    TransportTimeout,
)
from trading.core.authz import Principal, Role, mint_execution_token
from trading.core.clock import ManualClock
from trading.core.money import USD, Price, Quantity
from trading.core.orders import Order, OrderIntent, OrderSide, OrderState
from trading.ports.broker import (
    AckOutcome,
    BrokerOrderInventoryPort,
    BrokerOrderStatus,
)

BID = Price("49990", USD)
ASK = Price("50010", USD)


class ScriptedTransport(SandboxTransport):
    """A transport that returns whatever it was told to, or raises instead.

    ``failures`` is a queue consumed one per call; when empty, ``responses`` is
    consulted by action name. Every request is recorded, which is what lets the
    retry tests assert the *count* of calls rather than only their outcome.
    """

    def __init__(self) -> None:
        self.requests: list[SandboxRequest] = []
        self.responses: dict[str, SandboxResponse | list[SandboxResponse]] = {}
        self.failures: list[BaseException] = []

    def send(self, request: SandboxRequest) -> SandboxResponse:
        self.requests.append(request)
        if self.failures:
            raise self.failures.pop(0)
        answer = self.responses.get(request.action)
        if answer is None:
            return SandboxResponse(404, {})
        if isinstance(answer, list):
            return answer.pop(0) if answer else SandboxResponse(404, {})
        return answer

    def calls(self, action: str) -> list[SandboxRequest]:
        return [r for r in self.requests if r.action == action]


def open_body(order_id: str = "SB-1") -> dict[str, object]:
    return {"order_id": order_id, "status": "open"}


def filled_body(
    order_id: str = "SB-1",
    quantity: str = "0.001",
    price: str = "50000",
    status: str = "filled",
) -> dict[str, object]:
    return {
        "order_id": order_id,
        "status": status,
        "filled_quantity": quantity,
        "average_price": price,
    }


class SandboxCase(unittest.TestCase):
    """A sandbox adapter with a scripted transport and a way to place orders."""

    def setUp(self) -> None:
        self.clock = ManualClock()
        self.transport = ScriptedTransport()
        self.broker = SandboxBroker(clock=self.clock, transport=self.transport)
        self.gateway_id = Principal("gateway-1", Role.EXECUTION_GATEWAY)
        self._counter = 0

    def order(
        self,
        *,
        side: OrderSide = OrderSide.BUY,
        quantity: Quantity = DEFAULT_QUANTITY,
    ) -> Order:
        self._counter += 1
        intent = OrderIntent(
            strategy_id="strat-1",
            signal_id=f"sig-{self._counter}",
            symbol=SYMBOL,
            side=side,
            quantity=quantity,
        )
        return Order(intent, clock=self.clock)

    def token(self, order: Order):
        return mint_execution_token(
            self.gateway_id,
            order_id=order.order_id,
            idempotency_key=order.idempotency_key,
            clock=self.clock,
        )

    def place(self, order: Order | None = None, **kwargs):
        order = order or self.order(**kwargs)
        return order, self.broker.place_order(order, token=self.token(order))


# -- construction --------------------------------------------------------------


class TestConstruction(SandboxCase):
    def test_it_implements_both_ports(self) -> None:
        from trading.ports.broker import BrokerPort

        self.assertIsInstance(self.broker, BrokerPort)
        self.assertIsInstance(self.broker, BrokerOrderInventoryPort)

    def test_a_non_transport_is_refused(self) -> None:
        with self.assertRaises(TypeError) as ctx:
            SandboxBroker(clock=self.clock, transport=object())
        self.assertIn("SandboxTransport", str(ctx.exception))

    def test_the_adapter_holds_no_endpoint_or_credentials(self) -> None:
        """The whole point of the injected boundary: nothing to leak here."""
        attrs = " ".join(dir(self.broker)).lower()
        for forbidden in ("url", "host", "endpoint", "secret", "api_key", "token_"):
            self.assertNotIn(forbidden, attrs)

    def test_it_starts_with_no_history(self) -> None:
        self.assertEqual(self.broker.placement_count, 0)
        self.assertEqual(self.broker.duplicate_keys, frozenset())

    def test_a_bad_quote_currency_is_refused(self) -> None:
        with self.assertRaises(TypeError):
            SandboxBroker(
                clock=self.clock, transport=self.transport, quote_currency="USD"
            )

    def test_an_empty_id_prefix_is_refused(self) -> None:
        for bad in ("", "   "):
            with self.subTest(prefix=repr(bad)):
                with self.assertRaises(ValueError):
                    SandboxBroker(
                        clock=self.clock, transport=self.transport, id_prefix=bad
                    )

    def test_the_attempts_property_is_the_public_record(self) -> None:
        self.transport.responses["place_order"] = SandboxResponse(200, open_body())
        order, _ = self.place()
        self.assertEqual(self.broker.attempts, [(order.order_id, order.idempotency_key)])


class TestCancellation(SandboxCase):
    """Cancel can only reduce exposure, so it needs no token -- but it is still
    read, not assumed: an unreadable answer is uncertainty, not a cancel."""

    def test_an_accepted_cancel_is_reported(self) -> None:
        self.transport.responses["cancel_order"] = SandboxResponse(200, {"status": "canceled"})
        ack = self.broker.cancel_order(self.order())
        self.assertIs(ack.outcome, AckOutcome.ACCEPTED)

    def test_a_refused_cancel_is_reported(self) -> None:
        self.transport.responses["cancel_order"] = SandboxResponse(
            400, {"error": "order already filled"}
        )
        ack = self.broker.cancel_order(self.order())
        self.assertIs(ack.outcome, AckOutcome.REJECTED)

    def test_a_transport_failure_on_a_cancel_is_uncertain(self) -> None:
        """We do not know whether the cancel landed, so we must not claim it did."""
        self.transport.failures.append(TransportTimeout("no answer"))
        ack = self.broker.cancel_order(self.order())
        self.assertIs(ack.outcome, AckOutcome.UNCERTAIN)

    def test_an_unexpected_exception_on_a_cancel_is_uncertain(self) -> None:
        self.transport.failures.append(ValueError("odd"))
        ack = self.broker.cancel_order(self.order())
        self.assertIs(ack.outcome, AckOutcome.UNCERTAIN)


# -- the happy paths -----------------------------------------------------------


class TestAcceptedPlacements(SandboxCase):
    def test_an_open_response_is_accepted(self) -> None:
        self.transport.responses["place_order"] = SandboxResponse(200, open_body())
        order, ack = self.place()
        self.assertIs(ack.outcome, AckOutcome.ACCEPTED)
        self.assertEqual(ack.broker_order_id, "SB-1")

    def test_an_accepted_order_with_no_id_gets_one(self) -> None:
        self.transport.responses["place_order"] = SandboxResponse(
            200, {"status": "open"}
        )
        _, ack = self.place()
        self.assertIs(ack.outcome, AckOutcome.ACCEPTED)
        self.assertTrue(ack.broker_order_id.startswith("SANDBOX-"))

    def test_a_filled_response_is_a_fill(self) -> None:
        self.transport.responses["place_order"] = SandboxResponse(200, filled_body())
        order, ack = self.place()
        self.assertIs(ack.outcome, AckOutcome.FILLED)
        self.assertEqual(ack.filled_quantity, DEFAULT_QUANTITY)
        self.assertEqual(ack.fill_price, Price("50000", USD))

    def test_an_immediate_rejection_is_definitive(self) -> None:
        self.transport.responses["place_order"] = SandboxResponse(
            400, {"error": "insufficient balance"}
        )
        _, ack = self.place()
        self.assertIs(ack.outcome, AckOutcome.REJECTED)

    def test_status_words_are_matched_case_insensitively(self) -> None:
        self.transport.responses["place_order"] = SandboxResponse(
            200, {"order_id": "SB-1", "status": "OPEN"}
        )
        _, ack = self.place()
        self.assertIs(ack.outcome, AckOutcome.ACCEPTED)


# -- the failures that must be uncertain ---------------------------------------


class TestAmbiguousOutcomesAreUncertain(SandboxCase):
    """Every one of these means "we may have an order we cannot see"."""

    def test_a_timeout_is_uncertain(self) -> None:
        self.transport.failures.append(TransportTimeout("no response in 5s"))
        _, ack = self.place()
        self.assertIs(ack.outcome, AckOutcome.UNCERTAIN)
        self.assertIn("TransportTimeout", ack.message)

    def test_a_disconnect_is_uncertain(self) -> None:
        self.transport.failures.append(TransportDisconnected("connection reset"))
        _, ack = self.place()
        self.assertIs(ack.outcome, AckOutcome.UNCERTAIN)

    def test_a_rate_limit_is_uncertain(self) -> None:
        self.transport.failures.append(TransportRateLimited("429 slow down"))
        _, ack = self.place()
        self.assertIs(ack.outcome, AckOutcome.UNCERTAIN)
        self.assertIn("rate-limited", ack.message)

    def test_an_unexpected_exception_is_uncertain(self) -> None:
        """Anything unclassified says nothing about whether it arrived."""
        self.transport.failures.append(ValueError("something odd"))
        _, ack = self.place()
        self.assertIs(ack.outcome, AckOutcome.UNCERTAIN)

    def test_a_malformed_body_is_uncertain(self) -> None:
        self.transport.responses["place_order"] = SandboxResponse(
            200, {"nonsense": True}
        )
        _, ack = self.place()
        self.assertIs(ack.outcome, AckOutcome.UNCERTAIN)
        self.assertIn("status", ack.message)

    def test_an_unrecognised_status_is_uncertain(self) -> None:
        """Inventing a meaning for an unknown word is how a live order is retired."""
        self.transport.responses["place_order"] = SandboxResponse(
            200, {"order_id": "SB-1", "status": "pending_settlement_of_doom"}
        )
        _, ack = self.place()
        self.assertIs(ack.outcome, AckOutcome.UNCERTAIN)
        self.assertIn("unrecognised", ack.message)

    def test_a_server_error_is_uncertain_not_a_rejection(self) -> None:
        """A 5xx is the venue's problem, not a refusal of our order."""
        self.transport.responses["place_order"] = SandboxResponse(
            503, {"error": "service unavailable"}
        )
        _, ack = self.place()
        self.assertIs(ack.outcome, AckOutcome.UNCERTAIN)

    def test_an_overfill_is_refused_rather_than_booked(self) -> None:
        self.transport.responses["place_order"] = SandboxResponse(
            200, filled_body(quantity="0.002")
        )
        _, ack = self.place()
        self.assertIs(ack.outcome, AckOutcome.UNCERTAIN)
        self.assertIn("overfill", ack.message)

    def test_a_float_from_the_venue_is_refused(self) -> None:
        """A venue returning a JSON number cannot be trusted to be exact."""
        body = filled_body()
        body["filled_quantity"] = 0.001
        self.transport.responses["place_order"] = SandboxResponse(200, body)
        _, ack = self.place()
        self.assertIs(ack.outcome, AckOutcome.UNCERTAIN)
        self.assertIn("exactly", ack.message)

    def test_a_429_response_is_uncertain(self) -> None:
        """A rate limit returned as a status, not raised as an exception."""
        self.transport.responses["place_order"] = SandboxResponse(
            429, {"error": "too many requests"}
        )
        _, ack = self.place()
        self.assertIs(ack.outcome, AckOutcome.UNCERTAIN)
        self.assertIn("rate-limited", ack.message)

    def test_a_zero_quantity_fill_is_refused(self) -> None:
        """A fill of nothing is a contradiction, not a resting order."""
        self.transport.responses["place_order"] = SandboxResponse(
            200, filled_body(quantity="0")
        )
        _, ack = self.place()
        self.assertIs(ack.outcome, AckOutcome.UNCERTAIN)

    def test_a_partial_fill_on_placement_is_accepted_not_booked(self) -> None:
        """The ack carries one trade; a partial is a cumulative snapshot.

        Booking it as a fill here would be a second, worse answer than the
        follow-up query's, so it is reported as accepted and left to sync.
        """
        self.transport.responses["place_order"] = SandboxResponse(
            200, filled_body(quantity="0.0004", status="partially_filled")
        )
        _, ack = self.place()
        self.assertIs(ack.outcome, AckOutcome.ACCEPTED)
        self.assertIn("partially_filled", ack.message)

    def test_a_canceled_status_on_placement_is_accepted_not_a_rejection(self) -> None:
        """A venue reporting immediate cancellation is not a refusal of the order."""
        self.transport.responses["place_order"] = SandboxResponse(
            200, {"order_id": "SB-1", "status": "canceled"}
        )
        _, ack = self.place()
        self.assertIs(ack.outcome, AckOutcome.ACCEPTED)

    def test_a_rejected_status_on_a_state_query_is_reported(self) -> None:
        order = self.order()
        self.transport.responses["fetch_order_state"] = SandboxResponse(
            200, {"order_id": "SB-1", "status": "rejected"}
        )
        ack = self.broker.fetch_order_state(order)
        self.assertIs(ack.outcome, AckOutcome.REJECTED)

    def test_a_non_2xx_status_on_a_state_query_raises(self) -> None:
        order = self.order()
        self.transport.responses["fetch_order_state"] = SandboxResponse(500, {})
        with self.assertRaises(TransportMalformed):
            self.broker.fetch_order_state(order)

    def test_a_zero_quantity_on_a_fill_query_raises(self) -> None:
        """A "filled" status reporting nothing filled is a contradiction."""
        order = self.order()
        self.transport.responses["fetch_order_state"] = SandboxResponse(
            200, filled_body(quantity="0", status="filled")
        )
        with self.assertRaises(TransportMalformed):
            self.broker.fetch_order_state(order)

    def test_a_non_positive_price_raises(self) -> None:
        order = self.order()
        self.transport.responses["fetch_order_state"] = SandboxResponse(
            200, filled_body(price="0")
        )
        with self.assertRaises(TransportMalformed):
            self.broker.fetch_order_state(order)

    def test_a_float_price_is_refused(self) -> None:
        order = self.order()
        body = filled_body()
        body["average_price"] = 50000.0
        self.transport.responses["fetch_order_state"] = SandboxResponse(200, body)
        with self.assertRaises(TransportMalformed):
            self.broker.fetch_order_state(order)

    def test_an_integer_price_is_accepted(self) -> None:
        """A whole number is exact, so an int is fine where a float is not."""
        order = self.order()
        body = filled_body()
        body["average_price"] = 50000
        self.transport.responses["fetch_order_state"] = SandboxResponse(200, body)
        ack = self.broker.fetch_order_state(order)
        self.assertEqual(ack.fill_price, Price("50000", USD))

    def test_a_non_string_id_is_refused(self) -> None:
        self.transport.responses["place_order"] = SandboxResponse(
            200, {"order_id": 12345, "status": "open"}
        )
        _, ack = self.place()
        self.assertIs(ack.outcome, AckOutcome.UNCERTAIN)


class TestNoAutomaticRetry(SandboxCase):
    """An ambiguous placement must produce exactly one request, not two."""

    def test_a_timeout_sends_once(self) -> None:
        self.transport.failures.append(TransportTimeout("timeout"))
        self.place()
        self.assertEqual(len(self.transport.calls("place_order")), 1)

    def test_every_failure_mode_sends_once(self) -> None:
        for failure in (
            TransportTimeout("t"),
            TransportDisconnected("d"),
            TransportRateLimited("r"),
            ValueError("v"),
        ):
            with self.subTest(failure=type(failure).__name__):
                self.setUp()
                self.transport.failures.append(failure)
                self.place()
                self.assertEqual(len(self.transport.calls("place_order")), 1)

    def test_a_retry_by_the_caller_is_visible_to_the_transport(self) -> None:
        """The adapter cannot stop a *caller* resending -- but it records it.

        The evidence INVARIANT 12 depends on: if the same idempotency key is
        placed twice, the transport says so, rather than our having to trust the
        gateway's internals.
        """
        self.transport.responses["place_order"] = SandboxResponse(200, open_body())
        order = self.order()
        self.broker.place_order(order, token=self.token(order))
        # A fresh token for the same order (as a careless retry would mint).
        self.broker.place_order(order, token=self.token(order))
        self.assertIn(order.idempotency_key, self.broker.duplicate_keys)
        self.assertEqual(self.broker.times_seen(order.idempotency_key), 2)


class TestTokenIsConsumedFirst(SandboxCase):
    """The token rule is the same here as in every other adapter."""

    def test_a_replayed_token_cannot_reach_the_transport_twice(self) -> None:
        self.transport.responses["place_order"] = SandboxResponse(200, open_body())
        order = self.order()
        token = self.token(order)
        self.broker.place_order(order, token=token)
        with self.assertRaises(Exception):
            self.broker.place_order(order, token=token)
        self.assertEqual(len(self.transport.calls("place_order")), 1)

    def test_a_failed_placement_still_consumes_the_token(self) -> None:
        """Otherwise a lost connection would be retryable with the same token."""
        self.transport.failures.append(TransportTimeout("timeout"))
        order = self.order()
        token = self.token(order)
        self.broker.place_order(order, token=token)
        with self.assertRaises(Exception):
            self.broker.place_order(order, token=token)


# -- state queries -------------------------------------------------------------


class TestStateQueries(SandboxCase):
    def test_no_record_is_reported_as_rejected(self) -> None:
        """Same reading as every other adapter: it never existed."""
        order = self.order()
        self.transport.responses["fetch_order_state"] = SandboxResponse(404, {})
        ack = self.broker.fetch_order_state(order)
        self.assertIs(ack.outcome, AckOutcome.REJECTED)

    def test_a_resting_order_reports_accepted(self) -> None:
        order = self.order()
        self.transport.responses["fetch_order_state"] = SandboxResponse(
            200, open_body("SB-9")
        )
        ack = self.broker.fetch_order_state(order)
        self.assertIs(ack.outcome, AckOutcome.ACCEPTED)
        self.assertEqual(ack.broker_order_id, "SB-9")

    def test_a_cumulative_partial_fill_is_reported_as_a_fill(self) -> None:
        """``sync_order`` applies only the delta, so cumulative is the right shape."""
        order = self.order()
        self.transport.responses["fetch_order_state"] = SandboxResponse(
            200, filled_body(quantity="0.0004", status="partially_filled")
        )
        ack = self.broker.fetch_order_state(order)
        self.assertIs(ack.outcome, AckOutcome.FILLED)
        self.assertEqual(ack.filled_quantity, Quantity("0.0004", ASSET))

    def test_a_canceled_order_is_reported_as_no_longer_held(self) -> None:
        order = self.order()
        self.transport.responses["fetch_order_state"] = SandboxResponse(
            200, {"order_id": "SB-1", "status": "canceled"}
        )
        ack = self.broker.fetch_order_state(order)
        self.assertIs(ack.outcome, AckOutcome.REJECTED)
        self.assertIn("canceled", ack.message)

    def test_a_transport_failure_on_a_state_query_raises(self) -> None:
        """A read that failed must not be reported as an order state."""
        order = self.order()
        self.transport.failures.append(TransportTimeout("timeout"))
        with self.assertRaises(TransportMalformed):
            self.broker.fetch_order_state(order)

    def test_an_unrecognised_status_on_a_query_raises(self) -> None:
        order = self.order()
        self.transport.responses["fetch_order_state"] = SandboxResponse(
            200, {"order_id": "SB-1", "status": "who_knows"}
        )
        with self.assertRaises(TransportMalformed):
            self.broker.fetch_order_state(order)


# -- inventory and positions ---------------------------------------------------


class TestInventory(SandboxCase):
    def snapshot_body(self, **overrides) -> dict[str, object]:
        record = {
            "order_id": "SB-1",
            "symbol": SYMBOL,
            "side": "buy",
            "status": "open",
            "quantity": "0.001",
            "base_asset": ASSET,
            "client_order_id": "key-1",
        }
        record.update(overrides)
        return {"orders": [record]}

    def test_an_order_list_becomes_snapshots(self) -> None:
        self.transport.responses["fetch_orders"] = SandboxResponse(
            200, self.snapshot_body()
        )
        snapshots = self.broker.fetch_order_inventory()
        self.assertEqual(len(snapshots), 1)
        self.assertEqual(snapshots[0].broker_order_id, "SB-1")
        self.assertIs(snapshots[0].status, BrokerOrderStatus.OPEN)
        self.assertEqual(snapshots[0].idempotency_key, "key-1")

    def test_a_filled_record_carries_its_fill(self) -> None:
        self.transport.responses["fetch_orders"] = SandboxResponse(
            200,
            self.snapshot_body(status="filled", filled_quantity="0.001",
                               average_price="50000"),
        )
        snapshot = self.broker.fetch_order_inventory()[0]
        self.assertIs(snapshot.status, BrokerOrderStatus.FILLED)
        self.assertEqual(snapshot.filled_quantity, DEFAULT_QUANTITY)

    def test_an_empty_book_is_an_empty_tuple(self) -> None:
        self.transport.responses["fetch_orders"] = SandboxResponse(200, {"orders": []})
        self.assertEqual(self.broker.fetch_order_inventory(), ())

    def test_a_missing_orders_array_raises(self) -> None:
        self.transport.responses["fetch_orders"] = SandboxResponse(200, {"nope": 1})
        with self.assertRaises(TransportMalformed):
            self.broker.fetch_order_inventory()

    def test_a_non_object_entry_raises(self) -> None:
        self.transport.responses["fetch_orders"] = SandboxResponse(
            200, {"orders": ["not an object"]}
        )
        with self.assertRaises(TransportMalformed):
            self.broker.fetch_order_inventory()

    def test_an_unrecognised_status_in_the_book_raises(self) -> None:
        self.transport.responses["fetch_orders"] = SandboxResponse(
            200, self.snapshot_body(status="mystery")
        )
        with self.assertRaises(TransportMalformed):
            self.broker.fetch_order_inventory()

    def test_a_transport_failure_on_the_book_raises(self) -> None:
        self.transport.failures.append(TransportDisconnected("dropped"))
        with self.assertRaises(TransportMalformed):
            self.broker.fetch_order_inventory()

    def test_an_error_status_on_the_book_raises(self) -> None:
        self.transport.responses["fetch_orders"] = SandboxResponse(500, {})
        with self.assertRaises(TransportMalformed):
            self.broker.fetch_order_inventory()


class TestPositions(SandboxCase):
    def test_positions_are_translated(self) -> None:
        self.transport.responses["fetch_positions"] = SandboxResponse(
            200,
            {"positions": [{"symbol": SYMBOL, "quantity": "0.001", "base_asset": ASSET}]},
        )
        snapshot = self.broker.fetch_positions()
        self.assertEqual(snapshot.positions[SYMBOL], DEFAULT_QUANTITY)

    def test_a_negative_position_is_translated(self) -> None:
        self.transport.responses["fetch_positions"] = SandboxResponse(
            200,
            {"positions": [{"symbol": SYMBOL, "quantity": "-0.001", "base_asset": ASSET}]},
        )
        snapshot = self.broker.fetch_positions()
        self.assertEqual(snapshot.positions[SYMBOL].amount, Decimal("-0.001"))

    def test_an_empty_position_list_is_empty(self) -> None:
        self.transport.responses["fetch_positions"] = SandboxResponse(
            200, {"positions": []}
        )
        self.assertEqual(self.broker.fetch_positions().positions, {})

    def test_a_missing_array_raises(self) -> None:
        self.transport.responses["fetch_positions"] = SandboxResponse(200, {})
        with self.assertRaises(TransportMalformed):
            self.broker.fetch_positions()

    def test_a_float_quantity_raises(self) -> None:
        self.transport.responses["fetch_positions"] = SandboxResponse(
            200, {"positions": [{"symbol": SYMBOL, "quantity": 0.001}]}
        )
        with self.assertRaises(TransportMalformed):
            self.broker.fetch_positions()

    def test_a_transport_failure_raises(self) -> None:
        self.transport.failures.append(TransportTimeout("timeout"))
        with self.assertRaises(TransportMalformed):
            self.broker.fetch_positions()


# -- through the real gateway --------------------------------------------------


class TestThroughTheGateway(unittest.TestCase):
    """An uncertain sandbox answer must become UNKNOWN, through the real gateway."""

    def setUp(self) -> None:
        self.clock = ManualClock()
        self.transport = ScriptedTransport()
        self.broker = SandboxBroker(clock=self.clock, transport=self.transport)
        self.rig = build_rig(broker=self.broker, clock=self.clock)
        # The sandbox broker's clock must be the rig's, or tokens expire apart.
        self.broker._clock = self.clock

    def test_a_timeout_becomes_an_unknown_order(self) -> None:
        self.transport.failures.append(TransportTimeout("no answer"))
        result = self.rig.submit()
        self.assertEqual(result.outcome, "unknown")
        self.assertIs(result.order.state, OrderState.UNKNOWN)

    def test_an_unknown_order_blocks_the_next_submission(self) -> None:
        self.transport.failures.append(TransportTimeout("no answer"))
        self.rig.submit()
        second = self.rig.submit()
        self.assertFalse(second.is_executed)
        self.assertIn("unknown", (second.reason or "").lower())

    def test_a_timeout_sends_exactly_one_request(self) -> None:
        self.transport.failures.append(TransportTimeout("no answer"))
        self.rig.submit()
        self.assertEqual(len(self.transport.calls("place_order")), 1)

    def test_a_clean_acceptance_executes(self) -> None:
        self.transport.responses["place_order"] = SandboxResponse(200, open_body())
        result = self.rig.submit()
        self.assertTrue(result.is_executed, result.reason)
        self.assertIs(result.order.state, OrderState.ACCEPTED)

    def test_an_overfill_never_reaches_risk_as_a_booked_fill(self) -> None:
        self.transport.responses["place_order"] = SandboxResponse(
            200, filled_body(quantity="0.002")
        )
        result = self.rig.submit()
        self.assertEqual(result.outcome, "unknown")
        self.assertEqual(
            self.rig.positions.position(SYMBOL, asset=ASSET),
            Quantity.zero(ASSET),
        )


if __name__ == "__main__":
    unittest.main()
