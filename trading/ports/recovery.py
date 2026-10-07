"""Restart recovery port.

This module defines the interface for restart recovery coordination,
which identifies ambiguous states (PENDING_NEW, UNKNOWN) after a crash
and provides an operator-facing reconciliation interface.

INVARIANT 5: An UNKNOWN order blocks new orders until reconciled.
This blocking must survive process restart.

INVARIANT: A PENDING_NEW order after restart is treated as potentially
submitted and blocks new orders until the venue is queried to determine
the actual state.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass

from ..core.authz import Principal
from ..core.orders import Order
from ..ports.broker import BrokerAck


__all__ = ["RestartRecoveryPort", "AmbiguousOrder", "RecoveryReport"]


@dataclass(frozen=True, slots=True)
class AmbiguousOrder:
    """An order whose fate is ambiguous after a crash.

    Either:
    - The order is in PENDING_NEW state with a reservation in SUBMITTED,
      meaning it may have reached the venue before the crash.
    - The order is in UNKNOWN state, meaning we do not know if the venue
      saw it.
    """

    order: Order
    reason: str


@dataclass(frozen=True, slots=True)
class RecoveryReport:
    """The result of a restart recovery scan."""

    total_orders: int
    ambiguous_orders: tuple[AmbiguousOrder, ...]
    unknown_orders: tuple[Order, ...]
    pending_new_orders: tuple[Order, ...]

    @property
    def has_ambiguous_orders(self) -> bool:
        """True if any orders have ambiguous state."""
        return len(self.ambiguous_orders) > 0

    @property
    def has_unknown(self) -> bool:
        """True if any orders are in UNKNOWN state (blocks new orders)."""
        return len(self.unknown_orders) > 0


class RestartRecoveryPort(ABC):
    """Interface for restart recovery coordination.

    After a process crash and restart, the recovery coordinator:
    1. Loads persistent state from repositories
    2. Identifies ambiguous orders (PENDING_NEW or UNKNOWN)
    3. Reports what needs reconciliation
    4. Provides methods for operator-initiated reconciliation
    """

    @abstractmethod
    def scan(self) -> RecoveryReport:
        """Scan persistent state and identify ambiguous orders.

        Returns a report listing:
        - All orders
        - Ambiguous orders (PENDING_NEW with SUBMITTED reservation, or UNKNOWN)
        - Orders in UNKNOWN state
        - Orders in PENDING_NEW state
        """

    @abstractmethod
    def sync_order(self, order: Order, operator: Principal) -> BrokerAck:
        """Sync an order with the venue to discover its actual state.

        This is the same as ExecutionGateway.sync_order() but exposed
        directly by the recovery coordinator for operator use.

        Returns the broker's acknowledgement of the sync.
        """

    @abstractmethod
    def resolve_unknown(self, order: Order, operator: Principal) -> BrokerAck:
        """Resolve an UNKNOWN order after reconciling with the venue.

        This is the same as ExecutionGateway.resolve_unknown() but exposed
        directly by the recovery coordinator for operator use.

        Returns the broker's acknowledgement of the resolution.
        """
