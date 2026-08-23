"""The market-data port.

Kept deliberately thin: the risk engine needs a mark price per symbol and
nothing more. A missing price must be reported as ``None`` rather than
substituted with a stale or guessed value -- the risk engine treats absence as a
refusal, and that only works if adapters are honest about not knowing.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Iterable, Mapping

from ..core.money import Price

__all__ = ["MarketDataPort"]


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
