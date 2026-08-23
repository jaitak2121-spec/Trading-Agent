"""Normalized market data, and the staleness rule that makes it safe to use.

Stage 1's :class:`~trading.ports.market_data.MarketDataPort` is deliberately
thin: one mark price per symbol, or ``None``. That is enough for a risk check but
it cannot answer the question live trading actually depends on -- *how old is
this number?* A price from four seconds ago and a price from four hours ago are
indistinguishable through that port, and sizing a position off the second one is
a way to lose money while every safety control reports green.

So Stage 2 adds a timestamped layer:

* :class:`Quote` -- a normalized two-sided quote carrying its own ``as_of`` and
  its source. Venue payloads differ wildly; everything is converted to this
  before any decision touches it.
* :class:`Candle` -- a normalized OHLCV bar, for strategies that need history.
* :class:`StalenessPolicy` -- how old is too old, and what to do when the answer
  is "I can't tell".
* :class:`FreshMarkPrices` -- the piece that matters. It wraps a
  :class:`~trading.ports.market_data.QuoteFeedPort` and *presents* a
  ``MarketDataPort``, returning ``None`` for any quote that fails the policy.

That last one is the whole design. Rather than teaching the risk engine and the
gateway about staleness -- new code on the paths Stage 1 already proved -- a
stale quote is converted into an absent one, and absence is a case the risk
engine has always refused (``RiskLimit.MARK_PRICE_AVAILABLE``). Stale data
therefore inherits a refusal path that is already tested end to end, and no gate
had to change to get it.

Fail-closed rules, in the order they are applied:

1. No quote at all -> ``MISSING``.
2. A quote older than ``max_age_seconds`` -> ``STALE``.
3. A quote timestamped in the *future* beyond a small tolerance -> ``STALE``.
   A clock disagreement is not evidence of freshness, and treating it as such
   would let a venue with a fast clock keep a dead feed looking alive.

Staleness is measured on the *wall* clock, unavoidably: a quote's ``as_of`` comes
from outside the process, so there is no monotonic reading to compare it against.
That is a real limitation and it is why :meth:`StalenessPolicy.assess` treats
both directions of clock disagreement as stale rather than only the past.
"""

from __future__ import annotations

import datetime as _dt
import decimal
import threading
from dataclasses import dataclass, field
from decimal import Decimal
from enum import Enum
from typing import Final, Iterable, Mapping

from .clock import UTC, Clock
from .errors import StaleMarketData
from .money import (
    FINANCIAL_CONTEXT,
    ROUND_HALF_EVEN,
    Currency,
    Price,
    Quantity,
)
from ..ports.market_data import MarketDataPort, QuoteFeedPort

__all__ = [
    "DEFAULT_MAX_AGE_SECONDS",
    "FUTURE_TOLERANCE_SECONDS",
    "Candle",
    "Freshness",
    "FreshMarkPrices",
    "MarketSnapshot",
    "Quote",
    "StalenessPolicy",
]

#: Default ceiling on quote age. Five seconds is short enough that a dead feed
#: is caught within one decision cycle, and long enough that ordinary jitter on
#: a polling adapter does not produce spurious refusals.
DEFAULT_MAX_AGE_SECONDS: Final = 5.0

#: How far ahead of our clock a venue timestamp may sit before we distrust it.
#: Small but non-zero: sub-second skew between hosts is normal, minutes are not.
FUTURE_TOLERANCE_SECONDS: Final = 2.0


class Freshness(Enum):
    """The verdict on a quote. Only ``FRESH`` may inform a decision."""

    FRESH = "fresh"
    STALE = "stale"
    MISSING = "missing"

    @property
    def is_usable(self) -> bool:
        return self is Freshness.FRESH


def _require_aware_utc(value: object, field_name: str) -> _dt.datetime:
    if not isinstance(value, _dt.datetime):
        raise TypeError(f"{field_name} must be a datetime, got {type(value).__name__}")
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(
            f"{field_name} must be timezone-aware; a naive timestamp cannot be "
            "compared against a UTC clock without guessing a zone"
        )
    return value.astimezone(UTC)


