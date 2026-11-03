"""The market-data ingestion driver.

Mirrors :class:`trading.adapters.lifecycle.LifecyclePoller`: :meth:`poll_once` is
the whole behaviour and is synchronous and deterministic, and it holds no
execution surface -- no gateway, no broker, no token -- so ingested data can
inform a decision but can never be one (INVARIANT 3). It drains the injected
:class:`~trading.adapters.marketdata.source.MarketDataSource`, normalizes each
raw message to a :class:`~trading.core.marketdata.Quote`, applies the good ones
to an :class:`~trading.adapters.memory.quote_feed.InMemoryQuoteFeed`, routes a
malformed one to a counted rejection (never a crash), classifies duplicates and
out-of-order messages against the per-symbol sequence, reports a forward
sequence gap, and -- on a connection error from the source -- marks the known
symbols unavailable (``go_dark``) so the risk engine's existing
``MARK_PRICE_AVAILABLE`` gate refuses while the feed is down.

The last point is the whole safety story of this module: it never decides
anything, it only makes data available or unavailable, and "unavailable" is a
refusal the gateway already enforces. A feed outage therefore fails closed with
no change to the execution chokepoint.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Iterable, Mapping

from ...core.clock import Clock
from ..memory.quote_feed import InMemoryQuoteFeed
from .normalizer import MalformedMessage, QuoteNormalizer
from .source import FeedError, MarketDataSource, RawMessage

__all__ = [
    "FeedState",
    "IngestReport",
    "MarketDataIngestor",
    "RejectedMessage",
    "SequenceGap",
]


class FeedState(Enum):
    """Where the ingestion link stands right now."""

    #: Messages are flowing and being applied.
    CONNECTED = "connected"
    #: The source raised; data has been marked unavailable until it returns.
    INTERRUPTED = "interrupted"
    #: The link is back but has not yet delivered a message to confirm it.
    RECOVERING = "recovering"


@dataclass(frozen=True, slots=True)
class RejectedMessage:
    """One raw message that could not be read, and why. Counted, never fatal."""

    reason: str
    preview: str

    def as_details(self) -> dict[str, object]:
        return {"reason": self.reason, "preview": self.preview}


@dataclass(frozen=True, slots=True)
class SequenceGap:
    """A forward jump in a symbol's sequence: messages between were never seen."""

    symbol: str
    expected: int
    got: int

    def as_details(self) -> dict[str, object]:
        return {"symbol": self.symbol, "expected": self.expected, "got": self.got}


@dataclass(frozen=True, slots=True)
class IngestReport:
    """What one :meth:`MarketDataIngestor.poll_once` did.

    ``error`` set with ``state == interrupted`` is the honest shape of an outage;
    a clean poll has ``error is None``. ``healthy`` is for a monitor to read.
    """

    at: str
    state: str
    accepted: tuple[str, ...] = ()
    rejected: tuple[RejectedMessage, ...] = ()
    duplicates: int = 0
    out_of_order: int = 0
    gaps: tuple[SequenceGap, ...] = ()
    dropped: tuple[str, ...] = ()
    error: str | None = None

    @property
    def healthy(self) -> bool:
        return self.error is None and not self.rejected and not self.gaps

    def as_details(self) -> dict[str, object]:
        return {
            "at": self.at,
            "state": self.state,
            "accepted": list(self.accepted),
            "rejected": [r.as_details() for r in self.rejected],
            "duplicates": self.duplicates,
            "out_of_order": self.out_of_order,
            "gaps": [g.as_details() for g in self.gaps],
            "dropped": list(self.dropped),
            "error": self.error,
        }


