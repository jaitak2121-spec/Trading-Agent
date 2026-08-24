"""Adapters: implementations of the ports in :mod:`trading.ports`.

This is the only layer allowed to know about infrastructure. In a later stage it
gains a PostgreSQL repository, a FastAPI inbound adapter, and a CoinSwitch REST
client. Today it holds two in-process subpackages and no infrastructure at all:

* :mod:`trading.adapters.memory` -- a deliberately *hostile* venue and feed, for
  proving the system survives timeouts, lies, and unknown outcomes.
* :mod:`trading.adapters.paper` -- an honest venue that fills against the quote
  feed the rest of the system reads, for producing a track record.

Both implement :class:`~trading.ports.broker.BrokerPort` and both sit behind the
one :class:`~trading.core.gateway.ExecutionGateway`. Neither is a second
execution path, and a third broker adapter would not be either.

**No adapter in this repository performs network I/O.** There is no HTTP client,
no socket, and no credential use anywhere under this package. That is a standing
constraint, and ``tests/test_core_purity.py`` checks the import side of it.

The dependency arrow points one way: adapters import ports and core; nothing in
``trading.core`` or ``trading.ports`` may import this package.
"""

from __future__ import annotations

__all__: list[str] = []
