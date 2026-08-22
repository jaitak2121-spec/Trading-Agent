"""Circuit breakers.

A breaker isolates a failing dependency so that repeated failure does not turn
into repeated *blind retry*. In a trading system the danger is specific: if
order submission starts timing out, hammering the venue can create duplicate
positions we cannot see. Stopping quickly is safer than trying harder.

Two details matter more than the state machine itself:

* **Cooldown uses the monotonic clock**, never wall time. A backwards NTP jump
  must not shorten a cooldown, and a forwards jump must not end one early.
* **A failure in HALF_OPEN reopens immediately** and restarts the full
  cooldown. One successful probe is not evidence of recovery, so the number of
  consecutive successes required to close is configurable.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass
from enum import Enum
from typing import Final

from .audit import AuditCategory, AuditLog, AuditOutcome
from .authz import Action, Principal, authorize
from .clock import Clock
from .errors import CircuitBreakerOpen

__all__ = ["BreakerState", "BreakerSnapshot", "CircuitBreaker", "BreakerRegistry"]


class BreakerState(Enum):
    #: Calls flow normally.
    CLOSED = "closed"
    #: Calls are refused.
    OPEN = "open"
    #: A limited number of trial calls are allowed through.
    HALF_OPEN = "half_open"


@dataclass(frozen=True, slots=True)
class BreakerSnapshot:
    name: str
    state: BreakerState
    consecutive_failures: int
    consecutive_successes: int
    opened_count: int

    def as_details(self) -> dict[str, object]:
        return {
            "breaker": self.name,
            "state": self.state.value,
            "consecutive_failures": self.consecutive_failures,
            "consecutive_successes": self.consecutive_successes,
            "opened_count": self.opened_count,
        }


class CircuitBreaker:
    """A single named breaker. Thread-safe."""

    def __init__(
        self,
        name: str,
        *,
        clock: Clock,
        audit: AuditLog,
        failure_threshold: int = 3,
        reset_timeout_seconds: float = 60.0,
        successes_to_close: int = 2,
        half_open_max_calls: int = 1,
    ) -> None:
        if not name:
            raise ValueError("breaker name is required")
        if failure_threshold < 1:
            raise ValueError("failure_threshold must be >= 1")
        if reset_timeout_seconds <= 0:
            raise ValueError("reset_timeout_seconds must be > 0")
        if successes_to_close < 1:
            raise ValueError("successes_to_close must be >= 1")
        if half_open_max_calls < 1:
            raise ValueError("half_open_max_calls must be >= 1")

        self._name = name
        self._clock = clock
        self._audit = audit
        self._failure_threshold = failure_threshold
        self._reset_timeout = float(reset_timeout_seconds)
        self._successes_to_close = successes_to_close
        self._half_open_max_calls = half_open_max_calls

        self._state = BreakerState.CLOSED
        self._failures = 0
        self._successes = 0
        self._opened_at_mono: float | None = None
        self._half_open_in_flight = 0
        self._opened_count = 0
        self._lock = threading.RLock()

    @property
    def name(self) -> str:
        return self._name

    # -- state -------------------------------------------------------------
    @property
    def state(self) -> BreakerState:
        """Current state, applying any due OPEN -> HALF_OPEN promotion."""
        with self._lock:
            self._maybe_promote_to_half_open()
            return self._state

    def snapshot(self) -> BreakerSnapshot:
        with self._lock:
            self._maybe_promote_to_half_open()
            return BreakerSnapshot(
                name=self._name,
                state=self._state,
                consecutive_failures=self._failures,
                consecutive_successes=self._successes,
                opened_count=self._opened_count,
            )

    def _maybe_promote_to_half_open(self) -> None:
        """Caller holds the lock."""
        if self._state is not BreakerState.OPEN or self._opened_at_mono is None:
            return
        elapsed = self._clock.monotonic_seconds() - self._opened_at_mono
        if elapsed >= self._reset_timeout:
            self._state = BreakerState.HALF_OPEN
            self._successes = 0
            self._half_open_in_flight = 0
            self._audit.record(
                AuditCategory.BREAKER,
                "breaker_half_open",
                outcome=AuditOutcome.INFO,
                actor="system",
                details={"breaker": self._name, "elapsed_seconds": str(round(elapsed, 3))},
            )

    # -- gate --------------------------------------------------------------
    def require_closed(self) -> None:
        """Permit a call, or raise :class:`CircuitBreakerOpen`.

        In HALF_OPEN this reserves one of the limited trial slots; the caller
        must report the outcome via :meth:`record_success` or
        :meth:`record_failure` so the slot is returned.
        """
        with self._lock:
            self._maybe_promote_to_half_open()
            if self._state is BreakerState.CLOSED:
                return
            if self._state is BreakerState.OPEN:
                remaining = self._remaining_cooldown()
                raise CircuitBreakerOpen(
                    f"circuit breaker {self._name!r} is open "
                    f"({self._failures} consecutive failures); retry in "
                    f"{remaining:.1f}s"
                )
            # HALF_OPEN
            if self._half_open_in_flight >= self._half_open_max_calls:
                raise CircuitBreakerOpen(
                    f"circuit breaker {self._name!r} is half-open and already has "
                    f"{self._half_open_in_flight} trial call(s) in flight"
                )
            self._half_open_in_flight += 1

    def _remaining_cooldown(self) -> float:
        if self._opened_at_mono is None:
            return 0.0
        elapsed = self._clock.monotonic_seconds() - self._opened_at_mono
        return max(0.0, self._reset_timeout - elapsed)

    def allows_call(self) -> bool:
        """Non-raising probe. Does NOT reserve a half-open slot."""
        with self._lock:
            self._maybe_promote_to_half_open()
            if self._state is BreakerState.CLOSED:
                return True
            if self._state is BreakerState.OPEN:
                return False
            return self._half_open_in_flight < self._half_open_max_calls

    # -- outcome reporting -------------------------------------------------
    def record_success(self) -> None:
        with self._lock:
            if self._state is BreakerState.HALF_OPEN:
                self._half_open_in_flight = max(0, self._half_open_in_flight - 1)
                self._successes += 1
                if self._successes >= self._successes_to_close:
                    self._close("recovered")
                return
            self._failures = 0
            self._successes += 1

    def record_failure(self, *, reason: str = "") -> None:
        with self._lock:
            if self._state is BreakerState.HALF_OPEN:
                self._half_open_in_flight = max(0, self._half_open_in_flight - 1)
                # One failed probe is enough: reopen and restart the cooldown.
                self._open(reason or "half-open probe failed")
                return
            self._failures += 1
            self._successes = 0
            if self._state is BreakerState.CLOSED and self._failures >= self._failure_threshold:
                self._open(reason or f"{self._failures} consecutive failures")

    def _open(self, reason: str) -> None:
        """Caller holds the lock."""
        self._state = BreakerState.OPEN
        self._opened_at_mono = self._clock.monotonic_seconds()
        self._successes = 0
        self._half_open_in_flight = 0
        self._opened_count += 1
        self._audit.record(
            AuditCategory.BREAKER,
            "breaker_opened",
            outcome=AuditOutcome.REFUSED,
            actor="system",
            details={
                "breaker": self._name,
                "reason": reason,
                "consecutive_failures": self._failures,
            },
        )

    def _close(self, reason: str) -> None:
        """Caller holds the lock."""
        self._state = BreakerState.CLOSED
        self._failures = 0
        self._successes = 0
        self._opened_at_mono = None
        self._half_open_in_flight = 0
        self._audit.record(
            AuditCategory.BREAKER,
            "breaker_closed",
            outcome=AuditOutcome.ALLOWED,
            actor="system",
            details={"breaker": self._name, "reason": reason},
        )

    def reset(self, principal: Principal, *, reason: str) -> None:
        """Force the breaker closed. Operator only."""
        authorize(principal, Action.RESET_BREAKER)
        if not reason or not reason.strip():
            raise ValueError("resetting a breaker requires a reason")
        with self._lock:
            self._audit.record(
                AuditCategory.BREAKER,
                "breaker_manual_reset",
                outcome=AuditOutcome.ALLOWED,
                actor=principal.principal_id,
                details={"breaker": self._name, "reason": reason, "from": self._state.value},
            )
            self._close(f"manual reset: {reason}")


class BreakerRegistry:
    """Named collection of breakers, so a gateway can require all of them."""

    def __init__(self) -> None:
        self._breakers: dict[str, CircuitBreaker] = {}
        self._lock = threading.RLock()

    def add(self, breaker: CircuitBreaker) -> CircuitBreaker:
        with self._lock:
            if breaker.name in self._breakers:
                raise ValueError(f"breaker {breaker.name!r} is already registered")
            self._breakers[breaker.name] = breaker
            return breaker

    def get(self, name: str) -> CircuitBreaker:
        with self._lock:
            if name not in self._breakers:
                raise KeyError(f"no breaker named {name!r}")
            return self._breakers[name]

    def names(self) -> list[str]:
        with self._lock:
            return sorted(self._breakers)

    def require_all_closed(self) -> None:
        """Raise if ANY registered breaker refuses a call."""
        with self._lock:
            breakers = list(self._breakers.values())
        for breaker in breakers:
            if not breaker.allows_call():
                raise CircuitBreakerOpen(
                    f"circuit breaker {breaker.name!r} is {breaker.state.value}"
                )

    def snapshots(self) -> list[BreakerSnapshot]:
        with self._lock:
            breakers = list(self._breakers.values())
        return [b.snapshot() for b in breakers]
