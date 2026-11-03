"""Repeatable end-to-end soak verification for the assembled paper app."""

from __future__ import annotations

import math
import threading
import time
from dataclasses import dataclass, replace
from typing import Callable

from ..core.secrets import global_redactor
from .runtime import PaperApplication

__all__ = ["SoakReport", "run_soak", "run_timed_soak"]


@dataclass(frozen=True, slots=True)
class SoakReport:
    cycles_requested: int
    cycles_completed: int
    healthy_cycles: int
    executed: int
    refused: int
    unknown: int
    not_sized: int
    total_orders: int
    open_positions: int
    final_cash: str
    realized_pnl: str
    audit_records: int
    failures: tuple[str, ...] = ()
    duration_requested_seconds: float | None = None
    duration_elapsed_seconds: float | None = None

    @property
    def passed(self) -> bool:
        duration_complete = (
            self.duration_requested_seconds is None
            or (
                self.duration_elapsed_seconds is not None
                and self.duration_elapsed_seconds >= self.duration_requested_seconds
            )
        )
        return (
            self.cycles_completed == self.cycles_requested
            and not self.failures
            and duration_complete
        )

    def as_details(self) -> dict[str, object]:
        return {
            "passed": self.passed,
            "cycles_requested": self.cycles_requested,
            "cycles_completed": self.cycles_completed,
            "healthy_cycles": self.healthy_cycles,
            "executed": self.executed,
            "refused": self.refused,
            "unknown": self.unknown,
            "not_sized": self.not_sized,
            "total_orders": self.total_orders,
            "open_positions": self.open_positions,
            "final_cash": self.final_cash,
            "realized_pnl": self.realized_pnl,
            "audit_records": self.audit_records,
            "duration_requested_seconds": self.duration_requested_seconds,
            "duration_elapsed_seconds": self.duration_elapsed_seconds,
            "failures": list(self.failures),
        }

    def render(self) -> str:
        verdict = "PASS" if self.passed else "FAIL"
        lines = [
            f"paper application soak: {verdict}",
            "============================",
            f"cycles          : {self.cycles_completed}/{self.cycles_requested}",
            f"healthy cycles  : {self.healthy_cycles}",
            f"outcomes        : {self.executed} executed, {self.refused} refused, "
            f"{self.unknown} unknown, {self.not_sized} not sized",
            f"orders          : {self.total_orders}",
            f"open positions  : {self.open_positions}",
            f"cash            : {self.final_cash}",
            f"realized P&L    : {self.realized_pnl}",
            f"audit records   : {self.audit_records}",
        ]
        if self.failures:
            lines.append("failures:")
            lines.extend(f"  - {failure}" for failure in self.failures)
        if self.duration_requested_seconds is not None:
            lines.append(
                "duration         : "
                f"{self.duration_elapsed_seconds:.3f}/"
                f"{self.duration_requested_seconds:.3f} seconds"
            )
        return "\n".join(lines)


def run_soak(application: PaperApplication, *, cycles: int) -> SoakReport:
    """Drive ``cycles`` synchronously and verify operational safety afterward."""
    if not isinstance(application, PaperApplication):
        raise TypeError("application must be a PaperApplication")
    if isinstance(cycles, bool) or not isinstance(cycles, int):
        raise TypeError("cycles must be an int")
    if cycles <= 0:
        raise ValueError("cycles must be positive")

    return _run_soak(application, cycles=cycles, delay_seconds=0, wait=None)


