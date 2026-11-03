"""Deterministic continuous market stream for the runnable local paper app.

This is demonstration data, not a backtest and not a market simulation.  It
exists so the supervised service and dashboard can run end to end before a
venue-specific transport is selected.  Quote and candle sources share one
cursor; the service polls the quote first, then the candle closes the interval
and advances the injected manual clock.
"""

from __future__ import annotations

import datetime as _dt
from decimal import Decimal

from ..adapters.marketdata import MarketDataSource, RawMessage
from ..core.clock import Clock

__all__ = ["DemoMarketStream"]

_PRICE_PATH = (
    "100", "99.5", "99", "98.5", "98", "97.5", "97",
    "98", "100", "103", "106", "109", "112",
    "110", "107", "103", "99", "95", "92",
)


class _QuoteSource(MarketDataSource):
    def __init__(self, stream: "DemoMarketStream") -> None:
        self._stream = stream

    def poll(self) -> list[RawMessage]:
        return [self._stream._quote_message()]


class _CandleSource(MarketDataSource):
    def __init__(self, stream: "DemoMarketStream") -> None:
        self._stream = stream

    def poll(self) -> list[RawMessage]:
        return [self._stream._close_interval()]


class DemoMarketStream:
    """Two coordinated inbound sources over a repeating deterministic path."""

    def __init__(
        self,
        clock: Clock,
        *,
        symbol: str = "DEMOUSD",
        asset: str = "DEMO",
        interval_seconds: int = 60,
    ) -> None:
        advance = getattr(clock, "advance", None)
        if not callable(advance):
            raise TypeError("DemoMarketStream requires an advanceable clock")
        if not isinstance(symbol, str) or not symbol.strip():
            raise ValueError("symbol must be a non-empty string")
        if not isinstance(asset, str) or not asset.strip():
            raise ValueError("asset must be a non-empty string")
        if (
            isinstance(interval_seconds, bool)
            or not isinstance(interval_seconds, int)
            or interval_seconds <= 0
        ):
            raise ValueError("interval_seconds must be a positive int")
        self.clock = clock
        self._advance = advance
        self.symbol = symbol
        self.asset = asset
        self.interval_seconds = interval_seconds
        self._index = 0
        self.quote_source: MarketDataSource = _QuoteSource(self)
        self.candle_source: MarketDataSource = _CandleSource(self)

    @property
    def intervals(self) -> int:
        return self._index

    def _current(self) -> Decimal:
        return Decimal(_PRICE_PATH[self._index % len(_PRICE_PATH)])

    def _previous(self) -> Decimal:
        if self._index == 0:
            return self._current()
        return Decimal(_PRICE_PATH[(self._index - 1) % len(_PRICE_PATH)])

    def _close_time(self) -> _dt.datetime:
        return self.clock.now() + _dt.timedelta(seconds=self.interval_seconds)

    def _quote_message(self) -> RawMessage:
        mid = self._current()
        half_spread = Decimal("0.1")
        return {
            "symbol": self.symbol,
            "bid": str(mid - half_spread),
            "ask": str(mid + half_spread),
            "timestamp": self._close_time().isoformat(),
            "sequence": self._index + 1,
        }

    def _close_interval(self) -> RawMessage:
        open_time = self.clock.now()
        open_ = self._previous()
        close = self._current()
        half_range = Decimal("0.5")
        message: RawMessage = {
            "symbol": self.symbol,
            "open_time": open_time.isoformat(),
            "interval_seconds": self.interval_seconds,
            "open": str(open_),
            "high": str(max(open_, close) + half_range),
            "low": str(min(open_, close) - half_range),
            "close": str(close),
            "volume": "1",
        }
        self._advance(self.interval_seconds)
        self._index += 1
        return message
