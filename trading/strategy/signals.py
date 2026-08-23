"""Signals: what a strategy decides, before anyone decides how much.

Stage 1's :class:`~trading.strategy.base.Strategy` returns
:class:`~trading.core.orders.OrderIntent` objects, which carry a quantity. That
puts sizing inside strategy code, and sizing is a *risk* decision -- it depends on
account equity, exposure limits, and the distance to the stop, none of which a
strategy should be reasoning about. A strategy that sizes its own orders is a
strategy that can quietly breach a limit before the risk engine ever sees it.

So Stage 2 splits the decision in two:

* A :class:`Signal` says *what* and *why*: direction, the price the decision was
  formed at, where the idea is wrong (the stop), where it is right (the target),
  how strongly it is held, and a rationale. It carries **no quantity**.
* Sizing turns a signal into an intent, using equity and the stop distance, under
  the risk engine's limits. That is a separate subsystem, and until it runs a
  signal cannot become an order.

Two properties are structural rather than advisory:

* **A signal cannot exist without a rationale.** ``rationale`` is required and
  must be non-empty. Advisory mode has to explain itself, and an explanation
  bolted on afterwards is a description of a decision rather than the reason for
  it.
* **A signal cannot exist without being internally coherent.** A long whose stop
  sits above its reference price is refused at construction. Accepting one would
  produce a negative risk-per-unit, and a sizer dividing by it would compute a
  position size that is nonsense in a plausible-looking way.

:class:`SignalStrategy` and :class:`SignalRunner` mirror ``Strategy`` and
``StrategyRunner`` exactly, including the execution-surface tripwire -- a signal
strategy is *less* privileged than an intent strategy, not more, so it inherits
every restriction and adds one: it cannot even name a quantity.

Stage 1's ``Strategy`` is untouched and still supported. The two coexist because
the intent-producing form is what the gateway's tests are written against, and
replacing it would mean re-proving the execution chain to gain nothing.
"""

from __future__ import annotations

import datetime as _dt
import decimal
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from decimal import Decimal
from enum import Enum
from typing import Mapping, Sequence

from ..core.audit import AuditCategory, AuditLog, AuditOutcome
from ..core.authz import Action, Principal, authorize
from ..core.clock import Clock
from ..core.errors import SafetyViolation
from ..core.money import FINANCIAL_CONTEXT, Currency, Price, to_decimal
from .base import _EXECUTION_ATTRIBUTES, refuse_execution_surface
from .context import MarketContext

__all__ = [
    "Signal",
    "SignalDirection",
    "SignalRunner",
    "SignalStrategy",
]


class SignalDirection(Enum):
    """What the strategy wants the position to become.

    Deliberately not a synonym for order side. ``EXIT`` becomes a buy or a sell
    depending on which way the position is currently facing, so mapping direction
    to side requires knowing the position -- see :meth:`Signal.side_for`. Folding
    the two concepts together is how an exit ends up doubling a position.
    """

    LONG = "long"
    SHORT = "short"
    EXIT = "exit"

    @property
    def is_entry(self) -> bool:
        return self in (SignalDirection.LONG, SignalDirection.SHORT)


