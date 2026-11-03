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

    def __post_init__(self) -> None:
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
        if notional < market.min_notional:
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
        return (
            self.market is None
            or self.market.valid_until is None
            or at <= self.market.valid_until
        )
