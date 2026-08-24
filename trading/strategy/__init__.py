"""Strategies: the layer that decides *what* to trade, never *whether* it may.

Contains no execution code. Two forms coexist:

* :class:`~trading.strategy.base.Strategy` -- Stage 1. Receives a
  :class:`~trading.strategy.base.MarketView` of plain values and returns
  :class:`~trading.core.orders.OrderIntent` objects, which are inert until the
  execution gateway acts on them (INVARIANT 3).
* :class:`~trading.strategy.signals.SignalStrategy` -- Stage 2. Receives a
  :class:`~trading.strategy.context.MarketContext`, which carries *timestamped*
  market data and closed bars, and returns :class:`~trading.strategy.signals.Signal`
  objects. A signal has a direction, a stop, a target, and a rationale -- but
  **no quantity**, because sizing depends on equity and stop distance and is a
  risk decision rather than a strategy one.

The second form is the one to write new strategies against. The first is kept
because the execution chain's tests are written against it, and replacing a proven
interface to gain nothing is not an improvement.

:class:`~trading.strategy.sizing.SignalSizer` is where a signal acquires the
quantity it deliberately lacks. It lives here rather than in the kernel only
because the kernel may not import a ``Signal``; it decides nothing about
permission, and refuses to size a signal whose risk is undefined.

Neither form can execute. Both runners refuse a strategy holding a broker, a
token, or anything exposing ``place_order``, and neither runner holds a gateway --
strategy code and execution code never appear in the same call stack.
"""

from __future__ import annotations

from .base import MarketView, Strategy, StrategyRunner, refuse_execution_surface
from .context import MarketContext
from .examples import MovingAverageCrossover
from .signals import Signal, SignalDirection, SignalRunner, SignalStrategy
from .sizing import SignalSizer

__all__ = [
    "MarketContext",
    "MarketView",
    "MovingAverageCrossover",
    "Signal",
    "SignalDirection",
    "SignalRunner",
    "SignalSizer",
    "SignalStrategy",
    "Strategy",
    "StrategyRunner",
    "refuse_execution_surface",
]
