"""Deterministic stop/rebuild/recovery verification for the paper app."""

from __future__ import annotations

from dataclasses import dataclass
from os import PathLike

from ..core.config import RiskConfig
from ..core.money import Money, USD
from .runtime import build_demo_paper_application
from .soak import SoakReport, run_soak

__all__ = ["RestartDrillReport", "run_restart_drill"]


@dataclass(frozen=True, slots=True)
class RestartDrillReport:
    first: SoakReport
    second: SoakReport
    recovered_cash: str
    recovered_audit_records: int
    failures: tuple[str, ...] = ()

    @property
    def passed(self) -> bool:
        return self.first.passed and self.second.passed and not self.failures

    def as_details(self) -> dict[str, object]:
        return {
            "passed": self.passed,
            "first": self.first.as_details(),
            "second": self.second.as_details(),
            "recovered_cash": self.recovered_cash,
            "recovered_audit_records": self.recovered_audit_records,
            "failures": list(self.failures),
        }

    def render(self) -> str:
        verdict = "PASS" if self.passed else "FAIL"
        lines = [
            f"paper restart recovery drill: {verdict}",
            "==================================",
            f"first run       : {'PASS' if self.first.passed else 'FAIL'}",
            f"recovered cash  : {self.recovered_cash}",
            f"recovered audit : {self.recovered_audit_records}",
            f"second run      : {'PASS' if self.second.passed else 'FAIL'}",
        ]
        if self.failures:
            lines.append("failures:")
            lines.extend(f"  - {failure}" for failure in self.failures)
        return "\n".join(lines)


def run_restart_drill(
    *,
    sqlite_path: str | PathLike[str],
    cycles_per_run: int = 19,
    starting_cash: Money = Money("100000.00", USD),
    risk: RiskConfig | None = None,
) -> RestartDrillReport:
    """Run, close, rebuild on the same state, and prove safe continuation."""
    if isinstance(cycles_per_run, bool) or not isinstance(cycles_per_run, int):
        raise TypeError("cycles_per_run must be an int")
    if cycles_per_run <= 0:
        raise ValueError("cycles_per_run must be positive")

    first_app = build_demo_paper_application(
        starting_cash=starting_cash,
        risk=risk,
        sqlite_path=sqlite_path,
        port=0,
    )
    try:
        first = run_soak(first_app, cycles=cycles_per_run)
    finally:
        first_app.stop()

    failures: list[str] = []
    second_app = build_demo_paper_application(
        starting_cash=starting_cash,
        risk=risk,
        sqlite_path=sqlite_path,
        port=0,
    )
    try:
        recovered_cash = (
            f"{second_app.stack.portfolio.cash.amount} "
            f"{second_app.stack.portfolio.base_currency.code}"
        )
        recovered_audit = second_app.stack.audit.count
        if recovered_cash != first.final_cash:
            failures.append("cash did not survive application rebuild")
        if recovered_audit < first.audit_records:
            failures.append("audit history regressed across application rebuild")
        if not second_app.stack.startup.ok:
            failures.append("restart recovery or initial reconciliation failed")
        if second_app.stack.orders.unknown_orders():
            failures.append("UNKNOWN orders remained after restart recovery")
        second = run_soak(second_app, cycles=cycles_per_run)
        second_app.stack.audit.verify()
    finally:
        second_app.stop()

    return RestartDrillReport(
        first=first,
        second=second,
        recovered_cash=recovered_cash,
        recovered_audit_records=recovered_audit,
        failures=tuple(failures),
    )
