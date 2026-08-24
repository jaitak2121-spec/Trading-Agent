"""Sizing a signal: the one place a decision acquires a quantity.

A :class:`~trading.strategy.signals.Signal` says what and why and carries no
quantity. :class:`~trading.core.sizing.PositionSizer` turns a risk budget into a
quantity but knows nothing about signals -- it cannot, since the safety kernel
must not import the strategy layer. This module is the join, and it exists
because the join is where two specific things go wrong quietly.

**A stop is not optional here.** ``Signal.stop_loss`` is optional by design: a
strategy may hold a view without one. But a *risk-based* size divides by the
distance to the stop, so a signal without one has no defined loss and no
computable size. Refusing is the entire point -- substituting a default stop
would invent the number the position size is derived from, and the invention
would be invisible in the result.

**An absent loss budget is not an unlimited one.**
:meth:`~trading.core.sizing.PositionSizer.size_for_stop` reads
``remaining_loss_budget=None`` as "no budget cap", which is the right default for
a caller with no ledger to consult. But
:attr:`~trading.core.risk.RiskEngine.remaining_loss_budget` *also* returns
``None``, meaning "today's realized loss is a lower bound, so the allowance
cannot be stated". Those two ``None``s mean opposite things, and forwarding the
second into the first would turn an unknown budget into an uncapped one -- the
largest position this system can produce, from the least information. So the
budget is read here, by this module, and an unknown one refuses.

Refusing to *open* a position is always safe, so both refusals above return a
non-tradeable :class:`~trading.core.sizing.SizingResult` rather than raising: a
batch of signals stays sizeable, and each declined one carries a machine-readable
constraint saying why. The zero quantity cannot become an order in any case --
``OrderIntent`` rejects a non-positive quantity outright.

Refusing to *close* is not safe, so an ``EXIT`` signal raises instead. A zero
quantity there would read as "nothing to do" while the position stayed open, and
an exit that silently does not happen is a worse failure than a loud one. Exits
are sized from the position held, which is not a risk-budget question and does
not belong here.

Nothing in this module submits anything (INVARIANT 3). A ``SizingResult`` is a
proposal; :mod:`trading.core.risk` still decides (INVARIANT 4).
"""

from __future__ import annotations

from decimal import Decimal

from ..core.money import Currency, Money, Quantity
from ..core.orders import OrderSide
from ..core.risk import RiskEngine
from ..core.sizing import (
    DEFAULT_LOT_STEP,
    PositionSizer,
    SizingConstraint,
    SizingResult,
)
from .signals import Signal, SignalDirection

__all__ = ["SignalSizer"]

#: LONG opens with a buy, SHORT with a sell. Deliberately exhaustive over the
#: *entry* directions only: ``EXIT`` has no intrinsic side (see
#: :meth:`Signal.side_for`) and is refused before this map is consulted.
_ENTRY_SIDE = {
    SignalDirection.LONG: OrderSide.BUY,
    SignalDirection.SHORT: OrderSide.SELL,
}


class SignalSizer:
    """Proposes a size for a signal, under the risk engine's own limits."""

    def __init__(self, risk: RiskEngine) -> None:
        if not isinstance(risk, RiskEngine):
            raise TypeError("risk must be a RiskEngine")
        self._risk = risk
        # Built from the engine's own config rather than accepted from the
        # caller, so the limits a size is proposed against and the limits
        # approve() enforces cannot come from two different configurations.
        self._sizer = PositionSizer(risk.config)

    @property
    def sizer(self) -> PositionSizer:
        return self._sizer

    @property
    def base_currency(self) -> Currency:
        return self._risk.config.base_currency

    def _no_position(
        self, *, asset: str, constraint: SizingConstraint, reason: str
    ) -> SizingResult:
        zero_cash = Money.zero(self.base_currency)
        return SizingResult(
            quantity=Quantity.zero(asset),
            notional=zero_cash,
            # Zero rather than None: SizingResult's fields are not optional, and
            # a zero paired with a non-tradeable constraint cannot be mistaken
            # for a measured risk of nothing.
            risk_per_unit=Decimal(0),
            risk_budget=zero_cash,
            max_loss_at_stop=zero_cash,
            binding_constraint=constraint,
            reason=reason,
        )

    def size(
        self,
        signal: Signal,
        *,
        equity: Money,
        asset: str,
        lot_step: object = DEFAULT_LOT_STEP,
    ) -> SizingResult:
        """Size ``signal`` against ``equity``, or refuse and say why.

        ``asset`` is required and not derived from ``signal.symbol``: no rule in
        this system splits a symbol into a base asset, and guessing one would
        mislabel the quantity of every instrument whose ticker does not follow
        the convention guessed at.
        """
        if not isinstance(signal, Signal):
            raise TypeError("signal must be a Signal")

        if not signal.direction.is_entry:
            raise ValueError(
                f"{signal.signal_id} is an {signal.direction.value} signal and "
                "cannot be risk-sized: an exit closes the position held, whose "
                "size this sizer does not know. Answering zero would read as "
                "'nothing to do' while the position stayed open"
            )

        # Checked before the refusals below, because a currency mismatch is a
        # wiring error worth surfacing even when the answer would have been
        # 'no trade' anyway.
        if signal.currency != self.base_currency:
            raise ValueError(
                f"{signal.signal_id} is priced in {signal.currency.code} but sizing "
                f"is denominated in {self.base_currency.code}"
            )

        if signal.stop_loss is None:
            return self._no_position(
                asset=asset,
                constraint=SizingConstraint.MISSING_STOP,
                reason=(
                    f"{signal.signal_id} carries no stop, so the loss it risks is "
                    "undefined and no size follows from it; refusing rather than "
                    "sizing against a stop this system made up"
                ),
            )

        budget = self._risk.remaining_loss_budget
        if budget is None:
            return self._no_position(
                asset=asset,
                constraint=SizingConstraint.LOSS_BUDGET_CAP,
                reason=(
                    "today's realized loss is a lower bound rather than the figure "
                    f"({self._risk.pnl.unattributed_fills} fill(s) closed with no "
                    "known cost basis), so the remaining loss budget cannot be "
                    "stated; refusing rather than sizing as though it were unlimited"
                ),
            )

        return self._sizer.size_for_stop(
            equity=equity,
            entry=signal.reference_price,
            stop=signal.stop_loss,
            side=_ENTRY_SIDE[signal.direction],
            asset=asset,
            lot_step=lot_step,
            remaining_loss_budget=budget,
        )
