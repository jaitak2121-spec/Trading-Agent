"""Advisory mode: the read-only half of the platform.

One layer, one rule: nothing here may reach execution. ``trading.advisory`` is
kept separate from ``trading.strategy`` and from the kernel so that "advisory
mode cannot place an order" is a property of the import graph rather than a
promise in a docstring -- ``test_core_purity.py`` refuses an import of
:mod:`trading.core.gateway` or :mod:`trading.ports.broker` from this layer.

:class:`~trading.advisory.advisor.Advisor` is the entry point. It takes signals
and a market context and returns :class:`~trading.advisory.advisor.Advice`: a
suggested side and size, the risk to the stop, the reasoning, and every reason
the idea might not be worth acting on. Position monitoring needs nothing new --
:meth:`trading.core.portfolio.Portfolio.open_positions` is already the list an
advisory report shows.

Advice is not permission. The advisor never mints a risk approval, so no approval
exists in the system that an execution attempt did not create, and every limit is
checked again by the gateway if an intent is ever built from an advice.
"""

from __future__ import annotations

from .advisor import (
    DEFAULT_DRIFT_WARNING_FRACTION,
    Advice,
    Advisor,
    Block,
    BlockReason,
)

__all__ = [
    "DEFAULT_DRIFT_WARNING_FRACTION",
    "Advice",
    "Advisor",
    "Block",
    "BlockReason",
]
