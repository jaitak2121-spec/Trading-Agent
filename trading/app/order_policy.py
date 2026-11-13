"""Venue-compatible order construction policies for the paper service."""

from __future__ import annotations

import decimal
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from ..core.marketdata import Quote
from ..core.money import (
    FINANCIAL_CONTEXT,
    ROUND_DOWN,
    ROUND_UP,
    Money,
    Price,
    Quantity,
    to_decimal,
)
from ..core.orders import OrderSide

__all__ = ["MarketableLimitPolicy", "VenueMarketSpecification"]


def bounded_exit_quantity(held, *, ceiling, valuation_price, limit_price=None, market=None):
    """A downward-rounded exit chunk; neither risk nor venue limits are waived."""
    if not held.is_positive:raise ValueError('exit requires positive available quantity')
    if ceiling.currency!=valuation_price.currency:raise ValueError('exit valuation currency mismatch')
    step=market.quantity_step if market is not None else Decimal('0.00000001')
    if market is not None:
        if market.max_notional.currency!=ceiling.currency:raise ValueError('exit market currency mismatch')
        ceiling=min(ceiling,market.max_notional)
    if limit_price is not None and limit_price.currency!=valuation_price.currency:raise ValueError('exit limit currency mismatch')
    price=max(valuation_price,limit_price) if limit_price is not None else valuation_price
    with decimal.localcontext(FINANCIAL_CONTEXT):
        tradeable=held.floor_to_step(step)
        amount=min(tradeable.amount,ceiling.amount/price.amount)
        quantity=Quantity(amount.quantize(Decimal('0.000000000001'),rounding=ROUND_DOWN),held.asset,max_scale=12).floor_to_step(step)
        if market is not None and quantity.amount<tradeable.amount:
            minimum_price=limit_price or valuation_price
            remaining=tradeable.amount-quantity.amount
            if remaining*minimum_price.amount<market.min_notional.amount:
                minimum_steps=(market.min_notional.amount/minimum_price.amount/step).to_integral_value(ROUND_UP)
                quantity=Quantity(tradeable.amount-minimum_steps*step,held.asset,max_scale=12).floor_to_step(step)
        if quantity.is_zero or quantity.amount<0:raise ValueError('remaining position cannot form a compliant exit chunk')
        if price.notional(quantity)>ceiling:raise ValueError('exit chunk exceeds notional ceiling')
        if market is not None and (limit_price or valuation_price).amount*quantity.amount<market.min_notional.amount:
            raise ValueError('remaining position is below venue minimum; retained as visible dust')
    return quantity


@dataclass(frozen=True, slots=True)
class VenueMarketSpecification:
    """Exact order constraints supplied by venue reference metadata."""

    price_decimals: int
    quantity_step: Decimal
    min_notional: Money
    max_notional: Money
    metadata_source: str | None = None
    captured_at: datetime | None = None
    valid_until: datetime | None = None

    def __post_init__(self) -> None:
        if isinstance(self.price_decimals, bool) or not isinstance(
            self.price_decimals, int
        ):
            raise TypeError("price_decimals must be an int")
        if not 0 <= self.price_decimals <= 12:
            raise ValueError("price_decimals must be between 0 and 12")
        step = to_decimal(self.quantity_step, field="quantity_step")
        if step <= 0:
            raise ValueError("quantity_step must be positive")
        object.__setattr__(self, "quantity_step", step)
        if not isinstance(self.min_notional, Money) or not isinstance(
            self.max_notional, Money
        ):
            raise TypeError("min_notional and max_notional must be Money")
        if self.min_notional.currency != self.max_notional.currency:
            raise ValueError("min_notional and max_notional currencies must match")
        if self.min_notional.amount < 0:
            raise ValueError("min_notional must not be negative")
        if self.max_notional.amount <= 0:
            raise ValueError("max_notional must be positive")
        if self.min_notional > self.max_notional:
            raise ValueError("min_notional must not exceed max_notional")
        if self.metadata_source is not None and (
            not isinstance(self.metadata_source, str)
            or not self.metadata_source.strip()
        ):
            raise TypeError("metadata_source must be a non-empty string or None")
        for field, value in (
            ("captured_at", self.captured_at),
            ("valid_until", self.valid_until),
        ):
            if value is not None:
                if not isinstance(value, datetime):
                    raise TypeError(f"{field} must be a datetime or None")
                if value.tzinfo is None:
                    raise ValueError(f"{field} must be timezone-aware")
        if (
            self.captured_at is not None
            and self.valid_until is not None
            and self.valid_until < self.captured_at
        ):
            raise ValueError("valid_until must not precede captured_at")


