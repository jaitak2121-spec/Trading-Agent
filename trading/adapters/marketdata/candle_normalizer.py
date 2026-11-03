"""Raw inbound message -> validated :class:`~trading.core.marketdata.Candle`.

The bar-side counterpart of :class:`~trading.adapters.marketdata.normalizer.QuoteNormalizer`,
built the same way: read each field as the type it must be, refuse a float where
money is expected (INVARIANT 8), and let :class:`~trading.core.marketdata.Candle`
be the real validator -- it already refuses an incoherent bar (``low`` above
``high``, open/close outside the range, mixed currencies, negative volume, a
non-positive interval, a naive timestamp), and every such refusal becomes a
:class:`~trading.adapters.marketdata.normalizer.MalformedMessage` here rather
than an exception that escapes the boundary.

``open_time`` is parsed by the shared
:func:`~trading.adapters.marketdata.timestamps.parse_timestamp`, so a bar's time
follows exactly the same venue-stamp / receipt-time rule as a quote's. ``currency``
and ``volume_asset`` are supplied by the caller rather than read from the message,
for the same reason the quote normalizer takes a currency: a guessed denomination
would mislabel every bar, and this system does not guess a base asset from a
ticker.
"""

from __future__ import annotations

from decimal import Decimal, InvalidOperation
from typing import Mapping

from ...core.clock import Clock
from ...core.marketdata import Candle
from ...core.money import USD, Currency, Price, Quantity
from .normalizer import MalformedMessage
from .source import RawMessage
from .timestamps import BadTimestamp, parse_timestamp

__all__ = ["CandleNormalizer"]


class CandleNormalizer:
    """Maps a venue-shaped raw bar onto a validated :class:`Candle`."""

    def __init__(
        self,
        *,
        clock: Clock,
        currency: Currency = USD,
        volume_asset: str = "UNIT",
        source: str = "ingest",
        default_interval_seconds: int | None = None,
        symbol_field: str = "symbol",
        open_time_field: str = "open_time",
        interval_field: str = "interval_seconds",
        open_field: str = "open",
        high_field: str = "high",
        low_field: str = "low",
        close_field: str = "close",
        volume_field: str = "volume",
    ) -> None:
        if not isinstance(clock, Clock):
            raise TypeError("clock must be a Clock")
        if not isinstance(currency, Currency):
            raise TypeError("currency must be a Currency")
        if not isinstance(volume_asset, str) or not volume_asset.strip():
            raise ValueError("volume_asset must be a non-empty string")
        if not isinstance(source, str) or not source.strip():
            raise ValueError("source must be a non-empty string")
        if default_interval_seconds is not None and (
            isinstance(default_interval_seconds, bool)
            or not isinstance(default_interval_seconds, int)
            or default_interval_seconds <= 0
        ):
            raise ValueError("default_interval_seconds must be a positive int or None")
        self._clock = clock
        self._currency = currency
        self._volume_asset = volume_asset
        self._source = source
        self._default_interval = default_interval_seconds
        self._symbol_field = symbol_field
        self._open_time_field = open_time_field
        self._interval_field = interval_field
        self._open_field = open_field
        self._high_field = high_field
        self._low_field = low_field
        self._close_field = close_field
        self._volume_field = volume_field

    def normalize(self, raw: RawMessage) -> Candle:
        """Return the :class:`Candle` ``raw`` describes, or raise :class:`MalformedMessage`."""
        if not isinstance(raw, Mapping):
            raise MalformedMessage(
                f"a message must be a mapping, got {type(raw).__name__}"
            )
        symbol = self._require_str(raw, self._symbol_field)
        open_time = self._timestamp(raw, self._open_time_field)
        interval = self._interval(raw)
        open_amount = self._require_decimal(raw, self._open_field)
        high_amount = self._require_decimal(raw, self._high_field)
        low_amount = self._require_decimal(raw, self._low_field)
        close_amount = self._require_decimal(raw, self._close_field)
        volume_amount = self._require_decimal(raw, self._volume_field)

        # Candle's __post_init__ is the real validator: an incoherent OHLC, a
        # non-positive price (via Price), a negative volume, a naive open_time --
        # all become a malformed message here rather than an escaping exception.
        try:
            return Candle(
                symbol=symbol,
                open_time=open_time,
                interval_seconds=interval,
                open=Price(open_amount, self._currency),
                high=Price(high_amount, self._currency),
                low=Price(low_amount, self._currency),
                close=Price(close_amount, self._currency),
                volume=Quantity(volume_amount, self._volume_asset),
                source=self._source,
            )
        except (ValueError, TypeError, ArithmeticError) as exc:
            raise MalformedMessage(
                f"message for {symbol!r} is not a valid candle: {exc}"
            ) from exc

    # -- field readers ----------------------------------------------------

    def _require_str(self, raw: RawMessage, field: str) -> str:
        value = raw.get(field)
        if not isinstance(value, str) or not value.strip():
            raise MalformedMessage(f"message is missing a usable {field!r}")
        return value

    def _require_decimal(self, raw: RawMessage, field: str) -> Decimal:
        value = raw.get(field)
        if isinstance(value, Decimal):
            return value
        if isinstance(value, bool) or isinstance(value, float):
            raise MalformedMessage(
                f"{field!r} arrived as {type(value).__name__}, which cannot "
                "represent money exactly (INVARIANT 8)"
            )
        if isinstance(value, int):
            return Decimal(value)
        if isinstance(value, str):
            try:
                return Decimal(value.strip())
            except (InvalidOperation, ValueError) as exc:
                raise MalformedMessage(f"{field!r} is not a number: {value!r}") from exc
        raise MalformedMessage(f"message is missing a usable {field!r}")

    def _interval(self, raw: RawMessage) -> int:
        value = raw.get(self._interval_field)
        if value is None:
            if self._default_interval is None:
                raise MalformedMessage(
                    f"message has no {self._interval_field!r} and the normalizer "
                    "was given no default interval"
                )
            return self._default_interval
        if isinstance(value, bool):
            raise MalformedMessage(f"{self._interval_field!r} must be an integer")
        if isinstance(value, int):
            parsed = value
        elif isinstance(value, str):
            try:
                parsed = int(value.strip())
            except ValueError as exc:
                raise MalformedMessage(
                    f"{self._interval_field!r} is not an integer: {value!r}"
                ) from exc
        else:
            raise MalformedMessage(f"{self._interval_field!r} must be an integer")
        if parsed <= 0:
            raise MalformedMessage(f"{self._interval_field!r} must be positive")
        return parsed

    def _timestamp(self, raw: RawMessage, field: str):
        try:
            return parse_timestamp(raw.get(field), clock=self._clock, field=field)
        except BadTimestamp as exc:
            raise MalformedMessage(str(exc)) from exc
