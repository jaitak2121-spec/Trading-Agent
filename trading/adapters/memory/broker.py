"""A simulated venue.

The simulator exists to be *hostile*, not convenient. Real venues acknowledge
late, answer ambiguously, drop connections mid-request, and disagree with us
about positions -- so this one can be told to do all of those on demand:

* :meth:`SimulatedBroker.script` queues an exact ack, including ``UNCERTAIN``.
* ``lands_at_venue=True`` on a scripted ``UNCERTAIN`` models the genuinely
  dangerous case: the venue *did* receive the order but we never found out.
* :meth:`SimulatedBroker.raise_on_next` makes a placement blow up *after* the
  token is spent, the way a socket dies after the bytes leave.
* :meth:`SimulatedBroker.set_venue_position` forces a disagreement with the
  local ledger, so reconciliation has something to catch (INVARIANT 6).

It is also a test instrument. Every placement attempt is recorded against its
idempotency key, so :attr:`SimulatedBroker.duplicate_keys` is direct evidence
about INVARIANT 12: if the gateway ever lets the same key through twice, the
venue -- not an assertion about the gateway's internals -- says so.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass
from typing import Mapping

from ...core.authz import ExecutionToken
from ...core.clock import Clock
from ...core.money import Price, Quantity
from ...core.orders import Order, OrderSide
from ...ports.broker import AckOutcome, BrokerAck, BrokerPort, BrokerPositionSnapshot

__all__ = ["BrokerFailure", "ScriptedAck", "SimulatedBroker"]


class BrokerFailure(Exception):
    """A transport-level failure. Says nothing about whether the venue got it."""


@dataclass(frozen=True, slots=True)
class ScriptedAck:
    """A queued response.

    ``lands_at_venue`` decouples what we *learn* from what actually *happened*:
    an ``UNCERTAIN`` ack that landed leaves a real order at the venue, which is
    exactly the state reconciliation must discover.
    """

    ack: BrokerAck
    lands_at_venue: bool = False


class SimulatedBroker(BrokerPort):
    """An in-process venue. No network, no credentials, no clock of its own."""

    def __init__(
        self,
        *,
        clock: Clock,
        default_outcome: AckOutcome = AckOutcome.ACCEPTED,
        fill_prices: Mapping[str, Price] | None = None,
    ) -> None:
        if not isinstance(default_outcome, AckOutcome):
            raise TypeError("default_outcome must be an AckOutcome")
        for symbol, price in (fill_prices or {}).items():
            if not isinstance(price, Price):
                raise TypeError(f"fill price for {symbol} must be a Price")
        self._clock = clock
        self._default_outcome = default_outcome
        self._fill_prices: dict[str, Price] = dict(fill_prices or {})
        # Reentrant: _record_at_venue and _apply_venue_fill both take it.
        self._lock = threading.RLock()
        self._script: list[ScriptedAck] = []
        self._raise_next: BrokerFailure | None = None
        self._seq = 0
        # Every attempt that got past token consumption, in order.
        self._attempts: list[tuple[str, str]] = []
        self._key_counts: dict[str, int] = {}
        self._duplicate_keys: set[str] = set()
        # What the venue believes exists, keyed by idempotency key.
        self._venue_orders: dict[str, BrokerAck] = {}
        self._venue_positions: dict[str, Quantity] = {}

    # -- test controls ----------------------------------------------------

    def script(self, ack: BrokerAck, *, lands_at_venue: bool = False) -> None:
        """Queue ``ack`` as the response to the next placement."""
        if not isinstance(ack, BrokerAck):
            raise TypeError("script() takes a BrokerAck")
        with self._lock:
            self._script.append(ScriptedAck(ack, lands_at_venue))

    def raise_on_next(self, message: str = "connection reset") -> None:
        """Make the next placement raise after the token is already spent."""
        with self._lock:
            self._raise_next = BrokerFailure(message)

    def set_venue_position(self, symbol: str, quantity: Quantity) -> None:
        """Force the venue's view of a position, to create a mismatch."""
        if not isinstance(quantity, Quantity):
            raise TypeError("quantity must be a Quantity")
        with self._lock:
            self._venue_positions[symbol] = quantity

    def set_fill_price(self, symbol: str, price: Price) -> None:
        if not isinstance(price, Price):
            raise TypeError("price must be a Price")
        with self._lock:
            self._fill_prices[symbol] = price

    # -- observations -----------------------------------------------------

    @property
    def attempts(self) -> list[tuple[str, str]]:
        """``(order_id, idempotency_key)`` for every placement that reached us."""
        with self._lock:
            return list(self._attempts)

    @property
    def placement_count(self) -> int:
        with self._lock:
            return len(self._attempts)

    @property
    def duplicate_keys(self) -> frozenset[str]:
        """Idempotency keys this venue saw more than once. Must stay empty."""
        with self._lock:
            return frozenset(self._duplicate_keys)

    def times_seen(self, idempotency_key: str) -> int:
        with self._lock:
            return self._key_counts.get(idempotency_key, 0)

    @property
    def resting_keys(self) -> frozenset[str]:
        """Keys the venue actually holds an order for."""
        with self._lock:
            return frozenset(self._venue_orders)

    # -- BrokerPort -------------------------------------------------------

    def place_order(self, order: Order, *, token: ExecutionToken) -> BrokerAck:
        # Token first, unconditionally. Nothing below this line runs for a
        # caller without gateway-minted authority (INVARIANT 3), and a
        # replayed token cannot get here twice (INVARIANT 12).
        token.consume(order_id=order.order_id, clock=self._clock)

        key = order.idempotency_key
        with self._lock:
            self._attempts.append((order.order_id, key))
            seen = self._key_counts.get(key, 0) + 1
            self._key_counts[key] = seen
            if seen > 1:
                self._duplicate_keys.add(key)

            failure, self._raise_next = self._raise_next, None
            scripted = self._script.pop(0) if self._script else None

        if scripted is not None:
            if scripted.lands_at_venue:
                self._record_at_venue(order, scripted.ack)
            return scripted.ack

        if failure is not None:
            # The request left; the answer never came back. Deliberately
            # ambiguous: the order may or may not be resting at the venue.
            raise failure

        ack = self._build_default_ack(order)
        if ack.outcome in (AckOutcome.ACCEPTED, AckOutcome.FILLED):
            self._record_at_venue(order, ack)
        return ack

    def cancel_order(self, order: Order) -> BrokerAck:
        with self._lock:
            self._venue_orders.pop(order.idempotency_key, None)
        return BrokerAck(AckOutcome.ACCEPTED, message="canceled")

    def fetch_order_state(self, order: Order) -> BrokerAck:
        """The query that resolves UNKNOWN.

        A venue with no record is reported as ``REJECTED`` -- the order does not
        exist, so treating it as never having happened is the honest reading.
        """
        with self._lock:
            resting = self._venue_orders.get(order.idempotency_key)
        if resting is None:
            return BrokerAck(
                AckOutcome.REJECTED, message="venue has no record of this order"
            )
        return resting

    def fetch_positions(self) -> BrokerPositionSnapshot:
        with self._lock:
            return BrokerPositionSnapshot(dict(self._venue_positions))

    # -- internals --------------------------------------------------------

    def _build_default_ack(self, order: Order) -> BrokerAck:
        outcome = self._default_outcome
        if outcome is AckOutcome.FILLED:
            price = self._fill_prices.get(order.symbol)
            if price is None:
                # No price to fill at; acknowledging is the honest answer.
                return BrokerAck(
                    AckOutcome.ACCEPTED,
                    broker_order_id=self._next_id(),
                    message="accepted (no simulated fill price)",
                )
            return BrokerAck(
                AckOutcome.FILLED,
                broker_order_id=self._next_id(),
                filled_quantity=order.intent.quantity,
                fill_price=price,
            )
        if outcome is AckOutcome.UNCERTAIN:
            return BrokerAck(AckOutcome.UNCERTAIN, message="simulated timeout")
        if outcome is AckOutcome.REJECTED:
            return BrokerAck(AckOutcome.REJECTED, message="simulated rejection")
        return BrokerAck(AckOutcome.ACCEPTED, broker_order_id=self._next_id())

    def _record_at_venue(self, order: Order, ack: BrokerAck) -> None:
        stored = ack
        if ack.outcome is AckOutcome.UNCERTAIN:
            # It landed, so from the venue's side it is a live order. A later
            # fetch_order_state must reveal that, not repeat our uncertainty.
            stored = BrokerAck(
                AckOutcome.ACCEPTED,
                broker_order_id=ack.broker_order_id or self._next_id(),
                message="landed despite an uncertain ack",
            )
        with self._lock:
            self._venue_orders[order.idempotency_key] = stored
        if stored.outcome is AckOutcome.FILLED and stored.filled_quantity is not None:
            self._apply_venue_fill(order.symbol, order.side, stored.filled_quantity)

    def _apply_venue_fill(
        self, symbol: str, side: OrderSide, quantity: Quantity
    ) -> None:
        signed = Quantity(quantity.amount * side.sign, quantity.asset)
        with self._lock:
            current = self._venue_positions.get(symbol)
            self._venue_positions[symbol] = (
                signed if current is None else current + signed
            )

    def _next_id(self) -> str:
        with self._lock:
            self._seq += 1
            return f"SIM-{self._seq:06d}"
