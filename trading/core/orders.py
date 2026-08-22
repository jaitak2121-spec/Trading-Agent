"""Order state machine, order intents, and the order store.

Relevant invariants:

* INVARIANT 3 -- :class:`OrderIntent` is what strategies produce. It is an
  inert value object: no broker reference, no execution method, no capability.
  Turning an intent into an :class:`Order` is the gateway's job.
* INVARIANT 5 -- :data:`OrderState.UNKNOWN` is a first-class state, not an
  error code. An order whose fate we cannot determine sits in UNKNOWN, and
  :meth:`OrderStore.unknown_orders` reports it so new orders can be blocked.
* INVARIANT 12 -- an order carries an idempotency key derived from the content
  of its intent, so the same logical decision cannot become two orders.

The most important design choice here is that **UNKNOWN can only be left via
reconciliation**. Every other transition is an ordinary state change;
``UNKNOWN -> anything`` requires ``via_reconciliation=True``, which callers can
only justify after actually querying the venue. This is what stops the tempting
"assume it failed and retry" shortcut that creates double positions.
"""

from __future__ import annotations

import decimal
import hashlib
import json
import threading
import uuid
from dataclasses import dataclass
from decimal import ROUND_HALF_EVEN, Decimal
from enum import Enum
from typing import Final, Mapping, Sequence

from .clock import Clock
from .errors import (
    InvalidOrderTransition,
    ReconciliationRequired,
    SafetyViolation,
)
from .money import (
    FINANCIAL_CONTEXT,
    Currency,
    Money,
    Price,
    Quantity,
    canonical_decimal_text,
)

#: Prices carry up to this many decimal places; see :class:`~trading.core.money.Price`.
_PRICE_SCALE: Final = 12

__all__ = [
    "OrderSide",
    "OrderType",
    "OrderState",
    "TERMINAL_STATES",
    "ORDER_TRANSITIONS",
    "OrderIntent",
    "OrderTransition",
    "Order",
    "OrderStore",
]


class OrderSide(Enum):
    BUY = "buy"
    SELL = "sell"

    @property
    def sign(self) -> int:
        """+1 for BUY, -1 for SELL. Direction lives here, never in a quantity."""
        return 1 if self is OrderSide.BUY else -1


class OrderType(Enum):
    MARKET = "market"
    LIMIT = "limit"


class OrderState(Enum):
    #: Created locally. Nothing has been sent anywhere.
    DRAFT = "draft"
    #: Sent to the venue, no acknowledgement yet. The dangerous window.
    PENDING_NEW = "pending_new"
    #: Venue acknowledged the order.
    ACCEPTED = "accepted"
    PARTIALLY_FILLED = "partially_filled"
    FILLED = "filled"
    REJECTED = "rejected"
    CANCELED = "canceled"
    EXPIRED = "expired"
    #: We do not know whether this order exists at the venue. Blocks new
    #: orders until reconciled (INVARIANT 5).
    UNKNOWN = "unknown"

    @property
    def is_terminal(self) -> bool:
        return self in TERMINAL_STATES

    @property
    def is_open(self) -> bool:
        """Whether the order may still consume or create exposure."""
        return self in (
            OrderState.PENDING_NEW,
            OrderState.ACCEPTED,
            OrderState.PARTIALLY_FILLED,
        )


TERMINAL_STATES: Final[frozenset[OrderState]] = frozenset(
    {
        OrderState.FILLED,
        OrderState.REJECTED,
        OrderState.CANCELED,
        OrderState.EXPIRED,
    }
)

#: The complete transition table. Anything absent is forbidden.
ORDER_TRANSITIONS: Final[Mapping[OrderState, frozenset[OrderState]]] = {
    OrderState.DRAFT: frozenset({OrderState.PENDING_NEW, OrderState.REJECTED}),
    OrderState.PENDING_NEW: frozenset(
        {
            OrderState.ACCEPTED,
            OrderState.PARTIALLY_FILLED,
            OrderState.FILLED,
            OrderState.REJECTED,
            OrderState.CANCELED,
            OrderState.UNKNOWN,
        }
    ),
    OrderState.ACCEPTED: frozenset(
        {
            OrderState.PARTIALLY_FILLED,
            OrderState.FILLED,
            OrderState.CANCELED,
            OrderState.EXPIRED,
            OrderState.REJECTED,
            OrderState.UNKNOWN,
        }
    ),
    OrderState.PARTIALLY_FILLED: frozenset(
        {
            OrderState.PARTIALLY_FILLED,  # further fills
            OrderState.FILLED,
            OrderState.CANCELED,
            OrderState.EXPIRED,
            OrderState.UNKNOWN,
        }
    ),
    OrderState.FILLED: frozenset(),
    OrderState.REJECTED: frozenset(),
    OrderState.CANCELED: frozenset(),
    OrderState.EXPIRED: frozenset(),
    # Reachable only with via_reconciliation=True; see Order.transition_to.
    OrderState.UNKNOWN: frozenset(
        {
            OrderState.ACCEPTED,
            OrderState.PARTIALLY_FILLED,
            OrderState.FILLED,
            OrderState.CANCELED,
            OrderState.REJECTED,
            OrderState.EXPIRED,
        }
    ),
}


