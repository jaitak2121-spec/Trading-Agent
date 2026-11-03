"""Explicit recovery for a persisted paper position after venue-memory loss."""

from __future__ import annotations

from dataclasses import dataclass
from os import PathLike

from ..adapters.paper import PaperBroker
from ..core.config import RiskConfig
from ..core.money import Money, USD
from .runtime import build_demo_paper_application

__all__ = [
    "PAPER_POSITION_RESET_CONFIRMATION",
    "PaperPositionRecoveryReport",
    "recover_interrupted_paper_position",
]


PAPER_POSITION_RESET_CONFIRMATION = "I_ACCEPT_PAPER_VENUE_POSITION_RESET"


@dataclass(frozen=True, slots=True)
class PaperPositionRecoveryReport:
    before: dict[str, str]
    adopted: dict[str, str]
    startup_clean_after_rebuild: bool
    audit_records: int

    @property
    def passed(self) -> bool:
        return self.startup_clean_after_rebuild

    def as_details(self) -> dict[str, object]:
        return {
            "passed": self.passed,
            "before": dict(self.before),
            "adopted": dict(self.adopted),
            "startup_clean_after_rebuild": self.startup_clean_after_rebuild,
            "audit_records": self.audit_records,
        }

    def render(self) -> str:
        return "\n".join(
            (
                f"paper interrupted-position recovery: {'PASS' if self.passed else 'FAIL'}",
                "=========================================",
                f"local before : {self.before}",
                f"venue adopted: {self.adopted}",
                f"audit records: {self.audit_records}",
                f"clean rebuild: {self.startup_clean_after_rebuild}",
            )
        )


def recover_interrupted_paper_position(
    *,
    sqlite_path: str | PathLike[str],
    confirmation: str,
    reason: str,
    starting_cash: Money = Money("100000.00", USD),
    risk: RiskConfig | None = None,
) -> PaperPositionRecoveryReport:
    """Adopt the rebuilt paper venue snapshot, audit it, and verify a rebuild.

    This is intentionally destructive to the persisted *paper* position. It is
    unavailable for any non-paper broker or live-authorized configuration.
    """
    if confirmation != PAPER_POSITION_RESET_CONFIRMATION:
        raise ValueError("exact paper position reset confirmation is required")
    if not isinstance(reason, str) or not reason.strip():
        raise ValueError("a non-empty operator recovery reason is required")

    application = build_demo_paper_application(
        starting_cash=starting_cash,
        risk=risk,
        sqlite_path=sqlite_path,
        port=0,
    )
    try:
        stack = application.stack
        if stack.config.environment != "paper" or stack.config.is_live_authorized:
            raise RuntimeError("paper position recovery is unavailable outside paper mode")
        if not isinstance(stack.broker, PaperBroker):
            raise RuntimeError("paper position recovery requires PaperBroker")
        if stack.orders.open_orders() or stack.orders.unknown_orders():
            raise RuntimeError("all orders must be terminal before position recovery")
        if not stack.reconciliation.has_mismatch:
            raise RuntimeError("no latched paper position mismatch requires recovery")

        before = {
            symbol: str(quantity.amount)
            for symbol, quantity in stack.portfolio.snapshot().items()
            if not quantity.is_zero
        }
        venue_positions = stack.broker.fetch_positions().positions
        adopted = {
            symbol: str(quantity.amount)
            for symbol, quantity in venue_positions.items()
            if not quantity.is_zero
        }
        operator_reason = reason.strip()
        stack.reconciliation.adopt_broker_positions(
            stack.operator_id,
            reason=operator_reason,
            broker_positions=venue_positions,
        )
        stack.reconciliation.clear_mismatch(
            stack.operator_id,
            reason=operator_reason,
            broker_positions=venue_positions,
        )
        stack.audit.verify()
        audit_records = stack.audit.count
    finally:
        application.stop()

    verification = build_demo_paper_application(
        starting_cash=starting_cash,
        risk=risk,
        sqlite_path=sqlite_path,
        port=0,
    )
    try:
        clean = (
            verification.stack.startup.ok
            and not verification.stack.reconciliation.has_mismatch
            and verification.stack.monitor.status().ready
        )
        verification.stack.audit.verify()
        audit_records = verification.stack.audit.count
    finally:
        verification.stop()

    return PaperPositionRecoveryReport(
        before=before,
        adopted=adopted,
        startup_clean_after_rebuild=clean,
        audit_records=audit_records,
    )
