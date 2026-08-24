"""Portfolio state: what we hold, what it cost, and what it is worth.

:class:`~trading.core.reconciliation.PositionLedger` answers "how much of this do
we hold" and that is all it needs to answer, because its consumer is the
reconciliation comparison against a venue snapshot. Both operating modes need
more than that:

* Advisory mode has to size a position from account equity, and equity is cash
  plus the marked value of what is held. It also has to explain a trade, and an
  explanation that quotes an entry price the system does not actually know is
  worse than no explanation.
* Autonomous mode has to feed the daily-loss limit
  (:class:`~trading.core.risk.PnlLedger`), which needs realized profit and loss,
  which needs a cost basis.

So this module adds the cost-basis layer *on top of* the ledger rather than
beside it. A :class:`Portfolio` is constructed around one
:class:`~trading.core.reconciliation.PositionLedger` and delegates every quantity
write to it, so there remains exactly one authority on quantity and no second
copy to diverge from it (INVARIANT 6 exists because two views of a position
disagreeing is the expensive failure; adding a third view locally would be
self-inflicted).

Four decisions worth stating.

**An unknown cost basis is reported as unknown, never as zero.** When positions
are adopted from a venue during reconciliation we learn the quantity and not what
was paid for it. A basis of zero would report a long position as pure profit; the
previous average belongs to a different quantity and is equally wrong. So
:attr:`Position.average_entry_price` is ``None`` in that case and
:meth:`Position.unrealized_pnl` returns ``None`` rather than a number.

**The basis self-invalidates when the ledger moves underneath it.** The portfolio
remembers the quantity its basis describes. Anything that writes to the ledger
directly -- ``set_position``, ``adopt_broker_positions``, a future persistence
adapter -- leaves the two disagreeing, and a disagreement is read as "this basis
is not about what we now hold" and reported as unknown. That holds without the
writer having to know the portfolio exists, which is the only version of this
check that cannot be forgotten at a new call site.

**Rounding never flatters the account.** Every rounding decision here is made in
the direction that reports less equity and less profit: a buy's cash outflow
rounds up, a sale's inflow rounds down, a long's market value rounds down, a
short's rounds up, and a profit-and-loss figure rounds toward a loss. A number
this module reports may be pessimistic by a cent. It is never optimistic, which
matters because the figures feed a loss limit and a position sizer.

**Equity is the authoritative figure, not an identity over the parts.** Equity is
computed as cash plus marked value, both of which are rounded conservatively and
independently, so ``cash + realized + unrealized`` may differ from
:meth:`Portfolio.equity` by rounding. Where a single number has to be right, it
is :meth:`Portfolio.equity`.

This module executes nothing. It holds no broker, no gateway, and no token
(INVARIANT 3), and it writes no audit records: a fill has already been audited by
the gateway that applied it, and a second record of the same event is a second
thing to keep consistent.
"""

from __future__ import annotations

import decimal
import threading
from dataclasses import dataclass
from decimal import Decimal
from typing import Mapping

from .errors import CurrencyMismatch, SafetyViolation
from .money import (
    FINANCIAL_CONTEXT,
    ROUND_DOWN,
    ROUND_UP,
    Currency,
    Money,
    Price,
    Quantity,
)
from .orders import OrderSide
from .reconciliation import PositionLedger

__all__ = ["Position", "FillEffect", "Portfolio"]


def _pnl(amount: Decimal, currency: Currency) -> Money:
    """Round a profit-or-loss figure toward the worse result.

    ``ROUND_DOWN`` is toward zero, so it shrinks a gain; ``ROUND_UP`` is away
    from zero, so it grows a loss. Choosing by sign gives rounding toward
    negative infinity without needing a fourth rounding mode in
    :mod:`~trading.core.money`, and states the intent at the point of use.
    """
    return Money.rounded(
        amount, currency, rounding=ROUND_DOWN if amount >= 0 else ROUND_UP
    )


def _flipped(before: Quantity, after: Quantity) -> bool:
    """True when a fill carried the position through zero to the other side."""
    return not after.is_zero and (before.amount > 0) != (after.amount > 0)


