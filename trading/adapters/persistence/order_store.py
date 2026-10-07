"""In-memory OrderRepository implementation.

This class wraps the existing :class:`~trading.core.orders.OrderStore` and
exposes it as an :class:`~trading.ports.repository.OrderRepositoryPort`.
The :class:`~trading.core.orders.Order` class already stores its state in a
way that supports persistence, but this adapter provides the port interface
and ensures the repository contract is satisfied.
"""

from __future__ import annotations

from typing import Mapping

from trading.core.orders import Order, OrderStore as CoreOrderStore
from trading.ports.repository import OrderRepositoryPort

__all__ = ["OrderStoreAdapter"]


class OrderStoreAdapter(CoreOrderStore):
    """In-memory adapter for :class:`~trading.ports.repository.OrderRepositoryPort`.

    This class delegates to :class:`~trading.core.orders.OrderStore` but
    satisfies the :class:`~trading.ports.repository.OrderRepositoryPort`
    interface contract.

    The key difference is that this class is explicitly designed to be
    replaceable by a persistent implementation. Today it is in-memory only,
    but a later Stage 2H adapter will implement the same interface with
    PostgreSQL or another database.
    """

    def __init__(self, *, store: CoreOrderStore | None = None) -> None:
        """Construct with an optional existing OrderStore to wrap."""
        super().__init__()
        # Use the underlying OrderStore for storage and synchronization.
        if store is not None:
            self._by_id = store._by_id
            self._by_key = store._by_key
            self._lock = store._lock

    # OrderRepositoryPort methods (OrderStore already implements these)
    def add(self, order: Order) -> Order:
        """Persist a new order. Rejects duplicates."""
        return super().add(order)

    def get(self, order_id: str) -> Order:
        """Fetch by id, raising KeyError if absent."""
        return super().get(order_id)

    def find_by_idempotency_key(self, key: str) -> Order | None:
        """Fetch by idempotency key, or None."""
        return super().find_by_idempotency_key(key)

    def all_orders(self) -> list[Order]:
        """Get all orders."""
        return super().all_orders()

    def unknown_orders(self) -> list[Order]:
        """Orders in UNKNOWN state."""
        return super().unknown_orders()

    def open_orders(self) -> list[Order]:
        """Orders that are open (not terminal, not UNKNOWN)."""
        return super().open_orders()

    def has_unknown_orders(self) -> bool:
        """True if any order is in UNKNOWN state."""
        return super().has_unknown_orders()

    def update(self, order: Order) -> Order:
        """Update an existing order's state in persistence.

        OrderStore doesn't have a separate update method because it stores
        mutable Order objects. This adapter adds update() to satisfy the
        repository port contract for explicit state persistence.
        """
        # Order objects are mutable, so just updating the in-memory order
        # is sufficient. The OrderStore already holds references to its orders.
        with self._lock:
            if order.order_id not in self._by_id:
                raise KeyError(f"no order with id {order.order_id!r}")
            # The order is already in the store by reference, so modifications
            # are automatically reflected. We just return it.
            return order


# Register the adapter as an OrderRepositoryPort
OrderRepositoryPort.register(OrderStoreAdapter)