@dataclass(frozen=True, slots=True)
class OrderIntent:
    """What a strategy produces: a request, not an action.

    Deliberately inert. It holds no broker, no session, no token, and no
    method that could reach a venue. The only thing a strategy can do with an
    intent is hand it to something more privileged (INVARIANT 3).

    ``signal_id`` identifies the *decision* that produced this intent. Two
    intents with the same signal are the same order, which is what makes
    content-derived idempotency keys meaningful (INVARIANT 12).
    """

    strategy_id: str
    signal_id: str
    symbol: str
    side: OrderSide
    quantity: Quantity
    order_type: OrderType = OrderType.MARKET
    limit_price: Price | None = None

    def __post_init__(self) -> None:
        for name in ("strategy_id", "signal_id", "symbol"):
            value = getattr(self, name)
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"{name} must be a non-empty string")
        if not isinstance(self.side, OrderSide):
            raise TypeError("side must be an OrderSide")
        if not isinstance(self.order_type, OrderType):
            raise TypeError("order_type must be an OrderType")
        if not isinstance(self.quantity, Quantity):
            raise TypeError("quantity must be a Quantity")
        if not self.quantity.is_positive:
            raise ValueError(
                f"order quantity must be strictly positive, got {self.quantity}; "
                "direction is carried by side, never by a negative quantity"
            )
        if self.order_type is OrderType.LIMIT:
            if self.limit_price is None:
                raise ValueError("a LIMIT order requires a limit_price")
            if not isinstance(self.limit_price, Price):
                raise TypeError("limit_price must be a Price")
        elif self.limit_price is not None:
            raise ValueError(
                f"a {self.order_type.value.upper()} order must not carry a limit_price"
            )

    @property
    def idempotency_key(self) -> str:
        """Deterministic key derived from the intent's content.

        Content-addressed on purpose. A random key would make duplicate
        detection useless, because a retry would produce a different key and
        sail straight past the registry.
        """
        payload = {
            "strategy_id": self.strategy_id,
            "signal_id": self.signal_id,
            "symbol": self.symbol,
            "side": self.side.value,
            # Canonical, scale-independent: 0.5 and 0.50 are the same order.
            "quantity": canonical_decimal_text(self.quantity.amount),
            "asset": self.quantity.asset,
            "order_type": self.order_type.value,
            "limit_price": (
                canonical_decimal_text(self.limit_price.amount)
                if self.limit_price
                else None
            ),
            "limit_currency": self.limit_price.currency.code if self.limit_price else None,
        }
        body = json.dumps(payload, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(body.encode("utf-8")).hexdigest()

    def as_details(self) -> dict[str, object]:
        return {
            "strategy_id": self.strategy_id,
            "signal_id": self.signal_id,
            "symbol": self.symbol,
            "side": self.side.value,
            "quantity": str(self.quantity.amount),
            "order_type": self.order_type.value,
            "limit_price": str(self.limit_price.amount) if self.limit_price else None,
            "idempotency_key": self.idempotency_key,
        }


@dataclass(frozen=True, slots=True)
class OrderTransition:
    """One immutable entry in an order's history."""

    from_state: OrderState
    to_state: OrderState
    at: str
    reason: str
    via_reconciliation: bool = False

    def as_details(self) -> dict[str, object]:
        return {
            "from": self.from_state.value,
            "to": self.to_state.value,
            "at": self.at,
            "reason": self.reason,
            "via_reconciliation": self.via_reconciliation,
        }


class Order:
    """A live order and its history.

    Mutable, but only through :meth:`transition_to` and :meth:`apply_fill`, so
    every state change is validated and recorded.
    """

    __slots__ = (
        "_order_id",
        "_intent",
        "_state",
        "_history",
        "_filled_quantity",
        "_notional_total",
        "_quote_currency",
        "_broker_order_id",
        "_created_at",
        "_updated_at",
        "_clock",
        "_lock",
    )

    def __init__(self, intent: OrderIntent, *, clock: Clock, order_id: str | None = None) -> None:
        if not isinstance(intent, OrderIntent):
            raise TypeError("intent must be an OrderIntent")
        self._order_id = order_id or f"ORD-{uuid.uuid4().hex[:16]}"
        self._intent = intent
        self._state = OrderState.DRAFT
        self._history: list[OrderTransition] = []
        self._filled_quantity = Quantity.zero(intent.quantity.asset)
        self._notional_total = Decimal(0)
        self._quote_currency: Currency | None = None
        self._broker_order_id: str | None = None
        self._clock = clock
        self._created_at = clock.now().isoformat()
        self._updated_at = self._created_at
        self._lock = threading.RLock()

    # -- identity ----------------------------------------------------------
    @property
    def order_id(self) -> str:
        return self._order_id

    @property
    def intent(self) -> OrderIntent:
        return self._intent

    @property
    def idempotency_key(self) -> str:
        return self._intent.idempotency_key

    @property
    def symbol(self) -> str:
        return self._intent.symbol

    @property
    def side(self) -> OrderSide:
        return self._intent.side

    @property
    def broker_order_id(self) -> str | None:
        with self._lock:
            return self._broker_order_id

    def attach_broker_order_id(self, broker_order_id: str) -> None:
        with self._lock:
            if self._broker_order_id is not None and self._broker_order_id != broker_order_id:
                raise SafetyViolation(
                    f"order {self._order_id} already has broker id "
                    f"{self._broker_order_id}; refusing to rebind to {broker_order_id}"
                )
            self._broker_order_id = broker_order_id

    # -- state -------------------------------------------------------------
    @property
    def state(self) -> OrderState:
        with self._lock:
            return self._state

    @property
    def history(self) -> Sequence[OrderTransition]:
        with self._lock:
            return list(self._history)

    @property
    def filled_quantity(self) -> Quantity:
        with self._lock:
            return self._filled_quantity

    @property
    def remaining_quantity(self) -> Quantity:
        with self._lock:
            return self._intent.quantity - self._filled_quantity

    @property
    def average_fill_price(self) -> Price | None:
        """Volume-weighted average fill price, derived from the exact notional.

        Derived rather than stored so that partial fills cannot accumulate
        rounding error into the average.
        """
        with self._lock:
            if self._quote_currency is None or self._filled_quantity.is_zero:
                return None
            with decimal.localcontext(FINANCIAL_CONTEXT):
                average = self._notional_total / self._filled_quantity.amount
                shown = average.quantize(
                    Decimal(1).scaleb(-_PRICE_SCALE), rounding=ROUND_HALF_EVEN
                )
            return Price(shown, self._quote_currency)

    @property
    def is_unknown(self) -> bool:
        with self._lock:
            return self._state is OrderState.UNKNOWN

    @property
    def is_open(self) -> bool:
        with self._lock:
            return self._state.is_open

    def transition_to(
        self,
        target: OrderState,
        *,
        reason: str,
        via_reconciliation: bool = False,
    ) -> OrderState:
        """Move to ``target`` or raise.

        Leaving :data:`OrderState.UNKNOWN` requires ``via_reconciliation=True``.
        """
        if not isinstance(target, OrderState):
            raise TypeError("target must be an OrderState")
        with self._lock:
            current = self._state

            if current is OrderState.UNKNOWN and not via_reconciliation:
                raise ReconciliationRequired(
                    f"order {self._order_id} is in UNKNOWN state and can only "
                    f"leave it through reconciliation; refusing "
                    f"{current.value} -> {target.value} (INVARIANT 5)"
                )

            if target not in ORDER_TRANSITIONS[current]:
                allowed = sorted(s.value for s in ORDER_TRANSITIONS[current])
                raise InvalidOrderTransition(
                    f"order {self._order_id}: {current.value} -> {target.value} "
                    f"is not a valid transition; allowed: {allowed or ['(terminal)']}"
                )

            self._state = target
            self._updated_at = self._clock.now().isoformat()
            self._history.append(
                OrderTransition(
                    from_state=current,
                    to_state=target,
                    at=self._updated_at,
                    reason=reason,
                    via_reconciliation=via_reconciliation,
                )
            )
            return target

    def mark_unknown(self, *, reason: str) -> None:
        """Move to UNKNOWN. Idempotent: already-unknown stays unknown."""
        with self._lock:
            if self._state is OrderState.UNKNOWN:
                return
            self.transition_to(OrderState.UNKNOWN, reason=reason)

    # -- fills -------------------------------------------------------------
    def apply_fill(
        self,
        quantity: Quantity,
        price: Price,
        *,
        reason: str = "fill",
        via_reconciliation: bool = False,
    ) -> OrderState:
        """Record a (partial) fill and advance the state accordingly.

        Overfills are refused: a venue reporting more filled than ordered is a
        signal that our view of the world is wrong, and silently accepting it
        would corrupt every downstream exposure calculation.
        """
        if not isinstance(quantity, Quantity):
            raise TypeError("fill quantity must be a Quantity")
        if not isinstance(price, Price):
            raise TypeError("fill price must be a Price")
        if not quantity.is_positive:
            raise ValueError("fill quantity must be strictly positive")

        with self._lock:
            if quantity.asset != self._intent.quantity.asset:
                raise SafetyViolation(
                    f"fill asset {quantity.asset} does not match order asset "
                    f"{self._intent.quantity.asset}"
                )
            if (
                self._quote_currency is not None
                and price.currency != self._quote_currency
            ):
                raise SafetyViolation(
                    f"fill currency {price.currency.code} does not match earlier "
                    f"fills in {self._quote_currency.code} on order {self._order_id}"
                )
            new_filled = self._filled_quantity + quantity
            if new_filled > self._intent.quantity:
                raise SafetyViolation(
                    f"overfill refused on order {self._order_id}: filled "
                    f"{new_filled} would exceed ordered {self._intent.quantity}"
                )

            # Accumulate the exact notional rather than an averaged price, so
            # repeated partial fills cannot accrete rounding drift.
            with decimal.localcontext(FINANCIAL_CONTEXT):
                self._notional_total += price.amount * quantity.amount
            self._filled_quantity = new_filled
            self._quote_currency = price.currency

            target = (
                OrderState.FILLED
                if new_filled == self._intent.quantity
                else OrderState.PARTIALLY_FILLED
            )
            return self.transition_to(
                target, reason=reason, via_reconciliation=via_reconciliation
            )

    def filled_notional(self) -> Money | None:
        """Cash value of everything filled so far, or None if nothing filled."""
        with self._lock:
            if self._quote_currency is None or self._filled_quantity.is_zero:
                return None
            with decimal.localcontext(FINANCIAL_CONTEXT):
                return Money.rounded(
                    self._notional_total,
                    self._quote_currency,
                    rounding=ROUND_HALF_EVEN,
                )

    # -- rendering ---------------------------------------------------------
    def as_details(self) -> dict[str, object]:
        average = self.average_fill_price
        with self._lock:
            return {
                "order_id": self._order_id,
                "state": self._state.value,
                "symbol": self._intent.symbol,
                "side": self._intent.side.value,
                "ordered_quantity": str(self._intent.quantity.amount),
                "filled_quantity": str(self._filled_quantity.amount),
                "average_fill_price": str(average.amount) if average else None,
                "broker_order_id": self._broker_order_id,
                "idempotency_key": self._intent.idempotency_key,
                "created_at": self._created_at,
                "updated_at": self._updated_at,
            }

    def __repr__(self) -> str:
        return (
            f"Order({self._order_id}, {self._intent.symbol}, "
            f"{self._intent.side.value}, state={self._state.value})"
        )


class OrderStore:
    """In-memory order repository.

    This is the seam a PostgreSQL-backed repository replaces in a later stage;
    the safety core only depends on the methods below.
    """

    def __init__(self) -> None:
        self._by_id: dict[str, Order] = {}
        self._by_key: dict[str, str] = {}
        self._lock = threading.RLock()

    def add(self, order: Order) -> Order:
        with self._lock:
            if order.order_id in self._by_id:
                raise SafetyViolation(f"order {order.order_id} is already stored")
            self._by_id[order.order_id] = order
            self._by_key[order.idempotency_key] = order.order_id
            return order

    def get(self, order_id: str) -> Order:
        with self._lock:
            if order_id not in self._by_id:
                raise KeyError(f"no order with id {order_id!r}")
            return self._by_id[order_id]

    def find_by_idempotency_key(self, key: str) -> Order | None:
        with self._lock:
            order_id = self._by_key.get(key)
            return self._by_id[order_id] if order_id else None

    def all_orders(self) -> list[Order]:
        with self._lock:
            return list(self._by_id.values())

    def unknown_orders(self) -> list[Order]:
        """Every order whose fate is undetermined (INVARIANT 5)."""
        return [o for o in self.all_orders() if o.is_unknown]

    def open_orders(self) -> list[Order]:
        return [o for o in self.all_orders() if o.is_open]

    def has_unknown_orders(self) -> bool:
        return bool(self.unknown_orders())

    def __len__(self) -> int:
        with self._lock:
            return len(self._by_id)
