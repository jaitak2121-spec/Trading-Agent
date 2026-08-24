"""Advisory mode: analysis, a size, and an explanation -- never an order.

A :class:`~trading.strategy.signals.Signal` says what and why.
:class:`~trading.strategy.sizing.SignalSizer` says how much. Neither says whether
the idea still holds against the market as it is *now*, and neither produces
something an operator can read. This module does both, and it is the surface
advisory mode is meant to be used through.

**Why this is a separate layer.** The one thing advisory mode must not be able to
do is execute, and "does not execute" is worth very little as a convention. So
``trading.advisory`` is its own layer with its own import rule: it may not import
:mod:`trading.core.gateway` or :mod:`trading.ports.broker`, checked mechanically
in ``test_core_purity.py``. There is no gateway to reach, no broker to call, and
no token to present. Three further guards sit on top:

* the identity advisory mode runs under must hold ``PROPOSE_ORDER`` and must
  *not* hold ``EXECUTE_ORDER`` -- an identity that could act on its own advice
  would erase the separation the layer exists to create;
* :func:`~trading.strategy.base.refuse_execution_surface` runs over the advisor's
  own attributes, so a broker passed in by mistake is rejected at construction;
* an :class:`Advice` holds no :class:`~trading.core.orders.OrderIntent`. Nothing
  in advisory output is the shape that anything downstream submits, so advice
  cannot be forwarded into execution -- it has to be deliberately turned into an
  intent by code that is not here.

**Advice is not permission.** The advisor never calls
:meth:`~trading.core.risk.RiskEngine.approve`, so no approval exists anywhere in
the system that an actual execution attempt did not create. Every limit is
re-checked by the gateway against the intent that eventually results; a
tradeable-looking :class:`Advice` is a proposal and nothing more.

**What advisory mode adds over sizing.** A size computed from a signal is only
as good as the signal's own reference price, and the market moves. So the
advisor compares the signal against live data and refuses -- with a stated
reason -- when the comparison invalidates it:

* market data missing or stale. Execution refuses on staleness; advisory mode has
  to *say* it, which is why :attr:`Advice.freshness` is carried even on a refusal;
* a signal from the future, or older than a threshold the operator set;
* a stop the market has already reached. This is the sharp one: sizing such a
  signal yields a "risk 500 USD" figure that is a fiction, because the loss is
  already incurred at entry;
* no computable size -- a stopless signal, an unknowable loss budget, an
  exhausted one. Those refusals come from ``SignalSizer`` and are passed through
  verbatim rather than re-derived.

A *reached target* is only a warning. The reward the signal claimed is gone, but
the loss is still bounded by the stop and the size is still real, so refusing
would be a judgement about trade quality rather than a safety matter -- the same
line :attr:`Signal.risk_reward_ratio` already draws.

**A blocked advice carries no size.** Enforced in :meth:`Advice.__post_init__`,
because a refusal that still shows a quantity is a refusal somebody will read the
quantity out of. For the same reason a sized advice must report exactly the
sizer's quantity: if the two could differ, the number an operator sees would not
be the number any limit was checked against.
"""

from __future__ import annotations

import decimal
from dataclasses import dataclass
from decimal import Decimal
from enum import Enum
from typing import Final, Mapping, Sequence

from ..core.audit import AuditCategory, AuditLog, AuditOutcome
from ..core.authz import Action, Principal, authorize, is_authorized
from ..core.errors import SafetyViolation
from ..core.marketdata import Freshness
from ..core.money import FINANCIAL_CONTEXT, Money, Price, Quantity, to_decimal
from ..core.sizing import SizingResult
from ..strategy.base import refuse_execution_surface
from ..strategy.context import MarketContext
from ..strategy.signals import Signal, SignalDirection
from ..strategy.sizing import SignalSizer

__all__ = [
    "BlockReason",
    "Block",
    "Advice",
    "Advisor",
    "DEFAULT_DRIFT_WARNING_FRACTION",
]

#: Price drift is measured in the trade's own risk units -- the distance to the
#: stop -- rather than in basis points. A quarter of the way to the stop is a
#: materially different trade from the one the signal described, and expressing
#: the threshold this way means it scales with each idea's risk instead of
#: applying one invented percentage to instruments that do not resemble
#: each other.
DEFAULT_DRIFT_WARNING_FRACTION: Final = Decimal("0.25")