class MarketDataIngestor:
    """Drains a source into a quote feed, safely. Holds no execution surface."""

    def __init__(
        self,
        *,
        source: MarketDataSource,
        normalizer: QuoteNormalizer,
        feed: InMemoryQuoteFeed,
        clock: Clock,
        symbols: Iterable[str] | None = None,
    ) -> None:
        if not isinstance(source, MarketDataSource):
            raise TypeError("source must be a MarketDataSource")
        if not isinstance(normalizer, QuoteNormalizer):
            raise TypeError("normalizer must be a QuoteNormalizer")
        if not isinstance(feed, InMemoryQuoteFeed):
            raise TypeError("feed must be an InMemoryQuoteFeed")
        if not isinstance(clock, Clock):
            raise TypeError("clock must be a Clock")
        self._source = source
        self._normalizer = normalizer
        self._feed = feed
        self._clock = clock
        self._configured_symbols = tuple(symbols) if symbols is not None else ()
        self._seen_symbols: set[str] = set()
        self._last_sequence: dict[str, int] = {}
        self._last_message_at: dict[str, str] = {}
        self._state = FeedState.CONNECTED
        self._passes = 0
        self._last_report: IngestReport | None = None

    # -- observation ------------------------------------------------------

    @property
    def state(self) -> FeedState:
        return self._state

    @property
    def passes(self) -> int:
        return self._passes

    @property
    def last_report(self) -> IngestReport | None:
        return self._last_report

    @property
    def last_message_at(self) -> dict[str, str]:
        return dict(self._last_message_at)

    # -- one sweep --------------------------------------------------------

    def poll_once(self) -> IngestReport:
        """Drain the source once, apply what is valid, report what happened.

        Never raises on a malformed message or a connection error: both become
        entries in the returned :class:`IngestReport`. The only way out is a
        report.
        """
        now = self._clock.now().isoformat()

        try:
            messages = self._source.poll()
        except FeedError as exc:
            return self._interrupt(now, f"{type(exc).__name__}: {exc}")
        except Exception as exc:  # noqa: BLE001 - unclassified == treat as a drop
            # An error we did not classify says nothing about whether the stream
            # survived, so the fail-safe reading is that it did not.
            return self._interrupt(now, f"{type(exc).__name__}: {exc}")

        if self._state is FeedState.INTERRUPTED:
            # The link answered without raising; it is back, but unconfirmed
            # until a message actually lands.
            self._state = FeedState.RECOVERING

        accepted: list[str] = []
        rejected: list[RejectedMessage] = []
        gaps: list[SequenceGap] = []
        duplicates = 0
        out_of_order = 0

        for raw in messages:
            try:
                quote = self._normalizer.normalize(raw)
            except MalformedMessage as exc:
                rejected.append(RejectedMessage(str(exc), self._preview(raw)))
                continue

            symbol = quote.symbol
            seq = quote.sequence
            self._seen_symbols.add(symbol)
            last = self._last_sequence.get(symbol)

            if seq is not None and last is not None and seq <= last:
                # Already seen it, or it arrived behind one we have. Either way
                # applying it would move the book backwards; drop and count.
                if seq == last:
                    duplicates += 1
                else:
                    out_of_order += 1
                continue

            if seq is not None and last is not None and seq > last + 1:
                # The newer quote is still applied -- we take the latest truth --
                # but the gap is reported, never filled in with a guess.
                gaps.append(SequenceGap(symbol, last + 1, seq))

            published = self._feed.publish(
                symbol, quote.bid, quote.ask, as_of=quote.as_of, sequence=seq
            )
            if published is None:
                # The feed's own out-of-order guard rejected it. Should not
                # happen given the checks above, but counting it is honest.
                out_of_order += 1
                continue

            if seq is not None:
                self._last_sequence[symbol] = seq
            self._last_message_at[symbol] = quote.as_of.isoformat()
            accepted.append(symbol)

        if accepted:
            self._state = FeedState.CONNECTED

        report = IngestReport(
            at=now,
            state=self._state.value,
            accepted=tuple(accepted),
            rejected=tuple(rejected),
            duplicates=duplicates,
            out_of_order=out_of_order,
            gaps=tuple(gaps),
        )
        self._record(report)
        return report

    # -- internals --------------------------------------------------------

    def _interrupt(self, at: str, error: str) -> IngestReport:
        """Mark every known symbol unavailable so the risk gate refuses."""
        self._state = FeedState.INTERRUPTED
        dropped = sorted(
            set(self._configured_symbols)
            | self._seen_symbols
            | set(self._feed.symbols())
        )
        for symbol in dropped:
            # go_dark drops the quote entirely, so mark_price becomes None at
            # once rather than aging out -- a known outage fails closed now, not
            # in max_age_seconds. Safe on a symbol with no quote.
            self._feed.go_dark(symbol)
        report = IngestReport(
            at=at, state=self._state.value, dropped=tuple(dropped), error=error
        )
        self._record(report)
        return report

    def _record(self, report: IngestReport) -> None:
        self._passes += 1
        self._last_report = report

    def _preview(self, raw: object) -> str:
        try:
            if isinstance(raw, Mapping):
                text = ", ".join(f"{k}={raw[k]!r}" for k in list(raw)[:6])
            else:
                text = repr(raw)
        except Exception:  # noqa: BLE001 - a preview must never itself raise
            text = "<unreadable message>"
        return text[:200]
