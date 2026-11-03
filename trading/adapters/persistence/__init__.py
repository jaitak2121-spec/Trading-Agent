"""In-memory persistence adapters for the repository ports.

This package provides in-memory implementations that satisfy the repository
interfaces. These are designed to be replaceable by persistent implementations
(e.g., PostgreSQL) in later stages.

This package provides:
- OrderStore: In-memory OrderRepository implementation
- PositionLedger: In-memory PositionRepository implementation
- ReservationRepository: In-memory ReservationRepository implementation
- DurableOrderStore / DurableReservationRepository / DurablePositionLedger:
  crash-safe, on-disk implementations of the repository ports (opt-in; the
  default wiring stays in-memory).

For restart recovery, see trading.adapters.recovery.RestartRecoveryCoordinator.
"""

from __future__ import annotations

from .durable import (
    DurableOrderStore,
    DurablePositionLedger,
    DurableReservationRepository,
    PersistenceError,
)
from .order_store import OrderStoreAdapter
from .position_ledger import PositionLedgerAdapter as PositionLedger
from .reservation_repository import ReservationRepository
from .sqlite import (
    SQLiteAuditSink,
    SQLiteOrderStore,
    SQLitePnlLedger,
    SQLitePortfolio,
    SQLitePositionLedger,
    SQLiteReservationRepository,
    SQLiteRiskEngine,
    SQLiteState,
)

# The package-level name is the adapter implementation, not the core class.
OrderStore = OrderStoreAdapter

__all__ = [
    "DurableOrderStore",
    "DurablePositionLedger",
    "DurableReservationRepository",
    "OrderStore",
    "OrderStoreAdapter",
    "PersistenceError",
    "PositionLedger",
    "ReservationRepository",
    "SQLiteAuditSink",
    "SQLiteOrderStore",
    "SQLitePnlLedger",
    "SQLitePortfolio",
    "SQLitePositionLedger",
    "SQLiteReservationRepository",
    "SQLiteRiskEngine",
    "SQLiteState",
]