class BlockReason(Enum):
    """Why an advice is not actionable. Machine-readable, so a report can group."""

    MISSING_MARKET_DATA = "missing_market_data"
    STALE_MARKET_DATA = "stale_market_data"
    SIGNAL_FROM_THE_FUTURE = "signal_from_the_future"
    SIGNAL_TOO_OLD = "signal_too_old"
    STOP_ALREADY_BREACHED = "stop_already_breached"
    NO_SIZE = "no_size"
    NOTHING_TO_CLOSE = "nothing_to_close"


@dataclass(frozen=True, slots=True)
class Block:
    """One reason, plus the specifics an operator needs to act on it.

    Shaped like :class:`~trading.core.risk.LimitBreach` on purpose: a code for
    machines and a sentence for people, so neither audience is served by
    parsing the other's format.
    """

    reason: BlockReason
    detail: str

    def __post_init__(self) -> None:
        if not isinstance(self.reason, BlockReason):
            raise TypeError("reason must be a BlockReason")
        if not isinstance(self.detail, str) or not self.detail.strip():
            raise ValueError("detail must be a non-empty string")

    def as_details(self) -> dict[str, str]:
        return {"reason": self.reason.value, "detail": self.detail}

    def __str__(self) -> str:
        return f"{self.reason.value}: {self.detail}"


