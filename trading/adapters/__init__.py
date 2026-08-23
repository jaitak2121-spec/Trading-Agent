"""Adapters: implementations of the ports in :mod:`trading.ports`.

This is the only layer allowed to know about infrastructure. In a later stage it
gains a PostgreSQL repository, a FastAPI inbound adapter, and a CoinSwitch REST
client; in Stage 1 it holds exactly one subpackage, :mod:`trading.adapters.memory`,
which simulates a venue in process.

**No adapter in this repository performs network I/O.** There is no HTTP client,
no socket, and no credential use anywhere under this package. That is a Stage 1
constraint, and ``tests/test_core_purity.py`` checks the import side of it.

The dependency arrow points one way: adapters import ports and core; nothing in
``trading.core`` or ``trading.ports`` may import this package.
"""

from __future__ import annotations

__all__: list[str] = []
