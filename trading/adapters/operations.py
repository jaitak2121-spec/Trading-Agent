"""Operational status: what an operator needs to see, and whether we may trade.

Everything the safety kernel knows is already exposed on the component that
knows it -- the kill switch has ``is_engaged``, the gate has ``has_mismatch``,
the store has ``unknown_orders``. What was missing is a single place that asks
all of them at once and answers the two questions an operator actually has:

* **What is true right now?** :class:`OperationalStatus` -- a redacted,
  immutable summary of mode, gate state, order counts, breakers, and worker
  health.
* **May new orders be accepted?** :attr:`OperationalStatus.ready` -- a single
  fail-closed predicate over all of it.

The readiness rule is fail-closed by construction
=================================================

``ready`` is False unless everything it can see is healthy. Specifically it is
False for: an engaged kill switch, any UNKNOWN order, a latched position
mismatch, a venue that cannot be fully reconciled, a snapshot the venue cannot
be read, a worker that died, a failed startup recovery, or a reconciliation
stale past the gate's own limit. It is *not* a second copy of the gateway's
decisions -- every one of those conditions is already enforced by the gate that
owns it. This is the summary, not the enforcement, and it can never authorize
anything: there is no method here that submits, cancels, or reconciles.

That distinction is the whole reason this is an adapter rather than a kernel
component. It reads runtime state and composes it for a human; it holds no
authority, takes no order, and is not on any path a submission travels.

What it deliberately is not
===========================

**Not a service.** There is no HTTP server, no route, no framework. The
architecture has no inbound adapter layer, and inventing one here to "expose"
status would put a network surface on a system whose whole safety story is that
it has none. An operator reads this in-process, or a future inbound adapter
reads it and renders it -- but that adapter does not exist yet, and this module
does not pretend to be it.

**Not a metrics backend.** Counters are observational and derived from what the
components already track. Nothing here is load-bearing for a safety decision,
and nothing written here can un-block an order.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Mapping

from ..core.audit import AuditCategory, AuditLog, AuditOutcome
from ..core.breaker import BreakerRegistry
from ..core.config import TradingConfig
from ..core.errors import ConfigurationError
from ..core.killswitch import KillSwitch
from ..core.orders import OrderStore
from ..core.reconciliation import ReconciliationGate

__all__ = [
    "BlockedReason",
    "ComponentHealth",
    "OperationalMonitor",
    "OperationalStatus",
    "StartupCoordinator",
    "StartupReport",
]


class BlockedReason:
    """Why the system is not ready. Strings, so they grep in a log or a dashboard."""

    KILL_SWITCH_ENGAGED = "kill_switch_engaged"
    UNKNOWN_ORDERS = "unknown_orders"
    UNKNOWN_RESERVATION = "unknown_reservation"
    POSITION_MISMATCH = "position_mismatch"
    RECONCILIATION_NEVER_RAN = "reconciliation_never_ran"
    RECONCILIATION_STALE = "reconciliation_stale"
    INVENTORY_UNAVAILABLE = "inventory_unavailable"
    WORKER_DIED = "worker_died"
    WORKER_NOT_RUNNING = "worker_not_running"
    STARTUP_RECOVERY_FAILED = "startup_recovery_failed"
    BREAKER_OPEN = "breaker_open"


@dataclass(frozen=True, slots=True)
class ComponentHealth:
    """One component's health, as an operator would read it."""

    name: str
    healthy: bool
    detail: str = ""

    def as_details(self) -> dict[str, object]:
        return {"name": self.name, "healthy": self.healthy, "detail": self.detail}


@dataclass(frozen=True, slots=True)
class StartupReport:
    """What safe startup did, and whether it completed.

    ``ok`` is False when anything failed. Startup deliberately does not enable
    live trading, submit orders, or clear a mismatch -- it verifies, and reports
    what it found.
    """

    stages: tuple[ComponentHealth, ...] = ()
    recovery_findings: tuple[str, ...] = ()
    error: str | None = None

    @property
    def ok(self) -> bool:
        return self.error is None and all(s.healthy for s in self.stages)

    def as_details(self) -> dict[str, object]:
        return {
            "ok": self.ok,
            "error": self.error,
            "stages": [s.as_details() for s in self.stages],
            "recovery_findings": list(self.recovery_findings),
        }


