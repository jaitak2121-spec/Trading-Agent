"""The paper-trading venue.

A :class:`~trading.ports.broker.BrokerPort` that executes deterministically
against the live book, so that paper results are produced by the same chain, in
the same order, with the same refusals as live ones would be. Nothing here
weakens a gate: the gateway still runs all ten, and
:meth:`PaperBroker.place_order` still consumes an execution token before doing
anything.

What it models
==============

**The side of the book you actually cross.** A buy lifts the ask, a sell hits the
bid -- :meth:`~trading.core.marketdata.Quote.price_for`. Filling at the mid is
the single most common way a paper track record flatters itself, because half the
spread is a real cost on every round trip.

**Slippage, as a signed fraction of that price.** ``slippage_bps`` moves the fill
*against* the order in both directions. Deterministic: a function of side and
size only, with no randomness anywhere, so the same inputs produce the same fill
on every run.

**Depth, and therefore partial fills.** ``depth`` caps what a single placement
can take. An order larger than the cap fills for the cap and leaves the rest
unfilled -- which is the outcome the order-lifecycle work needs and which nothing
in this repository produced before.

**A limit order that does not cross.** It rests, and
:meth:`fetch_order_state` reports it resting. It does not fill later on its own:
there is no path today for a fill discovered outside ``place_order`` to reach the
portfolio except operator reconciliation, and inventing one here would be an
order-lifecycle change made in an adapter.

**A missing or stale quote.** Both are refusals, never a guess. A paper venue
that fills off a frozen feed reports profits that could not have been earned, so
the staleness policy is applied *here* as well as at the risk gate -- the same
number is checked twice by design, on the way in and at the venue.

What it deliberately does not model
===================================

* **Fees and commissions.** Trading cost is expressible only through the fill
  price, because the fill price is the only number the portfolio's cost basis and
  therefore the daily-loss limit ever read. A separate fee field would be money
  that no risk control can see, which is worse than no fee model at all. Use
  ``slippage_bps`` to make crossing cost something.
* **Market impact.** ``depth`` is the size available to one placement, not a
  depleting pool. A venue whose book thins as you trade it is an impact model,
  and an impact model that is wrong is more misleading than none.
* **Latency, queue position, and time priority.** Everything is instantaneous.
* **Uncertainty.** See :mod:`trading.adapters.paper`.

A paper fill is therefore an optimistic estimate of a live fill, always. That is
a limitation of the approach, not a defect in this file, and it is why the
progression through a broker sandbox exists.
"""

from __future__ import annotations

import decimal
import threading
from decimal import Decimal
from typing import Mapping

from ...core.authz import ExecutionToken
from ...core.clock import Clock
from ...core.marketdata import Quote, StalenessPolicy
from ...core.money import FINANCIAL_CONTEXT, Price, Quantity
from ...core.orders import Order, OrderSide, OrderType
from ...ports.broker import AckOutcome, BrokerAck, BrokerPort, BrokerPositionSnapshot
from ...ports.market_data import QuoteFeedPort

__all__ = ["PaperBroker", "PaperReject"]

#: A rejection at 100% would drive a sell price to zero, and :class:`Price`
#: refuses a non-positive amount -- which would surface as an exception from
#: ``place_order`` and be read by the gateway as an unknown outcome. Refused at
#: construction instead, where it is a configuration error rather than a
#: mid-flight surprise.
_MAX_SLIPPAGE_BPS = 10_000


class PaperReject:
    """Why the paper venue refused. Prefixed onto the ack message so it greps.

    A plain namespace rather than an enum, matching
    :class:`~trading.core.gateway.ExecutionGate`: these are labels for a human
    reading an audit trail.
    """

    NO_QUOTE = "no_quote"
    STALE_QUOTE = "stale_quote"
    CURRENCY_MISMATCH = "currency_mismatch"
    NO_LIQUIDITY = "no_liquidity"