@dataclass(frozen=True)
class Advice:
    """What advisory mode has to say about one signal.

    Carries the signal itself rather than a copy of its fields, so the rationale
    and evidence an operator reads are the ones the strategy produced. Carries no
    ``OrderIntent``: advice is read, not submitted.
    """

    signal: Signal
    asset: str
    #: The verdict on the market data, reported even when it is the reason for a
    #: refusal -- advisory mode says why, where execution merely declines.
    freshness: Freshness
    #: The live mid, or ``None`` when no usable quote existed.
    live_price: Price | None
    #: Age of the signal at the moment the context was taken. Negative means the
    #: signal is stamped in the future.
    signal_age_seconds: float
    #: The suggested quantity, always positive-or-zero. Zero whenever blocked.
    quantity: Quantity
    #: Cash value of :attr:`quantity`, or ``None`` when there is no size.
    notional: Money | None
    #: ``"buy"`` or ``"sell"``. Resolved against the held position for an exit.
    side: str
    #: The sizer's own reasoning, for an entry. ``None`` for an exit, which is
    #: sized from the position held and is not a risk-budget question, and
    #: ``None`` when a refusal landed before sizing was reached.
    sizing: SizingResult | None
    blocks: tuple[Block, ...] = ()
    #: The signal's own warnings verbatim, plus anything advisory mode noticed.
    warnings: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.signal, Signal):
            raise TypeError("signal must be a Signal")
        if not isinstance(self.quantity, Quantity):
            raise TypeError("quantity must be a Quantity")
        if self.quantity.amount < 0:
            raise ValueError(
                "advice quantity must not be negative; a direction is carried by "
                "side, and a signed quantity here would be a second, "
                "contradictable statement of it"
            )
        object.__setattr__(self, "blocks", tuple(self.blocks))
        object.__setattr__(self, "warnings", tuple(self.warnings))
        for block in self.blocks:
            if not isinstance(block, Block):
                raise TypeError("blocks must contain Block objects")
        if self.blocks and not self.quantity.is_zero:
            raise SafetyViolation(
                f"advice for {self.signal.signal_id} is blocked by "
                f"{[b.reason.value for b in self.blocks]} yet reports "
                f"{self.quantity}; a refusal that still shows a size is a refusal "
                "somebody reads the size out of"
            )
        if self.sizing is not None and self.quantity != self.sizing.quantity:
            raise SafetyViolation(
                f"advice for {self.signal.signal_id} reports {self.quantity} but "
                f"the sizer produced {self.sizing.quantity}; the quantity an "
                "operator sees must be the one the limits were applied to"
            )

    # -- verdict -----------------------------------------------------------

    @property
    def is_actionable(self) -> bool:
        """Whether there is something to do. Not whether it is permitted.

        Both halves are required rather than implied: no blocks *and* a positive
        size. A zero quantity with an empty block list would be a bug, and the
        answer to a bug here should be "do nothing".
        """
        return not self.blocks and self.quantity.is_positive

    @property
    def symbol(self) -> str:
        return self.signal.symbol

    @property
    def block_reasons(self) -> tuple[BlockReason, ...]:
        return tuple(block.reason for block in self.blocks)

    # -- rendering ---------------------------------------------------------

    def as_details(self) -> dict[str, object]:
        """Audit-safe representation. Nested, like ``Signal.as_details``."""
        return {
            "signal": self.signal.as_details(),
            "asset": self.asset,
            "freshness": self.freshness.value,
            "live_price": None if self.live_price is None else str(self.live_price.amount),
            "signal_age_seconds": f"{self.signal_age_seconds:.3f}",
            "quantity": str(self.quantity.amount),
            "notional": None if self.notional is None else str(self.notional.amount),
            "side": self.side,
            "actionable": self.is_actionable,
            "sizing": None if self.sizing is None else self.sizing.as_details(),
            "blocks": [block.as_details() for block in self.blocks],
            "warnings": list(self.warnings),
        }

    def explain(self) -> str:
        """The advice as an operator reads it: what, how much, why, and caveats."""
        signal = self.signal
        head = f"{signal.symbol} {signal.direction.value}"
        if self.is_actionable:
            head += f" -- {self.side} {self.quantity}"
            if self.notional is not None:
                head += f" ({self.notional})"
        else:
            head += " -- NOT ACTIONABLE"
        lines = [head]

        levels = [f"reference {signal.reference_price}"]
        if signal.stop_loss is not None:
            levels.append(f"stop {signal.stop_loss}")
        if signal.take_profit is not None:
            levels.append(f"target {signal.take_profit}")
        if signal.risk_reward_ratio is not None:
            levels.append(f"reward:risk {signal.risk_reward_ratio:.2f}")
        lines.append("  " + ", ".join(levels))

        live = "no usable quote" if self.live_price is None else str(self.live_price)
        lines.append(
            f"  market {live} ({self.freshness.value}), "
            f"signal {self.signal_age_seconds:.1f}s old"
        )

        if self.sizing is not None and self.sizing.is_tradeable:
            lines.append(
                f"  risking {self.sizing.max_loss_at_stop} to the stop, "
                f"bound by {self.sizing.binding_constraint.value}"
            )
        lines.append(f"  because: {signal.rationale}")
        for key, value in sorted(signal.evidence.items()):
            lines.append(f"    {key} = {value}")
        for block in self.blocks:
            lines.append(f"  blocked -- {block}")
        for warning in self.warnings:
            lines.append(f"  warning -- {warning}")
        if self.is_actionable:
            lines.append(
                "  advice only: no order exists, and every limit is checked "
                "again if one is ever submitted"
            )
        return "\n".join(lines)

    def __str__(self) -> str:
        return self.explain()


