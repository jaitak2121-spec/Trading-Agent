"""A market-data source backed by a dictionary.

Prices are set explicitly by the caller. There is no polling, no cache, and no
extrapolation -- a symbol either has a price or it does not, and the honest
``None`` for the second case is what makes the risk engine's fail-closed
behaviour testable (a price outage must refuse orders, not pass unchecked ones).

:meth:`StaticMarketData.set_price` rejects anything that is not a
:class:`~trading.core.money.Price`, so a float cannot enter the system through
the price boundary (INVARIANT 8).
"""

from __future__ import annotations

import threading
from typing import Mapping

from ...core.money import Price
from ...ports.market_data import MarketDataPort

__all__ = ["StaticMarketData"]


class StaticMarketData(MarketDataPort):
    """An in-process price source. No network."""

    def __init__(self, prices: Mapping[str, Price] | None = None) -> None:
        self._lock = threading.Lock()
        self._prices: dict[str, Price] = {}
        for symbol, price in (prices or {}).items():
            self.set_price(symbol, price)

    def set_price(self, symbol: str, price: Price) -> None:
        if not isinstance(symbol, str) or not symbol.strip():
            raise ValueError("symbol must be a non-empty string")
        if not isinstance(price, Price):
            raise TypeError(
                f"price for {symbol} must be a Price, got {type(price).__name__}; "
                "floats are rejected at the boundary (INVARIANT 8)"
            )
        with self._lock:
            self._prices[symbol] = price

    def go_dark(self, symbol: str) -> None:
        """Drop a price, simulating a feed outage for one symbol."""
        with self._lock:
            self._prices.pop(symbol, None)

    def mark_price(self, symbol: str) -> Price | None:
        with self._lock:
            return self._prices.get(symbol)

    def symbols(self) -> list[str]:
        with self._lock:
            return sorted(self._prices)
