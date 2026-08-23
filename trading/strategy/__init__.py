"""Strategies: the layer that decides *what* to trade, never *whether* it may.

Contains no execution code. See :mod:`trading.strategy.base` for the contract;
the short version is that a strategy receives a :class:`~trading.strategy.base.MarketView`
of plain values and returns :class:`~trading.core.orders.OrderIntent` objects,
which are inert until the execution gateway acts on them (INVARIANT 3).

Stage 1 ships the base classes only. Actual strategies arrive in a later stage,
once the safety core they run behind has been exercised.
"""

from __future__ import annotations

from .base import MarketView, Strategy, StrategyRunner

__all__ = ["MarketView", "Strategy", "StrategyRunner"]