@dataclass(frozen=True)
class Signal:
    """One strategy decision. Inert, self-explaining, and unsized.

    ``reference_price`` is the price the decision was formed at -- not a limit
    price and not a prediction. It exists so that ``stop_loss`` and
    ``take_profit`` can be checked for coherence at construction, and so an
    audit record shows what the strategy was looking at rather than what the
    market did afterwards.
    """

    strategy_name: str
    signal_id: str
    symbol: str
    direction: SignalDirection
    reference_price: Price
    as_of: _dt.datetime
    rationale: str
    #: How strongly the view is held, in ``(0, 1]``. A sizer may scale risk by
    #: this; nothing is obliged to. ``Decimal`` because it participates in
    #: position arithmetic, and floats are rejected system-wide (INVARIANT 8).
    conviction: Decimal = field(default_factory=lambda: Decimal("1"))
    #: Where the idea is wrong. Optional here, but required by the risk-based
    #: sizer -- a position sized without a stop has no defined loss.
    stop_loss: Price | None = None
    take_profit: Price | None = None
    #: Indicator readings that justify the signal. Strings only: this goes
    #: straight into the audit log, which redacts but does not serialise objects.
    evidence: Mapping[str, str] = field(default_factory=dict)
    #: Conditions the operator should know about. Surfaced by advisory mode
    #: verbatim; a signal may be actionable *and* carry warnings.
    warnings: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        for name in ("strategy_name", "signal_id", "symbol", "rationale"):
            value = getattr(self, name)
            if not isinstance(value, str) or not value.strip():
                raise ValueError(
                    f"{name} must be a non-empty string; a signal without a "
                    "rationale cannot be explained to an operator"
                )
        if not isinstance(self.direction, SignalDirection):
            raise TypeError("direction must be a SignalDirection")
        if not isinstance(self.reference_price, Price):
            raise TypeError("reference_price must be a Price")
        if not isinstance(self.as_of, _dt.datetime):
            raise TypeError("as_of must be a datetime")
        if self.as_of.tzinfo is None or self.as_of.utcoffset() is None:
            raise ValueError("as_of must be timezone-aware")

        conviction = to_decimal(self.conviction)
        if conviction <= 0 or conviction > 1:
            raise ValueError(
                f"conviction must lie in (0, 1], got {conviction}; a signal with "
                "zero conviction is not a signal"
            )
        object.__setattr__(self, "conviction", conviction)

        for name in ("stop_loss", "take_profit"):
            value = getattr(self, name)
            if value is None:
                continue
            if not isinstance(value, Price):
                raise TypeError(f"{name} must be a Price or None")
            if value.currency != self.reference_price.currency:
                raise ValueError(
                    f"{name} is in {value.currency.code} but reference_price is "
                    f"in {self.reference_price.currency.code}"
                )

        self._check_coherence()

        for key, value in self.evidence.items():
            if not isinstance(key, str) or not isinstance(value, str):
                raise TypeError(
                    "evidence must be a str->str mapping; it is written to the "
                    "audit log, which does not serialise arbitrary objects"
                )
        if not isinstance(self.warnings, tuple) or any(
            not isinstance(w, str) for w in self.warnings
        ):
            raise TypeError("warnings must be a tuple of strings")
        object.__setattr__(self, "evidence", dict(self.evidence))

    def _check_coherence(self) -> None:
        """Refuse a signal whose own levels contradict its direction.

        ``Price`` implements only ``<`` and ``>`` -- no ``<=`` -- so equality is
        spelled out. That is deliberate in ``money.py``: an accidental ``<=`` on
        a boundary is exactly the kind of off-by-one that silently permits a stop
        *at* the entry, which is a stop with zero risk and therefore an infinite
        position size.
        """
        reference, stop, target = self.reference_price, self.stop_loss, self.take_profit

        if self.direction is SignalDirection.EXIT:
            if stop is not None or target is not None:
                raise ValueError(
                    "an EXIT signal must not carry a stop or a target; it closes "
                    "a position rather than opening one with defined risk"
                )
            return

        if stop is not None:
            if self.direction is SignalDirection.LONG and not stop < reference:
                raise ValueError(
                    f"a LONG stop must sit below the reference price: stop "
                    f"{stop} is not below {reference}"
                )
            if self.direction is SignalDirection.SHORT and not stop > reference:
                raise ValueError(
                    f"a SHORT stop must sit above the reference price: stop "
                    f"{stop} is not above {reference}"
                )
        if target is not None:
            if self.direction is SignalDirection.LONG and not target > reference:
                raise ValueError(
                    f"a LONG target must sit above the reference price: target "
                    f"{target} is not above {reference}"
                )
            if self.direction is SignalDirection.SHORT and not target < reference:
                raise ValueError(
                    f"a SHORT target must sit below the reference price: target "
                    f"{target} is not below {reference}"
                )

    # -- derived quantities ----------------------------------------------

    @property
    def currency(self) -> Currency:
        return self.reference_price.currency

    @property
    def risk_per_unit(self) -> Decimal | None:
        """Distance from reference to stop, as a positive ``Decimal``.

        The number a risk-based sizer divides by. ``None`` when there is no stop,
        which is why the sizer must refuse a stopless signal rather than
        substitute a default -- a made-up stop is a made-up position size.
        """
        if self.stop_loss is None:
            return None
        with decimal.localcontext(FINANCIAL_CONTEXT):
            return abs(self.reference_price.amount - self.stop_loss.amount)

    @property
    def reward_per_unit(self) -> Decimal | None:
        if self.take_profit is None:
            return None
        with decimal.localcontext(FINANCIAL_CONTEXT):
            return abs(self.take_profit.amount - self.reference_price.amount)

    @property
    def risk_reward_ratio(self) -> Decimal | None:
        """Reward divided by risk, or ``None`` if either is absent.

        Not enforced: a low ratio is a judgement, not a safety violation, and the
        place to reject one is a strategy or an operator policy, not a value type.
        """
        risk, reward = self.risk_per_unit, self.reward_per_unit
        if risk is None or reward is None or risk == 0:
            return None
        with decimal.localcontext(FINANCIAL_CONTEXT):
            return reward / risk

    def side_for(self, position_is_long: bool | None = None) -> str:
        """The order side this signal implies, as ``"buy"`` or ``"sell"``.

        For an entry the answer is intrinsic. For an ``EXIT`` it depends on the
        position being closed, so ``position_is_long`` is required -- guessing
        would turn an exit into a doubling.
        """
        if self.direction is SignalDirection.LONG:
            return "buy"
        if self.direction is SignalDirection.SHORT:
            return "sell"
        if position_is_long is None:
            raise ValueError(
                "an EXIT signal needs to know which way the position faces; "
                "without it the closing side cannot be determined"
            )
        return "sell" if position_is_long else "buy"

    def age_seconds(self, now: _dt.datetime) -> float:
        return (now - self.as_of).total_seconds()

    def as_details(self) -> dict[str, object]:
        """Audit-safe representation."""
        return {
            "strategy": self.strategy_name,
            "signal_id": self.signal_id,
            "symbol": self.symbol,
            "direction": self.direction.value,
            "reference_price": str(self.reference_price.amount),
            "currency": self.currency.code,
            "conviction": str(self.conviction),
            "stop_loss": None if self.stop_loss is None else str(self.stop_loss.amount),
            "take_profit": (
                None if self.take_profit is None else str(self.take_profit.amount)
            ),
            "risk_per_unit": (
                None if self.risk_per_unit is None else str(self.risk_per_unit)
            ),
            "rationale": self.rationale,
            "evidence": dict(self.evidence),
            "warnings": list(self.warnings),
            "as_of": self.as_of.isoformat(),
        }


