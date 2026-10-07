"""In-memory restart recovery adapter.

This module implements the restart recovery coordinator using the existing
in-memory repositories. It is designed to be replaceable by a persistent
implementation (e.g., PostgreSQL-backed) in a later stage.

The recovery coordinator:
1. Scans persistent state to identify ambiguous orders
2. Provides operator-facing reconciliation methods
3. Maintains UNKNOWN blocking semantics across restarts
"""
from __future__ import annotations

from trading.adapters.persistence import OrderStore, ReservationRepository
from trading.core.authz import Principal
from trading.core.gateway import ExecutionGateway
from trading.core.orders import Order
from trading.ports.broker import BrokerAck
from trading.ports.recovery import (
    AmbiguousOrder,
    RecoveryReport,
    RestartRecoveryPort,
)


__all__ = ["RestartRecoveryCoordinator"]


class RestartRecoveryCoordinator(RestartRecoveryPort):
    """Restart recovery coordinator using injected repositories.

    This coordinator is constructed with the same repositories that the
    ExecutionGateway uses, so it sees the same persistent state. After a
    crash and restart, it scans for ambiguous orders and provides methods
    for operator-initiated reconciliation.

    The coordinator does NOT create new orders or reservations. It only
    reads from the existing repositories and delegates to the gateway
    for any operations that would modify state.
    """

    def __init__(
        self,
        *,
        gateway: ExecutionGateway,
        orders: OrderStore,
        reservations: ReservationRepository,
    ) -> None:
        """Construct with the gateway and its repositories.

        The gateway must share repositories with the coordinator so they see the
        same persistent state. This is the same contract the coordinator already
        relies on for ``orders``: it reads order state from the injected
        ``orders`` store, trusting the caller to hand it the store the gateway
        writes through. ``reservations`` is treated identically -- it must be the
        same :class:`~trading.ports.reservation_repository.ReservationRepositoryPort`
        the gateway's :class:`~trading.core.dedupe.IdempotencyRegistry` persists
        to, so a scan reads the registry's authoritative view of reservation
        state through the port rather than reaching into gateway internals.
        """
        self._gateway = gateway
        self._orders = orders
        self._reservations = reservations

    def scan(self) -> RecoveryReport:
        """Scan persistent state and identify ambiguous orders.

        An order is ambiguous if:
        - It is in UNKNOWN state (INVARIANT 5: blocks new orders)
        - It is in PENDING_NEW state with a SUBMITTED reservation
          (may have reached the venue before crash)

        Reservation state is read from the injected reservation repository (the
        port), which is the same store the gateway's idempotency registry writes
        through. Returns a report listing all orders and identifying ambiguous
        ones.
        """
        all_orders = self._orders.all_orders()
        unknown_orders: list[Order] = []
        pending_new_with_submitted: list[Order] = []

        for order in all_orders:
            if order.state.value == "unknown":
                unknown_orders.append(order)
            elif order.state.value == "pending_new":
                # Check reservation state for PENDING_NEW orders
                reservation = self._reservations.get(order.idempotency_key)
                if reservation is not None and reservation.state.value == "submitted":
                    pending_new_with_submitted.append(order)

        ambiguous: list[AmbiguousOrder] = []
        for order in unknown_orders:
            ambiguous.append(
                AmbiguousOrder(
                    order=order,
                    reason="Order is in UNKNOWN state; venue query required",
                )
            )
        for order in pending_new_with_submitted:
            ambiguous.append(
                AmbiguousOrder(
                    order=order,
                    reason="Order is PENDING_NEW with SUBMITTED reservation; "
                    "may have reached venue before crash",
                )
            )

        return RecoveryReport(
            total_orders=len(all_orders),
            ambiguous_orders=tuple(ambiguous),
            unknown_orders=tuple(unknown_orders),
            pending_new_orders=tuple(pending_new_with_submitted),
        )

    def sync_order(self, order: Order, operator: Principal) -> BrokerAck:
        """Sync an order with the venue to discover its actual state.

        Delegates to ExecutionGateway.sync_order().

        Returns the broker's acknowledgement of the sync.
        """
        return self._gateway.sync_order(order, operator=operator)

    def resolve_unknown(self, order: Order, operator: Principal) -> BrokerAck:
        """Resolve an UNKNOWN order after reconciling with the venue.

        Delegates to ExecutionGateway.resolve_unknown().

        Returns the broker's acknowledgement of the resolution.
        """
        return self._gateway.resolve_unknown(order, operator=operator)
