"""Venue-wide reconciliation: comparing the whole book, not one order at a time.

``sync_order`` answers "what happened to *this* order", which is the right
question for an order we know about and useless for one we do not. An order the
venue holds that we have no local record of -- placed out of band, or created by
a crash between the venue accepting it and our writing it down -- is invisible to
a per-order query by construction, because there is no local order to ask about.

This module is what asks the other question: *what does the venue think it
holds?* It compares that against local storage and the local ledger, and turns
every disagreement into a finding.

Three things about the shape are load-bearing
=============================================

**It observes; the gateway acts.** Nothing here transitions an order, books a
fill, writes the ledger, or touches a reservation. A matched, non-contradictory
order is handed to :meth:`~trading.core.gateway.ExecutionGateway.sync_order`,
which applies the cumulative delta under its own lock, through its own audit, and
with its own authorization. The coordinator could reach into the store directly
and would be a second execution path if it did -- which is exactly what
:class:`~trading.core.gateway.ExecutionGateway` exists to prevent.

**It never resolves the ambiguous.** An UNKNOWN order is *reported*, never
touched. Leaving UNKNOWN is a deliberate operator act with its own reservation
bookkeeping (INVARIANT 5); a background sweep that quietly performed it would
turn "we do not know what happened" into "we have decided what happened", which
is the failure this whole design refuses.

**Disagreement blocks, but does not repair.** A missing order, a ghost order, a
contradicted identity, a regressed fill: each is a finding, and a report
containing a blocking finding is not a clean reconciliation. Nothing here
auto-corrects, because the honest answer to "the venue and we disagree" is to
stop and tell an operator, not to pick a winner. A venue snapshot that reads
clean is still what refreshes the gate's freshness timestamp -- and per
:class:`~trading.core.reconciliation.ReconciliationGate`, a clean sweep does not
clear an existing mismatch latch.

Requiring the inventory capability
==================================

A venue adapter without :class:`~trading.ports.broker.BrokerOrderInventoryPort`
cannot support a complete sweep, and this refuses to pretend otherwise: it
reports ``INVENTORY_UNAVAILABLE`` and blocks, rather than running a partial
sweep that would look identical to a clean one while detecting nothing. An
incomplete check that reports success is worse than no check, because it
manufactures the confidence the gate is built to require.

No network, no credentials, no live-trading capability: this module can do
nothing an operator calling ``sync_order`` by hand could not do, plus tell them
which orders they did not know to ask about.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from ..core.audit import AuditCategory, AuditLog, AuditOutcome
from ..core.authz import Principal
from ..core.clock import Clock
from ..core.errors import SafetyViolation, UnauthorizedAction
from ..core.gateway import ExecutionGateway
from ..core.orders import Order, OrderState, OrderStore
from ..core.reconciliation import ReconciliationGate
from ..ports.broker import (
    BrokerOrderInventoryPort,
    BrokerOrderSnapshot,
    BrokerPort,
)

__all__ = [
    "FindingKind",
    "FindingSeverity",
    "ReconciliationFinding",
    "ReconciliationCoordinator",
    "VenueSweepReport",
]


class FindingSeverity(Enum):
    """Whether a finding stops the system or merely needs saying."""

    #: New orders must not be accepted while this stands.
    BLOCKING = "blocking"
    #: Worth recording; the system is not proven unsound by it.
    ADVISORY = "advisory"


class FindingKind:
    """Labels for what went wrong. Strings, so they grep in an audit trail."""

    #: The venue does not hold an order we believe is open. The dangerous one:
    #: our book says exposure exists and the venue says it does not.
    LOCAL_ORDER_MISSING_AT_VENUE = "local_order_missing_at_venue"
    #: The venue holds an order we have no local record of -- out of band, or
    #: created in a window we did not survive.
    VENUE_ORDER_UNKNOWN_LOCALLY = "venue_order_unknown_locally"
    #: Matched by one key but the identities disagree.
    IDENTITY_MISMATCH = "identity_mismatch"
    SYMBOL_MISMATCH = "symbol_mismatch"
    SIDE_MISMATCH = "side_mismatch"
    QUANTITY_MISMATCH = "quantity_mismatch"
    #: The venue reports less filled than we have already booked.
    FILL_REGRESSION = "fill_regression"
    #: The venue still holds an order we consider finished.
    VENUE_HOLDS_TERMINAL_ORDER = "venue_holds_terminal_order"
    #: Two venue records share one broker order id.
    DUPLICATE_VENUE_ORDER = "duplicate_venue_order"
    #: The venue adapter cannot enumerate its book at all.
    INVENTORY_UNAVAILABLE = "inventory_unavailable"
    #: A venue read raised. Nothing is known about the orders it did not cover.
    TRANSPORT_FAILURE = "transport_failure"
    #: An order is UNKNOWN. Reported so it is visible; never resolved here.
    UNKNOWN_ORDER_UNRESOLVED = "unknown_order_unresolved"
    #: A matched order the gateway refused to sync, with its reason.
    SYNC_REFUSED = "sync_refused"


@dataclass(frozen=True, slots=True)
class ReconciliationFinding:
    """One disagreement between local state and the venue, or one blocker."""

    kind: str
    severity: FindingSeverity
    detail: str
    order_id: str | None = None
    broker_order_id: str | None = None

    @property
    def is_blocking(self) -> bool:
        return self.severity is FindingSeverity.BLOCKING

    def as_details(self) -> dict[str, object]:
        return {
            "kind": self.kind,
            "severity": self.severity.value,
            "detail": self.detail,
            "order_id": self.order_id,
            "broker_order_id": self.broker_order_id,
        }


@dataclass(frozen=True, slots=True)
class VenueSweepReport:
    """What one venue-wide sweep found and did.

    ``blocked`` is the field the operational layer reads: it is true when any
    blocking finding stands, or when the sweep could not complete at all. A
    sweep that failed is emphatically not a clean sweep -- ``error`` being set
    and ``blocked`` being false would be the silent-success failure this report
    exists to make impossible.
    """

    at: str
    findings: tuple[ReconciliationFinding, ...] = ()
    synced: tuple[str, ...] = ()
    local_open_count: int = 0
    venue_order_count: int = 0
    symbols_checked: tuple[str, ...] = ()
    error: str | None = None

    @property
    def blocking(self) -> tuple[ReconciliationFinding, ...]:
        return tuple(f for f in self.findings if f.is_blocking)

    @property
    def blocked(self) -> bool:
        return bool(self.blocking) or self.error is not None

    @property
    def is_clean(self) -> bool:
        return not self.blocked

    def as_details(self) -> dict[str, object]:
        return {
            "at": self.at,
            "blocked": self.blocked,
            "error": self.error,
            "synced": list(self.synced),
            "local_open_count": self.local_open_count,
            "venue_order_count": self.venue_order_count,
            "symbols_checked": list(self.symbols_checked),
            "findings": [f.as_details() for f in self.findings],
        }


class ReconciliationCoordinator:
    """Compares the venue's whole book against ours, and reports.

    ``identity`` must hold :attr:`~trading.core.authz.Action.RECONCILE`, which
    is what the gateway checks when this calls ``sync_order``. It is not
    re-checked here: a second copy of the authorization rule could drift from
    the real one, and an unauthorized identity instead raises out of the first
    sweep -- loudly, which is the intent.
    """

    def __init__(
        self,
        *,
        gateway: ExecutionGateway,
        orders: OrderStore,
        broker: BrokerPort,
        reconciliation: ReconciliationGate,
        audit: AuditLog,
        clock: Clock,
        identity: Principal,
    ) -> None:
        self._gateway = gateway
        self._orders = orders
        self._broker = broker
        self._reconciliation = reconciliation
        self._audit = audit
        self._clock = clock
        self._identity = identity
        self._sweeps = 0
        self._last_report: VenueSweepReport | None = None

    # -- observation ------------------------------------------------------

    @property
    def sweeps(self) -> int:
        return self._sweeps

    @property
    def last_report(self) -> VenueSweepReport | None:
        return self._last_report

    @property
    def supports_venue_inventory(self) -> bool:
        """Whether this venue can be swept in full. Read by the readiness check."""
        return isinstance(self._broker, BrokerOrderInventoryPort)

    # -- one sweep --------------------------------------------------------

    def sweep(self) -> VenueSweepReport:
        """Read the venue whole, compare, sync what is unambiguous, report.

        A read that fails produces a report with ``error`` set and a blocking
        finding, and leaves every order exactly as it was. Local state is never
        changed on the strength of a read that did not complete.
        """
        now = self._clock.now().isoformat()

        if not isinstance(self._broker, BrokerOrderInventoryPort):
            finding = ReconciliationFinding(
                kind=FindingKind.INVENTORY_UNAVAILABLE,
                severity=FindingSeverity.BLOCKING,
                detail=(
                    f"{type(self._broker).__name__} cannot enumerate its orders, "
                    "so a complete reconciliation is impossible; ghost and "
                    "out-of-band orders cannot be detected against it"
                ),
            )
            return self._finish(now, findings=(finding,), error=None)

        try:
            snapshots = self._broker.fetch_order_inventory()
            positions = self._broker.fetch_positions()
        except UnauthorizedAction:
            raise
        except Exception as exc:  # noqa: BLE001 - reported as a failure, see below
            # A read that failed says nothing about the orders it did not reach,
            # so this is a blocking failure rather than an empty clean sweep.
            finding = ReconciliationFinding(
                kind=FindingKind.TRANSPORT_FAILURE,
                severity=FindingSeverity.BLOCKING,
                detail=(
                    f"venue inventory read raised {type(exc).__name__}: {exc}; "
                    "local state left untouched"
                ),
            )
            return self._finish(
                now, findings=(finding,), error=f"{type(exc).__name__}: {exc}"
            )

        findings, syncable = self._compare(snapshots)

        # The venue's positions go through the gate, which owns the latch and
        # the freshness rule. A clean snapshot refreshes it; a discrepancy
        # latches it and will block the next submission.
        try:
            report = self._reconciliation.reconcile(positions.positions)
        except SafetyViolation as exc:
            findings.append(
                ReconciliationFinding(
                    kind=FindingKind.SYMBOL_MISMATCH,
                    severity=FindingSeverity.BLOCKING,
                    detail=f"venue positions could not be compared: {exc}",
                )
            )
            symbols: tuple[str, ...] = ()
        else:
            symbols = report.symbols_checked

        synced: list[str] = []
        for order in syncable:
            try:
                self._gateway.sync_order(order, operator=self._identity)
            except UnauthorizedAction:
                # Ordered before SafetyViolation, which it subclasses. This
                # identity may not reconcile at all, so every remaining order
                # would fail identically, and authorize() refused without
                # auditing -- swallowing it would leave a silent dead sweep.
                raise
            except SafetyViolation as exc:
                findings.append(
                    ReconciliationFinding(
                        kind=FindingKind.SYNC_REFUSED,
                        severity=FindingSeverity.ADVISORY,
                        detail=f"gateway refused to sync: {exc}",
                        order_id=order.order_id,
                        broker_order_id=order.broker_order_id,
                    )
                )
            except Exception as exc:  # noqa: BLE001 - one order, not the sweep
                findings.append(
                    ReconciliationFinding(
                        kind=FindingKind.TRANSPORT_FAILURE,
                        severity=FindingSeverity.BLOCKING,
                        detail=(
                            f"syncing {order.order_id} raised "
                            f"{type(exc).__name__}: {exc}"
                        ),
                        order_id=order.order_id,
                        broker_order_id=order.broker_order_id,
                    )
                )
            else:
                synced.append(order.order_id)

        return self._finish(
            now,
            findings=tuple(findings),
            synced=tuple(synced),
            symbols=symbols,
            venue_order_count=len(snapshots),
        )

    # -- comparison -------------------------------------------------------

    def _compare(
        self, snapshots: tuple[BrokerOrderSnapshot, ...]
    ) -> tuple[list[ReconciliationFinding], list[Order]]:
        """Match the venue's book against ours. Returns findings and sync work."""
        findings: list[ReconciliationFinding] = []
        syncable: list[Order] = []

        by_broker_id: dict[str, BrokerOrderSnapshot] = {}
        by_key: dict[str, BrokerOrderSnapshot] = {}
        for snapshot in snapshots:
            if snapshot.broker_order_id in by_broker_id:
                findings.append(
                    ReconciliationFinding(
                        kind=FindingKind.DUPLICATE_VENUE_ORDER,
                        severity=FindingSeverity.BLOCKING,
                        detail=(
                            f"the venue reported {snapshot.broker_order_id!r} more "
                            "than once; matching is ambiguous"
                        ),
                        broker_order_id=snapshot.broker_order_id,
                    )
                )
                continue
            by_broker_id[snapshot.broker_order_id] = snapshot
            if snapshot.idempotency_key:
                by_key[snapshot.idempotency_key] = snapshot

        local_orders = self._orders.all_orders()
        matched: set[str] = set()

        for order in local_orders:
            snapshot = self._match(order, by_broker_id, by_key)

            if order.state is OrderState.UNKNOWN:
                # Surfaced, never resolved. The sweep deliberately does not
                # touch it: leaving UNKNOWN is an operator act (INVARIANT 5).
                findings.append(
                    ReconciliationFinding(
                        kind=FindingKind.UNKNOWN_ORDER_UNRESOLVED,
                        severity=FindingSeverity.BLOCKING,
                        detail=(
                            f"order {order.order_id} is UNKNOWN and blocks new "
                            "orders until an operator resolves it"
                        ),
                        order_id=order.order_id,
                        broker_order_id=order.broker_order_id,
                    )
                )
                if snapshot is not None:
                    matched.add(snapshot.broker_order_id)
                continue

            if order.state.is_terminal:
                if snapshot is not None:
                    matched.add(snapshot.broker_order_id)
                    if snapshot.status.value in ("open", "partially_filled"):
                        findings.append(
                            ReconciliationFinding(
                                kind=FindingKind.VENUE_HOLDS_TERMINAL_ORDER,
                                severity=FindingSeverity.BLOCKING,
                                detail=(
                                    f"order {order.order_id} is {order.state.value} "
                                    f"locally but the venue still reports it "
                                    f"{snapshot.status.value}; it can still trade"
                                ),
                                order_id=order.order_id,
                                broker_order_id=snapshot.broker_order_id,
                            )
                        )
                continue

            if not order.is_open:
                continue

            if snapshot is None:
                findings.append(
                    ReconciliationFinding(
                        kind=FindingKind.LOCAL_ORDER_MISSING_AT_VENUE,
                        severity=FindingSeverity.BLOCKING,
                        detail=(
                            f"order {order.order_id} is {order.state.value} locally "
                            "but the venue holds no record of it; exposure we "
                            "believe exists may not"
                        ),
                        order_id=order.order_id,
                        broker_order_id=order.broker_order_id,
                    )
                )
                continue

            matched.add(snapshot.broker_order_id)
            contradiction = self._contradiction(order, snapshot)
            if contradiction is not None:
                findings.append(contradiction)
                continue

            syncable.append(order)

        for snapshot in snapshots:
            if snapshot.broker_order_id in matched:
                continue
            findings.append(
                ReconciliationFinding(
                    kind=FindingKind.VENUE_ORDER_UNKNOWN_LOCALLY,
                    severity=FindingSeverity.BLOCKING,
                    detail=(
                        f"the venue holds {snapshot.broker_order_id} "
                        f"({snapshot.symbol} {snapshot.side} "
                        f"{snapshot.ordered_quantity.amount}, {snapshot.status.value}) "
                        "with no local order; it was placed out of band or its "
                        "record was lost"
                    ),
                    broker_order_id=snapshot.broker_order_id,
                )
            )

        return findings, syncable

    def _match(
        self,
        order: Order,
        by_broker_id: dict[str, BrokerOrderSnapshot],
        by_key: dict[str, BrokerOrderSnapshot],
    ) -> BrokerOrderSnapshot | None:
        """Find the venue's record for ``order``, preferring the venue's own id.

        The broker order id is the venue's identity for the order and is the
        stronger key; the idempotency key is ours, and is used only when the
        order never got an id attached -- which is exactly the UNKNOWN case,
        where the ack that would have carried it never arrived.
        """
        if order.broker_order_id is not None:
            found = by_broker_id.get(order.broker_order_id)
            if found is not None:
                return found
        return by_key.get(order.idempotency_key)

    def _contradiction(
        self, order: Order, snapshot: BrokerOrderSnapshot
    ) -> ReconciliationFinding | None:
        """The first way this pair disagrees, or None if they are consistent.

        Returns on the first contradiction rather than collecting all of them:
        once the records disagree about what the order *is*, the remaining
        fields are not describing the same order and comparing them further
        would produce noise, not evidence.
        """
        if (
            order.broker_order_id is not None
            and snapshot.broker_order_id != order.broker_order_id
        ):
            return ReconciliationFinding(
                kind=FindingKind.IDENTITY_MISMATCH,
                severity=FindingSeverity.BLOCKING,
                detail=(
                    f"order {order.order_id} carries broker id "
                    f"{order.broker_order_id} but the venue record matching its "
                    f"idempotency key is {snapshot.broker_order_id}"
                ),
                order_id=order.order_id,
                broker_order_id=snapshot.broker_order_id,
            )
        if snapshot.symbol != order.symbol:
            return ReconciliationFinding(
                kind=FindingKind.SYMBOL_MISMATCH,
                severity=FindingSeverity.BLOCKING,
                detail=(
                    f"order {order.order_id} is for {order.symbol} but the venue "
                    f"reports {snapshot.symbol}"
                ),
                order_id=order.order_id,
                broker_order_id=snapshot.broker_order_id,
            )
        if snapshot.side != order.side.value:
            return ReconciliationFinding(
                kind=FindingKind.SIDE_MISMATCH,
                severity=FindingSeverity.BLOCKING,
                detail=(
                    f"order {order.order_id} is a {order.side.value} but the venue "
                    f"reports a {snapshot.side}"
                ),
                order_id=order.order_id,
                broker_order_id=snapshot.broker_order_id,
            )
        if snapshot.ordered_quantity != order.intent.quantity:
            return ReconciliationFinding(
                kind=FindingKind.QUANTITY_MISMATCH,
                severity=FindingSeverity.BLOCKING,
                detail=(
                    f"order {order.order_id} was for {order.intent.quantity} but "
                    f"the venue reports {snapshot.ordered_quantity}"
                ),
                order_id=order.order_id,
                broker_order_id=snapshot.broker_order_id,
            )
        if snapshot.filled_quantity < order.filled_quantity:
            return ReconciliationFinding(
                kind=FindingKind.FILL_REGRESSION,
                severity=FindingSeverity.BLOCKING,
                detail=(
                    f"order {order.order_id} has {order.filled_quantity} booked but "
                    f"the venue reports only {snapshot.filled_quantity} filled; "
                    "fills are cumulative and cannot go backwards"
                ),
                order_id=order.order_id,
                broker_order_id=snapshot.broker_order_id,
            )
        return None

    # -- recording --------------------------------------------------------

    def _finish(
        self,
        at: str,
        *,
        findings: tuple[ReconciliationFinding, ...],
        synced: tuple[str, ...] = (),
        symbols: tuple[str, ...] = (),
        venue_order_count: int = 0,
        error: str | None = None,
    ) -> VenueSweepReport:
        """Build the report and write it to the audit trail before returning."""
        report = VenueSweepReport(
            at=at,
            findings=findings,
            synced=synced,
            local_open_count=len(self._orders.open_orders()),
            venue_order_count=venue_order_count,
            symbols_checked=symbols,
            error=error,
        )
        self._audit.record(
            AuditCategory.RECONCILIATION,
            "gateway.venue_sweep",
            outcome=(
                AuditOutcome.ALLOWED if report.is_clean else AuditOutcome.REFUSED
            ),
            actor=self._identity.principal_id,
            details=report.as_details(),
        )
        self._sweeps += 1
        self._last_report = report
        return report
