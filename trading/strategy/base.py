"""The strategy layer: proposals only.

A strategy's entire output is a list of :class:`~trading.core.orders.OrderIntent`
objects. An intent is inert -- it carries no permission, no token, and no route
to a venue. Turning one into an order is the execution gateway's job and nobody
else's (INVARIANT 3).

Two things make that separation more than a convention:

* :class:`MarketView` is the only input a strategy gets. It holds prices,
  positions, and equity: numbers, not collaborators. There is no broker, no
  order store, and no gateway reachable through it, so a strategy has nothing
  to call even if it wants to.
* :class:`StrategyRunner` inspects a strategy before running it and refuses one
  that has smuggled in an execution surface -- a :class:`BrokerPort`, an
  execution token, or any object exposing ``place_order``. The check is shallow
  by design (see :meth:`StrategyRunner._refuse_execution_surface`); it is a
  tripwire for accidents, not a sandbox against a determined author.

The runner deliberately does *not* hold a gateway. It returns intents to its
caller, who passes them to the gateway. Strategy code and execution code never
appear in the same call stack.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime
from typing import Mapping, Sequence

from ..core.audit import AuditCategory, AuditLog, AuditOutcome
from ..core.authz import Action, ExecutionToken, Principal, authorize
from ..core.clock import Clock
from ..core.errors import SafetyViolation
from ..core.money import Money, Price, Quantity
from ..core.orders import OrderIntent
from ..ports.broker import BrokerPort

__all__ = ["MarketView", "Strategy", "StrategyRunner", "refuse_execution_surface"]

#: Attribute names that would give a strategy a way to act rather than propose.
_EXECUTION_ATTRIBUTES: frozenset[str] = frozenset(
    {
        "place_order",
        "submit_order",
        "submit",
        "execute",
        "execute_order",
        "send_order",
    }
)


def refuse_execution_surface(strategy: object) -> None:
    """Reject a strategy holding a way to execute.

    Shallow on purpose: it walks the instance's own ``__dict__`` one level deep.
    Deeper reachability analysis would be arbitrarily expensive and still
    defeatable, and the structural guarantee does not rest here -- it rests on
    :class:`~trading.ports.broker.BrokerPort` requiring a token the strategy
    layer cannot mint. This check catches the honest mistake of passing a broker
    into a strategy constructor.

    Module-level so that every proposer-shaped runner shares one implementation.
    A second copy of a safety check is a second place for it to drift.
    """
    for attribute, value in vars(strategy).items():
        if isinstance(value, (BrokerPort, ExecutionToken)):
            raise SafetyViolation(
                f"strategy {type(strategy).__name__} holds "
                f"{type(value).__name__} in '{attribute}'; strategies may "
                "not hold an execution surface (INVARIANT 3)"
            )
        for forbidden in _EXECUTION_ATTRIBUTES:
            if callable(getattr(value, forbidden, None)):
                raise SafetyViolation(
                    f"strategy {type(strategy).__name__} holds an object "
                    f"exposing '{forbidden}' in '{attribute}'; strategies "
                    "propose intents only (INVARIANT 3)"
                )


@dataclass(frozen=True, slots=True)
class MarketView:
    """Everything a strategy is allowed to see.

    Immutable and inert: values only. A strategy that wants to know something
    not here must have it added deliberately, which is the point -- widening a
    strategy's view is a reviewable change, not an accident.
    """

    as_of: datetime
    equity: Money
    prices: Mapping[str, Price] = field(default_factory=dict)
    positions: Mapping[str, Quantity] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not isinstance(self.equity, Money):
            raise TypeError("equity must be Money")
        for symbol, price in self.prices.items():
            if not isinstance(price, Price):
                raise TypeError(f"price for {symbol} must be a Price")
        for symbol, quantity in self.positions.items():
            if not isinstance(quantity, Quantity):
                raise TypeError(f"position for {symbol} must be a Quantity")

    def price(self, symbol: str) -> Price | None:
        """Mark price, or ``None``. A strategy must handle absence."""
        return self.prices.get(symbol)

    def position(self, symbol: str, *, asset: str) -> Quantity:
        held = self.positions.get(symbol)
        return Quantity.zero(asset) if held is None else held

    def is_flat(self, symbol: str) -> bool:
        held = self.positions.get(symbol)
        return held is None or held.is_zero


class Strategy(ABC):
    """Base class for signal generation.

    Subclasses implement :meth:`propose` and nothing else that matters. They must
    not define any of :data:`_EXECUTION_ATTRIBUTES`; ``__init_subclass__``
    rejects that at class-definition time so the mistake cannot even be
    imported.
    """

    #: Human-readable identifier, recorded on every intent.
    name: str = "unnamed"

    def __init_subclass__(cls, **kwargs: object) -> None:
        super().__init_subclass__(**kwargs)
        offending = sorted(_EXECUTION_ATTRIBUTES & set(vars(cls)))
        if offending:
            raise SafetyViolation(
                f"strategy {cls.__name__} defines {', '.join(offending)}; "
                "strategies propose intents and never execute them (INVARIANT 3)"
            )

    @abstractmethod
    def propose(self, view: MarketView) -> Sequence[OrderIntent]:
        """Return intents implied by ``view``. Must have no side effects."""


class StrategyRunner:
    """Runs one strategy under an identity that may only propose.

    The runner is the strategy layer's audit boundary: every proposal is
    recorded before anything downstream sees it, so a later refusal by the
    gateway can be lined up against what was asked for.
    """

    def __init__(
        self,
        strategy: Strategy,
        *,
        identity: Principal,
        audit: AuditLog,
        clock: Clock,
    ) -> None:
        if not isinstance(strategy, Strategy):
            raise TypeError("strategy must be a Strategy instance")
        # Proposing is a permission, and it is the *only* one a runner needs.
        authorize(identity, Action.PROPOSE_ORDER)
        self._refuse_execution_surface(strategy)
        self._strategy = strategy
        self._identity = identity
        self._audit = audit
        self._clock = clock

    @staticmethod
    def _refuse_execution_surface(strategy: Strategy) -> None:
        """Delegate to :func:`refuse_execution_surface`.

        Kept as a method because the rule belongs to the runner conceptually and
        the existing tests name it here; the implementation is shared with
        :class:`~trading.strategy.signals.SignalRunner`.
        """
        refuse_execution_surface(strategy)

    @property
    def strategy_name(self) -> str:
        return self._strategy.name

    def propose(self, view: MarketView) -> list[OrderIntent]:
        """Ask the strategy for intents, validate them, and audit each one."""
        if not isinstance(view, MarketView):
            raise TypeError("view must be a MarketView")

        try:
            produced = self._strategy.propose(view)
        except Exception as exc:  # a broken strategy proposes nothing
            self._audit.record(
                AuditCategory.SIGNAL,
                "strategy.failed",
                outcome=AuditOutcome.ERROR,
                actor=self._identity.principal_id,
                details={"strategy": self._strategy.name, "error": str(exc)},
            )
            raise

        intents: list[OrderIntent] = []
        for item in produced:
            if not isinstance(item, OrderIntent):
                raise SafetyViolation(
                    f"strategy {self._strategy.name} returned "
                    f"{type(item).__name__}; only OrderIntent is accepted"
                )
            intents.append(item)

        for intent in intents:
            self._audit.record(
                AuditCategory.SIGNAL,
                "strategy.proposed",
                outcome=AuditOutcome.INFO,
                actor=self._identity.principal_id,
                details=intent.as_details(),
            )
        return intents
