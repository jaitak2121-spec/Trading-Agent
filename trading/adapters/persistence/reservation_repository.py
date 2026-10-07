"""In-memory ReservationRepository implementation.

This class implements :class:`~trading.ports.reservation_repository.ReservationRepositoryPort`
using an in-memory dictionary. It is designed to be replaceable by a persistent
implementation in a later Stage 2H adapter.

The reservation stores the state of an idempotency key's lifecycle, which determines
whether a submitted order can be safely retried (RESERVED) or must be reconciled
via the venue (SUBMITTED/UNKNOWN).
"""
from __future__ import annotations

import threading
from typing import Mapping

from trading.core.dedupe import Reservation, ReservationState
from trading.ports.reservation_repository import ReservationRepositoryPort

__all__ = ["ReservationRepository"]


class ReservationRepository(ReservationRepositoryPort):
    """In-memory implementation of :class:`~trading.ports.reservation_repository.ReservationRepositoryPort`.

    This class stores reservations in an in-memory dictionary. It is designed
    to be replaceable by a persistent implementation (e.g., PostgreSQL) in a
    later stage.

    The reservation state machine:
    - RESERVED: Key claimed locally; nothing sent yet
    - SUBMITTED: Request has left the process
    - SETTLED: Outcome known and final
    - UNKNOWN: Outcome unknown; blocks new orders

    A key in UNKNOWN blocks the whole registry via :meth:`has_unknown`.
    """

    def __init__(self) -> None:
        self._reservations: dict[str, Reservation] = {}
        self._lock = threading.RLock()

    def add(self, reservation: Reservation) -> Reservation:
        """Persist a new reservation. Must reject a duplicate key."""
        with self._lock:
            if reservation.key in self._reservations:
                raise ValueError(
                    f"reservation for key {reservation.key[:16]}... already exists"
                )
            self._reservations[reservation.key] = reservation
            return reservation

    def get(self, key: str) -> Reservation | None:
        """Fetch by key, or None if absent."""
        with self._lock:
            return self._reservations.get(key)

    def update(self, reservation: Reservation) -> Reservation:
        """Update an existing reservation's state."""
        with self._lock:
            if reservation.key not in self._reservations:
                raise KeyError(f"no reservation for key {reservation.key[:16]}...")
            self._reservations[reservation.key] = reservation
            return reservation

    def all_reservations(self) -> list[Reservation]:
        """Get all reservations."""
        with self._lock:
            return list(self._reservations.values())

    def unknown_reservations(self) -> list[Reservation]:
        """Reservations in UNKNOWN state (blocks new orders)."""
        with self._lock:
            return [
                r for r in self._reservations.values()
                if r.state is ReservationState.UNKNOWN
            ]

    def has_unknown(self) -> bool:
        """True if any reservation is in UNKNOWN state."""
        with self._lock:
            return any(
                r.state is ReservationState.UNKNOWN
                for r in self._reservations.values()
            )

    def delete(self, key: str) -> None:
        """Delete a reservation."""
        with self._lock:
            if key in self._reservations:
                del self._reservations[key]

    def __len__(self) -> int:
        with self._lock:
            return len(self._reservations)