@dataclass(frozen=True, slots=True)
class OperationalStatus:
    """A redacted, immutable picture of the running system.

    Built by :func:`build_status`; never constructed directly by an operator.
    """

    mode: str
    live_trading: bool
    environment: str

    ready: bool
    blocked_reasons: tuple[str, ...] = ()

    unknown_order_count: int = 0
    open_order_count: int = 0
    total_order_count: int = 0

    position_mismatch: bool = False
    seconds_since_clean_reconciliation: float | None = None

    kill_switch_engaged: bool = False
    kill_switch_reason: str = ""

    breakers: Mapping[str, str] = field(default_factory=dict)

    inventory_supported: bool = False
    lifecycle_running: bool = False
    lifecycle_passes: int = 0
    lifecycle_last_error: str | None = None
    last_sweep_blocked: bool | None = None

    components: tuple[ComponentHealth, ...] = ()
    config: Mapping[str, object] = field(default_factory=dict)

    @property
    def healthy(self) -> bool:
        """Whether every component reported healthy. Distinct from ``ready``."""
        return all(c.healthy for c in self.components)

    def as_details(self) -> dict[str, object]:
        """A JSON-serialisable, redacted dict. Safe to log or hand to an operator."""
        return {
            "mode": self.mode,
            "live_trading": self.live_trading,
            "environment": self.environment,
            "ready": self.ready,
            "healthy": self.healthy,
            "blocked_reasons": list(self.blocked_reasons),
            "unknown_order_count": self.unknown_order_count,
            "open_order_count": self.open_order_count,
            "total_order_count": self.total_order_count,
            "position_mismatch": self.position_mismatch,
            "seconds_since_clean_reconciliation": (
                None
                if self.seconds_since_clean_reconciliation is None
                else round(self.seconds_since_clean_reconciliation, 3)
            ),
            "kill_switch_engaged": self.kill_switch_engaged,
            "kill_switch_reason": self.kill_switch_reason,
            "breakers": dict(self.breakers),
            "inventory_supported": self.inventory_supported,
            "lifecycle_running": self.lifecycle_running,
            "lifecycle_passes": self.lifecycle_passes,
            "lifecycle_last_error": self.lifecycle_last_error,
            "last_sweep_blocked": self.last_sweep_blocked,
            "components": [c.as_details() for c in self.components],
            "config": dict(self.config),
        }


