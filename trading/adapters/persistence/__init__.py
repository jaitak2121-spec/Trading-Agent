"""In-memory persistence adapters for the repository ports.

This package provides in-memory implementations that satisfy the repository
interfaces. These are designed to be replaceable by persistent implementations
(e.g., PostgreSQL) in later stages.

This package provides:
- OrderStore: In-memory OrderRepository implementation
- PositionLedger: In-memory PositionRepository implementation
- ReservationRepository: In-memory ReservationRepository implementation
- DurableReservationRepository / DurablePositionLedger: crash-safe, on-disk
  implementations of the reservation and position ports (opt-in; the default
  wiring stays in-memory). See durable.py for why orders are not among them.

For restart recovery, see trading.adapters.recovery.RestartRecoveryCoordinator.
"""

from __future__ import annotations

from .durable import (
    DurablePositionLedger,
    DurableReservationRepository,
    PersistenceError,
)
from .order_store import OrderStoreAdapter
from .position_ledger import PositionLedgerAdapter as PositionLedger
from .reservation_repository import ReservationRepository

# The package-level name is the adapter implementation, not the core class.
OrderStore = OrderStoreAdapter

__all__ = [
    "DurablePositionLedger",
    "DurableReservationRepository",
    "OrderStore",
    "OrderStoreAdapter",
    "PersistenceError",
    "PositionLedger",
    "ReservationRepository",
]