@dataclass(frozen=True)
class Quote:
    """A normalized two-sided quote for one symbol at one instant.

    Immutable, ``Decimal``-only, and self-describing about *when* and *where* it
    came from. Every adapter converts its venue's payload into this shape, so
    nothing downstream ever parses a venue-specific format.
    """

    symbol: str
    bid: Price
    ask: Price
    as_of: _dt.datetime
    source: str
    #: Venue sequence number where one is available. Used to discard
    #: out-of-order deliveries; ``None`` when the venue does not provide one.
    sequence: int | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.symbol, str) or not self.symbol.strip():
            raise ValueError("symbol must be a non-empty string")
        for name, value in (("bid", self.bid), ("ask", self.ask)):
            if not isinstance(value, Price):
                raise TypeError(
                    f"{name} for {self.symbol} must be a Price, got "
                    f"{type(value).__name__}; floats are rejected at the "
                    "boundary (INVARIANT 8)"
                )
        if self.bid.currency != self.ask.currency:
            raise ValueError(
                f"{self.symbol}: bid is in {self.bid.currency.code} but ask is "
                f"in {self.ask.currency.code}"
            )
        # A crossed book is a data error, not a trading opportunity. Accepting
        # one would let a mid-price land outside the spread and mis-size a
        # position in the direction of the error.
        if self.bid > self.ask:
            raise ValueError(
                f"{self.symbol}: crossed quote, bid {self.bid} exceeds ask "
                f"{self.ask}"
            )
        if not isinstance(self.source, str) or not self.source.strip():
            raise ValueError("source must be a non-empty string")
        if self.sequence is not None:
            if isinstance(self.sequence, bool) or not isinstance(self.sequence, int):
                raise TypeError("sequence must be an int or None")
            if self.sequence < 0:
                raise ValueError("sequence must not be negative")
        object.__setattr__(self, "as_of", _require_aware_utc(self.as_of, "as_of"))

    @property
    def currency(self) -> Currency:
        return self.bid.currency

    @property
    def mid(self) -> Price:
        """Midpoint of the spread.

        ``ROUND_HALF_EVEN`` because a mid is a neutral reference, not a number
        anyone trades at -- there is no conservative direction to prefer. The
        directional numbers are :attr:`bid` and :attr:`ask`, and callers who need
        an executable price must use those.
        """
        with decimal.localcontext(FINANCIAL_CONTEXT):
            raw = (self.bid.amount + self.ask.amount) / Decimal(2)
            # Quantize to the finer of the two inputs' scales, so a mid never
            # invents precision the venue did not provide.
            exponent = min(
                self.bid.amount.as_tuple().exponent,
                self.ask.amount.as_tuple().exponent,
            )
            quantized = raw.quantize(
                Decimal(1).scaleb(exponent), rounding=ROUND_HALF_EVEN
            )
        return Price(quantized, self.currency)

    @property
    def spread(self) -> Decimal:
        """Absolute spread, as a bare ``Decimal`` -- it is a difference, not a price."""
        with decimal.localcontext(FINANCIAL_CONTEXT):
            return self.ask.amount - self.bid.amount

    @property
    def spread_bps(self) -> Decimal:
        """Spread in basis points of the mid. A liquidity signal, and a sanity check."""
        with decimal.localcontext(FINANCIAL_CONTEXT):
            return self.spread / self.mid.amount * Decimal(10_000)

    def age_seconds(self, now: _dt.datetime) -> float:
        """Seconds between this quote and ``now``. Negative if the quote leads."""
        return (_require_aware_utc(now, "now") - self.as_of).total_seconds()

    def price_for(self, side: "OrderSideLike") -> Price:
        """The side of the book a taker would actually cross.

        A buy lifts the ask, a sell hits the bid. Using the mid for either would
        understate cost, which is the wrong direction for a risk check.
        """
        name = getattr(side, "value", side)
        if name in ("buy", "BUY"):
            return self.ask
        if name in ("sell", "SELL"):
            return self.bid
        raise ValueError(f"unrecognised side {side!r}")

    def as_details(self) -> dict[str, object]:
        """Audit-safe representation. No secrets, all strings."""
        return {
            "symbol": self.symbol,
            "bid": str(self.bid.amount),
            "ask": str(self.ask.amount),
            "currency": self.currency.code,
            "as_of": self.as_of.isoformat(),
            "source": self.source,
            "sequence": self.sequence,
        }


#: Structural stand-in so this module need not import the order layer. Anything
#: with a ``.value`` of "buy"/"sell", which is what ``OrderSide`` provides.
OrderSideLike = object