class OperationalMonitor:
    """Composes live component state into a redacted status, and nothing else.

    Holds references only. Every property it reads is read at call time, so a
    status is a snapshot of *now* rather than of when the monitor was built --
    which matters because the things an operator watches (the kill switch, the
    gate latch, the workers) change underneath it.
    """

    def __init__(
        self,
        *,
        config: TradingConfig,
        orders: OrderStore,
        reconciliation: ReconciliationGate,
        kill_switch: KillSwitch,
        breakers: BreakerRegistry | None = None,
        modes=None,
        lifecycle=None,
        coordinator=None,
        recovery: StartupReport | None = None,
        audit: AuditLog | None = None,
        dedupe=None,
    ) -> None:
        self._config = config
        self._orders = orders
        self._reconciliation = reconciliation
        self._kill_switch = kill_switch
        self._breakers = breakers
        self._modes = modes
        self._lifecycle = lifecycle
        self._coordinator = coordinator
        self._recovery = recovery
        self._audit = audit
        # Optional: the idempotency registry. Supplied so the monitor's
        # readiness matches the gateway's reservation-level UNKNOWN backstop --
        # a durable UNKNOWN reservation whose order did not survive a restart
        # blocks the gateway, and the monitor must report that rather than
        # reading ready=True off an order store that no longer holds it.
        self._dedupe = dedupe

    # -- the snapshot -----------------------------------------------------

    def status(self) -> OperationalStatus:
        """Read every component and compose one redacted picture."""
        components: list[ComponentHealth] = []
        reasons: list[str] = []

        orders = self._orders.all_orders()
        unknown = [o for o in orders if o.is_unknown]
        open_orders = [o for o in orders if o.is_open]

        # -- orders and the UNKNOWN block (INVARIANT 5) -------------------
        if unknown:
            reasons.append(BlockedReason.UNKNOWN_ORDERS)
            components.append(
                ComponentHealth(
                    "orders",
                    False,
                    f"{len(unknown)} order(s) in UNKNOWN state; resolve before "
                    "new orders are accepted",
                )
            )
        else:
            components.append(
                ComponentHealth("orders", True, f"{len(open_orders)} open order(s)")
            )

        # -- reservation-level UNKNOWN (survives a restart that orders do not) --
        # Mirrors the gateway's own backstop. Only meaningful when a reservation
        # registry is wired in; otherwise the order-level check above is the
        # whole of INVARIANT 5's surface here.
        unknown_reservations = 0
        if self._dedupe is not None and self._dedupe.has_unknown():
            unknown_reservations = len(self._dedupe.unknown_reservations())
            reasons.append(BlockedReason.UNKNOWN_RESERVATION)
            components.append(
                ComponentHealth(
                    "reservations",
                    False,
                    f"{unknown_reservations} idempotency reservation(s) in UNKNOWN "
                    "state (order may not have survived a restart); blocks new "
                    "orders until reconciled",
                )
            )
        elif self._dedupe is not None:
            components.append(
                ComponentHealth("reservations", True, "no UNKNOWN reservations"))

        # -- the kill switch ----------------------------------------------
        engaged = self._kill_switch.is_engaged
        if engaged:
            reasons.append(BlockedReason.KILL_SWITCH_ENGAGED)
        components.append(
            ComponentHealth(
                "kill_switch",
                not engaged,
                self._kill_switch.reason if engaged else "not engaged",
            )
        )

        # -- positions and reconciliation (INVARIANT 6) -------------------
        mismatch = self._reconciliation.has_mismatch
        age = self._reconciliation.seconds_since_clean()
        if mismatch:
            reasons.append(BlockedReason.POSITION_MISMATCH)
        if not mismatch:
            if age is None:
                # Only a blocker in live mode, matching the gate's own rule:
                # a paper system is not required to have reconciled.
                if self._config.is_live_authorized:
                    reasons.append(BlockedReason.RECONCILIATION_NEVER_RAN)
            elif age > self._gate_staleness_limit():
                reasons.append(BlockedReason.RECONCILIATION_STALE)
        components.append(
            ComponentHealth(
                "reconciliation",
                not mismatch,
                (
                    "position mismatch latched"
                    if mismatch
                    else (
                        "never reconciled"
                        if age is None
                        else f"clean {age:.1f}s ago"
                    )
                ),
            )
        )

        # -- the venue's ability to be reconciled whole -------------------
        inventory = (
            self._coordinator.supports_venue_inventory
            if self._coordinator is not None
            else True
        )
        if not inventory:
            reasons.append(BlockedReason.INVENTORY_UNAVAILABLE)
        components.append(
            ComponentHealth(
                "venue_inventory",
                inventory,
                (
                    "venue can be swept in full"
                    if inventory
                    else "venue cannot enumerate its orders; ghost orders are "
                    "undetectable"
                ),
            )
        )

        # -- breakers ------------------------------------------------------
        breakers: dict[str, str] = {}
        if self._breakers is not None:
            for snapshot in self._breakers.snapshots():
                breakers[snapshot.name] = snapshot.state.value
            if any(s != "closed" for s in breakers.values()):
                reasons.append(BlockedReason.BREAKER_OPEN)
        components.append(
            ComponentHealth(
                "breakers",
                not any(s != "closed" for s in breakers.values()),
                ", ".join(f"{k}={v}" for k, v in sorted(breakers.items())) or "none",
            )
        )

        # -- the lifecycle worker ------------------------------------------
        lifecycle_running = False
        passes = 0
        last_error: str | None = None
        lifecycle_healthy = True
        if self._lifecycle is not None:
            lifecycle_running = self._lifecycle.is_running
            passes = self._lifecycle.passes
            error = self._lifecycle.last_error
            last_error = None if error is None else f"{type(error).__name__}: {error}"
            if last_error is not None:
                # A worker that died is worse than one never started: the system
                # looks supervised while nothing is watching.
                reasons.append(BlockedReason.WORKER_DIED)
                lifecycle_healthy = False
            elif passes == 0:
                reasons.append(BlockedReason.WORKER_NOT_RUNNING)
                lifecycle_healthy = False
        components.append(
            ComponentHealth(
                "lifecycle",
                lifecycle_healthy,
                (
                    last_error
                    if last_error is not None
                    else (
                        f"{passes} sweep(s), running"
                        if lifecycle_running
                        else f"{passes} sweep(s)"
                    )
                )
                if self._lifecycle is not None
                else "not wired",
            )
        )

        # -- startup recovery ----------------------------------------------
        if self._recovery is not None and not self._recovery.ok:
            reasons.append(BlockedReason.STARTUP_RECOVERY_FAILED)
        components.append(
            ComponentHealth(
                "startup_recovery",
                self._recovery is None or self._recovery.ok,
                "not run" if self._recovery is None else ("ok" if self._recovery.ok else "failed"),
            )
        )

        last_sweep_blocked: bool | None = None
        if self._coordinator is not None:
            report = self._coordinator.last_report
            last_sweep_blocked = None if report is None else report.blocked

        return OperationalStatus(
            mode=self._mode_value(),
            live_trading=self._config.is_live_authorized,
            environment=self._config.environment,
            ready=not reasons,
            blocked_reasons=tuple(sorted(set(reasons))),
            unknown_order_count=len(unknown),
            open_order_count=len(open_orders),
            total_order_count=len(orders),
            position_mismatch=mismatch,
            seconds_since_clean_reconciliation=age,
            kill_switch_engaged=engaged,
            kill_switch_reason=self._kill_switch.reason if engaged else "",
            breakers=breakers,
            inventory_supported=inventory,
            lifecycle_running=lifecycle_running,
            lifecycle_passes=passes,
            lifecycle_last_error=last_error,
            last_sweep_blocked=last_sweep_blocked,
            components=tuple(components),
            config=self._config.redacted_summary(),
        )

    # -- readiness --------------------------------------------------------

    def ready(self) -> bool:
        """The single fail-closed predicate. Never authorizes anything by itself."""
        return self.status().ready

    def blocked_reasons(self) -> tuple[str, ...]:
        return self.status().blocked_reasons

    # -- internals --------------------------------------------------------

    def _mode_value(self) -> str:
        if self._modes is None:
            return "unknown"
        return self._modes.mode.value

    def _gate_staleness_limit(self) -> float:
        """The gate's own limit, read from it rather than copied here.

        A second constant would drift from the gate and report staleness the
        gate does not enforce (or miss staleness it does).
        """
        limit = getattr(self._reconciliation, "_max_staleness", None)
        return float(limit) if limit is not None else float("inf")