def run_timed_soak(
    application: PaperApplication,
    *,
    duration_seconds: float,
    interval_seconds: float = 1.0,
    cycle_multiple: int = 1,
    wait: Callable[[float], object] | None = None,
    monotonic: Callable[[], float] | None = None,
) -> SoakReport:
    """Run paced complete cycles for at least the requested wall duration."""
    if not isinstance(application, PaperApplication):
        raise TypeError("application must be a PaperApplication")
    if isinstance(duration_seconds, bool) or not isinstance(
        duration_seconds, (int, float)
    ):
        raise TypeError("duration_seconds must be numeric")
    if isinstance(interval_seconds, bool) or not isinstance(
        interval_seconds, (int, float)
    ):
        raise TypeError("interval_seconds must be numeric")
    if isinstance(cycle_multiple, bool) or not isinstance(cycle_multiple, int):
        raise TypeError("cycle_multiple must be an int")
    if cycle_multiple <= 0:
        raise ValueError("cycle_multiple must be positive")
    duration = float(duration_seconds)
    interval = float(interval_seconds)
    if not math.isfinite(duration) or duration <= 0:
        raise ValueError("duration_seconds must be finite and positive")
    if not math.isfinite(interval) or interval <= 0:
        raise ValueError("interval_seconds must be finite and positive")
    minimum_cycles = max(1, math.ceil(duration / interval))
    cycles = math.ceil(minimum_cycles / cycle_multiple) * cycle_multiple
    paced_interval = duration / cycles
    monotonic_clock = monotonic or time.monotonic
    started = monotonic_clock()
    report = _run_soak(
        application,
        cycles=cycles,
        delay_seconds=paced_interval,
        wait=wait or threading.Event().wait,
    )
    elapsed = max(0.0, monotonic_clock() - started)
    return replace(
        report,
        duration_requested_seconds=duration,
        duration_elapsed_seconds=elapsed,
    )


def _run_soak(
    application: PaperApplication,
    *,
    cycles: int,
    delay_seconds: float,
    wait: Callable[[float], object] | None,
) -> SoakReport:
    completed = healthy = executed = refused = unknown = not_sized = 0
    failures: list[str] = []
    redactor = global_redactor()

    for number in range(1, cycles + 1):
        try:
            report = application.service.poll_once()
        except Exception as exc:  # noqa: BLE001 - recorded as a failed soak
            failures.append(
                redactor.redact(
                    f"cycle {number} raised {type(exc).__name__}: {exc}"
                )
            )
            break
        completed += 1
        if report.healthy and report.ready:
            healthy += 1
        else:
            reason = report.skipped_reason or "cycle reported not ready"
            failures.append(f"cycle {number}: {reason}")
        if report.reconciliation.blocked:
            failures.append(f"cycle {number}: venue reconciliation blocked")
        for outcome in report.outcomes:
            if outcome.outcome == "executed":
                executed += 1
            elif outcome.outcome == "refused":
                refused += 1
            elif outcome.outcome == "unknown":
                unknown += 1
            elif outcome.outcome == "not_sized":
                not_sized += 1
        if wait is not None:
            wait(delay_seconds)

    stack = application.stack
    unknown_orders = stack.orders.unknown_orders()
    if unknown or unknown_orders:
        failures.append(
            f"uncertain execution remains: {unknown} UNKNOWN outcome(s), "
            f"{len(unknown_orders)} UNKNOWN order(s)"
        )
    status = stack.monitor.status()
    if not status.ready:
        failures.append(
            "operational readiness blocked: "
            + ", ".join(status.blocked_reasons)
        )
    try:
        stack.audit.verify()
    except ValueError as exc:
        failures.append(redactor.redact(f"audit verification failed: {exc}"))
    open_positions = len(stack.portfolio.open_positions())
    if open_positions:
        failures.append(f"soak ended with {open_positions} open position(s)")

    return SoakReport(
        cycles_requested=cycles,
        cycles_completed=completed,
        healthy_cycles=healthy,
        executed=executed,
        refused=refused,
        unknown=unknown,
        not_sized=not_sized,
        total_orders=len(stack.orders.all_orders()),
        open_positions=open_positions,
        final_cash=f"{stack.portfolio.cash.amount} {stack.portfolio.base_currency.code}",
        realized_pnl=(
            f"{stack.portfolio.realized().amount} "
            f"{stack.portfolio.base_currency.code}"
        ),
        audit_records=stack.audit.count,
        failures=tuple(dict.fromkeys(failures)),
    )
