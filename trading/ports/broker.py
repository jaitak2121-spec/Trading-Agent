"""The broker port.

The kernel's only route to a venue. Two things about the shape of this interface
are load-bearing for safety:

**A token is required to place an order.** :meth:`BrokerPort.place_order` takes
an :class:`~trading.core.authz.ExecutionToken`, which only the execution gateway
can mint. A strategy holding a broker reference still cannot place an order
(INVARIANT 3).

**Ack is not confirmation.** :class:`BrokerAck` carries an explicit
:attr:`BrokerAck.outcome`, and ``UNCERTAIN`` is a first-class answer. An adapter
that times out, loses its connection, or gets an ambiguous response must return
``UNCERTAIN`` rather than guessing -- the gateway turns that into an UNKNOWN
order state (INVARIANT 5). An adapter that raises rather than answering is also
treated as uncertain by the gateway, because a raised exception says nothing
about whether the request reached the venue.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import Enum
from typing import Mapping

from ..core.authz import ExecutionToken
from ..core.money import Price, Quantity
from ..core.orders import Order

__all__ = [
    "AckOutcome",
    "BrokerAck",
    "BrokerOrderInventoryPort",
    "BrokerOrderSnapshot",
    "BrokerOrderStatus",
    "BrokerPositionSnapshot",
    "BrokerPort",
]


class AckOutcome(Enum):
    """What the venue said, including the honest "I do not know"."""

    #: The venue accepted the order.
    ACCEPTED = "accepted"
    #: The venue rejected it outright. Safe to treat as never having existed.
    REJECTED = "rejected"
    #: The venue filled it immediately.
    FILLED = "filled"
    #: We do not know whether the venue received it. Never retry on this;
    #: reconcile (INVARIANT 5, INVARIANT 12).
    UNCERTAIN = "uncertain"


class BrokerOrderStatus(Enum):
    """Lifecycle state in a venue-wide order inventory snapshot."""

    OPEN = "open"
    PARTIALLY_FILLED = "partially_filled"
    FILLED = "filled"
    CANCELED = "canceled"
    REJECTED = "rejected"
    EXPIRED = "expired"
    UNCERTAIN = "uncertain"


@dataclass(frozen=True, slots=True)
class BrokerOrderSnapshot:
    """Validated, cumulative observation of one venue order."""

    broker_order_id: str
    symbol: str
    side: str
    ordered_quantity: Quantity
    filled_quantity: Quantity
    status: BrokerOrderStatus
    idempotency_key: str | None = None
    fill_price: Price | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.broker_order_id, str) or not self.broker_order_id.strip():
            raise ValueError("broker_order_id must be a non-empty string")
        if not isinstance(self.symbol, str) or not self.symbol.strip():
            raise ValueError("symbol must be a non-empty string")
        if self.side not in {"buy", "sell"}:
            raise ValueError("side must be 'buy' or 'sell'")
        if not isinstance(self.ordered_quantity, Quantity) or not self.ordered_quantity.is_positive:
            raise ValueError("ordered_quantity must be a positive Quantity")
        if not isinstance(self.filled_quantity, Quantity):
            raise TypeError("filled_quantity must be a Quantity")
        if self.filled_quantity.amount < 0:
            raise ValueError("filled_quantity must not be negative")
        if self.filled_quantity.asset != self.ordered_quantity.asset:
            raise ValueError("filled_quantity asset must match ordered_quantity")
        if self.filled_quantity > self.ordered_quantity:
            raise ValueError("filled_quantity must not exceed ordered_quantity")
        if not isinstance(self.status, BrokerOrderStatus):
            raise TypeError("status must be a BrokerOrderStatus")
        if self.idempotency_key is not None and (
            not isinstance(self.idempotency_key, str) or not self.idempotency_key.strip()
        ):
            raise ValueError("idempotency_key must be non-empty when supplied")
        if self.fill_price is not None and not isinstance(self.fill_price, Price):
            raise TypeError("fill_price must be a Price")
        if self.status is BrokerOrderStatus.FILLED and (
            self.filled_quantity != self.ordered_quantity or self.fill_price is None
        ):
            raise ValueError("FILLED snapshot must be fully filled and priced")
        if self.status is BrokerOrderStatus.PARTIALLY_FILLED and (
            self.filled_quantity.is_zero or self.filled_quantity == self.ordered_quantity
        ):
            raise ValueError("PARTIALLY_FILLED snapshot must be between zero and full")


@dataclass(frozen=True, slots=True)
class BrokerAck:
    """A venue's response to a placement attempt."""

    outcome: AckOutcome
    broker_order_id: str | None = None
    filled_quantity: Quantity | None = None
    fill_price: Price | None = None
    message: str = ""

    def __post_init__(self) -> None:
        if not isinstance(self.outcome, AckOutcome):
            raise TypeError("outcome must be an AckOutcome")
        if self.outcome is AckOutcome.FILLED and (
            self.filled_quantity is None or self.fill_price is None
        ):
            raise ValueError(
                "a FILLED ack must carry both filled_quantity and fill_price"
            )
        if self.filled_quantity is not None and not isinstance(
            self.filled_quantity, Quantity
        ):
            raise TypeError("filled_quantity must be a Quantity")
        if self.fill_price is not None and not isinstance(self.fill_price, Price):
            raise TypeError("fill_price must be a Price")

    @property
    def is_uncertain(self) -> bool:
        return self.outcome is AckOutcome.UNCERTAIN

    def as_details(self) -> dict[str, object]:
        return {
            "outcome": self.outcome.value,
            "broker_order_id": self.broker_order_id,
            "filled_quantity": (
                str(self.filled_quantity.amount) if self.filled_quantity else None
            ),
            "fill_price": str(self.fill_price.amount) if self.fill_price else None,
            "message": self.message,
        }


