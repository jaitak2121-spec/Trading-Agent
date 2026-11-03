"""The candle/bar ingestion driver.

The bar-side counterpart of :class:`~trading.adapters.marketdata.ingestor.MarketDataIngestor`,
and it follows the same discipline: :meth:`poll_once` is the whole behaviour, it
is synchronous and deterministic, and it holds no gateway, broker, or token, so an
ingested bar can inform a decision but can never be one.

It drains the injected :class:`~trading.adapters.marketdata.source.MarketDataSource`,
normalizes each raw message to a :class:`~trading.core.marketdata.Candle`, and
appends the valid, forward ones to an
:class:`~trading.adapters.marketdata.candle_store.InMemoryCandleStore`. Bars are a
regular grid of ``interval_seconds`` keyed on ``open_time``, so ordering is judged
on time rather than a sequence number:

* a bar whose ``open_time`` equals the last stored one is a **duplicate** -- dropped;
* a bar whose ``open_time`` is behind the last is **out of order** -- dropped;
* a bar more than one interval ahead leaves a **gap** -- the newer bar is applied
  and the gap is reported, and **the missing bars are never synthesized**;
* a bar whose interval disagrees with the one the symbol established is rejected.

On a source error it records ``INTERRUPTED`` and returns -- never raising, and
**never fabricating a bar to paper over the outage**. Unlike the quote ingestor it
does not drop the stored history: candles are historical, and discarding real bars
on a blip would lose data. Fail-closed here means *do not invent, and do not treat
missing data as valid*: the gap and the interruption are reported, loudly.
"""

from __future__ import annotations

import datetime as _dt
from dataclasses import dataclass
from typing import Mapping

from ...core.clock import Clock
from .candle_normalizer import CandleNormalizer
from .candle_store import InMemoryCandleStore
from .ingestor import FeedState, RejectedMessage
from .normalizer import MalformedMessage
from .source import FeedError, MarketDataSource

__all__ = ["BarGap", "CandleIngestReport", "CandleIngestor"]


@dataclass(frozen=True, slots=True)
class BarGap:
    """A run of missing bars between two received ones. Reported, never filled."""

    symbol: str
    expected: str  # ISO-8601 open_time of the first missing bar
    got: str  # ISO-8601 open_time of the bar that arrived instead
    missing: int

    def as_details(self) -> dict[str, object]:
        return {
            "symbol": self.symbol,
            "expected": self.expected,
            "got": self.got,
            "missing": self.missing,
        }


@dataclass(frozen=True, slots=True)
class CandleIngestReport:
    """What one :meth:`CandleIngestor.poll_once` did."""

    at: str
    state: str
    accepted: tuple[str, ...] = ()
    rejected: tuple[RejectedMessage, ...] = ()
    duplicates: int = 0
    out_of_order: int = 0
    gaps: tuple[BarGap, ...] = ()
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
            "error": self.error,
        }


class CandleIngestor:
    """Drains a source into a candle store, safely. Holds no execution surface."""

    def __init__(
        self,
        *,
        source: MarketDataSource,
        normalizer: CandleNormalizer,
        store: InMemoryCandleStore,
        clock: Clock,
    ) -> None:
        if not isinstance(source, MarketDataSource):
            raise TypeError("source must be a MarketDataSource")
        if not isinstance(normalizer, CandleNormalizer):
            raise TypeError("normalizer must be a CandleNormalizer")
        if not isinstance(store, InMemoryCandleStore):
            raise TypeError("store must be an InMemoryCandleStore")
        if not isinstance(clock, Clock):
            raise TypeError("clock must be a Clock")
        self._source = source
        self._normalizer = normalizer
        self._store = store
        self._clock = clock
        self._last_open: dict[str, _dt.datetime] = {}
        self._interval: dict[str, int] = {}
        self._state = FeedState.CONNECTED
        self._passes = 0
        self._last_report: CandleIngestReport | None = None

    # -- observation ------------------------------------------------------

    @property
    def state(self) -> FeedState:
        return self._state

    @property
    def passes(self) -> int:
        return self._passes

    @property
    def last_report(self) -> CandleIngestReport | None:
        return self._last_report

    # -- one sweep --------------------------------------------------------

    def poll_once(self) -> CandleIngestReport:
        """Drain the source once, append valid forward bars, report what happened.

        Never raises on a malformed bar or a connection error: both become
        entries in the returned report.
        """
        now = self._clock.now().isoformat()

        try:
            messages = self._source.poll()
        except FeedError as exc:
            return self._interrupt(now, f"{type(exc).__name__}: {exc}")
        except Exception as exc:  # noqa: BLE001 - unclassified == treat as a drop
            return self._interrupt(now, f"{type(exc).__name__}: {exc}")

        if self._state is FeedState.INTERRUPTED:
            self._state = FeedState.RECOVERING

        accepted: list[str] = []
        rejected: list[RejectedMessage] = []
        gaps: list[BarGap] = []
        duplicates = 0
        out_of_order = 0

        for raw in messages:
            try:
                candle = self._normalizer.normalize(raw)
            except MalformedMessage as exc:
                rejected.append(RejectedMessage(str(exc), self._preview(raw)))
                continue

            symbol = candle.symbol
            established = self._interval.get(symbol)
            if established is not None and candle.interval_seconds != established:
                rejected.append(
                    RejectedMessage(
                        f"bar for {symbol!r} has interval {candle.interval_seconds}s "
                        f"but the feed established {established}s",
                        self._preview(raw),
                    )
                )
                continue

            last = self._last_open.get(symbol)
            if last is not None and candle.open_time <= last:
                if candle.open_time == last:
                    duplicates += 1
                else:
                    out_of_order += 1
                continue

            if last is not None:
                interval = established or candle.interval_seconds
                delta = int((candle.open_time - last).total_seconds())
                # ceil-div minus one: full interval slots strictly between the
                # two bars. Zero unless the newer bar is more than one interval on.
                missing = max(0, (-(-delta // interval)) - 1)
                if missing >= 1:
                    expected = last + _dt.timedelta(seconds=interval)
                    gaps.append(
                        BarGap(
                            symbol,
                            expected.isoformat(),
                            candle.open_time.isoformat(),
                            missing,
                        )
                    )

            self._store.append(candle)
            self._last_open[symbol] = candle.open_time
            self._interval.setdefault(symbol, candle.interval_seconds)
            accepted.append(symbol)

        if accepted:
            self._state = FeedState.CONNECTED

        report = CandleIngestReport(
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

    def _interrupt(self, at: str, error: str) -> CandleIngestReport:
        # Candles are historical: retain the store, record the interruption, and
        # fabricate nothing. A gap across the outage is detected on the next bar.
        self._state = FeedState.INTERRUPTED
        report = CandleIngestReport(at=at, state=self._state.value, error=error)
        self._record(report)
        return report

    def _record(self, report: CandleIngestReport) -> None:
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
