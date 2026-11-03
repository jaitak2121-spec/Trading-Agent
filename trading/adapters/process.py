"""Reviewed process-signal adapter for graceful application shutdown."""

from __future__ import annotations

import signal
import threading
from types import FrameType
from typing import Callable

__all__ = ["GracefulShutdown"]


class GracefulShutdown:
    """Turn SIGINT/SIGTERM into one idempotent shutdown event.

    The signal handler performs no cleanup itself. Cleanup stays in the main
    control flow, where the application closes owned components in order.
    """

    def __init__(self) -> None:
        self._event = threading.Event()
        self._reason: str | None = None
        self._previous: dict[int, Callable | int | None] = {}

    @property
    def requested(self) -> bool:
        return self._event.is_set()

    @property
    def reason(self) -> str | None:
        return self._reason

    def request(self, reason: str = "requested") -> None:
        if not isinstance(reason, str) or not reason.strip():
            raise ValueError("shutdown reason must be a non-empty string")
        if not self._event.is_set():
            self._reason = reason.strip()
            self._event.set()

    def wait(self, timeout: float | None = None) -> bool:
        return self._event.wait(timeout)

    def __enter__(self) -> "GracefulShutdown":
        if threading.current_thread() is not threading.main_thread():
            raise RuntimeError("process signal handlers require the main thread")
        for signum in (signal.SIGINT, signal.SIGTERM):
            self._previous[signum] = signal.getsignal(signum)
            signal.signal(signum, self._handle)
        return self

    def __exit__(self, *_exc: object) -> None:
        for signum, previous in self._previous.items():
            signal.signal(signum, previous)
        self._previous.clear()

    def _handle(self, signum: int, _frame: FrameType | None) -> None:
        try:
            name = signal.Signals(signum).name
        except ValueError:
            name = str(signum)
        self.request(name)