@dataclass(frozen=True, slots=True)
class BrokerPositionSnapshot:
    """Positions as the venue sees them, for reconciliation (INVARIANT 6)."""

    positions: Mapping[str, Quantity] = field(default_factory=dict)

    def __post_init__(self) -> None:
        for symbol, quantity in self.positions.items():
            if not isinstance(symbol, str) or not symbol:
                raise ValueError("position symbols must be non-empty strings")
            if not isinstance(quantity, Quantity):
                raise TypeError(
                    f"position for {symbol} must be a Quantity, "
                    f"got {type(quantity).__name__}"
                )


class BrokerPort(ABC):
    """Outbound interface to a trading venue.

    Implementations live in :mod:`trading.adapters`. Stage 1 ships only an
    in-memory simulator; no implementation in this repository performs network
    I/O.
    """

    @abstractmethod
    def place_order(self, order: Order, *, token: ExecutionToken) -> BrokerAck:
        """Submit ``order``, consuming ``token``.

        Implementations must call ``token.consume(order.order_id, clock)`` before
        doing anything else, and must return :attr:`AckOutcome.UNCERTAIN` rather
        than guessing when the outcome is genuinely unknown.
        """

    @abstractmethod
    def cancel_order(self, order: Order) -> BrokerAck:
        """Request cancellation. Cancellation needs no execution token: it can
        only ever reduce exposure."""

    @abstractmethod
    def fetch_order_state(self, order: Order) -> BrokerAck:
        """Ask the venue what happened to ``order``. Used to resolve UNKNOWN."""

    @abstractmethod
    def fetch_positions(self) -> BrokerPositionSnapshot:
        """The venue's view of positions, for reconciliation."""


class BrokerOrderInventoryPort(ABC):
    """A venue that can describe *every* order it holds, not just the ones we ask about.

    :meth:`BrokerPort.fetch_order_state` answers about an order we already know
    of, which means it can never reveal an order we have no local record of --
    a ghost, or one placed out of band. Detecting those needs the venue to
    enumerate its own book, and this is that capability.

    Deliberately separate from :class:`BrokerPort` rather than a method on it,
    for two reasons. It is a capability, not a requirement: a venue adapter with
    no list endpoint is still a perfectly usable broker, and forcing every
    implementation to grow a method it cannot honour would push adapters toward
    inventing an answer. And it is checkable at the type level, so a
    reconciliation coordinator can ask whether the venue supports a *complete*
    sweep and block when it does not, instead of discovering the gap mid-sweep.

    Implementations must be observation-only. A read here must not fill, cancel,
    re-decide, or otherwise change what the venue holds: reconciliation compares
    two readings of the world, and a read that changed one of them would compare
    a reading against itself.
    """

    @abstractmethod
    def fetch_order_inventory(self) -> tuple["BrokerOrderSnapshot", ...]:
        """Every order the venue currently holds, as validated snapshots."""