class StartupCoordinator:
    """Brings the system up safely, and reports what it found.

    Safe startup is mostly a list of things it must *not* do: it must not enable
    live trading, must not submit an order, must not clear a mismatch, and must
    not resolve an UNKNOWN order. What it does do is verify that the system is in
    a state an operator can trust before anything is allowed near a venue, and
    that means two steps in a fixed order:

    1. **Validate configuration**, fail-closed. A configuration that would
       enable live trading without the confirmation phrase, or name an
       environment it does not recognise, stops startup rather than being
       quietly accepted.
    2. **Run the restart recovery scan**, which finds PENDING_NEW orders that may
       have reached the venue before a crash and UNKNOWN orders that never left
       it. Recovery *finds*; it does not resolve. Every ambiguous order is
       reported and left for an operator, because deciding the fate of an
       ambiguous order from inside the startup path is the exact automation this
       system refuses.

    Optionally it then takes one initial reconciliation sweep, so a system that
    comes up with a stale or absent reconciliation is not silently un-ready. A
    sweep that fails does not stop startup -- it is reported, and it leaves the
    readiness predicate False, which is where that fact belongs.
    """

    def __init__(
        self,
        *,
        config: TradingConfig,
        audit: AuditLog,
        recovery=None,
        coordinator=None,
    ) -> None:
        self._config = config
        self._audit = audit
        self._recovery = recovery
        self._coordinator = coordinator

    def run(self, *, sweep: bool = True) -> StartupReport:
        """Validate, recover, and optionally reconcile. Never enables live trading."""
        stages: list[ComponentHealth] = []
        findings: list[str] = []

        # -- 1. configuration ---------------------------------------------
        try:
            self._validate_config()
        except Exception as exc:  # noqa: BLE001 - reported, and startup stops
            stages.append(
                ComponentHealth(
                    "configuration", False, f"{type(exc).__name__}: {exc}"
                )
            )
            return self._finish(stages, findings, error=f"{type(exc).__name__}: {exc}")
        stages.append(
            ComponentHealth(
                "configuration",
                True,
                f"environment={self._config.environment}, "
                f"live_trading={self._config.is_live_authorized}",
            )
        )

        # -- 2. restart recovery -------------------------------------------
        if self._recovery is not None:
            try:
                report = self._recovery.scan()
            except Exception as exc:  # noqa: BLE001
                stages.append(
                    ComponentHealth(
                        "recovery_scan", False, f"{type(exc).__name__}: {exc}"
                    )
                )
                return self._finish(
                    stages, findings, error=f"recovery scan failed: {exc}"
                )
            for ambiguous in report.ambiguous_orders:
                findings.append(
                    f"order {ambiguous.order.order_id}: {ambiguous.reason}"
                )
            stages.append(
                ComponentHealth(
                    "recovery_scan",
                    # Ambiguous orders do not make the scan *unhealthy* -- the
                    # scan did its job. They make the system un-ready, which is
                    # what the sweep and the monitor report.
                    True,
                    f"{report.total_orders} order(s), "
                    f"{len(report.ambiguous_orders)} ambiguous",
                )
            )
        else:
            stages.append(
                ComponentHealth("recovery_scan", True, "no recovery coordinator wired")
            )

        # -- 3. initial reconciliation -------------------------------------
        if sweep and self._coordinator is not None:
            try:
                sweep_report = self._coordinator.sweep()
            except Exception as exc:  # noqa: BLE001
                stages.append(
                    ComponentHealth(
                        "initial_sweep", False, f"{type(exc).__name__}: {exc}"
                    )
                )
                findings.append(f"initial sweep failed: {exc}")
            else:
                stages.append(
                    ComponentHealth(
                        "initial_sweep",
                        not sweep_report.blocked,
                        "clean"
                        if not sweep_report.blocked
                        else f"blocked: {', '.join(f.kind for f in sweep_report.blocking)}",
                    )
                )
                for finding in sweep_report.blocking:
                    findings.append(f"{finding.kind}: {finding.detail}")
        else:
            stages.append(
                ComponentHealth("initial_sweep", True, "not requested")
            )

        return self._finish(stages, findings)

    # -- validation --------------------------------------------------------

    def _validate_config(self) -> None:
        """Fail closed on anything that would start a system we cannot trust.

        Deliberately narrow. It checks what startup is responsible for -- that
        live trading is not enabled by accident, and that the environment is one
        we recognise -- and leaves the rest to ``TradingConfig``, which already
        refuses unknown keys and a missing confirmation phrase at construction.
        """
        known = {"development", "paper", "staging", "production"}
        if self._config.environment not in known:
            raise ConfigurationError(
                f"unrecognised environment {self._config.environment!r}; "
                f"expected one of {sorted(known)}"
            )
        if self._config.live_trading and not self._config.live_confirmation:
            raise ConfigurationError(
                "live trading is enabled without a confirmation phrase; "
                "refusing to start (INVARIANT 10)"
            )
        if self._config.live_trading and self._config.environment != "production":
            raise ConfigurationError(
                f"live trading is enabled in environment "
                f"{self._config.environment!r}; live is only permitted in "
                "production"
            )

    def _finish(
        self,
        stages: list[ComponentHealth],
        findings: list[str],
        *,
        error: str | None = None,
    ) -> StartupReport:
        report = StartupReport(
            stages=tuple(stages), recovery_findings=tuple(findings), error=error
        )
        self._audit.record(
            AuditCategory.SYSTEM,
            "startup_completed",
            outcome=(
                AuditOutcome.ALLOWED if report.ok else AuditOutcome.REFUSED
            ),
            actor="system",
            details=report.as_details(),
        )
        return report
