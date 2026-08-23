"""Persistence ports.

:class:`~trading.core.orders.OrderStore` and
:class:`~trading.core.reconciliation.PositionLedger` are the in-memory
implementations the kernel uses today; these interfaces are the seam a
PostgreSQL-backed repository slots into in a later stage.

One durability note that matters for Stage 2 and beyond: an order must be
persisted in ``PENDING_NEW`` *before* it is sent to a venue. A crash between
sending and persisting is indistinguishable from a crash before sending, and the
only safe way to tell them apart afterwards is to have written the intent down
first. The in-memory implementations trivially satisfy this because there is no
separate commit step; a real database adapter must be careful to.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Mapping

from ..core.money import Quantity
from ..core.orders import Order

__all__ = ["OrderRepositoryPort", "PositionRepositoryPort"]


class OrderRepositoryPort(ABC):
    """Durable storage for orders."""

    @abstractmethod
    def add(self, order: Order) -> Order:
        """Persist a new order. Must reject a duplicate order id."""

    @abstractmethod
    def get(self, order_id: str) -> Order:
        """Fetch by id, raising ``KeyError`` if absent."""

    @abstractmethod
    def find_by_idempotency_key(self, key: str) -> Order | None:
        """Fetch by idempotency key, or ``None`` (supports INVARIANT 12)."""

    @abstractmethod
    def all_orders(self) -> list[Order]:
        ...

    @abstractmethod
    def unknown_orders(self) -> list[Order]:
        """Orders whose fate is undetermined (INVARIANT 5)."""

    @abstractmethod
    def open_orders(self) -> list[Order]:
        ...

    @abstractmethod
    def has_unknown_orders(self) -> bool:
        ...


class PositionRepositoryPort(ABC):
    """Durable storage for the local position ledger."""

    @abstractmethod
    def position(self, symbol: str, *, asset: str | None = None) -> Quantity:
        ...

    @abstractmethod
    def set_position(self, symbol: str, quantity: Quantity) -> None:
        ...

    @abstractmethod
    def snapshot(self) -> Mapping[str, Quantity]:
        """A copy of every non-trivial position."""

    @abstractmethod
    def symbols(self) -> list[str]:
        ...


# Conformance is declared here rather than by inheritance in ``trading.core``.
# ``trading.ports.broker`` imports ``trading.core.orders`` at runtime, so having
# ``OrderStore`` name ``OrderRepositoryPort`` as a base class would close an
# import cycle. Registering from this side works because this module already
# depends on ``trading.core``, and the dependency arrow still points one way.
#
# ``register`` buys the ``issubclass`` relationship but enforces no method, so it
# is not the whole guarantee: ``tests/test_ports.py`` checks that every abstract
# method exists on each implementation with a compatible signature. That is the
# check that catches drift, and it is strictly stronger than inheritance, which
# would notice a missing method but not a changed one.
from ..core.orders import OrderStore  # noqa: E402  (deliberately after the ports)
from ..core.reconciliation import PositionLedger  # noqa: E402

OrderRepositoryPort.register(OrderStore)
PositionRepositoryPort.register(PositionLedger)
