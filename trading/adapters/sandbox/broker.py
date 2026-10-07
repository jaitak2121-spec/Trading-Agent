"""A broker adapter with no network of its own -- the transport is injected.

This is the boundary a real exchange adapter will eventually be built behind. It
exists now, ahead of any live integration, for one reason: the *translation* from
bytes on a wire to a domain outcome is where the safety-critical mistakes live,
and that translation can be written, tested, and reviewed without a socket, an
endpoint, or a credential.

What is deliberately not here
=============================

**No endpoint, no signing, no base URL.** Nothing in this package names a host,
constructs an ``Authorization`` header, or holds a key. A transport is handed in
by the caller, and this adapter only decides what a response *means*. The
mapping from a real venue's documented contract to :class:`SandboxTransport` is
the step that requires a verified sandbox specification, and it is not taken
here -- see ``docs/SAFETY.md``.

**No retry.** A placement that times out, loses its connection, or comes back
unparseable becomes :attr:`~trading.ports.broker.AckOutcome.UNCERTAIN`, and
nothing in this module tries it again. This is INVARIANT 12's teeth: the one
thing an ambiguous submission must never do is produce a second copy at the
venue. Recovery is a reconciliation, performed by an operator against an order
in UNKNOWN, not an automatic resend.

**No execution token minted, and none bypassed.** ``place_order`` consumes the
token exactly as every other adapter must, before the transport is touched --
so a request that never leaves still cannot be replayed.

The transport contract
======================

:class:`SandboxTransport` takes a :class:`SandboxRequest` and returns a
:class:`SandboxResponse`, or raises. Raising is not a bug to be caught and
papered over: :class:`TransportTimeout` and :class:`TransportDisconnected`
describe failures where the request *may well have arrived*, and the honest
reading of those is uncertainty.

A rate limit is the one failure with a definite meaning -- the venue refused to
look at us -- and it is modelled as a distinct exception so an operator sees
"we were throttled" rather than "something went wrong".
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from decimal import Decimal
from typing import Mapping

from ...core.authz import ExecutionToken
from ...core.clock import Clock
from ...core.money import Currency, Price, Quantity, USD
from ...core.orders import Order, OrderSide
from ...ports.broker import (
    AckOutcome,
    BrokerAck,
    BrokerOrderInventoryPort,
    BrokerOrderSnapshot,
    BrokerOrderStatus,
    BrokerPort,
    BrokerPositionSnapshot,
)

__all__ = [
    "SandboxBroker",
    "SandboxRequest",
    "SandboxResponse",
    "SandboxTransport",
    "TransportDisconnected",
    "TransportError",
    "TransportMalformed",
    "TransportRateLimited",
    "TransportTimeout",
]


# -- transport-level failures -------------------------------------------------


class TransportError(Exception):
    """Base for a transport that could not complete a request."""


class TransportTimeout(TransportError):
    """The request left; no answer came back in time.

    Emphatically *not* a failure of the order. It may be resting at the venue.
    """


class TransportDisconnected(TransportError):
    """The connection died, possibly after the request was sent."""


class TransportRateLimited(TransportError):
    """The venue refused the request outright. Nothing was processed."""


class TransportMalformed(TransportError):
    """The venue answered something we cannot read as an order state."""


# -- the wire shapes ----------------------------------------------------------


@dataclass(frozen=True, slots=True)
class SandboxRequest:
    """One outbound request, in the shape a venue adapter would send.

    ``action`` names the operation rather than encoding a URL, so this adapter
    stays ignorant of a venue's routing. A real adapter maps these onto whatever
    endpoints the venue documents.
    """

    action: str
    order_id: str
    idempotency_key: str
    payload: Mapping[str, str]


@dataclass(frozen=True, slots=True)
class SandboxResponse:
    """One inbound response, already parsed but not yet trusted."""

    status_code: int
    body: Mapping[str, object]


class SandboxTransport(ABC):
    """The injected I/O boundary. Implementations live outside the kernel.

    The only implementation in this repository is in-process and rule-driven
    (:mod:`tests.test_sandbox_broker` builds them). A future live adapter
    implements the same interface over HTTP, and nothing in
    :class:`SandboxBroker` changes when it does.
    """

    @abstractmethod
    def send(self, request: SandboxRequest) -> SandboxResponse:
        """Perform ``request`` and return the parsed response.

        Raise :class:`TransportTimeout`, :class:`TransportDisconnected`,
        :class:`TransportRateLimited`, or :class:`TransportMalformed` for the
        corresponding conditions. Any other exception is treated as a
        disconnect by :class:`SandboxBroker`, because an unclassified failure
        says nothing about whether the request was received.
        """


# -- translation ---------------------------------------------------------------

#: Venue status strings this adapter understands, mapped to our states. Anything
#: absent is NOT guessed at -- an unrecognised status becomes UNCERTAIN, because
#: inventing a lifecycle meaning for a word we do not know is how a resting order
#: gets silently retired.
_STATUS_WORDS: Mapping[str, BrokerOrderStatus] = {
    "open": BrokerOrderStatus.OPEN,
    "new": BrokerOrderStatus.OPEN,
    "accepted": BrokerOrderStatus.OPEN,
    "working": BrokerOrderStatus.OPEN,
    "partially_filled": BrokerOrderStatus.PARTIALLY_FILLED,
    "partial": BrokerOrderStatus.PARTIALLY_FILLED,
    "filled": BrokerOrderStatus.FILLED,
    "closed": BrokerOrderStatus.FILLED,
    "canceled": BrokerOrderStatus.CANCELED,
    "cancelled": BrokerOrderStatus.CANCELED,
    "rejected": BrokerOrderStatus.REJECTED,
    "expired": BrokerOrderStatus.EXPIRED,
}


class SandboxBroker(BrokerPort, BrokerOrderInventoryPort):
    """A venue adapter whose only job is to translate, never to decide.

    Every method here is a pure function of what the transport returns, with one
    rule applied consistently: **an answer we cannot fully read is UNCERTAIN,
    never a guess**. That rule is what makes this adapter safe to point at a
    sandbox, and it is the same rule that must hold when it is pointed at a
    real venue.
    """

    #: Paths the adapter uses within a response body. Named constants so a real
    #: venue's field names are changed in one place rather than scattered.
    _ID_FIELD = "order_id"
    _STATUS_FIELD = "status"
    _FILLED_FIELD = "filled_quantity"
    _PRICE_FIELD = "average_price"
    _SYMBOL_FIELD = "symbol"
    _SIDE_FIELD = "side"
    _ORDERED_FIELD = "quantity"
    _QUOTE_FIELD = "quote_currency"
    _ASSET_FIELD = "base_asset"

    def __init__(
        self,
        *,
        clock: Clock,
        transport: SandboxTransport,
        quote_currency: Currency = USD,
        id_prefix: str = "SANDBOX",
    ) -> None:
        if not isinstance(transport, SandboxTransport):
            raise TypeError(
                f"transport must implement SandboxTransport, "
                f"got {type(transport).__name__}"
            )
        if not isinstance(quote_currency, Currency):
            raise TypeError("quote_currency must be a Currency")
        if not isinstance(id_prefix, str) or not id_prefix.strip():
            raise ValueError("id_prefix must be a non-empty string")
        self._clock = clock
        self._transport = transport
        self._quote_currency = quote_currency
        self._id_prefix = id_prefix
        self._seq = 0
        #: Every placement that reached the transport, for INVARIANT 12 evidence.
        self._attempts: list[tuple[str, str]] = []
        self._key_counts: dict[str, int] = {}
        self._duplicate_keys: set[str] = set()

    # -- observations -----------------------------------------------------

    @property
    def attempts(self) -> list[tuple[str, str]]:
        return list(self._attempts)

    @property
    def placement_count(self) -> int:
        return len(self._attempts)

    @property
    def duplicate_keys(self) -> frozenset[str]:
        """Keys the transport saw more than once. Must stay empty."""
        return frozenset(self._duplicate_keys)

    def times_seen(self, idempotency_key: str) -> int:
        return self._key_counts.get(idempotency_key, 0)

    # -- BrokerPort -------------------------------------------------------

    def place_order(self, order: Order, *, token: ExecutionToken) -> BrokerAck:
        # Token first, unconditionally, before the transport is touched -- the
        # same rule every adapter follows. A replayed token cannot get past this
        # line, and neither can a caller without gateway-minted authority
        # (INVARIANTS 3, 12).
        token.consume(order_id=order.order_id, clock=self._clock)

        key = order.idempotency_key
        self._attempts.append((order.order_id, key))
        seen = self._key_counts.get(key, 0) + 1
        self._key_counts[key] = seen
        if seen > 1:
            self._duplicate_keys.add(key)

        request = SandboxRequest(
            action="place_order",
            order_id=order.order_id,
            idempotency_key=key,
            payload={
                "symbol": order.symbol,
                "side": order.side.value,
                "quantity": str(order.intent.quantity.amount),
                self._ASSET_FIELD: order.intent.quantity.asset,
            },
        )

        try:
            response = self._transport.send(request)
        except TransportRateLimited as exc:
            # The venue refused to look at us, so nothing was processed. This is
            # the one failure with a definite meaning, and it is still not a
            # rejection of the order -- it is a request that will have to be
            # made again by an operator.
            return BrokerAck(
                AckOutcome.UNCERTAIN,
                message=f"venue rate-limited the request: {exc}; "
                "nothing was processed, but do not resend automatically",
            )
        except TransportError as exc:
            return BrokerAck(
                AckOutcome.UNCERTAIN,
                message=f"{type(exc).__name__}: {exc}",
            )
        except Exception as exc:  # noqa: BLE001 - unclassified means ambiguous
            # An exception we did not expect says nothing about whether the
            # request arrived. Never retry; never assume it did not.
            return BrokerAck(
                AckOutcome.UNCERTAIN,
                message=(
                    f"transport raised {type(exc).__name__}: {exc}; "
                    "outcome unknown, reconcile before retrying"
                ),
            )

        return self._interpret_placement(order, response)

    def cancel_order(self, order: Order) -> BrokerAck:
        request = SandboxRequest(
            action="cancel_order",
            order_id=order.order_id,
            idempotency_key=order.idempotency_key,
            payload={"broker_order_id": order.broker_order_id or ""},
        )
        try:
            response = self._transport.send(request)
        except TransportError as exc:
            return BrokerAck(AckOutcome.UNCERTAIN, message=f"{type(exc).__name__}: {exc}")
        except Exception as exc:  # noqa: BLE001
            return BrokerAck(AckOutcome.UNCERTAIN, message=f"{type(exc).__name__}: {exc}")
        if not self._ok(response):
            return BrokerAck(
                AckOutcome.REJECTED,
                message=f"cancel refused with status {response.status_code}",
            )
        return BrokerAck(AckOutcome.ACCEPTED, message="canceled")

    def fetch_order_state(self, order: Order) -> BrokerAck:
        request = SandboxRequest(
            action="fetch_order_state",
            order_id=order.order_id,
            idempotency_key=order.idempotency_key,
            payload={"broker_order_id": order.broker_order_id or ""},
        )
        try:
            response = self._transport.send(request)
        except TransportError as exc:
            raise TransportMalformed(
                f"{type(exc).__name__}: {exc}; cannot read the order's state"
            ) from exc

        if response.status_code == 404:
            # The venue does not hold it. Treated exactly as the other adapters
            # treat "no record": the honest reading is that it never existed.
            return BrokerAck(
                AckOutcome.REJECTED, message="venue has no record of this order"
            )
        if not self._ok(response):
            raise TransportMalformed(
                f"venue returned status {response.status_code} for a state query"
            )
        return self._interpret_state(order, response, cumulative=True)

    def fetch_positions(self) -> BrokerPositionSnapshot:
        request = SandboxRequest(
            action="fetch_positions", order_id="", idempotency_key="", payload={}
        )
        try:
            response = self._transport.send(request)
            return self._interpret_positions(response)
        except Exception as exc:  # noqa: BLE001 - the gate refuses on a failed read
            raise TransportMalformed(
                f"could not read venue positions: {type(exc).__name__}: {exc}"
            ) from exc

    def fetch_order_inventory(self) -> tuple[BrokerOrderSnapshot, ...]:
        request = SandboxRequest(
            action="fetch_orders", order_id="", idempotency_key="", payload={}
        )
        try:
            response = self._transport.send(request)
        except Exception as exc:  # noqa: BLE001
            raise TransportMalformed(
                f"could not read venue orders: {type(exc).__name__}: {exc}"
            ) from exc
        if not self._ok(response):
            raise TransportMalformed(
                f"venue returned status {response.status_code} for an order list"
            )
        raw = response.body.get("orders")
        if not isinstance(raw, (list, tuple)):
            raise TransportMalformed("order list response has no 'orders' array")
        snapshots: list[BrokerOrderSnapshot] = []
        for record in raw:
            if not isinstance(record, Mapping):
                raise TransportMalformed("an entry in the order list is not an object")
            snapshots.append(self._interpret_record(record))
        return tuple(snapshots)

    # -- translation helpers ----------------------------------------------

    def _ok(self, response: SandboxResponse) -> bool:
        return 200 <= response.status_code < 300

    def _interpret_placement(self, order: Order, response: SandboxResponse) -> BrokerAck:
        """Turn a placement response into an ack, or UNCERTAIN."""
        if response.status_code == 429:
            return BrokerAck(
                AckOutcome.UNCERTAIN, message="venue rate-limited the placement"
            )
        if not self._ok(response):
            # A 4xx that is not 429 is the venue refusing the order outright.
            if 400 <= response.status_code < 500:
                return BrokerAck(
                    AckOutcome.REJECTED,
                    message=f"venue rejected the placement with "
                    f"status {response.status_code}",
                )
            # A 5xx says the venue had a problem, not that it refused us.
            return BrokerAck(
                AckOutcome.UNCERTAIN,
                message=f"venue returned status {response.status_code}; "
                "whether the order was accepted is unknown",
            )

        try:
            status_word = self._require_str(response.body, self._STATUS_FIELD)
        except TransportMalformed as exc:
            return BrokerAck(AckOutcome.UNCERTAIN, message=str(exc))

        status = _STATUS_WORDS.get(status_word.lower())
        if status is None:
            return BrokerAck(
                AckOutcome.UNCERTAIN,
                message=f"unrecognised order status {status_word!r}; "
                "refusing to guess what it means",
            )

        try:
            broker_order_id = self._optional_str(response.body, self._ID_FIELD)
        except TransportMalformed as exc:
            # A response whose identifier we cannot read tells us nothing about
            # which order it refers to. That is uncertainty, not a rejection --
            # the same shape as a body we could not parse at all.
            return BrokerAck(AckOutcome.UNCERTAIN, message=str(exc))

        if status is BrokerOrderStatus.OPEN:
            return BrokerAck(
                AckOutcome.ACCEPTED,
                broker_order_id=broker_order_id or self._next_id(),
                message="accepted",
            )
        if status is BrokerOrderStatus.REJECTED:
            return BrokerAck(
                AckOutcome.REJECTED,
                broker_order_id=broker_order_id,
                message="rejected by venue",
            )

        # FILLED, and only FILLED, may come back from a placement as a fill.
        # A partially-filled placement is deliberately *not* reported as a fill
        # here: the ack shape carries one quantity and one price, and a partial
        # is a cumulative snapshot rather than a single trade. It is reported as
        # accepted, and the follow-up query -- or a sweep -- finds the real
        # cumulative state, which is the path that cannot double-book.
        if status is BrokerOrderStatus.FILLED:
            try:
                return self._filled_ack(order, response, broker_order_id)
            except TransportMalformed as exc:
                return BrokerAck(AckOutcome.UNCERTAIN, message=str(exc))

        return BrokerAck(
            AckOutcome.ACCEPTED,
            broker_order_id=broker_order_id or self._next_id(),
            message=f"venue reports {status.value}; syncing will apply it",
        )

    def _filled_ack(
        self,
        order: Order,
        response: SandboxResponse,
        broker_order_id: str | None,
    ) -> BrokerAck:
        """Build a FILLED ack, refusing anything inconsistent with the order."""
        quantity = self._require_quantity(
            response.body, self._FILLED_FIELD, asset=order.intent.quantity.asset
        )
        price = self._require_price(response.body, self._PRICE_FIELD)
        if quantity.is_zero:
            raise TransportMalformed(
                "venue reported a fill of zero quantity; that is not a fill"
            )
        if quantity > order.intent.quantity:
            # An overfill is the venue disagreeing about the order's size. It is
            # not something to book and hope: the local risk view would be
            # understated, so it becomes an unknown outcome and an operator call.
            raise TransportMalformed(
                f"venue reported {quantity} filled against an order for "
                f"{order.intent.quantity}; refusing to book an overfill"
            )
        return BrokerAck(
            AckOutcome.FILLED,
            broker_order_id=broker_order_id or self._next_id(),
            filled_quantity=quantity,
            fill_price=price,
            message="" if quantity == order.intent.quantity else "partial fill",
        )

    def _interpret_state(
        self, order: Order, response: SandboxResponse, *, cumulative: bool
    ) -> BrokerAck:
        """Turn a state query into the cumulative snapshot ``sync_order`` wants."""
        status_word = self._require_str(response.body, self._STATUS_FIELD)
        status = _STATUS_WORDS.get(status_word.lower())
        if status is None:
            raise TransportMalformed(
                f"unrecognised order status {status_word!r}; refusing to guess"
            )

        broker_order_id = self._optional_str(response.body, self._ID_FIELD)
        if status in (BrokerOrderStatus.CANCELED, BrokerOrderStatus.EXPIRED):
            # Terminal and unfilled. The venue no longer holds it, which for a
            # reconciliation read is the same answer as having no record.
            return BrokerAck(
                AckOutcome.REJECTED,
                broker_order_id=broker_order_id,
                message=f"venue reports the order {status.value}",
            )
        if status is BrokerOrderStatus.REJECTED:
            return BrokerAck(
                AckOutcome.REJECTED,
                broker_order_id=broker_order_id,
                message="rejected by venue",
            )
        if status is BrokerOrderStatus.OPEN:
            return BrokerAck(
                AckOutcome.ACCEPTED,
                broker_order_id=broker_order_id,
                message="accepted",
            )

        # FILLED or PARTIALLY_FILLED: a cumulative state, even if it came back
        # from a placement. Only the delta against what is booked is ever
        # applied, so reporting the full quantity here is safe.
        quantity = self._require_quantity(
            response.body, self._FILLED_FIELD, asset=order.intent.quantity.asset
        )
        if quantity.is_zero:
            # A "filled" status with nothing filled is a contradiction, not a
            # resting order.
            raise TransportMalformed(
                f"venue reports status {status.value} with zero filled quantity"
            )
        price = self._require_price(response.body, self._PRICE_FIELD)
        return BrokerAck(
            AckOutcome.FILLED,
            broker_order_id=broker_order_id,
            filled_quantity=quantity,
            fill_price=price,
            message="partial fill" if quantity < order.intent.quantity else "",
        )

    def _interpret_record(self, record: Mapping[str, object]) -> BrokerOrderSnapshot:
        """Turn one order-list entry into a validated snapshot, or raise."""
        broker_order_id = self._require_str(record, self._ID_FIELD)
        symbol = self._require_str(record, self._SYMBOL_FIELD)
        side = self._require_str(record, self._SIDE_FIELD).lower()
        if side not in ("buy", "sell"):
            raise TransportMalformed(f"order {broker_order_id} has side {side!r}")

        status_word = self._require_str(record, self._STATUS_FIELD).lower()
        status = _STATUS_WORDS.get(status_word)
        if status is None:
            raise TransportMalformed(
                f"order {broker_order_id} has unrecognised status {status_word!r}"
            )

        asset = self._optional_str(record, self._ASSET_FIELD) or symbol.removesuffix(
            self._quote_currency.code
        )
        ordered = self._require_quantity(record, self._ORDERED_FIELD, asset=asset)
        filled_raw = record.get(self._FILLED_FIELD)
        filled = (
            Quantity.zero(asset)
            if filled_raw in (None, "")
            else self._require_quantity(record, self._FILLED_FIELD, asset=asset)
        )
        price = None
        if filled.amount > 0:
            price = self._require_price(record, self._PRICE_FIELD)

        return BrokerOrderSnapshot(
            broker_order_id=broker_order_id,
            symbol=symbol,
            side=side,
            ordered_quantity=ordered,
            filled_quantity=filled,
            status=status,
            idempotency_key=self._optional_str(record, "client_order_id"),
            fill_price=price,
        )

    def _interpret_positions(self, response: SandboxResponse) -> BrokerPositionSnapshot:
        if not self._ok(response):
            raise TransportMalformed(
                f"venue returned status {response.status_code} for positions"
            )
        raw = response.body.get("positions")
        if not isinstance(raw, (list, tuple)):
            raise TransportMalformed("position response has no 'positions' array")
        positions: dict[str, Quantity] = {}
        for entry in raw:
            if not isinstance(entry, Mapping):
                raise TransportMalformed("a position entry is not an object")
            symbol = self._require_str(entry, self._SYMBOL_FIELD)
            asset = self._optional_str(entry, self._ASSET_FIELD) or symbol.removesuffix(
                self._quote_currency.code
            )
            amount = self._require_decimal(entry, "quantity")
            positions[symbol] = Quantity(amount, asset)
        return BrokerPositionSnapshot(positions)

    # -- field readers -----------------------------------------------------

    def _require_str(self, body: Mapping[str, object], field: str) -> str:
        value = body.get(field)
        if not isinstance(value, str) or not value.strip():
            raise TransportMalformed(f"response is missing a usable {field!r}")
        return value

    def _optional_str(self, body: Mapping[str, object], field: str) -> str | None:
        value = body.get(field)
        if value is None:
            return None
        if not isinstance(value, str):
            raise TransportMalformed(f"{field!r} must be a string, got {type(value).__name__}")
        return value or None

    def _require_decimal(self, body: Mapping[str, object], field: str) -> Decimal:
        value = body.get(field)
        if isinstance(value, Decimal):
            return value
        if isinstance(value, bool) or isinstance(value, float):
            # A float from a venue is a number we cannot trust to be exact
            # (INVARIANT 8). Refuse it rather than converting and hoping.
            raise TransportMalformed(
                f"{field!r} arrived as {type(value).__name__}, which cannot "
                "represent money exactly"
            )
        if isinstance(value, int):
            return Decimal(value)
        if isinstance(value, str):
            try:
                return Decimal(value.strip())
            except Exception as exc:  # noqa: BLE001 - any parse failure is malformed
                raise TransportMalformed(f"{field!r} is not a number: {value!r}") from exc
        raise TransportMalformed(f"response is missing a usable {field!r}")

    def _require_quantity(
        self, body: Mapping[str, object], field: str, *, asset: str
    ) -> Quantity:
        return Quantity(self._require_decimal(body, field), asset)

    def _require_price(self, body: Mapping[str, object], field: str) -> Price:
        amount = self._require_decimal(body, field)
        if amount <= 0:
            raise TransportMalformed(f"{field!r} must be positive, got {amount}")
        return Price(amount, self._quote_currency)

    def _next_id(self) -> str:
        self._seq += 1
        return f"{self._id_prefix}-{self._seq:06d}"
