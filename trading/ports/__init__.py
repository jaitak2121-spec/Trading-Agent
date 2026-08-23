"""Port interfaces: the boundary between the safety kernel and the outside world.

Every abstract class here is a *port* in the hexagonal sense -- an interface the
kernel calls, implemented by an adapter in :mod:`trading.adapters`. Ports are
where FastAPI, PostgreSQL, and the CoinSwitch REST client will attach in later
stages, without the safety core changing.

**This package holds no implementation and no infrastructure dependency.** It
imports only the standard library and :mod:`trading.core` value types.
Together, ``trading.core`` and ``trading.ports`` form the pure kernel;
``tests/test_core_purity.py`` enforces that mechanically.

The one design rule worth stating: :meth:`~trading.ports.broker.BrokerPort.place_order`
requires an :class:`~trading.core.authz.ExecutionToken`. A token can only be
minted by the execution gateway, so an adapter cannot be driven directly by a
strategy even if the strategy somehow gets a reference to it (INVARIANT 3).
"""

from __future__ import annotations

from .broker import BrokerAck, BrokerPort, BrokerPositionSnapshot
from .market_data import MarketDataPort
from .repository import OrderRepositoryPort, PositionRepositoryPort

__all__ = [
    "BrokerAck",
    "BrokerPort",
    "BrokerPositionSnapshot",
    "MarketDataPort",
    "OrderRepositoryPort",
    "PositionRepositoryPort",
]
