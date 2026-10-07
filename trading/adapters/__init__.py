"""Adapters: implementations of the ports in :mod:`trading.ports`.

This is the only layer allowed to know about infrastructure. In a later stage it
gains a PostgreSQL repository, a FastAPI inbound adapter, and a CoinSwitch REST
client. Today it holds two in-process venues, one driver, and a persistence
adapter layer, and no infrastructure at all:

* :mod:`trading.adapters.memory` -- a deliberately *hostile* venue and feed, for
  proving the system survives timeouts, lies, and unknown outcomes.
* :mod:`trading.adapters.paper` -- an honest venue that fills against the quote
  feed the rest of the system reads, for producing a track record.
* :mod:`trading.adapters.lifecycle` -- the periodic order poller. Not a port
  implementation: it is the first thing here that *calls* the kernel rather than
  being called by it, which is why it is an adapter and not a core component.
* :mod:`trading.adapters.reconciliation` -- the venue-wide sweep. It reads the
  whole book the venue holds, compares it against local state, and reports every
  disagreement. It observes; the gateway acts, and UNKNOWN is never resolved by
  it.
* :mod:`trading.adapters.sandbox` -- a broker adapter with its transport
  injected. This is the boundary a real exchange client will be built behind.
  Nothing in it names an endpoint, holds a credential, or retries an ambiguous
  placement.
* :mod:`trading.adapters.operations` -- operational status, fail-closed
  readiness, and safe startup. It reads state and composes it for an operator;
  it holds no execution surface and cannot authorize anything.
* :mod:`trading.adapters.persistence` -- in-memory implementations of the
  persistence ports, used by the kernel today but replaceable by a PostgreSQL
  adapter in a later Stage 2H.
* :mod:`trading.adapters.recovery` -- the restart-recovery scan. It *finds*
  ambiguous orders; resolving one remains an operator act.

Both venues implement :class:`~trading.ports.broker.BrokerPort` and both sit
behind the one :class:`~trading.core.gateway.ExecutionGateway`. Neither is a
second execution path, and a third broker adapter would not be either.

**No adapter in this repository performs network I/O.** There is no HTTP client,
no socket, and no credential use anywhere under this package. That is a standing
constraint, and ``tests/test_core_purity.py`` checks the import side of it.

The dependency arrow points one way: adapters import ports and core; nothing in
``trading.core`` or ``trading.ports`` may import this package.
"""

from __future__ import annotations

__all__: list[str] = []
