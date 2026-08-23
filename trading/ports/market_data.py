"""The market-data ports.

Two of them, at different levels of detail.

:class:`MarketDataPort` is deliberately thin: the risk engine needs a mark price
per symbol and nothing more. A missing price must be reported as ``None`` rather
than substituted with a stale or guessed value -- the risk engine treats absence
as a refusal, and that only works if adapters are honest about not knowing.

:class:`QuoteFeedPort` is what a real feed implements. It returns timestamped
:class:`~trading.core.marketdata.Quote` objects, so *how old is this price?* has
an answer. That question cannot be asked through ``MarketDataPort``, which is why
sizing a position off a frozen feed would otherwise look identical to sizing one
off a live feed.

The two are bridged by :class:`~trading.core.marketdata.FreshMarkPrices`, which
implements ``MarketDataPort`` over a ``QuoteFeedPort`` and reports a stale quote
as *absent*. Staleness therefore becomes an ordinary risk refusal at a gate that
already exists, rather than a new condition threaded through the gateway.

``MarketDataPort`` is kept as-is rather than widened, on purpose: the risk engine
and the sizer are proven against it, and a port that answers exactly one question
is a port an adapter cannot get subtly wrong.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import TYPE_CHECKING, Iterable, Mapping

from ..core.money import Price

if TYPE_CHECKING:  # pragma: no cover - imported for typing only
    # Runtime import would close a cycle: trading.core.marketdata imports this
    # module for QuoteFeedPort. Annotations are strings under
    # ``from __future__ import annotations``, so the type is available to a
    # checker without either module needing the other at import time.
    from ..core.marketdata import Quote

__all__ = ["MarketDataPort", "QuoteFeedPort"]


class MarketDataPort(ABC):
    """Outbound interface to a price source."""

    @abstractmethod
    def mark_price(self, symbol: str) -> Price | None:
        """Current mark price for ``symbol``, or ``None`` if unavailable.

        Must not fabricate, extrapolate, or return a stale price silently.
        """

    def mark_prices(self, symbols: Iterable[str]) -> Mapping[str, Price]:
        """Prices for several symbols, omitting any that are unavailable.

        Omission is deliberate: the risk engine refuses when a symbol with
        exposure has no price, so a gap here becomes a refusal rather than an
        unchecked order.
        """
        result: dict[str, Price] = {}
        for symbol in symbols:
            price = self.mark_price(symbol)
            if price is not None:
                result[symbol] = price
        return result


class QuoteFeedPort(ABC):
    """Outbound interface to a timestamped quote source.

    The contract an adapter must honour:

    * Return ``None`` for a symbol you do not have. Never a guess, never the
      last value you happen to remember, never a synthesised one.
    * Set ``as_of`` to the venue's own timestamp for the quote where the venue
      provides one, and to the moment of receipt where it does not -- never to
      "now" as a matter of course. A receipt time stamped on a replayed message
      makes stale data look fresh, which is the precise failure this port exists
      to make visible.
    * Do not filter on age. Report what you have, with its timestamp, and let
      :class:`~trading.core.marketdata.StalenessPolicy` decide -- different
      consumers tolerate different ages, and an adapter cannot know which is
      asking.
    """

    @abstractmethod
    def quote(self, symbol: str) -> "Quote | None":
        """Latest known quote for ``symbol``, or ``None`` if there is none."""

    @abstractmethod
    def symbols(self) -> Iterable[str]:
        """Symbols this feed currently has a quote for.

        Needed for monitoring: a feed that has silently stopped covering a
        symbol we hold a position in is an operational event, and it is
        invisible if the only question askable is per-symbol.
        """

    def quotes(self, symbols: Iterable[str]) -> Mapping[str, "Quote"]:
        """Quotes for several symbols, omitting any that are unknown.

        Omission rather than a ``None`` value, matching
        :meth:`MarketDataPort.mark_prices`: a gap stays a gap, and a caller that
        forgets to check cannot mistake ``None`` for a price.
        """
        result: dict[str, "Quote"] = {}
        for symbol in symbols:
            quote = self.quote(symbol)
            if quote is not None:
                result[symbol] = quote
        return result
