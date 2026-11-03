"""Raw inbound message -> validated :class:`~trading.core.marketdata.Quote`.

Translation, never trust. Mirrors :class:`~trading.adapters.sandbox.broker.SandboxBroker`'s
``_interpret_*`` helpers and its float-rejecting field readers: a field that
cannot be read as the type it must be is a :class:`MalformedMessage`, not a
guess. A float where money is expected is refused (INVARIANT 8), a crossed book
is refused (delegated to :class:`~trading.core.marketdata.Quote`), and a
timestamp is parsed rather than assumed.

The timestamp rule is the one in :class:`~trading.ports.market_data.QuoteFeedPort`'s
contract: use the venue's own stamp where the message carries one, fall back to
receipt time only where it does not, and **never** override a present stamp with
"now" -- a receipt time written over a replayed older stamp makes stale data
look fresh, which is the precise failure the staleness machinery exists to catch.
"""

from __future__ import annotations

import datetime as _dt
from decimal import Decimal, InvalidOperation
from typing import Mapping

from ...core.clock import Clock
from ...core.marketdata import Quote
from ...core.money import USD, Currency, Price
from .source import RawMessage
from .timestamps import BadTimestamp, parse_timestamp

__all__ = ["MalformedMessage", "QuoteNormalizer"]


class MalformedMessage(ValueError):
    """A raw message could not be read as a quote. Rejected and counted by the
    ingestor, never fatal."""


class QuoteNormalizer:
    """Maps a venue-shaped raw message onto a validated :class:`Quote`.

    Field names are configurable because every venue names them differently;
    the defaults are the obvious ones. ``currency`` is supplied here rather than
    read from the message because a venue rarely stamps it on every tick and a
    guessed currency would mislabel every price.
    """

    def __init__(
        self,
        *,
        clock: Clock,
        currency: Currency = USD,
        source: str = "ingest",
        symbol_field: str = "symbol",
        bid_field: str = "bid",
        ask_field: str = "ask",
        timestamp_field: str = "timestamp",
        sequence_field: str = "sequence",
    ) -> None:
        if not isinstance(clock, Clock):
            raise TypeError("clock must be a Clock")
        if not isinstance(currency, Currency):
            raise TypeError("currency must be a Currency")
        if not isinstance(source, str) or not source.strip():
            raise ValueError("source must be a non-empty string")
        self._clock = clock
        self._currency = currency
        self._source = source
        self._symbol_field = symbol_field
        self._bid_field = bid_field
        self._ask_field = ask_field
        self._timestamp_field = timestamp_field
        self._sequence_field = sequence_field

    def normalize(self, raw: RawMessage) -> Quote:
        """Return the :class:`Quote` ``raw`` describes, or raise :class:`MalformedMessage`."""
        if not isinstance(raw, Mapping):
            raise MalformedMessage(
                f"a message must be a mapping, got {type(raw).__name__}"
            )
        symbol = self._require_str(raw, self._symbol_field)
        bid_amount = self._require_decimal(raw, self._bid_field)
        ask_amount = self._require_decimal(raw, self._ask_field)
        as_of = self._timestamp(raw)
        sequence = self._optional_sequence(raw)

        # Quote's own __post_init__ is the real validator: non-positive price
        # (via Price), crossed book, naive timestamp. Everything it can refuse is
        # a malformed message here, not an exception that escapes the boundary.
        try:
            return Quote(
                symbol=symbol,
                bid=Price(bid_amount, self._currency),
                ask=Price(ask_amount, self._currency),
                as_of=as_of,
                source=self._source,
                sequence=sequence,
            )
        except (ValueError, TypeError, ArithmeticError) as exc:
            raise MalformedMessage(
                f"message for {symbol!r} is not a valid quote: {exc}"
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
            # A float cannot represent money exactly, and a venue that sends one
            # is a venue whose price we refuse rather than round (INVARIANT 8).
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

    def _optional_sequence(self, raw: RawMessage) -> int | None:
        value = raw.get(self._sequence_field)
        if value is None:
            return None
        if isinstance(value, bool) or not isinstance(value, int):
            raise MalformedMessage(f"{self._sequence_field!r} must be an int")
        if value < 0:
            raise MalformedMessage(f"{self._sequence_field!r} must not be negative")
        return value

    # -- timestamps -------------------------------------------------------

    def _timestamp(self, raw: RawMessage) -> _dt.datetime:
        # The venue stamp where present, receipt time where absent -- the shared
        # parser is the single place that rule lives (the candle normalizer uses
        # it too). Its neutral BadTimestamp becomes this layer's MalformedMessage
        # so the ingestor's one except-clause still catches it.
        try:
            return parse_timestamp(
                raw.get(self._timestamp_field),
                clock=self._clock,
                field=self._timestamp_field,
            )
        except BadTimestamp as exc:
            raise MalformedMessage(str(exc)) from exc
