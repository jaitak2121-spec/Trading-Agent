"""What a signal strategy is allowed to see.

:class:`~trading.strategy.base.MarketView` is Stage 1's answer, and it is still
right about the important thing: values only, no collaborators, nothing a strategy
could call. But it holds bare prices, which means a strategy reading it cannot
tell a price from four seconds ago from one from four hours ago, and cannot tell
whether a bar has closed.

:class:`MarketContext` is the Stage 2 widening. It carries the *timestamped*
market -- a :class:`~trading.core.marketdata.MarketSnapshot` plus candle history
-- and it applies a :class:`~trading.core.marketdata.StalenessPolicy` on the way
out. Three rules follow from that:

* :meth:`MarketContext.quote` returns ``None`` for a stale symbol. A strategy
  that forgets to check gets nothing rather than something wrong.
* :meth:`MarketContext.require_quote` raises instead, for a strategy that would
  rather fail loudly than silently skip.
* :meth:`MarketContext.candles` yields only *closed* bars. An in-progress bar
  read as if it were complete is the classic reason a strategy behaves
  differently live than in a backtest, and the only reliable fix is to not hand
  it over.

It remains inert. There is no broker, no gateway, no order store, and no mutable
state reachable through it -- the same guarantee ``MarketView`` makes, with more
information behind it. :meth:`MarketContext.to_market_view` converts down for the
Stage 1 interface, so a strategy written either way sees a consistent market.
"""

from __future__ import annotations

import datetime as _dt
from dataclasses import dataclass, field
from typing import Mapping, Sequence

from ..core.clock import UTC
from ..core.marketdata import (
    Candle,
    Freshness,
    MarketSnapshot,
    Quote,
    StalenessPolicy,
)
from ..core.money import Money, Price, Quantity
from .base import MarketView

__all__ = ["MarketContext"]


@dataclass(frozen=True)
class MarketContext:
    """The timestamped market, filtered for freshness on read.

    ``as_of`` is the instant the context was taken and is what every freshness
    and bar-completion question is answered against. Fixing it once, rather than
    reading a clock per access, means a strategy cannot see a symbol at one moment
    and another symbol at a later one within a single decision.
    """

    as_of: _dt.datetime
    equity: Money
    snapshot: MarketSnapshot
    positions: Mapping[str, Quantity] = field(default_factory=dict)
    #: Chronological bars per symbol, oldest first. May include an in-progress
    #: bar; :meth:`candles` filters it out.
    history: Mapping[str, Sequence[Candle]] = field(default_factory=dict)
    policy: StalenessPolicy = field(default_factory=StalenessPolicy)

    def __post_init__(self) -> None:
        if not isinstance(self.as_of, _dt.datetime):
            raise TypeError("as_of must be a datetime")
        if self.as_of.tzinfo is None or self.as_of.utcoffset() is None:
            raise ValueError("as_of must be timezone-aware")
        if not isinstance(self.equity, Money):
            raise TypeError("equity must be Money")
        if not isinstance(self.snapshot, MarketSnapshot):
            raise TypeError("snapshot must be a MarketSnapshot")
        if not isinstance(self.policy, StalenessPolicy):
            raise TypeError("policy must be a StalenessPolicy")
        for symbol, quantity in self.positions.items():
            if not isinstance(quantity, Quantity):
                raise TypeError(f"position for {symbol} must be a Quantity")
        for symbol, bars in self.history.items():
            for bar in bars:
                if not isinstance(bar, Candle):
                    raise TypeError(f"history for {symbol} must contain Candles")
                if bar.symbol != symbol:
                    raise ValueError(
                        f"history keyed as {symbol!r} contains a bar for "
                        f"{bar.symbol!r}"
                    )
            times = [bar.open_time for bar in bars]
            if times != sorted(times):
                raise ValueError(
                    f"history for {symbol} is not in chronological order; "
                    "indicators computed over it would be meaningless"
                )
        object.__setattr__(self, "positions", dict(self.positions))
        object.__setattr__(
            self, "history", {s: tuple(b) for s, b in self.history.items()}
        )
        object.__setattr__(self, "as_of", self.as_of.astimezone(UTC))

    # -- market data ------------------------------------------------------

    def freshness(self, symbol: str) -> Freshness:
        """The verdict, unfiltered -- for code that must *report* staleness."""
        return self.policy.assess(self.snapshot.quote(symbol), now=self.as_of)

    def quote(self, symbol: str) -> Quote | None:
        """The quote if it passes the policy, else ``None``."""
        quote = self.snapshot.quote(symbol)
        return quote if self.policy.assess(quote, now=self.as_of).is_usable else None

    def require_quote(self, symbol: str) -> Quote:
        """The quote, or :class:`~trading.core.errors.StaleMarketData`."""
        return self.policy.require_fresh(
            self.snapshot.quote(symbol), symbol=symbol, now=self.as_of
        )

    def price(self, symbol: str) -> Price | None:
        """Mid price if fresh, else ``None``. A strategy must handle absence."""
        quote = self.quote(symbol)
        return None if quote is None else quote.mid

    @property
    def tradable_symbols(self) -> list[str]:
        """Symbols with a fresh quote. The only ones a decision may reference."""
        return self.snapshot.fresh_symbols(self.policy)

    @property
    def stale_symbols(self) -> list[str]:
        return self.snapshot.stale_symbols(self.policy)

    # -- history ----------------------------------------------------------

    def candles(self, symbol: str, *, count: int | None = None) -> tuple[Candle, ...]:
        """Closed bars for ``symbol``, oldest first.

        An in-progress bar is excluded, so an indicator cannot be computed over a
        partial period. ``count`` returns the most recent ``count`` bars.
        """
        bars = tuple(
            bar for bar in self.history.get(symbol, ()) if bar.is_complete(self.as_of)
        )
        if count is None:
            return bars
        if isinstance(count, bool) or not isinstance(count, int):
            raise TypeError("count must be an int or None")
        if count < 0:
            raise ValueError("count must not be negative")
        return bars[-count:] if count else ()

    def has_history(self, symbol: str, *, at_least: int) -> bool:
        """Whether enough closed bars exist. The guard before any indicator."""
        return len(self.candles(symbol)) >= at_least

    # -- portfolio --------------------------------------------------------

    def position(self, symbol: str, *, asset: str) -> Quantity:
        held = self.positions.get(symbol)
        return Quantity.zero(asset) if held is None else held

    def is_flat(self, symbol: str) -> bool:
        held = self.positions.get(symbol)
        return held is None or held.is_zero

    def is_long(self, symbol: str) -> bool:
        held = self.positions.get(symbol)
        return held is not None and held.is_positive

    def is_short(self, symbol: str) -> bool:
        held = self.positions.get(symbol)
        return held is not None and not held.is_zero and not held.is_positive

    # -- bridge -----------------------------------------------------------

    def to_market_view(self) -> MarketView:
        """A Stage 1 ``MarketView`` over the fresh half of this context.

        Stale symbols are omitted rather than carried across, so a Stage 1
        strategy reading the view sees absence -- which it already has to handle
        -- instead of a price whose age it has no way to ask about.
        """
        return MarketView(
            as_of=self.as_of,
            equity=self.equity,
            prices=self.snapshot.mark_prices(self.policy),
            positions=dict(self.positions),
        )
