"""In-memory adapters: a venue and a price feed that live inside the process.

Everything here is deterministic and offline. Nothing opens a socket, reads a
credential, or touches a wall clock of its own -- the clock is injected, so a
test can move time without sleeping.

These are the only :mod:`trading.ports` implementations that ship in Stage 1.
They exist so the safety chain can be exercised end to end against a venue that
misbehaves on purpose.
"""

from __future__ import annotations

from .broker import BrokerFailure, ScriptedAck, SimulatedBroker
from .market_data import StaticMarketData
from .quote_feed import InMemoryQuoteFeed

__all__ = [
    "BrokerFailure",
    "InMemoryQuoteFeed",
    "ScriptedAck",
    "SimulatedBroker",
    "StaticMarketData",
]
