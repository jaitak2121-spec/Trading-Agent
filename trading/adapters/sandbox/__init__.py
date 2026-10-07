"""The sandbox broker boundary: translation, with the network injected.

This package exists so the *meaning* of a venue's answers can be written and
tested before there is anything to talk to. It holds no endpoint, no credential,
and no HTTP client -- a :class:`~trading.adapters.sandbox.broker.SandboxTransport`
is handed in, and :class:`~trading.adapters.sandbox.broker.SandboxBroker` decides
what a response means.

The rule it exists to enforce: **an answer that cannot be fully read is
uncertain**. A timeout, a disconnect, an unparseable body, an unrecognised status
word, an overfill -- each becomes ``UNCERTAIN`` (which the gateway turns into an
UNKNOWN order) rather than a guess, and nothing is ever resent automatically.

A real exchange adapter is built by implementing ``SandboxTransport`` against
that exchange's documented sandbox contract. Until a verified sandbox
specification and credentials exist, this stops at the injected boundary -- see
``docs/SAFETY.md``.
"""

from __future__ import annotations

from .broker import (
    SandboxBroker,
    SandboxRequest,
    SandboxResponse,
    SandboxTransport,
    TransportDisconnected,
    TransportError,
    TransportMalformed,
    TransportRateLimited,
    TransportTimeout,
)

__all__ = [
    "SandboxBroker",
    "SandboxRequest",
    "SandboxResponse",
    "SandboxTransport",
    "TransportDisconnected",
    "TransportError",
    "TransportMalformed",
    "TransportRateLimited",
    "TransportTimeout",
]
