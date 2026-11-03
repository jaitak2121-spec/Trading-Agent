"""An in-process store of closed candles, one ordered series per symbol.

The bar-side counterpart of :class:`~trading.adapters.memory.quote_feed.InMemoryQuoteFeed`:
somewhere for ingested bars to live so a strategy or an indicator can read a
symbol's history. It is a passive container -- the
:class:`~trading.adapters.marketdata.candle_ingestor.CandleIngestor` is the
gatekeeper that decides what is a duplicate, out of order, or across a gap, and
appends only valid, forward bars. The store therefore holds **only real bars, in
arrival order, and never fabricates one**.

No network, no credentials. Lock-guarded so a background poller and a reader can
touch it from different threads, matching the quote feed's contract.
"""

from __future__ import annotations

import threading

from ...core.marketdata import Candle

__all__ = ["InMemoryCandleStore"]


class InMemoryCandleStore:
    """Closed candles per symbol, in append order."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._bars: dict[str, list[Candle]] = {}
        self._intervals: dict[str, int] = {}

    def append(self, candle: Candle) -> None:
        """Append a validated candle. Records the symbol's interval on first sight."""
        if not isinstance(candle, Candle):
            raise TypeError("append expects a Candle")
        with self._lock:
            self._bars.setdefault(candle.symbol, []).append(candle)
            self._intervals.setdefault(candle.symbol, candle.interval_seconds)

    def candles(self, symbol: str) -> tuple[Candle, ...]:
        """Every stored bar for ``symbol``, oldest first."""
        with self._lock:
            return tuple(self._bars.get(symbol, ()))

    def last(self, symbol: str) -> Candle | None:
        with self._lock:
            bars = self._bars.get(symbol)
            return bars[-1] if bars else None

    def symbols(self) -> list[str]:
        with self._lock:
            return sorted(self._bars)

    def interval(self, symbol: str) -> int | None:
        """The bar interval seen for ``symbol``, or ``None`` if none yet."""
        with self._lock:
            return self._intervals.get(symbol)

    def __len__(self) -> int:
        with self._lock:
            return sum(len(bars) for bars in self._bars.values())