@dataclass(frozen=True, slots=True)
class MarketableLimitPolicy:
    """Build a protected marketable limit from the current two-sided quote.

    A buy is capped above the ask and rounded upward to the configured venue
    price precision. A sell is floored below the bid and rounded downward.
    The result remains marketable at the observed quote while placing an
    explicit worst price on execution. No float is accepted or produced.
    """

    protection_bps: int = 10
    price_decimals: int = 2
    market: VenueMarketSpecification | None = None
    require_verified_metadata: bool = False

    def __post_init__(self) -> None:
        if type(self.require_verified_metadata) is not bool:raise TypeError('metadata requirement must be boolean')
        if isinstance(self.protection_bps, bool) or not isinstance(
            self.protection_bps, int
        ):
            raise TypeError("protection_bps must be an int")
        if not 0 <= self.protection_bps <= 1000:
            raise ValueError("protection_bps must be between 0 and 1000")
        if isinstance(self.price_decimals, bool) or not isinstance(
            self.price_decimals, int
        ):
            raise TypeError("price_decimals must be an int")
        if not 0 <= self.price_decimals <= 12:
            raise ValueError("price_decimals must be between 0 and 12")
        if self.market is not None and not isinstance(
            self.market, VenueMarketSpecification
        ):
            raise TypeError("market must be a VenueMarketSpecification or None")

    def limit_price(self, side: OrderSide, quote: Quote) -> Price:
        if not isinstance(side, OrderSide):
            raise TypeError("side must be an OrderSide")
        if not isinstance(quote, Quote):
            raise TypeError("quote must be a Quote")
        with decimal.localcontext(FINANCIAL_CONTEXT):
            fraction = Decimal(self.protection_bps) / Decimal(10_000)
            if side is OrderSide.BUY:
                raw = quote.ask.amount * (Decimal(1) + fraction)
                rounding = ROUND_UP
            else:
                raw = quote.bid.amount * (Decimal(1) - fraction)
                rounding = ROUND_DOWN
            decimals = (
                self.market.price_decimals
                if self.market is not None
                else self.price_decimals
            )
            quantum = Decimal(1).scaleb(-decimals)
            rounded = raw.quantize(quantum, rounding=rounding)
        if rounded <= 0:
            raise ValueError(
                "configured price precision rounds the protected limit to zero"
            )
        return Price(
            rounded,
            quote.currency,
            max_scale=decimals,
        )

    def prepare(
        self, side: OrderSide, quote: Quote, quantity: Quantity
    ) -> tuple[Quantity, Price]:
        """Return venue-stepped quantity and protected limit, or refuse."""
        if not isinstance(quantity, Quantity):
            raise TypeError("quantity must be a Quantity")
        if self.require_verified_metadata and not self.metadata_valid_at(quote.as_of):raise ValueError('verified, unexpired venue metadata is required')
        limit = self.limit_price(side, quote)
        market = self.market
        if market is None:
            return quantity, limit
        if market.valid_until is not None and quote.as_of > market.valid_until:
            raise ValueError("venue market metadata is stale")
        if limit.currency != market.min_notional.currency:
            raise ValueError(
                "market notional currency does not match the quote currency"
            )
        stepped = quantity.floor_to_step(market.quantity_step)
        if stepped.is_zero:
            raise ValueError("quantity rounds to zero at the venue step")
        notional = limit.notional(stepped)
        if limit.amount*stepped.amount < market.min_notional.amount:
            raise ValueError(
                f"order notional {notional} is below venue minimum "
                f"{market.min_notional}"
            )
        if notional > market.max_notional:
            raise ValueError(
                f"order notional {notional} exceeds venue maximum "
                f"{market.max_notional}"
            )
        return stepped, limit

    def metadata_valid_at(self, at: datetime) -> bool:
        """Whether time-bounded venue metadata still permits new orders."""
        if not isinstance(at, datetime) or at.tzinfo is None:
            raise TypeError("at must be a timezone-aware datetime")
        if self.require_verified_metadata and (self.market is None or self.market.captured_at is None or self.market.valid_until is None
                or self.market.metadata_source!='/trade/api/v2/tradeInfo' or at<self.market.captured_at):
            return False
        return (
            self.market is None
            or self.market.valid_until is None
            or at <= self.market.valid_until
        )

    def validate_order(self,intent,quote,at):
        """Validate already-constructed orders without silently rounding them."""
        if not self.metadata_valid_at(at):raise ValueError('verified venue metadata missing, unsupported or expired')
        market=self.market
        if market is None:return
        if intent.symbol!=quote.symbol or intent.quantity.asset!=quote.symbol.split('/')[0]:
            raise ValueError('order market or base asset mismatch')
        if intent.limit_price is None:raise ValueError('real-market paper execution requires a protected limit')
        if intent.limit_price.currency!=market.min_notional.currency:raise ValueError('order currency mismatch')
        with decimal.localcontext(FINANCIAL_CONTEXT):
            if intent.quantity.floor_to_step(market.quantity_step)!=intent.quantity:raise ValueError('order quantity violates venue increment')
            quantum=Decimal(1).scaleb(-market.price_decimals)
            if intent.limit_price.amount.quantize(quantum)!=intent.limit_price.amount:raise ValueError('order price violates venue precision')
            notional=intent.quantity.amount*intent.limit_price.amount
            if not market.min_notional.amount<=notional<=market.max_notional.amount:raise ValueError('order notional violates venue bounds')
