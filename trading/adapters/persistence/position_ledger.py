"""In-memory PositionRepository implementation.

This class wraps the existing :class:`~trading.core.reconciliation.PositionLedger`
and exposes it as an :class:`~trading.ports.repository.PositionRepositoryPort`.
"""

from __future__ import annotations

from typing import Mapping

from trading.core.money import Quantity
from trading.core.reconciliation import PositionLedger
from trading.ports.repository import PositionRepositoryPort

__all__ = ["PositionLedger"]  # Re-export as the adapter implementation


class PositionLedgerAdapter(PositionLedger):
    """In-memory adapter for :class:`~trading.ports.repository.PositionRepositoryPort`.

    This class delegates to :class:`~trading.core.reconciliation.PositionLedger`
    but satisfies the :class:`~trading.ports.repository.PositionRepositoryPort`
    interface contract.

    The key difference is that this class is explicitly designed to be
    replaceable by a persistent implementation. Today it is in-memory only,
    but a later Stage 2H adapter will implement the same interface with
    PostgreSQL or another database.
    """

    def __init__(self, *, ledger: PositionLedger | None = None) -> None:
        """Construct with an optional existing PositionLedger to wrap."""
        super().__init__()
        # Use the underlying PositionLedger for storage
        if ledger is not None:
            self._positions = ledger._positions
            self._lock = ledger._lock

    # PositionRepositoryPort methods (PositionLedger already implements these)
    def position(self, symbol: str, *, asset: str | None = None) -> Quantity:
        """Get the position for a symbol."""
        return super().position(symbol, asset=asset)

    def set_position(self, symbol: str, quantity: Quantity) -> None:
        """Set the position for a symbol."""
        super().set_position(symbol, quantity)

    def snapshot(self) -> Mapping[str, Quantity]:
        """Get a copy of all positions."""
        return super().snapshot()

    def symbols(self) -> list[str]:
        """Get all symbols with positions."""
        return super().symbols()

    def apply_fill(
        self,
        symbol: str,
        side: str,  # OrderSide not available here to avoid circular import
        quantity: Quantity,
        price: str | None = None,  # For compatibility
    ) -> Quantity:
        """Apply a fill to the position ledger.

        This method is added for compatibility with the existing API.
        """
        from trading.core.orders import OrderSide
        if price is not None:
            # Ignore price here; PositionLedger doesn't track prices
            pass
        return super().apply_fill(symbol, OrderSide(side), quantity)


# Register the adapter as a PositionRepositoryPort
PositionRepositoryPort.register(PositionLedgerAdapter)
