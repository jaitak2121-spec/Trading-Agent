"""Position sizing.

Turns a risk budget into a quantity. Two rules govern everything here:

**Round down, always.** A lot-size or precision adjustment may only ever shrink
a position. :meth:`~trading.core.money.Quantity.floor_to_step` does the work;
this module never calls anything that could round a size up.

**Verify the result, do not trust the arithmetic.** Sizes are floored but
notionals are rounded *up* (exposure must never be understated), so at a
boundary a floored quantity can still price out just above the ceiling. Rather
than reason about when that can happen, :meth:`PositionSizer.size_for_stop`
re-prices the candidate and steps down until it genuinely fits, or returns zero.

A zero result means "do not trade". It is a legitimate answer, not an error, and
:attr:`SizingResult.is_tradeable` exists so no caller can mistake one for the
other.

Sizing proposes; :mod:`trading.core.risk` still decides. Nothing here grants
permission to send an order (INVARIANT 4).
"""

from __future__ import annotations

import decimal
from dataclasses import dataclass
from decimal import Decimal
from enum import Enum
from typing import Final

from .config import RiskConfig
from .errors import SafetyViolation
from .money import (
    FINANCIAL_CONTEXT,
    ROUND_DOWN,
    Currency,
    Money,
    Price,
    Quantity,
    to_decimal,
)
from .orders import OrderSide

__all__ = [
    "SizingConstraint",
    "SizingResult",
    "PositionSizer",
    "DEFAULT_LOT_STEP",
]

#: Smallest quantity increment assumed when a venue does not specify one. Equal
#: to ``Quantity``'s maximum scale, so it never introduces excess precision.
DEFAULT_LOT_STEP: Final = Decimal("0.00000001")

#: Bound on the step-down retry loop. Two would do; eight is cheap insurance
#: against an unexpected rounding interaction, and exceeding it is a bug.
_MAX_STEP_DOWNS: Final = 8


class SizingConstraint(Enum):
    """Which rule determined the size. Reported so a small size is explainable."""

    RISK_FRACTION = "risk_fraction_per_trade"
    ORDER_NOTIONAL_CAP = "max_order_notional"
    EQUITY_CAP = "available_equity"
    LOSS_BUDGET_CAP = "remaining_loss_budget"
    LOT_STEP = "lot_step"
    NOT_TRADEABLE = "not_tradeable"


@dataclass(frozen=True, slots=True)
class SizingResult:
    """A proposed size, plus enough context to audit the decision."""

    quantity: Quantity
    notional: Money
    risk_per_unit: Decimal
    risk_budget: Money
    max_loss_at_stop: Money
    binding_constraint: SizingConstraint
    reason: str | None = None

    @property
    def is_tradeable(self) -> bool:
        """A zero size must never be turned into an order."""
        return self.quantity.is_positive

    def as_details(self) -> dict[str, object]:
        return {
            "quantity": str(self.quantity.amount),
            "asset": self.quantity.asset,
            "notional": str(self.notional.amount),
            "currency": self.notional.currency.code,
            "risk_per_unit": str(self.risk_per_unit),
            "risk_budget": str(self.risk_budget.amount),
            "max_loss_at_stop": str(self.max_loss_at_stop.amount),
            "binding_constraint": self.binding_constraint.value,
            "reason": self.reason,
            "tradeable": self.is_tradeable,
        }

    def __str__(self) -> str:
        if not self.is_tradeable:
            return f"no position ({self.binding_constraint.value}: {self.reason})"
        return (
            f"{self.quantity} @ {self.notional} "
            f"(bound by {self.binding_constraint.value})"
        )