class SignalStrategy(ABC):
    """Base class for a strategy that emits :class:`Signal` objects.

    Subclasses implement :meth:`generate` and must define none of the
    execution-shaped attribute names, rejected at class-definition time so the
    mistake cannot be imported. They also must not define ``quantity`` or
    ``size`` methods: sizing is not theirs to do, and a subclass that tries is a
    subclass that has misunderstood the split.
    """

    name: str = "unnamed"

    #: Names that would mean the strategy is sizing rather than deciding.
    _SIZING_ATTRIBUTES: frozenset[str] = frozenset(
        {"size", "size_order", "quantity_for", "position_size"}
    )

    def __init_subclass__(cls, **kwargs: object) -> None:
        super().__init_subclass__(**kwargs)
        defined = set(vars(cls))
        offending = sorted(_EXECUTION_ATTRIBUTES & defined)
        if offending:
            raise SafetyViolation(
                f"signal strategy {cls.__name__} defines {', '.join(offending)}; "
                "strategies decide and never execute (INVARIANT 3)"
            )
        sizing = sorted(SignalStrategy._SIZING_ATTRIBUTES & defined)
        if sizing:
            raise SafetyViolation(
                f"signal strategy {cls.__name__} defines {', '.join(sizing)}; "
                "position size is a risk decision made from equity and stop "
                "distance, not a strategy decision"
            )

    @abstractmethod
    def generate(self, context: MarketContext) -> Sequence[Signal]:
        """Return the signals implied by ``context``. Must have no side effects.

        Returning an empty sequence is the normal case and is not a failure. A
        strategy that cannot see enough data must return nothing rather than
        guess.
        """


class SignalRunner:
    """Runs one signal strategy under an identity that may only propose.

    Holds no gateway and no broker, exactly like
    :class:`~trading.strategy.base.StrategyRunner`: signal code and execution
    code never appear in the same call stack. Every signal is audited before
    anything downstream sees it, so a later refusal can be lined up against what
    was decided and why.
    """

    def __init__(
        self,
        strategy: SignalStrategy,
        *,
        identity: Principal,
        audit: AuditLog,
        clock: Clock,
    ) -> None:
        if not isinstance(strategy, SignalStrategy):
            raise TypeError("strategy must be a SignalStrategy instance")
        authorize(identity, Action.PROPOSE_ORDER)
        refuse_execution_surface(strategy)
        self._strategy = strategy
        self._identity = identity
        self._audit = audit
        self._clock = clock

    @property
    def strategy_name(self) -> str:
        return self._strategy.name

    def generate(self, context: MarketContext) -> list[Signal]:
        """Ask the strategy for signals, validate them, and audit each one."""
        if not isinstance(context, MarketContext):
            raise TypeError("context must be a MarketContext")

        try:
            produced = self._strategy.generate(context)
        except Exception as exc:  # a broken strategy signals nothing
            self._audit.record(
                AuditCategory.SIGNAL,
                "strategy.failed",
                outcome=AuditOutcome.ERROR,
                actor=self._identity.principal_id,
                details={"strategy": self._strategy.name, "error": str(exc)},
            )
            raise

        signals: list[Signal] = []
        for item in produced:
            if not isinstance(item, Signal):
                raise SafetyViolation(
                    f"signal strategy {self._strategy.name} returned "
                    f"{type(item).__name__}; only Signal is accepted"
                )
            signals.append(item)

        for signal in signals:
            self._audit.record(
                AuditCategory.SIGNAL,
                "strategy.signalled",
                outcome=AuditOutcome.INFO,
                actor=self._identity.principal_id,
                details=signal.as_details(),
            )
        return signals