class PaperBroker(BrokerPort):
    """An in-process venue that fills from a quote feed. No network, no credentials."""

    def __init__(
        self,
        *,
        clock: Clock,
        quotes: QuoteFeedPort,
        staleness: StalenessPolicy | None = None,
        slippage_bps: int = 0,
        depth: Mapping[str, Quantity] | None = None,
        id_prefix: str = "PAPER",
    ) -> None:
        if not isinstance(clock, Clock):
            raise TypeError("clock must be a Clock")
        if not isinstance(quotes, QuoteFeedPort):
            raise TypeError(
                f"quotes must implement QuoteFeedPort, got {type(quotes).__name__}"
            )
        if staleness is not None and not isinstance(staleness, StalenessPolicy):
            raise TypeError("staleness must be a StalenessPolicy")
        if isinstance(slippage_bps, bool) or not isinstance(slippage_bps, int):
            raise TypeError("slippage_bps must be an int")
        if not 0 <= slippage_bps < _MAX_SLIPPAGE_BPS:
            raise ValueError(
                f"slippage_bps must be between 0 and {_MAX_SLIPPAGE_BPS - 1}, "
                f"got {slippage_bps}"
            )
        if not isinstance(id_prefix, str) or not id_prefix.strip():
            raise ValueError("id_prefix must be a non-empty string")
        for symbol, available in (depth or {}).items():
            _require_depth(symbol, available)

        self._clock = clock
        self._quotes = quotes
        self._staleness = staleness or StalenessPolicy()
        self._slippage_bps = slippage_bps
        self._depth: dict[str, Quantity] = dict(depth or {})
        self._id_prefix = id_prefix
        # Reentrant: _apply takes it while already holding it for the placement
        # bookkeeping.
        self._lock = threading.RLock()
        self._seq = 0
        self._placements: list[tuple[str, str]] = []
        self._key_counts: dict[str, int] = {}
        self._duplicate_keys: set[str] = set()
        # What the venue holds, keyed by idempotency key, plus its own positions.
        self._acks: dict[str, BrokerAck] = {}
        self._positions: dict[str, Quantity] = {}

    # -- configuration ----------------------------------------------------

    @property
    def slippage_bps(self) -> int:
        return self._slippage_bps

    @property
    def staleness(self) -> StalenessPolicy:
        return self._staleness

    def set_depth(self, symbol: str, available: Quantity) -> None:
        """Set the size a single placement may take in ``symbol``.

        A book thinning is an ordinary venue condition, so this is configuration
        rather than failure injection. Absent a setting the depth is unbounded,
        which is the honest default for a venue with no book of its own.
        """
        _require_depth(symbol, available)
        with self._lock:
            self._depth[symbol] = available

    # -- observations -----------------------------------------------------

    @property
    def placements(self) -> list[tuple[str, str]]:
        """``(order_id, idempotency_key)`` for every placement that reached us."""
        with self._lock:
            return list(self._placements)

    @property
    def placement_count(self) -> int:
        with self._lock:
            return len(self._placements)

    @property
    def duplicate_keys(self) -> frozenset[str]:
        """Keys this venue saw more than once. Must stay empty (INVARIANT 12)."""
        with self._lock:
            return frozenset(self._duplicate_keys)

    def times_seen(self, idempotency_key: str) -> int:
        with self._lock:
            return self._key_counts.get(idempotency_key, 0)

    @property
    def resting_keys(self) -> frozenset[str]:
        """Keys the venue holds a record for: resting, filled, or part-filled."""
        with self._lock:
            return frozenset(self._acks)

    # -- BrokerPort -------------------------------------------------------

    def place_order(self, order: Order, *, token: ExecutionToken) -> BrokerAck:
        # Token first, unconditionally. Nothing below this line runs for a caller
        # without gateway-minted authority (INVARIANT 3), and a replayed token
        # cannot get here twice (INVARIANT 12).
        token.consume(order_id=order.order_id, clock=self._clock)

        with self._lock:
            key = order.idempotency_key
            self._placements.append((order.order_id, key))
            seen = self._key_counts.get(key, 0) + 1
            self._key_counts[key] = seen
            if seen > 1:
                self._duplicate_keys.add(key)

            ack = self._decide(order)
            self._apply(order, ack)
        return ack

    def cancel_order(self, order: Order) -> BrokerAck:
        """Cancel whatever is resting. Needs no token: it can only reduce exposure."""
        with self._lock:
            held = self._acks.get(order.idempotency_key)
            if held is None:
                return BrokerAck(
                    AckOutcome.ACCEPTED, message="nothing resting; cancel is idempotent"
                )
            if held.outcome is AckOutcome.FILLED:
                if held.filled_quantity == order.intent.quantity:
                    return BrokerAck(
                        AckOutcome.REJECTED,
                        broker_order_id=held.broker_order_id,
                        message="already filled; there is nothing left to cancel",
                    )
                # A partial fill is history. Only the remainder is cancellable,
                # and the fill record stays so fetch_order_state cannot forget
                # that quantity changed hands.
                return BrokerAck(
                    AckOutcome.ACCEPTED,
                    broker_order_id=held.broker_order_id,
                    message="canceled the unfilled remainder",
                )
            self._acks.pop(order.idempotency_key)
            return BrokerAck(
                AckOutcome.ACCEPTED,
                broker_order_id=held.broker_order_id,
                message="canceled",
            )

    def fetch_order_state(self, order: Order) -> BrokerAck:
        """What the venue holds for this order.

        A venue with no record reports ``REJECTED``: the order does not exist, so
        treating it as never having happened is the honest reading. Worded and
        shaped exactly as the simulator's answer, because
        :meth:`~trading.core.gateway.ExecutionGateway.resolve_unknown` reads both.
        """
        with self._lock:
            held = self._acks.get(order.idempotency_key)
        if held is None:
            return BrokerAck(
                AckOutcome.REJECTED, message="venue has no record of this order"
            )
        return held

    def fetch_positions(self) -> BrokerPositionSnapshot:
        with self._lock:
            return BrokerPositionSnapshot(dict(self._positions))

    # -- the fill model ---------------------------------------------------

    def _decide(self, order: Order) -> BrokerAck:
        """Everything the venue knows, turned into one definitive ack.

        Never ``UNCERTAIN`` and never an exception: a venue in this process knows
        what it did, and claiming otherwise would put the whole system into the
        UNKNOWN state over a local arithmetic problem.
        """
        now = self._clock.now()
        quote = self._quotes.quote(order.symbol)
        if quote is None:
            return _reject(
                PaperReject.NO_QUOTE,
                f"no quote for {order.symbol}; refusing to invent a fill price",
            )
        if not self._staleness.assess(quote, now=now).is_usable:
            return _reject(
                PaperReject.STALE_QUOTE,
                f"quote for {order.symbol} is {quote.age_seconds(now):.3f}s old, "
                f"limit {self._staleness.max_age_seconds}s (source {quote.source})",
            )

        executable = self._executable_price(quote, order.side)
        limit = order.intent.limit_price
        if limit is not None:
            if limit.currency != executable.currency:
                return _reject(
                    PaperReject.CURRENCY_MISMATCH,
                    f"limit price is in {limit.currency.code} but {order.symbol} "
                    f"quotes in {executable.currency.code}",
                )
            if not _crosses(executable, limit, order.side):
                return self._resting(
                    f"limit {limit.amount} not reached by {executable.amount}"
                )

        ordered = order.intent.quantity
        available = self._depth.get(order.symbol)
        if available is not None and available.asset != ordered.asset:
            return _reject(
                PaperReject.CURRENCY_MISMATCH,
                f"depth for {order.symbol} is quoted in {available.asset} but the "
                f"order is in {ordered.asset}",
            )
        fillable = ordered if available is None else min(available, ordered)

        if fillable.is_zero:
            if order.intent.order_type is OrderType.MARKET:
                # A market order cannot rest, so an empty book is a rejection.
                return _reject(
                    PaperReject.NO_LIQUIDITY,
                    f"no depth available in {order.symbol} for a market order",
                )
            return self._resting(f"no depth available in {order.symbol}")

        partial = "" if fillable == ordered else f"partial fill: {fillable} of {ordered}"
        return BrokerAck(
            AckOutcome.FILLED,
            broker_order_id=self._next_id(),
            filled_quantity=fillable,
            fill_price=executable,
            message=partial,
        )

    def _executable_price(self, quote: Quote, side: OrderSide) -> Price:
        """The price this order actually crosses at, slippage included.

        ``side.sign`` carries the direction, so the adjustment is against the
        order either way: a buy pays more, a sell receives less. There is no
        branch on side here on purpose -- a sign error would otherwise show up as
        a paper account that profits from its own slippage.
        """
        base = quote.price_for(side)
        if self._slippage_bps == 0:
            return base
        with decimal.localcontext(FINANCIAL_CONTEXT):
            factor = Decimal(1) + Decimal(self._slippage_bps * side.sign) / Decimal(
                10_000
            )
            return Price.rounded(base.amount * factor, base.currency)

    def _resting(self, detail: str) -> BrokerAck:
        return BrokerAck(
            AckOutcome.ACCEPTED,
            broker_order_id=self._next_id(),
            message=f"resting: {detail}",
        )

    def _apply(self, order: Order, ack: BrokerAck) -> None:
        """Record what the venue now holds. Called with the lock held."""
        if ack.outcome is AckOutcome.REJECTED:
            # A rejected order never existed, so it leaves no trace to reconcile
            # against.
            return
        self._acks[order.idempotency_key] = ack
        if ack.outcome is not AckOutcome.FILLED:
            return
        assert ack.filled_quantity is not None
        signed = Quantity(
            ack.filled_quantity.amount * order.side.sign, ack.filled_quantity.asset
        )
        current = self._positions.get(order.symbol)
        self._positions[order.symbol] = signed if current is None else current + signed

    def _next_id(self) -> str:
        self._seq += 1
        return f"{self._id_prefix}-{self._seq:06d}"


def _crosses(executable: Price, limit: Price, side: OrderSide) -> bool:
    """Whether a limit at ``limit`` would trade at ``executable``.

    :class:`Price` implements ``<`` and ``>`` and no inclusive comparison, so
    "at or better" is spelled as the negation of the strict one. A limit sitting
    exactly on the executable price does cross, which is the boundary a venue
    fills and a naive ``>``/``<`` would drop.
    """
    if side is OrderSide.BUY:
        return not executable > limit
    return not executable < limit


def _reject(reason: str, detail: str) -> BrokerAck:
    return BrokerAck(AckOutcome.REJECTED, message=f"{reason}: {detail}")


def _require_depth(symbol: str, available: object) -> None:
    if not isinstance(available, Quantity):
        raise TypeError(
            f"depth for {symbol} must be a Quantity, got {type(available).__name__} "
            "(INVARIANT 8)"
        )
    if available.amount < 0:
        raise ValueError(f"depth for {symbol} must not be negative, got {available}")
