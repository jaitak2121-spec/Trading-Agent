"""Process-level ownership for the synchronous real-time paper cycle.

The service deliberately exposes one deterministic ``poll_once`` operation.
This module adds only scheduling, shutdown, and observation.  It never submits
an order itself and it never retries a failed cycle: an unexpected exception
stops the worker and remains visible until an operator starts a new supervisor.
"""

from __future__ import annotations

import datetime as _dt
import threading
from dataclasses import dataclass
from typing import Protocol

from ..core.errors import ConfigurationError, SafetyViolation
from ..core.secrets import global_redactor
from .paper_service import PaperServiceCycle

__all__ = ["PaperServiceSupervisor", "SupervisorStatus"]

DEFAULT_POLL_INTERVAL_SECONDS = 1.0
DEFAULT_STOP_TIMEOUT_SECONDS = 30.0


class _PollablePaperService(Protocol):
    @property
    def cycles(self) -> int: ...

    @property
    def last_cycle(self) -> PaperServiceCycle | None: ...

    def poll_once(self) -> PaperServiceCycle: ...


def _utc_now() -> str:
    return _dt.datetime.now(_dt.timezone.utc).isoformat()


@dataclass(frozen=True, slots=True)
class SupervisorStatus:
    running: bool
    passes: int
    interval_seconds: float
    started_at: str | None = None
    stopped_at: str | None = None
    last_error: str | None = None
    last_cycle: PaperServiceCycle | None = None

    @property
    def healthy(self) -> bool:
        return self.running and self.last_error is None

    def as_details(self) -> dict[str, object]:
        return {
            "running": self.running,
            "healthy": self.healthy,
            "passes": self.passes,
            "interval_seconds": self.interval_seconds,
            "started_at": self.started_at,
            "stopped_at": self.stopped_at,
            "last_error": self.last_error,
            "last_cycle": (
                None if self.last_cycle is None else self.last_cycle.as_details()
            ),
        }


class PaperServiceSupervisor:
    """Run one paper service on one worker, stopping visibly on failure."""

    def __init__(
        self,
        service: _PollablePaperService,
        *,
        interval_seconds: float = DEFAULT_POLL_INTERVAL_SECONDS,
    ) -> None:
        if not callable(getattr(service, "poll_once", None)):
            raise TypeError("service must provide poll_once()")
        interval = float(interval_seconds)
        if not interval > 0:
            raise ConfigurationError(
                f"interval_seconds must be positive, got {interval_seconds!r}"
            )
        self._service = service
        self._interval = interval
        self._lock = threading.RLock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._passes = 0
        self._started_at: str | None = None
        self._stopped_at: str | None = None
        self._last_error: str | None = None
        self._last_cycle: PaperServiceCycle | None = None

    def start(self) -> None:
        with self._lock:
            if self._thread is not None:
                raise SafetyViolation("paper service supervisor is already running")
            self._stop.clear()
            self._passes = 0
            self._started_at = _utc_now()
            self._stopped_at = None
            self._last_error = None
            self._last_cycle = None
            thread = threading.Thread(
                target=self._run,
                name="paper-service-supervisor",
                daemon=True,
            )
            self._thread = thread
            thread.start()

    def stop(
        self, *, timeout_seconds: float = DEFAULT_STOP_TIMEOUT_SECONDS
    ) -> None:
        self._stop.set()
        with self._lock:
            thread = self._thread
        if thread is None:
            return
        thread.join(timeout_seconds)
        if thread.is_alive():
            raise SafetyViolation(
                f"paper service supervisor did not stop within {timeout_seconds} "
                "seconds; a cycle is still in progress"
            )
        with self._lock:
            if self._thread is thread:
                self._thread = None
            if self._stopped_at is None:
                self._stopped_at = _utc_now()

    @property
    def is_running(self) -> bool:
        with self._lock:
            thread = self._thread
        return thread is not None and thread.is_alive()

    def status(self) -> SupervisorStatus:
        with self._lock:
            return SupervisorStatus(
                running=self.is_running,
                passes=self._passes,
                interval_seconds=self._interval,
                started_at=self._started_at,
                stopped_at=self._stopped_at,
                last_error=self._last_error,
                last_cycle=self._last_cycle,
            )

    def _run(self) -> None:
        try:
            while not self._stop.is_set():
                cycle = self._service.poll_once()
                with self._lock:
                    self._last_cycle = cycle
                    self._passes += 1
                self._stop.wait(self._interval)
        except BaseException as exc:  # noqa: BLE001 - worker failure is reported
            message = global_redactor().redact(f"{type(exc).__name__}: {exc}")
            with self._lock:
                self._last_error = message
            self._stop.set()
        finally:
            with self._lock:
                self._stopped_at = _utc_now()
                # Clear our ownership only after every observable field above is
                # final, so a reader never sees "stopped" with stale status.
                if self._thread is threading.current_thread():
                    self._thread = None
