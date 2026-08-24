"""Paper trading: an honest venue that fills against the real book.

Distinct from :mod:`trading.adapters.memory`, which holds a deliberately
*hostile* simulator for failure injection. This package holds the opposite: a
venue that always knows what happened and prices every fill from the same quote
feed the rest of the system reads. Both implement
:class:`~trading.ports.broker.BrokerPort` and both sit behind the one
:class:`~trading.core.gateway.ExecutionGateway`; neither is a second execution
path.

The separation is the point. A paper venue that can be told to lie is a paper
venue whose track record means nothing, and a simulator that cannot lie cannot
prove the UNKNOWN path. So :class:`~trading.adapters.paper.PaperBroker` has no
``script`` and no ``raise_on_next``: it never returns ``UNCERTAIN`` and never
raises, because a venue inside this process is never genuinely in doubt. When a
test needs doubt, it uses ``SimulatedBroker``, which exists for that.

What it models, and what it does not, is documented on
:class:`~trading.adapters.paper.PaperBroker` -- read that before treating a
paper result as a forecast of a live one.
"""

from __future__ import annotations

from .broker import PaperBroker, PaperReject

__all__ = ["PaperBroker", "PaperReject"]