@dataclass(frozen=True, slots=True)
class Position:
    """One symbol's holding, its basis, and the profit already taken on it.

    A value, not a handle: it is a reading of the portfolio at one moment and
    does not change when the portfolio does.
    """

    symbol: str
    quantity: Quantity
    #: Volume-weighted average price of the open quantity, or ``None`` when the
    #: basis is not known -- see the module docstring.
    average_entry_price: Price | None
    realized_pnl: Money

    def __post_init__(self) -> None:
        if not isinstance(self.quantity, Quantity):
            raise TypeError("quantity must be a Quantity")
        if not isinstance(self.realized_pnl, Money):
            raise TypeError("realized_pnl must be Money")
        if self.average_entry_price is not None:
            if not isinstance(self.average_entry_price, Price):
                raise TypeError("average_entry_price must be a Price or None")
            if self.average_entry_price.currency != self.realized_pnl.currency:
                raise CurrencyMismatch(
                    f"{self.symbol}: entry price is in "
                    f"{self.average_entry_price.currency.code} but realized pnl is "
                    f"in {self.realized_pnl.currency.code}"
                )
            if self.quantity.is_zero:
                raise ValueError(
                    f"{self.symbol}: a flat position cannot have an entry price; "
                    "there is nothing held for it to be the basis of"
                )

    @property
    def currency(self) -> Currency:
        return self.realized_pnl.currency

    @property
    def is_flat(self) -> bool:
        return self.quantity.is_zero

    @property
    def is_long(self) -> bool:
        return self.quantity.amount > 0

    @property
    def is_short(self) -> bool:
        return self.quantity.amount < 0

    @property
    def basis_is_known(self) -> bool:
        """False only when something is held and we do not know what it cost."""
        return self.quantity.is_zero or self.average_entry_price is not None

    def _require_mark(self, mark: Price) -> None:
        if not isinstance(mark, Price):
            raise TypeError("mark must be a Price")
        if mark.currency != self.currency:
            raise CurrencyMismatch(
                f"{self.symbol}: mark is in {mark.currency.code} but the position "
                f"is denominated in {self.currency.code}"
            )

    def cost_basis(self) -> Money | None:
        """What the open quantity was paid for, or ``None`` if not known."""
        if self.quantity.is_zero:
            return Money.zero(self.currency)
        if self.average_entry_price is None:
            return None
        return self.average_entry_price.notional(self.quantity, rounding=ROUND_UP)

    def market_value(self, mark: Price) -> Money:
        """Signed contribution to equity at ``mark``.

        Rounded so that the contribution is never overstated: a long's value
        rounds down, a short's liability rounds up. Needs no cost basis, which is
        why equity survives an adopted position while attribution does not.
        """
        self._require_mark(mark)
        rounding = ROUND_DOWN if self.quantity.amount > 0 else ROUND_UP
        return mark.notional(self.quantity, rounding=rounding)

    def unrealized_pnl(self, mark: Price) -> Money | None:
        """Open profit at ``mark``, or ``None`` when the basis is unknown."""
        self._require_mark(mark)
        if self.quantity.is_zero:
            return Money.zero(self.currency)
        if self.average_entry_price is None:
            return None
        with decimal.localcontext(FINANCIAL_CONTEXT):
            raw = (mark.amount - self.average_entry_price.amount) * self.quantity.amount
        return _pnl(raw, self.currency)

    def as_details(self) -> dict[str, object]:
        entry = self.average_entry_price
        return {
            "symbol": self.symbol,
            "quantity": str(self.quantity.amount),
            "asset": self.quantity.asset,
            "average_entry_price": None if entry is None else str(entry.amount),
            "basis_known": self.basis_is_known,
            "realized_pnl": str(self.realized_pnl.amount),
            "currency": self.currency.code,
        }

    def __str__(self) -> str:
        entry = "basis unknown" if self.average_entry_price is None else (
            f"@ {self.average_entry_price.amount}"
        )
        return f"{self.symbol} {self.quantity.amount} {entry}"


@dataclass(frozen=True, slots=True)
class FillEffect:
    """What one fill did to the portfolio.

    Returned rather than merely applied because the realized figure is what
    :class:`~trading.core.risk.PnlLedger` needs, and handing it back lets the
    caller feed the loss limit without re-deriving it from state that has already
    moved on.
    """

    symbol: str
    side: OrderSide
    quantity: Quantity
    price: Price
    cash_delta: Money
    #: Profit taken by the closing part of this fill. Zero when the fill only
    #: opened or added, and zero when the basis was unknown -- in which case
    #: ``basis_was_known`` is False and the zero means "not computable", not "no
    #: profit".
    realized_pnl: Money
    basis_was_known: bool
    #: Whether :attr:`realized_pnl` is the whole story. False only when a fill
    #: *closed against* an unknown basis, where the zero above means "not
    #: computable". A fill that merely added to an unknown-basis position
    #: realizes genuinely nothing, so this stays True. The daily-loss limit
    #: reads this to tell a real zero from a missing number.
    realized_is_known: bool
    position: Position

    def as_details(self) -> dict[str, object]:
        return {
            "symbol": self.symbol,
            "side": self.side.value,
            "quantity": str(self.quantity.amount),
            "price": str(self.price.amount),
            "cash_delta": str(self.cash_delta.amount),
            "realized_pnl": str(self.realized_pnl.amount),
            "basis_was_known": self.basis_was_known,
            "realized_is_known": self.realized_is_known,
            "position": self.position.as_details(),
        }


