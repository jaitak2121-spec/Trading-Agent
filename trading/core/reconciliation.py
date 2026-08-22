"""Position reconciliation and the mismatch gate.

Covers:

* INVARIANT 5 -- an order in :data:`~trading.core.orders.OrderState.UNKNOWN`
  (or an idempotency key in ``UNKNOWN``) blocks new orders until reconciled.
* INVARIANT 6 -- a mismatch between our local position and the venue's blocks
  new live orders.

Two design choices are worth calling out.

**The mismatch latches.** Like the kill switch, a detected mismatch stays
latched until an operator clears it, and clearing re-verifies against a fresh
snapshot rather than taking the operator's word for it. A flapping condition
must not silently unblock trading between two checks.

**Staleness counts as a mismatch.** The obvious way to defeat "position
mismatch blocks live orders" is never to check. So live orders additionally
require a *recent* clean reconciliation; if the last one is older than
``max_staleness_seconds``, the gate refuses. Only a clean snapshot refreshes
that timestamp -- a snapshot that found a discrepancy does not count as having
reconciled.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass
from decimal import Decimal
from typing import Mapping

from .audit import AuditCategory, AuditLog, AuditOutcome
from .authz import Action, Principal, authorize
from .clock import Clock
from .errors import PositionMismatch, SafetyViolation, UnknownOrderStateBlocked
from .money import Quantity, to_decimal
from .orders import OrderSide, OrderStore

__all__ = [
    "PositionLedger",
    "PositionDiscrepancy",
    "ReconciliationReport",
    "ReconciliationGate",
]


class PositionLedger:
    """Our own view of what we hold, built from fills we have seen."""

    def __init__(self) -> None:
        self._positions: dict[str, Quantity] = {}
        self._lock = threading.RLock()

    def apply_fill(self, symbol: str, side: OrderSide, quantity: Quantity) -> Quantity:
        """Apply a fill. BUY increases the position, SELL decreases it."""
        if not isinstance(side, OrderSide):
            raise TypeError("side must be an OrderSide")
        if not isinstance(quantity, Quantity):
            raise TypeError("quantity must be a Quantity")
        if not quantity.is_positive:
            raise ValueError("fill quantity must be strictly positive")
        with self._lock:
            current = self._positions.get(symbol, Quantity.zero(quantity.asset))
            delta = quantity if side is OrderSide.BUY else -quantity
            updated = current + delta
            self._positions[symbol] = updated
            return updated

    def set_position(self, symbol: str, quantity: Quantity) -> None:
        """Overwrite a position outright. Used when adopting a venue snapshot."""
        if not isinstance(quantity, Quantity):
            raise TypeError("quantity must be a Quantity")
        with self._lock:
            self._positions[symbol] = quantity

    def position(self, symbol: str, *, asset: str | None = None) -> Quantity:
        with self._lock:
            existing = self._positions.get(symbol)
            if existing is not None:
                return existing
            return Quantity.zero(asset if asset is not None else symbol)

    def snapshot(self) -> dict[str, Quantity]:
        with self._lock:
            return dict(self._positions)

    def symbols(self) -> list[str]:
        with self._lock:
            return sorted(self._positions)

    def __len__(self) -> int:
        with self._lock:
            return len(self._positions)


@dataclass(frozen=True, slots=True)
class PositionDiscrepancy:
    symbol: str
    local: Quantity
    remote: Quantity
    difference: Quantity

    def as_details(self) -> dict[str, object]:
        return {
            "symbol": self.symbol,
            "local": str(self.local.amount),
            "remote": str(self.remote.amount),
            "difference": str(self.difference.amount),
        }

    def __str__(self) -> str:
        return (
            f"{self.symbol}: local={self.local.amount} remote={self.remote.amount} "
            f"diff={self.difference.amount}"
        )


@dataclass(frozen=True, slots=True)
class ReconciliationReport:
    at: str
    discrepancies: tuple[PositionDiscrepancy, ...]
    symbols_checked: tuple[str, ...]

    @property
    def is_clean(self) -> bool:
        return not self.discrepancies

    def as_details(self) -> dict[str, object]:
        return {
            "at": self.at,
            "clean": self.is_clean,
            "symbols_checked": list(self.symbols_checked),
            "discrepancies": [d.as_details() for d in self.discrepancies],
        }


class ReconciliationGate:
    """Blocks new orders while our view of the world is untrustworthy."""

    def __init__(
        self,
        ledger: PositionLedger,
        order_store: OrderStore,
        audit: AuditLog,
        *,
        clock: Clock,
        max_staleness_seconds: float = 300.0,
        tolerances: Mapping[str, object] | None = None,
    ) -> None:
        if max_staleness_seconds <= 0:
            raise ValueError("max_staleness_seconds must be > 0")
        self._ledger = ledger
        self._orders = order_store
        self._audit = audit
        self._clock = clock
        self._max_staleness = float(max_staleness_seconds)
        self._tolerances: dict[str, Decimal] = {}
        for symbol, value in (tolerances or {}).items():
            tolerance = to_decimal(value, field=f"tolerance[{symbol}]")
            if tolerance < 0:
                raise ValueError(f"tolerance for {symbol} must be >= 0")
            self._tolerances[symbol] = tolerance
        self._mismatch: tuple[PositionDiscrepancy, ...] = ()
        self._last_clean_mono: float | None = None
        self._last_report: ReconciliationReport | None = None
        self._lock = threading.RLock()

    # -- state -------------------------------------------------------------
    @property
    def has_mismatch(self) -> bool:
        with self._lock:
            return bool(self._mismatch)

    @property
    def discrepancies(self) -> tuple[PositionDiscrepancy, ...]:
        with self._lock:
            return self._mismatch

    @property
    def last_report(self) -> ReconciliationReport | None:
        with self._lock:
            return self._last_report

    def seconds_since_clean(self) -> float | None:
        """Age of the last clean reconciliation, or None if there never was one."""
        with self._lock:
            if self._last_clean_mono is None:
                return None
            return max(0.0, self._clock.monotonic_seconds() - self._last_clean_mono)

    # -- checking ----------------------------------------------------------
    def reconcile(self, broker_positions: Mapping[str, Quantity]) -> ReconciliationReport:
        """Compare a venue snapshot against the local ledger.

        A discrepancy latches the mismatch. A clean result refreshes the
        freshness timestamp but does NOT clear an existing latch -- see
        :meth:`clear_mismatch`.
        """
        for symbol, quantity in broker_positions.items():
            if not isinstance(quantity, Quantity):
                raise TypeError(
                    f"broker position for {symbol} must be a Quantity, "
                    f"got {type(quantity).__name__}"
                )

        with self._lock:
            local = self._ledger.snapshot()
            symbols = sorted(set(local) | set(broker_positions))
            found: list[PositionDiscrepancy] = []
            for symbol in symbols:
                remote = broker_positions.get(symbol)
                mine = local.get(symbol)
                asset = (remote or mine).asset  # at least one is present
                remote_q = remote if remote is not None else Quantity.zero(asset)
                local_q = mine if mine is not None else Quantity.zero(asset)
                if local_q.asset != remote_q.asset:
                    raise SafetyViolation(
                        f"{symbol}: local asset {local_q.asset} does not match "
                        f"venue asset {remote_q.asset}; cannot compare"
                    )
                difference = local_q - remote_q
                tolerance = self._tolerances.get(symbol, Decimal(0))
                if abs(difference.amount) > tolerance:
                    found.append(
                        PositionDiscrepancy(
                            symbol=symbol,
                            local=local_q,
                            remote=remote_q,
                            difference=difference,
                        )
                    )

            report = ReconciliationReport(
                at=self._clock.now().isoformat(),
                discrepancies=tuple(found),
                symbols_checked=tuple(symbols),
            )
            self._last_report = report

            if found:
                self._mismatch = tuple(found)
                self._audit.record(
                    AuditCategory.RECONCILIATION,
                    "position_mismatch_detected",
                    outcome=AuditOutcome.REFUSED,
                    actor="system",
                    details=report.as_details(),
                )
            else:
                self._last_clean_mono = self._clock.monotonic_seconds()
                self._audit.record(
                    AuditCategory.RECONCILIATION,
                    "reconciliation_clean",
                    outcome=AuditOutcome.ALLOWED,
                    actor="system",
                    details=report.as_details(),
                )
            return report

    def clear_mismatch(
        self,
        principal: Principal,
        *,
        reason: str,
        broker_positions: Mapping[str, Quantity],
    ) -> None:
        """Clear a latched mismatch. Operator only, and only if actually clean now.

        Requires a fresh snapshot rather than trusting the operator's assertion:
        the same reasoning as refusing to release the kill switch while its
        trigger file still exists.
        """
        authorize(principal, Action.RECONCILE)
        if not reason or not reason.strip():
            raise ValueError("clearing a position mismatch requires a reason")
        with self._lock:
            report = self.reconcile(broker_positions)
            if not report.is_clean:
                self._audit.record(
                    AuditCategory.RECONCILIATION,
                    "mismatch_clear_refused",
                    outcome=AuditOutcome.REFUSED,
                    actor=principal.principal_id,
                    details={"reason": reason, **report.as_details()},
                )
                raise PositionMismatch(
                    "cannot clear the position mismatch: the venue snapshot still "
                    f"disagrees ({'; '.join(str(d) for d in report.discrepancies)})"
                )
            previous = self._mismatch
            self._mismatch = ()
            self._audit.record(
                AuditCategory.RECONCILIATION,
                "position_mismatch_cleared",
                outcome=AuditOutcome.ALLOWED,
                actor=principal.principal_id,
                details={
                    "reason": reason,
                    "previous": [d.as_details() for d in previous],
                },
            )

    def adopt_broker_positions(
        self,
        principal: Principal,
        *,
        reason: str,
        broker_positions: Mapping[str, Quantity],
    ) -> None:
        """Overwrite the local ledger with the venue's view. Operator only.

        The deliberate escape hatch for "the venue is right and we are wrong".
        It is loud in the audit log because it discards local state, and it does
        not clear the mismatch latch by itself -- the operator must still call
        :meth:`clear_mismatch`, which re-verifies.
        """
        authorize(principal, Action.RECONCILE)
        if not reason or not reason.strip():
            raise ValueError("adopting venue positions requires a reason")
        with self._lock:
            before = {s: str(q.amount) for s, q in self._ledger.snapshot().items()}
            for symbol, quantity in broker_positions.items():
                if not isinstance(quantity, Quantity):
                    raise TypeError(f"broker position for {symbol} must be a Quantity")
            for symbol in list(self._ledger.snapshot()):
                if symbol not in broker_positions:
                    existing = self._ledger.position(symbol)
                    self._ledger.set_position(symbol, Quantity.zero(existing.asset))
            for symbol, quantity in broker_positions.items():
                self._ledger.set_position(symbol, quantity)
            self._audit.record(
                AuditCategory.RECONCILIATION,
                "broker_positions_adopted",
                outcome=AuditOutcome.ALLOWED,
                actor=principal.principal_id,
                details={
                    "reason": reason,
                    "before": before,
                    "after": {s: str(q.amount) for s, q in broker_positions.items()},
                },
            )

    # -- gate --------------------------------------------------------------
    def require_clean(self, *, live: bool) -> None:
        """Raise unless it is safe to accept a new order.

        Checks, in order:

        1. Any order in UNKNOWN state (INVARIANT 5).
        2. A latched position mismatch (INVARIANT 6).
        3. For live orders only: whether the last clean reconciliation is
           recent enough.
        """
        unknown = self._orders.unknown_orders()
        if unknown:
            ids = ", ".join(o.order_id for o in unknown[:5])
            more = "" if len(unknown) <= 5 else f" (+{len(unknown) - 5} more)"
            self._audit.record(
                AuditCategory.RECONCILIATION,
                "blocked_by_unknown_orders",
                outcome=AuditOutcome.REFUSED,
                actor="system",
                details={"unknown_order_count": len(unknown), "order_ids": ids},
            )
            raise UnknownOrderStateBlocked(
                f"{len(unknown)} order(s) are in UNKNOWN state ({ids}{more}); no new "
                "orders will be accepted until they are reconciled (INVARIANT 5)"
            )

        with self._lock:
            if self._mismatch:
                detail = "; ".join(str(d) for d in self._mismatch)
                self._audit.record(
                    AuditCategory.RECONCILIATION,
                    "blocked_by_position_mismatch",
                    outcome=AuditOutcome.REFUSED,
                    actor="system",
                    details={"discrepancies": [d.as_details() for d in self._mismatch]},
                )
                raise PositionMismatch(
                    f"local positions disagree with the venue ({detail}); no new "
                    "orders will be accepted until this is reconciled (INVARIANT 6)"
                )

            if not live:
                return

            age = self.seconds_since_clean()
            if age is None:
                self._audit.record(
                    AuditCategory.RECONCILIATION,
                    "blocked_by_missing_reconciliation",
                    outcome=AuditOutcome.REFUSED,
                    actor="system",
                    details={"max_staleness_seconds": str(self._max_staleness)},
                )
                raise PositionMismatch(
                    "no successful position reconciliation has ever run; refusing "
                    "live orders because an unchecked position cannot be proven to "
                    "match (INVARIANT 6)"
                )
            if age > self._max_staleness:
                self._audit.record(
                    AuditCategory.RECONCILIATION,
                    "blocked_by_stale_reconciliation",
                    outcome=AuditOutcome.REFUSED,
                    actor="system",
                    details={
                        "age_seconds": str(round(age, 3)),
                        "max_staleness_seconds": str(self._max_staleness),
                    },
                )
                raise PositionMismatch(
                    f"last clean reconciliation was {age:.1f}s ago, older than the "
                    f"{self._max_staleness:.0f}s limit; refusing live orders until "
                    "positions are re-checked (INVARIANT 6)"
                )