class PositionSizer:
    """Sizes positions from a :class:`~trading.core.config.RiskConfig`."""

    def __init__(self, config: RiskConfig) -> None:
        if not isinstance(config, RiskConfig):
            raise TypeError("config must be a RiskConfig")
        self._config = config

    @property
    def config(self) -> RiskConfig:
        return self._config

    @property
    def base_currency(self) -> Currency:
        return self._config.base_currency

    # -- helpers -----------------------------------------------------------
    def _require_base_money(self, value: Money, field: str) -> None:
        if not isinstance(value, Money):
            raise TypeError(f"{field} must be a Money, got {type(value).__name__}")
        if value.currency != self.base_currency:
            raise ValueError(
                f"{field} must be in {self.base_currency.code}, "
                f"got {value.currency.code}"
            )

    def _require_base_price(self, value: Price, field: str) -> None:
        if not isinstance(value, Price):
            raise TypeError(f"{field} must be a Price, got {type(value).__name__}")
        if value.currency != self.base_currency:
            raise ValueError(
                f"{field} must be in {self.base_currency.code}, "
                f"got {value.currency.code}"
            )

    def _zero(
        self,
        *,
        asset: str,
        risk_per_unit: Decimal,
        risk_budget: Money,
        constraint: SizingConstraint,
        reason: str,
    ) -> SizingResult:
        zero_cash = Money.zero(self.base_currency)
        return SizingResult(
            quantity=Quantity.zero(asset),
            notional=zero_cash,
            risk_per_unit=risk_per_unit,
            risk_budget=risk_budget,
            max_loss_at_stop=zero_cash,
            binding_constraint=constraint,
            reason=reason,
        )

    # -- the sizing rule ---------------------------------------------------
    def size_for_stop(
        self,
        *,
        equity: Money,
        entry: Price,
        stop: Price,
        side: OrderSide,
        asset: str,
        lot_step: object = DEFAULT_LOT_STEP,
        remaining_loss_budget: Money | None = None,
    ) -> SizingResult:
        """Size so that being stopped out costs at most ``risk_fraction_per_trade``.

        ``remaining_loss_budget``, when supplied, further caps the risk budget --
        having already lost most of the day's allowance, a full-size trade would
        be able to breach it in one move.
        """
        self._require_base_money(equity, "equity")
        self._require_base_price(entry, "entry")
        self._require_base_price(stop, "stop")
        if not isinstance(side, OrderSide):
            raise TypeError("side must be an OrderSide")
        if not isinstance(asset, str) or not asset.strip():
            raise ValueError("asset must be a non-empty string")
        step = to_decimal(lot_step, field="lot_step")
        if step <= 0:
            raise ValueError("lot_step must be positive")
        if remaining_loss_budget is not None:
            self._require_base_money(remaining_loss_budget, "remaining_loss_budget")

        # A stop must sit on the losing side of entry, or it is not a stop and
        # the resulting "risk per unit" would be meaningless.
        if side is OrderSide.BUY and stop.amount >= entry.amount:
            raise ValueError(
                f"a long stop must be below entry: entry={entry.amount} "
                f"stop={stop.amount}"
            )
        if side is OrderSide.SELL and stop.amount <= entry.amount:
            raise ValueError(
                f"a short stop must be above entry: entry={entry.amount} "
                f"stop={stop.amount}"
            )

        with decimal.localcontext(FINANCIAL_CONTEXT):
            risk_per_unit = abs(entry.amount - stop.amount)

        zero_cash = Money.zero(self.base_currency)
        if not equity.is_positive:
            return self._zero(
                asset=asset,
                risk_per_unit=risk_per_unit,
                risk_budget=zero_cash,
                constraint=SizingConstraint.EQUITY_CAP,
                reason=f"equity is {equity}; nothing to risk",
            )

        # Risk budget: round DOWN so the fraction is a ceiling, not a target.
        risk_budget = equity.times(
            self._config.risk_fraction_per_trade, rounding=ROUND_DOWN
        )
        constraint = SizingConstraint.RISK_FRACTION

        if remaining_loss_budget is not None:
            if not remaining_loss_budget.is_positive:
                return self._zero(
                    asset=asset,
                    risk_per_unit=risk_per_unit,
                    risk_budget=zero_cash,
                    constraint=SizingConstraint.LOSS_BUDGET_CAP,
                    reason=(
                        "the daily loss budget is exhausted "
                        f"({remaining_loss_budget} remaining)"
                    ),
                )
            if remaining_loss_budget < risk_budget:
                risk_budget = remaining_loss_budget
                constraint = SizingConstraint.LOSS_BUDGET_CAP

        if not risk_budget.is_positive:
            return self._zero(
                asset=asset,
                risk_per_unit=risk_per_unit,
                risk_budget=risk_budget,
                constraint=constraint,
                reason=(
                    f"{self._config.risk_fraction_per_trade} of {equity} rounds down "
                    "to nothing at this currency's precision"
                ),
            )

        with decimal.localcontext(FINANCIAL_CONTEXT):
            by_risk = risk_budget.amount / risk_per_unit
            by_order_cap = self._config.max_order_notional.amount / entry.amount
            by_equity = equity.amount / entry.amount

        # Smallest cap wins, and we remember which one so the size is explainable.
        caps: list[tuple[Decimal, SizingConstraint]] = [
            (by_risk, constraint),
            (by_order_cap, SizingConstraint.ORDER_NOTIONAL_CAP),
            (by_equity, SizingConstraint.EQUITY_CAP),
        ]
        raw, binding = min(caps, key=lambda pair: pair[0])

        # Quantity() would reject excess scale outright, so truncate first --
        # downwards, via quantize with ROUND_DOWN, then again onto the lot step.
        with decimal.localcontext(FINANCIAL_CONTEXT):
            truncated = raw.quantize(DEFAULT_LOT_STEP, rounding=ROUND_DOWN)
        candidate = Quantity(truncated, asset).floor_to_step(step)
        if candidate.amount != truncated:
            binding = SizingConstraint.LOT_STEP

        # Re-price and step down until the ROUND_UP notional actually fits.
        ceiling = min(self._config.max_order_notional, equity)
        step_quantity = Quantity(step, asset)
        for _ in range(_MAX_STEP_DOWNS):
            if not candidate.is_positive:
                break
            notional = entry.notional(candidate)
            if notional <= ceiling:
                break
            candidate = candidate - step_quantity
            binding = SizingConstraint.LOT_STEP
        else:
            raise SafetyViolation(
                "position sizing failed to converge below the notional ceiling "
                f"after {_MAX_STEP_DOWNS} step-downs; refusing to propose a size "
                f"(entry={entry.amount}, ceiling={ceiling}, step={step})"
            )

        if not candidate.is_positive:
            return self._zero(
                asset=asset,
                risk_per_unit=risk_per_unit,
                risk_budget=risk_budget,
                constraint=SizingConstraint.NOT_TRADEABLE,
                reason=(
                    f"the smallest tradeable size ({step} {asset}) does not fit "
                    f"within a risk budget of {risk_budget} at a stop distance of "
                    f"{risk_per_unit}"
                ),
            )

        notional = entry.notional(candidate)
        # Loss if the stop is hit, rounded UP: the pessimistic figure.
        with decimal.localcontext(FINANCIAL_CONTEXT):
            loss_amount = risk_per_unit * candidate.amount
        max_loss = Money.rounded(
            loss_amount, self.base_currency, rounding=decimal.ROUND_UP
        )

        # Post-conditions. If any of these fail the arithmetic above is wrong,
        # and a wrong size is worse than no size.
        if notional > ceiling:
            raise SafetyViolation(
                f"sized notional {notional} exceeds the ceiling {ceiling} "
                "after step-down; refusing to propose a size"
            )
        if max_loss > risk_budget:
            raise SafetyViolation(
                f"sized position risks {max_loss} at the stop, over the budget of "
                f"{risk_budget}; refusing to propose a size"
            )

        return SizingResult(
            quantity=candidate,
            notional=notional,
            risk_per_unit=risk_per_unit,
            risk_budget=risk_budget,
            max_loss_at_stop=max_loss,
            binding_constraint=binding,
        )

    def size_for_notional(
        self,
        *,
        target_notional: Money,
        entry: Price,
        asset: str,
        lot_step: object = DEFAULT_LOT_STEP,
    ) -> SizingResult:
        """Size to spend at most ``target_notional``, ignoring stop distance.

        For strategies that express conviction as cash rather than as a stop.
        The per-order ceiling still applies, and the result is still floored.
        """
        self._require_base_money(target_notional, "target_notional")
        self._require_base_price(entry, "entry")
        if not isinstance(asset, str) or not asset.strip():
            raise ValueError("asset must be a non-empty string")
        step = to_decimal(lot_step, field="lot_step")
        if step <= 0:
            raise ValueError("lot_step must be positive")

        zero_cash = Money.zero(self.base_currency)
        if not target_notional.is_positive:
            return self._zero(
                asset=asset,
                risk_per_unit=Decimal(0),
                risk_budget=zero_cash,
                constraint=SizingConstraint.NOT_TRADEABLE,
                reason=f"target notional is {target_notional}",
            )

        ceiling = self._config.max_order_notional
        binding = SizingConstraint.RISK_FRACTION
        if target_notional > ceiling:
            target_notional = ceiling
            binding = SizingConstraint.ORDER_NOTIONAL_CAP

        with decimal.localcontext(FINANCIAL_CONTEXT):
            raw = target_notional.amount / entry.amount
            truncated = raw.quantize(DEFAULT_LOT_STEP, rounding=ROUND_DOWN)
        candidate = Quantity(truncated, asset).floor_to_step(step)
        if candidate.amount != truncated:
            binding = SizingConstraint.LOT_STEP

        step_quantity = Quantity(step, asset)
        for _ in range(_MAX_STEP_DOWNS):
            if not candidate.is_positive:
                break
            if entry.notional(candidate) <= target_notional:
                break
            candidate = candidate - step_quantity
            binding = SizingConstraint.LOT_STEP
        else:
            raise SafetyViolation(
                "notional sizing failed to converge below "
                f"{target_notional} after {_MAX_STEP_DOWNS} step-downs"
            )

        if not candidate.is_positive:
            return self._zero(
                asset=asset,
                risk_per_unit=Decimal(0),
                risk_budget=target_notional,
                constraint=SizingConstraint.NOT_TRADEABLE,
                reason=(
                    f"{target_notional} does not buy one lot step ({step} {asset}) "
                    f"at {entry}"
                ),
            )

        notional = entry.notional(candidate)
        if notional > ceiling:
            raise SafetyViolation(
                f"sized notional {notional} exceeds the per-order ceiling {ceiling}"
            )
        return SizingResult(
            quantity=candidate,
            notional=notional,
            risk_per_unit=Decimal(0),
            risk_budget=target_notional,
            # No stop was given, so the honest worst case is the whole position.
            max_loss_at_stop=notional,
            binding_constraint=binding,
        )