@dataclass(frozen=True)
class Candle:
    """A normalized OHLCV bar.

    ``open_time`` is the bar's *start*, and a bar is only complete once
    ``open_time + interval`` has passed -- a strategy reading an in-progress bar
    as if it were closed is a classic backtest-to-live divergence, so
    :attr:`is_complete` makes the distinction explicit rather than implied.
    """

    symbol: str
    open_time: _dt.datetime
    interval_seconds: int
    open: Price
    high: Price
    low: Price
    close: Price
    volume: Quantity
    source: str = "unknown"

    def __post_init__(self) -> None:
        if not isinstance(self.symbol, str) or not self.symbol.strip():
            raise ValueError("symbol must be a non-empty string")
        if isinstance(self.interval_seconds, bool) or not isinstance(
            self.interval_seconds, int
        ):
            raise TypeError("interval_seconds must be an int")
        if self.interval_seconds <= 0:
            raise ValueError("interval_seconds must be positive")
        prices = {
            "open": self.open,
            "high": self.high,
            "low": self.low,
            "close": self.close,
        }
        for name, value in prices.items():
            if not isinstance(value, Price):
                raise TypeError(f"{name} must be a Price, got {type(value).__name__}")
        currencies = {p.currency for p in prices.values()}
        if len(currencies) != 1:
            raise ValueError(
                f"{self.symbol}: OHLC prices mix currencies "
                f"{sorted(c.code for c in currencies)}"
            )
        if not isinstance(self.volume, Quantity):
            raise TypeError("volume must be a Quantity")
        if self.volume.amount < 0:
            raise ValueError("volume must not be negative")
        # An inconsistent bar means the feed or the normalizer is broken.
        # Trading off it produces stops that could never have been hit.
        if self.low > self.high:
            raise ValueError(f"{self.symbol}: low {self.low} exceeds high {self.high}")
        for name in ("open", "close"):
            value = prices[name]
            if value < self.low or value > self.high:
                raise ValueError(
                    f"{self.symbol}: {name} {value} lies outside the "
                    f"low-high range [{self.low}, {self.high}]"
                )
        object.__setattr__(
            self, "open_time", _require_aware_utc(self.open_time, "open_time")
        )

    @property
    def close_time(self) -> _dt.datetime:
        return self.open_time + _dt.timedelta(seconds=self.interval_seconds)

    def is_complete(self, now: _dt.datetime) -> bool:
        """Whether the bar has closed as of ``now``."""
        return _require_aware_utc(now, "now") >= self.close_time

    @property
    def is_up(self) -> bool:
        return self.close > self.open

    @property
    def range(self) -> Decimal:
        with decimal.localcontext(FINANCIAL_CONTEXT):
            return self.high.amount - self.low.amount


