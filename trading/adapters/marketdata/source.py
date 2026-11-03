"""The injected inbound market-data boundary.

Mirrors :mod:`trading.adapters.sandbox.broker`'s ``SandboxTransport``: the
transport is handed in, holds no endpoint and no credential, and performs no
network I/O in this repository. A real feed adapter implements
:class:`MarketDataSource` over a websocket or REST stream; until a verified
vendor contract exists, the only implementations are in-process and rule-driven
(the tests build them). That is a deliberate stop boundary, exactly like the
sandbox broker's.

Pull-based on purpose. :meth:`MarketDataSource.poll` returns the raw messages
available since the last call, so a :class:`~trading.adapters.marketdata.ingestor.MarketDataIngestor`
driven by a :class:`~trading.core.clock.ManualClock` is fully deterministic --
the same philosophy as ``LifecyclePoller.poll_once``.

A connection problem is *raised*, not returned. :class:`FeedDisconnected` and
:class:`FeedTimeout` describe a link that may have dropped messages, and the
honest reading of either at the ingestor is "the data is no longer
trustworthy" -- the same shape as ``SandboxTransport`` raising to mean "the
outcome is uncertain".
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Mapping, Sequence

__all__ = [
    "FeedDisconnected",
    "FeedError",
    "FeedTimeout",
    "FeedUnavailable",
    "MarketDataSource",
    "RawMessage",
]

#: One raw inbound message, already decoded off the wire into a mapping (what a
#: JSON feed yields after parsing). The field names are the venue's, not ours --
#: :class:`~trading.adapters.marketdata.normalizer.QuoteNormalizer` maps them.
#: Deliberately a plain ``Mapping`` rather than a typed object: the whole point
#: of the boundary is that nothing arriving through it is trusted to be
#: well-shaped.
RawMessage = Mapping[str, object]


class FeedError(Exception):
    """The source could not deliver cleanly. The ingestor turns this into an
    interruption, never a crash."""


class FeedDisconnected(FeedError):
    """The link dropped. Messages may have been lost, so the data is stale until
    a fresh message arrives."""


class FeedTimeout(FeedError):
    """No data arrived in time. Treated like a disconnect: the data goes stale."""


class FeedUnavailable(FeedError):
    """The source cannot be reached at all (e.g. a (re)subscribe failed)."""


class MarketDataSource(ABC):
    """Inbound boundary to a market-data stream. Observation-only.

    Implementations live outside the kernel. The one shipped form is the
    in-process test double; a network implementation is deferred behind a
    verified vendor contract.
    """

    @abstractmethod
    def poll(self) -> Sequence[RawMessage]:
        """Raw messages available since the last call (possibly empty).

        Raise :class:`FeedDisconnected`, :class:`FeedTimeout`, or
        :class:`FeedUnavailable` to signal a connection problem. Any other
        exception the ingestor treats as a disconnect as well, because an
        unclassified failure says nothing about whether the stream is intact --
        the same fail-safe default the sandbox broker applies to an unclassified
        transport error.
        """