class Advisor:
    """Turns signals into advice. Holds nothing that can execute.

    Stateless between calls: everything it needs comes from the
    :class:`~trading.strategy.context.MarketContext` it is handed, so two callers
    advising on different snapshots cannot contaminate each other.
    """

    def __init__(
        self,
        sizer: SignalSizer,
        *,
        identity: Principal,
        audit: AuditLog,
        max_signal_age_seconds: float | None = None,
        drift_warning_fraction: object = DEFAULT_DRIFT_WARNING_FRACTION,
    ) -> None:
        """Wire an advisor.

        ``max_signal_age_seconds`` defaults to ``None``, meaning the age is
        reported on every advice but never blocks. There is no defensible
        universal figure -- a scalping signal is stale in seconds and a
        weekly-trend signal is not -- and inventing one would either block
        everything or block nothing while looking like a control. A signal
        stamped in the *future* is refused regardless: that is incoherent rather
        than a matter of policy, and the tolerance it is judged against is the
        context's own ``future_tolerance_seconds`` -- one configured allowance for
        clock skew rather than a second one here that could disagree with it.
        """
        if not isinstance(sizer, SignalSizer):
            raise TypeError("sizer must be a SignalSizer")
        if not isinstance(audit, AuditLog):
            raise TypeError("audit must be an AuditLog")

        # The identity must be able to propose and unable to execute. Under the
        # present permission matrix only Role.STRATEGY holds PROPOSE_ORDER and
        # only Role.EXECUTION_GATEWAY holds EXECUTE_ORDER, so the second check
        # cannot fire today -- it is a tripwire for a matrix that grows a role
        # holding both, which is the change that would make this layer's
        # separation cosmetic without touching a line of it.
        authorize(identity, Action.PROPOSE_ORDER)
        if is_authorized(identity, Action.EXECUTE_ORDER):
            raise SafetyViolation(
                f"{identity} may execute orders and so cannot be the identity "
                "advisory mode runs under; advice that its own author could act "
                "on is not advice (INVARIANT 3)"
            )

        if max_signal_age_seconds is not None:
            if isinstance(max_signal_age_seconds, bool) or not isinstance(
                max_signal_age_seconds, (int, float)
            ):
                raise TypeError("max_signal_age_seconds must be a number or None")
            if max_signal_age_seconds <= 0:
                raise ValueError(
                    "max_signal_age_seconds must be positive; zero would refuse "
                    "every signal including one formed this instant"
                )
        fraction = to_decimal(drift_warning_fraction)
        if fraction <= 0:
            raise ValueError("drift_warning_fraction must be positive")

        self._sizer = sizer
        self._identity = identity
        self._audit = audit
        self._max_signal_age = max_signal_age_seconds
        self._drift_fraction = fraction
        # On top of the layer rule: catches a broker or a token handed in by
        # mistake, one level deep. The same check the strategy runners use, so
        # there is one implementation of it rather than two that can drift.
        refuse_execution_surface(self)

    @property
    def identity(self) -> Principal:
        return self._identity

    @property
    def max_signal_age_seconds(self) -> float | None:
        return self._max_signal_age

    @property
    def drift_warning_fraction(self) -> Decimal:
        return self._drift_fraction

    # -- the workflow ------------------------------------------------------

    def advise(
        self,
        context: MarketContext,
        signals: Sequence[Signal],
        *,
        assets: Mapping[str, str],
    ) -> tuple[Advice, ...]:
        """Advise on every signal, in order.

        ``assets`` maps symbol to the asset its quantity is denominated in. It is
        required, and a symbol missing from it raises: no rule in this system
        splits a symbol into a base asset, and a guess would mislabel the
        quantity of every instrument whose ticker does not follow the guessed
        convention. Same reasoning as ``SignalSizer.size``.

        Every advice is audited, but only after the whole batch is built -- a
        malformed signal halfway through must not leave the first half in the log
        looking like a completed report.
        """
        if not isinstance(context, MarketContext):
            raise TypeError("context must be a MarketContext")
        if isinstance(signals, (str, bytes)) or not isinstance(signals, Sequence):
            raise TypeError("signals must be a sequence of Signal objects")

        advices = tuple(
            self._advise_one(context, self._require_signal(item), assets)
            for item in signals
        )
        for advice in advices:
            self._record(advice)
        return advices

    def advise_one(
        self,
        context: MarketContext,
        signal: Signal,
        *,
        asset: str,
    ) -> Advice:
        """Advise on a single signal. Convenience over :meth:`advise`."""
        return self.advise(context, [signal], assets={signal.symbol: asset})[0]

    @staticmethod
    def _require_signal(item: object) -> Signal:
        if not isinstance(item, Signal):
            raise TypeError(
                f"signals must contain Signal objects, got {type(item).__name__}"
            )
        return item

    def _record(self, advice: Advice) -> None:
        """Audit one advice.

        The outcome is always ``INFO``. ``ALLOWED`` would read as an approval and
        ``REFUSED`` as a risk refusal, and advice is neither -- it decides
        nothing about permission. The action name distinguishes the two shapes so
        a reader can filter without inferring from the outcome.
        """
        self._audit.record(
            AuditCategory.ADVICE,
            "advice.issued" if advice.is_actionable else "advice.declined",
            outcome=AuditOutcome.INFO,
            actor=self._identity.principal_id,
            details=advice.as_details(),
        )

    # -- per-signal --------------------------------------------------------

    def _advise_one(
        self,
        context: MarketContext,
        signal: Signal,
        assets: Mapping[str, str],
    ) -> Advice:
        asset = assets.get(signal.symbol)
        if not isinstance(asset, str) or not asset.strip():
            raise ValueError(
                f"no asset given for {signal.symbol}; advisory mode will not "
                "guess one from the ticker, because a wrong guess mislabels "
                "every quantity it produces"
            )

        freshness = context.freshness(signal.symbol)
        live = context.price(signal.symbol)
        age = signal.age_seconds(context.as_of)
        warnings = list(signal.warnings)
        blocks: list[Block] = []

        if age < -context.policy.future_tolerance_seconds:
            blocks.append(
                Block(
                    BlockReason.SIGNAL_FROM_THE_FUTURE,
                    f"the signal is stamped {-age:.1f}s ahead of the market data "
                    f"({signal.as_of.isoformat()} against {context.as_of.isoformat()}); "
                    "a decision cannot have been formed from data that does not "
                    "exist yet, so the two are not describing the same market",
                )
            )
        elif self._max_signal_age is not None and age > self._max_signal_age:
            blocks.append(
                Block(
                    BlockReason.SIGNAL_TOO_OLD,
                    f"the signal is {age:.1f}s old against a limit of "
                    f"{self._max_signal_age:.1f}s",
                )
            )

        if freshness is Freshness.MISSING:
            blocks.append(
                Block(
                    BlockReason.MISSING_MARKET_DATA,
                    f"no quote for {signal.symbol}, so nothing can be said about "
                    "whether this idea still holds",
                )
            )
        elif not freshness.is_usable:
            blocks.append(
                Block(
                    BlockReason.STALE_MARKET_DATA,
                    f"the quote for {signal.symbol} is stale under the context's "
                    f"policy (max {context.policy.max_age_seconds:.1f}s); a size "
                    "computed against it would look no different from a good one",
                )
            )

        if blocks:
            # Everything below compares against the live price or sizes off it.
            # Neither means anything yet, so stop here rather than produce
            # numbers that would have to be discarded.
            return self._blocked(signal, asset, freshness, live, age, blocks, warnings)

        assert live is not None  # a usable freshness implies a quote
        if live.currency != signal.currency:
            raise ValueError(
                f"{signal.signal_id} prices {signal.symbol} in "
                f"{signal.currency.code} but the market quotes it in "
                f"{live.currency.code}; the signal and the feed disagree about "
                "what is being priced, so no comparison between them is meaningful"
            )

        if signal.direction is SignalDirection.EXIT:
            return self._advise_exit(context, signal, asset, freshness, live, age, warnings)
        return self._advise_entry(context, signal, asset, freshness, live, age, warnings)

    def _blocked(
        self,
        signal: Signal,
        asset: str,
        freshness: Freshness,
        live: Price | None,
        age: float,
        blocks: list[Block],
        warnings: list[str],
        sizing: SizingResult | None = None,
    ) -> Advice:
        return Advice(
            signal=signal,
            asset=asset,
            freshness=freshness,
            live_price=live,
            signal_age_seconds=age,
            quantity=Quantity.zero(asset),
            notional=None,
            side=self._side_or_unknown(signal),
            sizing=sizing,
            blocks=tuple(blocks),
            warnings=tuple(warnings),
        )

    @staticmethod
    def _side_or_unknown(signal: Signal) -> str:
        """The implied side, or ``"none"`` when it cannot be determined.

        An ``EXIT`` needs the position to know which way it closes, and a blocked
        advice may have been blocked before the position was consulted. Naming
        the absence beats guessing: a guessed side on an exit is how a close
        becomes a double.
        """
        if signal.direction.is_entry:
            return signal.side_for()
        return "none"

    def _advise_exit(
        self,
        context: MarketContext,
        signal: Signal,
        asset: str,
        freshness: Freshness,
        live: Price,
        age: float,
        warnings: list[str],
    ) -> Advice:
        """An exit is sized from the position held, not from a risk budget.

        ``SignalSizer`` raises on an ``EXIT`` precisely so an exit cannot be
        risk-sized by accident. The quantity here is the position, and its
        absolute value: the closing direction is carried by ``side``.
        """
        held = context.position(signal.symbol, asset=asset)
        if held.is_zero:
            return self._blocked(
                signal,
                asset,
                freshness,
                live,
                age,
                [
                    Block(
                        BlockReason.NOTHING_TO_CLOSE,
                        f"the exit asks to close {signal.symbol} but the position "
                        "is flat; acting on it would open a new one in the "
                        "opposite direction",
                    )
                ],
                warnings,
            )
        quantity = abs(held)
        return Advice(
            signal=signal,
            asset=asset,
            freshness=freshness,
            live_price=live,
            signal_age_seconds=age,
            quantity=quantity,
            notional=live.notional(quantity),
            side=signal.side_for(held.is_positive),
            sizing=None,
            warnings=tuple(warnings),
        )

    def _advise_entry(
        self,
        context: MarketContext,
        signal: Signal,
        asset: str,
        freshness: Freshness,
        live: Price,
        age: float,
        warnings: list[str],
    ) -> Advice:
        stop = signal.stop_loss
        if stop is not None and self._stop_is_breached(signal, live, stop):
            return self._blocked(
                signal,
                asset,
                freshness,
                live,
                age,
                [
                    Block(
                        BlockReason.STOP_ALREADY_BREACHED,
                        f"the market is at {live} and the stop is {stop}, so this "
                        f"{signal.direction.value} would open already stopped out; "
                        "the loss it claims to risk is loss it has already taken",
                    )
                ],
                warnings,
            )

        sizing = self._sizer.size(signal, equity=context.equity, asset=asset)
        if not sizing.is_tradeable:
            # The sizer's own words, not a re-derivation of them: a stopless
            # signal and an unknowable loss budget are its refusals to explain.
            return self._blocked(
                signal,
                asset,
                freshness,
                live,
                age,
                [
                    Block(
                        BlockReason.NO_SIZE,
                        f"{sizing.binding_constraint.value} -- {sizing.reason}",
                    )
                ],
                warnings,
                sizing=sizing,
            )

        warnings.extend(self._market_warnings(signal, live))
        return Advice(
            signal=signal,
            asset=asset,
            freshness=freshness,
            live_price=live,
            signal_age_seconds=age,
            quantity=sizing.quantity,
            notional=sizing.notional,
            side=signal.side_for(),
            sizing=sizing,
            warnings=tuple(warnings),
        )

    # -- market comparisons ------------------------------------------------

    @staticmethod
    def _stop_is_breached(signal: Signal, live: Price, stop: Price) -> bool:
        """Whether the market has already reached the stop.

        Equality counts as breached. ``Price`` offers only ``<`` and ``>`` --
        deliberately, so that a boundary case has to be spelled out -- and a stop
        sitting exactly at the market is a stop that triggers, not one with a
        hair of room left.
        """
        if signal.direction is SignalDirection.LONG:
            return not live > stop
        return not live < stop

    @staticmethod
    def _target_is_reached(signal: Signal, live: Price, target: Price) -> bool:
        if signal.direction is SignalDirection.LONG:
            return not live < target
        return not live > target

    def _market_warnings(self, signal: Signal, live: Price) -> list[str]:
        """Things worth saying about an otherwise actionable idea."""
        notes: list[str] = []
        risk = signal.risk_per_unit
        if risk is not None and risk > 0:
            with decimal.localcontext(FINANCIAL_CONTEXT):
                drift = abs(live.amount - signal.reference_price.amount)
                fraction = drift / risk
            if fraction > self._drift_fraction:
                notes.append(
                    f"the market has moved to {live} from the reference price "
                    f"{signal.reference_price}, which is {fraction:.2f}x the stop "
                    "distance; the risk and reward this signal describes were "
                    "measured from a price no longer on offer"
                )
        target = signal.take_profit
        if target is not None and self._target_is_reached(signal, live, target):
            notes.append(
                f"the target {target} has already been reached at {live}, so the "
                "reward this signal describes is no longer available. The size is "
                "unaffected and the stop still bounds the loss; whether that "
                "leaves a trade worth taking is a judgement, not a safety refusal"
            )
        return notes