@dataclass(frozen=True)
class StalenessPolicy:
    """How old a quote may be before it stops being evidence.

    Separate policies for separate uses is the point: advisory output can
    tolerate a slightly older quote than an order about to be sent, because the
    consequence of being wrong differs by an order of magnitude.
    """

    max_age_seconds: float = DEFAULT_MAX_AGE_SECONDS
    future_tolerance_seconds: float = FUTURE_TOLERANCE_SECONDS

    def __post_init__(self) -> None:
        for name in ("max_age_seconds", "future_tolerance_seconds"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise TypeError(f"{name} must be a number")
            if value < 0:
                raise ValueError(f"{name} must not be negative")

    def assess(self, quote: Quote | None, *, now: _dt.datetime) -> Freshness:
        """Classify ``quote``. Anything but ``FRESH`` must not inform a decision."""
        if quote is None:
            return Freshness.MISSING
        age = quote.age_seconds(now)
        if age > self.max_age_seconds:
            return Freshness.STALE
        if age < -self.future_tolerance_seconds:
            # The venue claims a time we have not reached. Our clock and theirs
            # disagree, so we do not actually know this quote's age.
            return Freshness.STALE
        return Freshness.FRESH

    def require_fresh(self, quote: Quote | None, *, symbol: str, now) -> Quote:
        """Return ``quote`` if fresh, else raise :class:`StaleMarketData`.

        For callers that want a loud failure rather than a silent ``None`` --
        chiefly advisory code, which has no gateway to refuse on its behalf.
        """
        verdict = self.assess(quote, now=now)
        if verdict is Freshness.FRESH:
            assert quote is not None
            return quote
        if verdict is Freshness.MISSING:
            raise StaleMarketData(f"no quote available for {symbol}")
        assert quote is not None
        raise StaleMarketData(
            f"quote for {symbol} is stale: {quote.age_seconds(now):.3f}s old, "
            f"limit {self.max_age_seconds}s (source {quote.source})"
        )


@dataclass(frozen=True)
class MarketSnapshot:
    """Several quotes captured together, with the instant they were taken.

    A decision made across symbols should be made against one snapshot rather
    than several independent reads, so that a strategy cannot see symbol A at
    one moment and symbol B at another.
    """

    as_of: _dt.datetime
    quotes: Mapping[str, Quote] = field(default_factory=dict)

    def __post_init__(self) -> None:
        for symbol, quote in self.quotes.items():
            if not isinstance(quote, Quote):
                raise TypeError(f"quote for {symbol} must be a Quote")
            if quote.symbol != symbol:
                raise ValueError(
                    f"quote keyed as {symbol!r} reports symbol {quote.symbol!r}"
                )
        object.__setattr__(self, "quotes", dict(self.quotes))
        object.__setattr__(self, "as_of", _require_aware_utc(self.as_of, "as_of"))

    @property
    def symbols(self) -> list[str]:
        return sorted(self.quotes)

    def quote(self, symbol: str) -> Quote | None:
        return self.quotes.get(symbol)

    def fresh_symbols(self, policy: StalenessPolicy) -> list[str]:
        return sorted(
            s
            for s, q in self.quotes.items()
            if policy.assess(q, now=self.as_of).is_usable
        )

    def stale_symbols(self, policy: StalenessPolicy) -> list[str]:
        return sorted(
            s
            for s, q in self.quotes.items()
            if not policy.assess(q, now=self.as_of).is_usable
        )

    def mark_prices(self, policy: StalenessPolicy) -> dict[str, Price]:
        """Mid prices for the fresh quotes only.

        Stale symbols are *omitted* rather than included with a warning, so the
        result can be handed straight to the risk engine, which refuses on
        absence.
        """
        return {
            s: q.mid
            for s, q in self.quotes.items()
            if policy.assess(q, now=self.as_of).is_usable
        }


class FreshMarkPrices(MarketDataPort):
    """A ``MarketDataPort`` view over a quote feed that hides stale quotes.

    This is the join between Stage 2's timestamped data and Stage 1's proven
    refusal path. The risk engine already treats a missing mark price as a
    violation (``RiskLimit.MARK_PRICE_AVAILABLE``); by reporting a stale quote as
    missing, a frozen feed becomes an ordinary risk refusal at the existing gate
    instead of a new special case threaded through the gateway.

    It also counts what it suppressed, because a silent suppression is how a
    feed outage becomes invisible: :attr:`suppressed_count` and
    :attr:`last_suppressed` give monitoring something to alarm on.
    """

    def __init__(
        self,
        feed: QuoteFeedPort,
        *,
        clock: Clock,
        policy: StalenessPolicy | None = None,
    ) -> None:
        if not isinstance(feed, QuoteFeedPort):
            raise TypeError(
                f"feed must implement QuoteFeedPort, got {type(feed).__name__}"
            )
        if not isinstance(clock, Clock):
            raise TypeError("clock must be a Clock")
        self._feed = feed
        self._clock = clock
        self._policy = policy or StalenessPolicy()
        self._lock = threading.Lock()
        self._suppressed: dict[str, Freshness] = {}
        self._suppressed_count = 0

    @property
    def policy(self) -> StalenessPolicy:
        return self._policy

    @property
    def suppressed_count(self) -> int:
        """How many times a quote has been withheld for failing the policy."""
        with self._lock:
            return self._suppressed_count

    @property
    def last_suppressed(self) -> dict[str, Freshness]:
        """The most recent verdict per symbol that was withheld."""
        with self._lock:
            return dict(self._suppressed)

    def assess(self, symbol: str) -> Freshness:
        """The freshness verdict for ``symbol`` right now, without hiding it.

        Advisory and monitoring code needs to *say* that data is stale, which it
        cannot do if the only interface silently returns ``None``.
        """
        return self._policy.assess(self._feed.quote(symbol), now=self._clock.now())

    def quote(self, symbol: str) -> Quote | None:
        """The underlying quote if fresh, else ``None``."""
        quote = self._feed.quote(symbol)
        if self._policy.assess(quote, now=self._clock.now()).is_usable:
            return quote
        self._note_suppression(symbol, quote)
        return None

    def mark_price(self, symbol: str) -> Price | None:
        quote = self.quote(symbol)
        return None if quote is None else quote.mid

    def snapshot(self, symbols: Iterable[str]) -> MarketSnapshot:
        """Every quote for ``symbols``, fresh or not, captured at one instant.

        Deliberately unfiltered: this is the honest view, for advisory output and
        monitoring that must report staleness rather than route around it. Use
        :meth:`mark_price` when the consumer should simply refuse.
        """
        now = self._clock.now()
        quotes = {}
        for symbol in symbols:
            quote = self._feed.quote(symbol)
            if quote is not None:
                quotes[symbol] = quote
        return MarketSnapshot(as_of=now, quotes=quotes)

    def _note_suppression(self, symbol: str, quote: Quote | None) -> None:
        verdict = Freshness.MISSING if quote is None else Freshness.STALE
        with self._lock:
            self._suppressed[symbol] = verdict
            self._suppressed_count += 1
