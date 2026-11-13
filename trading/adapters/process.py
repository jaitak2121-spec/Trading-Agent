"""Reviewed process-signal adapter for graceful application shutdown."""

from __future__ import annotations

import signal
import subprocess
import os
import sys
import tempfile
import time
import threading
from types import FrameType
from typing import Callable

__all__ = ["GracefulShutdown"]



def notify_local_operator(message):
    """Opt-in macOS desktop delivery, using argv rather than interpolated code."""
    if sys.platform != 'darwin':
        return False
    script='on run argv\ndisplay notification (item 1 of argv) with title "Trading Agent — PAPER"\nend run'
    environment={key:os.environ[key] for key in ('PATH','LANG','TMPDIR','SYSTEMROOT') if key in os.environ}
    try:
        result=subprocess.run(['/usr/bin/osascript','-e',script,str(message)],
                              timeout=5,capture_output=True,text=True,env=environment)
        return result.returncode == 0
    except (OSError,subprocess.TimeoutExpired):
        return False


def _resident_bytes(pid, environment, timeout=2):
    try:
        result=subprocess.run(['/bin/ps','-o','rss=','-p',str(pid)],env=environment,
                              capture_output=True,text=True,timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        raise TimeoutError('research memory monitor timed out') from exc
    if result.returncode:
        if any(text in result.stderr.lower() for text in ('operation not permitted','permission denied')):
            raise PermissionError('environment denies child memory inspection')
        return None
    value=result.stdout.strip()
    return int(value)*1024 if value.isdecimal() else None


def run_bounded_research_job(config_path, *, timeout_seconds, memory_mb=1024,
                             cancelled=None):
    """Only the fixed research worker can execute; no shell or inherited secrets."""
    if type(timeout_seconds) is not int or not 1 <= timeout_seconds <= 86400:
        raise ValueError('research timeout must be 1..86400 seconds')
    if type(memory_mb) is not int or not 128 <= memory_mb <= 16384:
        raise ValueError('research memory budget must be 128..16384 MiB')
    if cancelled is not None and not callable(cancelled):
        raise TypeError('cancelled must be callable')
    if cancelled is not None and cancelled():
        raise InterruptedError('research shutdown requested')
    environment = {key: os.environ[key] for key in ('PATH', 'LANG', 'TMPDIR', 'SYSTEMROOT') if key in os.environ}
    environment['TRADING_RESEARCH_PARENT_PID']=str(os.getpid())
    started=time.monotonic()
    peak=None
    measurements=0
    with tempfile.TemporaryFile() as output, tempfile.TemporaryFile() as errors:
        with subprocess.Popen([sys.executable,'-m','trading.app.research_jobs',str(config_path),'--worker'],
                              env=environment,stdout=output,stderr=errors) as process:
            try:
                while process.poll() is None:
                    if cancelled is not None and cancelled():
                        raise InterruptedError('research shutdown requested')
                    if time.monotonic()-started >= timeout_seconds:
                        raise TimeoutError('research wall-time limit exceeded')
                    resident=_resident_bytes(process.pid,environment,
                        timeout=min(2,max(0.001,timeout_seconds-(time.monotonic()-started))))
                    if resident is None:
                        if process.poll() is None:
                            raise RuntimeError('memory monitor unavailable; research stopped')
                        break
                    peak=resident if peak is None else max(peak,resident)
                    measurements+=1
                    if resident > memory_mb*1024*1024:
                        raise RuntimeError('research memory budget exceeded')
                    try:
                        process.wait(timeout=min(0.2,max(0.001,timeout_seconds-(time.monotonic()-started))))
                    except subprocess.TimeoutExpired:
                        pass
                if process.returncode:
                    raise RuntimeError('bounded research worker failed; no candidate was deployed')
            finally:
                if process.poll() is None:
                    process.kill()
                process.wait()
    return {'wall_seconds':time.monotonic()-started,'memory_limit_mb':memory_mb,
            'memory_measurements':measurements,
            'peak_observed_rss_bytes':peak,'memory_enforcement':'sampled RSS; 0.2s polling plus measurement latency',
            'wall_timeout_seconds':timeout_seconds}


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
