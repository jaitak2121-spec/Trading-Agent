"""Injectable time source.

Wall-clock time is an input, not an ambient fact. Every safety control that
depends on time (circuit-breaker cooldowns, rate windows, daily loss resets,
audit timestamps) takes a :class:`Clock`. Tests use :class:`ManualClock` so
that time-dependent safety behaviour is deterministic and cannot be flaky.
"""

from __future__ import annotations

import datetime as _dt
import threading
from abc import ABC, abstractmethod

__all__ = ["Clock", "SystemClock", "ManualClock", "UTC"]

UTC = _dt.timezone.utc


class Clock(ABC):
    """A source of timezone-aware UTC timestamps."""

    @abstractmethod
    def now(self) -> _dt.datetime:
        """Return the current time as a timezone-aware UTC datetime."""

    def monotonic_seconds(self) -> float:
        """Seconds since an arbitrary epoch, guaranteed non-decreasing.

        Used for durations. Never use :meth:`now` differences for durations:
        wall clock can jump backwards (NTP, DST-adjacent misconfiguration) and
        a backwards jump could silently shorten a circuit-breaker cooldown.
        """
        raise NotImplementedError


class SystemClock(Clock):
    """Real wall-clock time. The only clock used outside tests."""

    __slots__ = ()

    def now(self) -> _dt.datetime:
        return _dt.datetime.now(tz=UTC)

    def monotonic_seconds(self) -> float:
        import time

        return time.monotonic()


class ManualClock(Clock):
    """A clock advanced explicitly by the caller. Test-only, but thread-safe."""

    __slots__ = ("_now", "_mono", "_lock")

    def __init__(self, start: _dt.datetime | None = None) -> None:
        if start is None:
            start = _dt.datetime(2026, 1, 1, tzinfo=UTC)
        if start.tzinfo is None:
            raise ValueError("ManualClock requires a timezone-aware datetime")
        self._now = start.astimezone(UTC)
        self._mono = 0.0
        self._lock = threading.Lock()

    def now(self) -> _dt.datetime:
        with self._lock:
            return self._now

    def monotonic_seconds(self) -> float:
        with self._lock:
            return self._mono

    def advance(self, seconds: float) -> None:
        """Move both wall clock and monotonic clock forward."""
        if seconds < 0:
            raise ValueError("ManualClock.advance requires seconds >= 0")
        with self._lock:
            self._now = self._now + _dt.timedelta(seconds=seconds)
            self._mono += seconds

    def set_wall_clock(self, when: _dt.datetime) -> None:
        """Move ONLY the wall clock, possibly backwards.

        Exists so tests can prove that safety timers do not rely on wall clock.
        """
        if when.tzinfo is None:
            raise ValueError("set_wall_clock requires a timezone-aware datetime")
        with self._lock:
            self._now = when.astimezone(UTC)