class Portfolio:
    """Cash, positions, and cost basis over a single :class:`PositionLedger`."""

    def __init__(self, cash: Money, *, ledger: PositionLedger | None = None) -> None:
        if not isinstance(cash, Money):
            raise TypeError("cash must be Money")
        if ledger is not None and not isinstance(ledger, PositionLedger):
            raise TypeError("ledger must be a PositionLedger")
        self._currency = cash.currency
        self._cash = cash
        self._ledger = ledger if ledger is not None else PositionLedger()
        # Absent keys read as "basis unknown", which is the right answer for a
        # ledger that arrived already holding positions: we did not see those
        # fills, so we do not know what they cost.
        self._basis: dict[str, Price] = {}
        self._realized: dict[str, Money] = {}
        #: The quantity each basis describes, so a direct ledger write can be
        #: detected rather than silently invalidating the basis.
        self._accounted: dict[str, Quantity] = {}
        self._lock = threading.RLock()

    # -- state -------------------------------------------------------------
    @property
    def base_currency(self) -> Currency:
        return self._currency

    @property
    def cash(self) -> Money:
        with self._lock:
            return self._cash

    @property
    def ledger(self) -> PositionLedger:
        """The quantity authority, for handing to the reconciliation gate."""
        return self._ledger

    def snapshot(self) -> dict[str, Quantity]:
        """Quantities per symbol, in the shape the risk engine takes."""
        return self._ledger.snapshot()

    def symbols(self) -> list[str]:
        return self._ledger.symbols()

    def position(self, symbol: str, *, asset: str | None = None) -> Position:
        with self._lock:
            held = self._ledger.position(symbol, asset=asset)
            basis = self._basis.get(symbol)
            if basis is not None and self._accounted.get(symbol) != held:
                # Someone wrote the ledger without going through here. The basis
                # describes a quantity we no longer hold, so it is not a basis.
                basis = None
            if held.is_zero:
                basis = None
            return Position(
                symbol=symbol,
                quantity=held,
                average_entry_price=basis,
                realized_pnl=self._realized.get(symbol, Money.zero(self._currency)),
            )

    def open_positions(self) -> list[Position]:
        """Every non-flat position, symbol order. What an advisory report lists."""
        return [
            position
            for position in (self.position(symbol) for symbol in self._ledger.symbols())
            if not position.is_flat
        ]

    def realized(self) -> Money:
        """Total profit taken across every symbol, including closed ones."""
        with self._lock:
            total = Money.zero(self._currency)
            for amount in self._realized.values():
                total = total + amount
            return total

    # -- fills -------------------------------------------------------------
    def apply_fill(
        self, symbol: str, side: OrderSide, quantity: Quantity, price: Price
    ) -> FillEffect:
        """Apply a fill to cash, quantity, and basis, and report what it did.

        The quantity write is delegated to the ledger, so the ledger remains the
        single answer to "how much do we hold" and this method cannot drift from
        it.
        """
        if not isinstance(side, OrderSide):
            raise TypeError("side must be an OrderSide")
        if not isinstance(quantity, Quantity):
            raise TypeError("quantity must be a Quantity")
        if not isinstance(price, Price):
            raise TypeError("price must be a Price")
        if not quantity.is_positive:
            raise ValueError("fill quantity must be strictly positive")
        if price.currency != self._currency:
            raise CurrencyMismatch(
                f"{symbol}: fill priced in {price.currency.code} but the portfolio "
                f"is denominated in {self._currency.code}"
            )

        with self._lock:
            before = self.position(symbol, asset=quantity.asset)
            held = before.quantity
            if held.asset != quantity.asset:
                raise SafetyViolation(
                    f"{symbol}: held in {held.asset} but the fill is in "
                    f"{quantity.asset}; refusing to combine two different assets "
                    "into one position"
                )

            signed = quantity if side is OrderSide.BUY else -quantity
            basis, realized, basis_was_known = self._reprice(
                held, before.average_entry_price, signed, price
            )
            # Only a fill that closes against an unknown basis leaves the
            # realized figure unknowable. Adding to one realizes nothing at all.
            reduced = not held.is_zero and (held.amount > 0) != (signed.amount > 0)
            realized_is_known = basis_was_known or not reduced

            if side is OrderSide.BUY:
                # Round the outflow up and the inflow down: cash is never
                # reported as more than it could be.
                cash_delta = -price.notional(quantity, rounding=ROUND_UP)
            else:
                cash_delta = price.notional(quantity, rounding=ROUND_DOWN)

            self._cash = self._cash + cash_delta
            after = self._ledger.apply_fill(symbol, side, quantity)
            self._accounted[symbol] = after
            if basis is None:
                self._basis.pop(symbol, None)
            else:
                self._basis[symbol] = basis
            total_realized = before.realized_pnl + realized
            self._realized[symbol] = total_realized

            return FillEffect(
                symbol=symbol,
                side=side,
                quantity=quantity,
                price=price,
                cash_delta=cash_delta,
                realized_pnl=realized,
                basis_was_known=basis_was_known,
                realized_is_known=realized_is_known,
                position=Position(
                    symbol=symbol,
                    quantity=after,
                    average_entry_price=None if after.is_zero else basis,
                    realized_pnl=total_realized,
                ),
            )

    def _reprice(
        self, held: Quantity, basis: Price | None, signed: Quantity, price: Price
    ) -> tuple[Price | None, Money, bool]:
        """The three cases a fill can be: open/add, reduce, or flip.

        Returns the new basis, the profit realized by this fill, and whether the
        basis it was computed from was known -- so a caller can tell a genuine
        zero from an uncomputable one.
        """
        zero = Money.zero(self._currency)
        if held.is_zero:
            return price, zero, True

        adding = (held.amount > 0) == (signed.amount > 0)
        if adding:
            if basis is None:
                # Averaging a known price onto an unknown one yields a number
                # with no meaning. Stay unknown rather than inventing a basis.
                return None, zero, False
            with decimal.localcontext(FINANCIAL_CONTEXT):
                held_size, fill_size = abs(held.amount), abs(signed.amount)
                weighted = (basis.amount * held_size + price.amount * fill_size) / (
                    held_size + fill_size
                )
            # ROUND_UP: a higher average entry understates a long's profit and
            # overstates a short's, and the pessimistic direction is the one a
            # loss limit should see.
            return Price.rounded(weighted, price.currency, rounding=ROUND_UP), zero, True

        after = held + signed
        if basis is None:
            # No basis means no realized figure. The one thing we do learn is
            # that a flip opened its new side at this price, so from here on the
            # basis is known again.
            return (price if _flipped(held, after) else None), zero, False

        with decimal.localcontext(FINANCIAL_CONTEXT):
            closed = min(abs(held.amount), abs(signed.amount))
            direction = Decimal(1) if held.amount > 0 else Decimal(-1)
            raw = (price.amount - basis.amount) * closed * direction
        realized = _pnl(raw, self._currency)

        if after.is_zero:
            return None, realized, True
        if _flipped(held, after):
            # The rest of the fill opened a new position on the other side, and
            # it was opened at this price.
            return price, realized, True
        return basis, realized, True

    # -- valuation ---------------------------------------------------------
    def equity(self, mark_prices: Mapping[str, Price]) -> Money:
        """Cash plus the marked value of everything held.

        Raises rather than skipping a symbol it cannot price. The same reasoning
        as :data:`~trading.core.risk.RiskLimit.MARK_PRICE_AVAILABLE`: "I could
        not value this" must never reach a position sizer looking like an equity
        figure, because the sizer would size against it.
        """
        if not isinstance(mark_prices, Mapping):
            raise TypeError("mark_prices must be a mapping of symbol to Price")
        with self._lock:
            total = self._cash
            for symbol, held in self._ledger.snapshot().items():
                if held.is_zero:
                    continue
                mark = mark_prices.get(symbol)
                if mark is None:
                    raise SafetyViolation(
                        f"no mark price for {symbol}, which holds {held.amount} "
                        f"{held.asset}; equity cannot be computed and a partial "
                        "total must not be mistaken for one"
                    )
                total = total + self.position(symbol, asset=held.asset).market_value(mark)
            return total

    def __repr__(self) -> str:
        with self._lock:
            return (
                f"Portfolio(cash={self._cash}, "
                f"positions={len(self._ledger)}, realized={self.realized()})"
            )
