"""Reservation persistence port.

:class:`~trading.core.dedupe.IdempotencyRegistry` is the in-memory implementation
the kernel uses today; this interface is the seam a PostgreSQL-backed reservation
repository slots into in a later stage.

The reservation stores the state of an idempotency key's lifecycle, which determines
whether a submitted order can be safely retried (RESERVED) or must be reconciled
via the venue (SUBMITTED/UNKNOWN).
"""
from __future__ import annotations

from abc import ABC, abstractmethod

from ..core.dedupe import Reservation

__all__ = ["ReservationRepositoryPort"]


class ReservationRepositoryPort(ABC):
    """Durable storage for idempotency key reservations."""

    @abstractmethod
    def add(self, reservation: Reservation) -> Reservation:
        """Persist a new reservation. Must reject a duplicate key."""

    @abstractmethod
    def get(self, key: str) -> Reservation | None:
        """Fetch by key, or None if absent."""

    @abstractmethod
    def update(self, reservation: Reservation) -> Reservation:
        """Update an existing reservation's state."""

    @abstractmethod
    def all_reservations(self) -> list[Reservation]:
        """Get all reservations."""

    @abstractmethod
    def unknown_reservations(self) -> list[Reservation]:
        """Reservations in UNKNOWN state (blocks new orders)."""

    @abstractmethod
    def has_unknown(self) -> bool:
        """True if any reservation is in UNKNOWN state."""

    @abstractmethod
    def delete(self, key: str) -> None:
        """Delete a reservation."""


# Registered from the ports side for the same reason the order and position
# repositories are: ``ReservationStore`` lives in the kernel, and having it name
# this port as a base class would close an import cycle. See ``repository.py``
# for the full argument. ``register`` buys the ``issubclass`` relationship and
# nothing else; ``tests/test_ports.py`` is what checks the method signatures.
from ..core.dedupe import ReservationStore  # noqa: E402  (deliberately after the port)

ReservationRepositoryPort.register(ReservationStore)
