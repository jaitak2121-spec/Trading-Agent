"""Finite, evidence-friendly observation of CoinSwitch public market data."""

from __future__ import annotations

import math
import threading
import time
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Callable, Protocol

from ..adapters.marketdata import FeedError, MarketDataSource
from ..core.secrets import global_redactor

__all__ = ["CoinSwitchObservationReport", "run_coinswitch_observation"]


class _ObservableCoinSwitchStream(Protocol):
    symbol: str
    exchange: str
    quote_source: MarketDataSource
    candle_source: MarketDataSource

    @property
    def reconnect_attempts(self) -> int: ...

    @property
    def successful_reconnects(self) -> int: ...

    def start(self) -> None: ...

    def stop(self) -> None: ...


@dataclass(frozen=True, slots=True)
class CoinSwitchObservationReport:
    symbol: str
    exchange: str
    duration_requested_seconds: float
    duration_elapsed_seconds: float
    poll_attempts: int
    quote_messages: int
    candle_messages: int
    feed_error_polls: int
    reconnect_attempts: int
    successful_reconnects: int
    last_feed_error: str | None
    failures: tuple[str, ...] = ()

    @property
    def passed(self) -> bool:
        return (
            self.duration_elapsed_seconds >= self.duration_requested_seconds
            and not self.failures
        )

    def as_details(self) -> dict[str, object]:
        return {
            "passed": self.passed,
            "symbol": self.symbol,
            "exchange": self.exchange,
            "duration_requested_seconds": self.duration_requested_seconds,
            "duration_elapsed_seconds": self.duration_elapsed_seconds,
            "poll_attempts": self.poll_attempts,
            "quote_messages": self.quote_messages,
            "candle_messages": self.candle_messages,
            "feed_error_polls": self.feed_error_polls,
            "reconnect_attempts": self.reconnect_attempts,
            "successful_reconnects": self.successful_reconnects,
            "last_feed_error": self.last_feed_error,
            "failures": list(self.failures),
        }

    def render(self) -> str:
        verdict = "PASS" if self.passed else "FAIL"
        lines = [
            f"CoinSwitch public-data observation: {verdict}",
            "========================================",
            f"market           : {self.exchange} {self.symbol}",
            f"duration         : {self.duration_elapsed_seconds:.3f}/"
            f"{self.duration_requested_seconds:.3f} seconds",
            f"poll attempts    : {self.poll_attempts}",
            f"messages         : {self.quote_messages} quotes, "
            f"{self.candle_messages} candles",
            f"feed-error polls : {self.feed_error_polls}",
            f"reconnections    : {self.successful_reconnects}/"
            f"{self.reconnect_attempts} successful/attempted",
        ]
        if self.last_feed_error is not None:
            lines.append(f"last feed error  : {self.last_feed_error}")
        if self.failures:
            lines.append("failures:")
            lines.extend(f"  - {failure}" for failure in self.failures)
        return "\n".join(lines)


def run_coinswitch_observation(
    stream: _ObservableCoinSwitchStream,
    *,
    duration_seconds: float,
    poll_interval_seconds: float = 1.0,
    wait: Callable[[float], object] | None = None,
    monotonic: Callable[[], float] | None = None,
    candle_sink: Callable[[Mapping[str, object]], object] | None = None,
) -> CoinSwitchObservationReport:
    """Observe both public channels for a fixed duration and stop the stream."""
    if not callable(getattr(stream, "start", None)) or not callable(
        getattr(stream, "stop", None)
    ):
        raise TypeError("stream must provide start() and stop()")
    if candle_sink is not None and not callable(candle_sink):
        raise TypeError("candle_sink must be callable or None")
    for name, value in (
        ("duration_seconds", duration_seconds),
        ("poll_interval_seconds", poll_interval_seconds),
    ):
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise TypeError(f"{name} must be numeric")
        if not math.isfinite(float(value)) or float(value) <= 0:
            raise ValueError(f"{name} must be finite and positive")

    duration = float(duration_seconds)
    interval = float(poll_interval_seconds)
    clock = monotonic or time.monotonic
    pause = wait or threading.Event().wait
    redactor = global_redactor()
    polls = quotes = candles = error_polls = 0
    last_error: str | None = None
    ended_in_error = False
    started = clock()

    try:
        stream.start()
    except FeedError as exc:
        message = redactor.redact(f"{type(exc).__name__}: {exc}")
        return CoinSwitchObservationReport(
            symbol=stream.symbol,
            exchange=stream.exchange,
            duration_requested_seconds=duration,
            duration_elapsed_seconds=max(0.0, clock() - started),
            poll_attempts=0,
            quote_messages=0,
            candle_messages=0,
            feed_error_polls=1,
            reconnect_attempts=stream.reconnect_attempts,
            successful_reconnects=stream.successful_reconnects,
            last_feed_error=message,
            failures=(f"public stream startup failed: {message}",),
        )
    observed_elapsed = 0.0
    try:
        while True:
            elapsed = max(0.0, clock() - started)
            if elapsed >= duration:
                break
            polls += 1
            poll_failed = False
            for source, kind in (
                (stream.quote_source, "quote"),
                (stream.candle_source, "candle"),
            ):
                try:
                    messages = source.poll()
                    count = len(messages)
                    if kind == "quote":
                        quotes += count
                    else:
                        candles += count
                        if candle_sink is not None:
                            for message in messages:
                                candle_sink(message)
                except FeedError as exc:
                    poll_failed = True
                    last_error = redactor.redact(
                        f"{type(exc).__name__}: {exc}"
                    )
            if poll_failed:
                error_polls += 1
                ended_in_error = True
            else:
                ended_in_error = False
            remaining = duration - max(0.0, clock() - started)
            if remaining > 0:
                pause(min(interval, remaining))
    finally:
        # The transport may need a bounded join while closing. That is cleanup
        # time, not market-observation time, so snapshot elapsed first.
        observed_elapsed = max(0.0, clock() - started)
        stream.stop()

    failures: list[str] = []
    if quotes == 0:
        failures.append("no public order-book quotes were observed")
    if candles == 0:
        failures.append("no closed one-minute trade candles were observed")
    if ended_in_error:
        failures.append("observation ended while the public feed was unavailable")
    return CoinSwitchObservationReport(
        symbol=stream.symbol,
        exchange=stream.exchange,
        duration_requested_seconds=duration,
        duration_elapsed_seconds=observed_elapsed,
        poll_attempts=polls,
        quote_messages=quotes,
        candle_messages=candles,
        feed_error_polls=error_polls,
        reconnect_attempts=stream.reconnect_attempts,
        successful_reconnects=stream.successful_reconnects,
        last_feed_error=last_error,
        failures=tuple(failures),
    )
