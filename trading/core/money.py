"""Decimal-based money, price, and quantity types.

INVARIANT 8: financial calculations use ``Decimal``, never ``float``.

This module enforces that invariant structurally rather than by convention:

* Every constructor and every arithmetic operand goes through
  :func:`to_decimal`, which raises ``TypeError`` on ``float`` and ``bool``.
  There is no code path that accepts a binary float.
* Values must be finite. ``NaN`` and ``Infinity`` are rejected, so a poisoned
  value cannot silently propagate through a risk check and compare ``False``
  against every limit.
* Construction is *strict* about precision: a value carrying more decimal
  places than its currency allows raises :class:`PrecisionError` instead of
  being silently rounded. Rounding is always explicit and always names its
  direction, because the safe direction differs by use: exposure rounds up,
  order size rounds down.
* Mixing currencies raises :class:`CurrencyMismatch`.

``bool`` is rejected alongside ``float`` even though ``bool`` is an ``int``
subclass: ``Money(True, USD)`` is far more likely to be a bug than an intent.
"""

from __future__ import annotations

import decimal
from dataclasses import dataclass
from decimal import Decimal
from typing import Final, Iterable, Sequence

from .errors import CurrencyMismatch, PrecisionError

__all__ = [
    "FINANCIAL_CONTEXT",
    "ROUND_DOWN",
    "ROUND_HALF_EVEN",
    "ROUND_UP",
    "Currency",
    "Money",
    "Price",
    "Quantity",
    "canonical_decimal_text",
    "to_decimal",
    "USD",
    "USDT",
    "INR",
    "BTC",
]

ROUND_HALF_EVEN: Final = decimal.ROUND_HALF_EVEN
ROUND_DOWN: Final = decimal.ROUND_DOWN
ROUND_UP: Final = decimal.ROUND_UP

#: Working context for all internal arithmetic.
#:
#: 34 significant digits matches IEEE 754 decimal128, which is comfortably
#: more than any crypto notional needs. The traps turn silent poisoning into
#: loud failure: an invalid operation or a division by zero raises rather than
#: producing NaN/Infinity that would defeat every downstream comparison.
FINANCIAL_CONTEXT: Final = decimal.Context(
    prec=34,
    rounding=ROUND_HALF_EVEN,
    traps=[decimal.InvalidOperation, decimal.DivisionByZero, decimal.Overflow],
)

_MAX_WORKING_SCALE: Final = 18


def to_decimal(value: object, *, field: str = "value") -> Decimal:
    """Coerce ``value`` to a finite :class:`Decimal`, or raise.

    Accepts ``int``, ``str``, and ``Decimal``. Rejects ``float`` and ``bool``
    unconditionally -- this is the single chokepoint that makes INVARIANT 8
    structural rather than aspirational.
    """
    if isinstance(value, bool):
        raise TypeError(
            f"{field}: bool is not a valid financial value (got {value!r}); "
            "pass an int, str, or Decimal"
        )
    if isinstance(value, float):
        raise TypeError(
            f"{field}: float is forbidden in financial calculations "
            f"(got {value!r}). Binary floats cannot represent decimal money "
            "exactly. Pass a str or Decimal instead, e.g. Decimal('0.1')."
        )
    if isinstance(value, Decimal):
        result = value
    elif isinstance(value, int):
        result = Decimal(value)
    elif isinstance(value, str):
        try:
            result = Decimal(value.strip())
        except decimal.InvalidOperation as exc:
            raise TypeError(f"{field}: {value!r} is not a valid decimal") from exc
    else:
        raise TypeError(
            f"{field}: expected int, str, or Decimal, got {type(value).__name__}"
        )

    if not result.is_finite():
        raise TypeError(f"{field}: non-finite values are forbidden (got {result})")
    return result


def canonical_decimal_text(value: object, *, field: str = "value") -> str:
    """A scale-independent, exact text form of a decimal number.

    ``Decimal("0.5")`` and ``Decimal("0.50")`` are the same number but different
    strings. Anything that derives an *identity* from a number -- an idempotency
    key above all -- must not treat them as different, or a retry that formats
    its quantity differently gets a fresh key and slips past duplicate
    detection (INVARIANT 12).

    Implemented on ``as_tuple`` rather than ``normalize()`` so it performs no
    context arithmetic and therefore cannot round, whatever the ambient decimal
    context happens to be.

    >>> canonical_decimal_text(Decimal("0.50")) == canonical_decimal_text(Decimal("0.5"))
    True
    >>> canonical_decimal_text(Decimal("1E+2")) == canonical_decimal_text(Decimal("100"))
    True
    """
    number = to_decimal(value, field=field)
    sign, digit_tuple, exponent = number.as_tuple()
    digits = list(digit_tuple)
    # Strip trailing zeros, raising the exponent to compensate. Exact.
    while len(digits) > 1 and digits[-1] == 0:
        digits.pop()
        exponent = int(exponent) + 1
    if digits == [0]:
        # Every zero is the same zero, including -0 and 0.000.
        sign, exponent = 0, 0
    body = "".join(str(d) for d in digits)
    return f"{'-' if sign else ''}{body}E{int(exponent)}"


def _scale_of(value: Decimal) -> int:
    exponent = value.as_tuple().exponent
    if not isinstance(exponent, int):  # pragma: no cover - guarded by to_decimal
        raise PrecisionError(f"cannot determine scale of non-finite value {value}")
    return -exponent if exponent < 0 else 0


@dataclass(frozen=True, slots=True)
class Currency:
    """A unit of account and the number of decimal places it supports."""

    code: str
    precision: int

    def __post_init__(self) -> None:
        if not self.code or not self.code.isalnum() or not self.code.isupper():
            raise ValueError(f"currency code must be uppercase alphanumeric: {self.code!r}")
        if not isinstance(self.precision, int) or isinstance(self.precision, bool):
            raise TypeError("currency precision must be an int")
        if not 0 <= self.precision <= _MAX_WORKING_SCALE:
            raise ValueError(
                f"currency precision must be between 0 and {_MAX_WORKING_SCALE}"
            )

    def __str__(self) -> str:
        return self.code


USD: Final = Currency("USD", 2)
INR: Final = Currency("INR", 2)
USDT: Final = Currency("USDT", 8)
BTC: Final = Currency("BTC", 8)


@dataclass(frozen=True, slots=True, eq=False, order=False)
class Money:
    """An exact amount of a single currency.

    Construction is strict: passing more decimal places than the currency
    supports raises :class:`PrecisionError`. Use :meth:`rounded` to round
    explicitly.
    """

    amount: Decimal
    currency: Currency

    def __init__(self, amount: object, currency: Currency) -> None:
        if not isinstance(currency, Currency):
            raise TypeError(f"currency must be a Currency, got {type(currency).__name__}")
        value = to_decimal(amount, field="amount")
        scale = _scale_of(value)
        if scale > currency.precision:
            raise PrecisionError(
                f"{value} has {scale} decimal places but {currency.code} supports "
                f"{currency.precision}. Refusing to round silently -- call "
                f"Money.rounded(value, {currency.code}, rounding=...) to be explicit."
            )
        object.__setattr__(self, "amount", value)
        object.__setattr__(self, "currency", currency)

    # -- constructors ----------------------------------------------------
    @classmethod
    def zero(cls, currency: Currency) -> "Money":
        return cls(0, currency)

    @classmethod
    def rounded(
        cls, amount: object, currency: Currency, *, rounding: str = ROUND_HALF_EVEN
    ) -> "Money":
        """Construct, rounding explicitly to the currency's precision."""
        value = to_decimal(amount, field="amount")
        quantum = Decimal(1).scaleb(-currency.precision)
        return cls(value.quantize(quantum, rounding=rounding, context=FINANCIAL_CONTEXT), currency)

    # -- guards ----------------------------------------------------------
    def _same_currency(self, other: "Money", op: str) -> None:
        if not isinstance(other, Money):
            raise TypeError(f"{op} requires another Money, got {type(other).__name__}")
        if other.currency != self.currency:
            raise CurrencyMismatch(
                f"cannot {op} {self.currency.code} and {other.currency.code}"
            )

    # -- arithmetic (exact; never rounds) --------------------------------
    def __add__(self, other: "Money") -> "Money":
        self._same_currency(other, "add")
        with decimal.localcontext(FINANCIAL_CONTEXT):
            return Money(self.amount + other.amount, self.currency)

    def __sub__(self, other: "Money") -> "Money":
        self._same_currency(other, "subtract")
        with decimal.localcontext(FINANCIAL_CONTEXT):
            return Money(self.amount - other.amount, self.currency)

    def __neg__(self) -> "Money":
        return Money(-self.amount, self.currency)

    def __abs__(self) -> "Money":
        return Money(abs(self.amount), self.currency)

    # -- scaling (rounding is always named) ------------------------------
    def times(self, factor: object, *, rounding: str = ROUND_HALF_EVEN) -> "Money":
        """Multiply by a scalar, rounding to the currency's precision."""
        multiplier = to_decimal(factor, field="factor")
        with decimal.localcontext(FINANCIAL_CONTEXT):
            return Money.rounded(self.amount * multiplier, self.currency, rounding=rounding)

    def divided_by(self, divisor: object, *, rounding: str = ROUND_HALF_EVEN) -> "Money":
        """Divide by a scalar, rounding to the currency's precision."""
        d = to_decimal(divisor, field="divisor")
        if d == 0:
            raise ZeroDivisionError("cannot divide Money by zero")
        with decimal.localcontext(FINANCIAL_CONTEXT):
            return Money.rounded(self.amount / d, self.currency, rounding=rounding)

    def ratio_to(self, other: "Money") -> Decimal:
        """Return ``self / other`` as an unrounded :class:`Decimal`."""
        self._same_currency(other, "compare")
        if other.amount == 0:
            raise ZeroDivisionError("cannot take a ratio to zero Money")
        with decimal.localcontext(FINANCIAL_CONTEXT):
            return self.amount / other.amount

    def allocate(self, weights: Sequence[object]) -> list["Money"]:
        """Split into parts proportional to ``weights``, conserving the total.

        The sum of the returned parts always equals ``self`` exactly; the
        remainder from rounding is distributed one quantum at a time. Prevents
        the classic "split 100 three ways and lose a cent" defect.
        """
        parsed = [to_decimal(w, field="weight") for w in weights]
        if not parsed:
            raise ValueError("allocate requires at least one weight")
        if any(w < 0 for w in parsed):
            raise ValueError("allocate weights must be non-negative")
        total_weight = sum(parsed, Decimal(0))
        if total_weight == 0:
            raise ValueError("allocate weights must not sum to zero")

        quantum = Decimal(1).scaleb(-self.currency.precision)
        with decimal.localcontext(FINANCIAL_CONTEXT):
            units_total = int((self.amount / quantum).to_integral_value(ROUND_DOWN))
            raw = [(units_total * w) / total_weight for w in parsed]
        floors = [int(r.to_integral_value(ROUND_DOWN)) for r in raw]
        remainder = units_total - sum(floors)
        # Hand the leftover quanta to the largest fractional parts first.
        order = sorted(range(len(raw)), key=lambda i: raw[i] - floors[i], reverse=True)
        for i in order[: max(remainder, 0)]:
            floors[i] += 1
        return [Money(quantum * n, self.currency) for n in floors]

    # -- predicates ------------------------------------------------------
    @property
    def is_zero(self) -> bool:
        return self.amount == 0

    @property
    def is_positive(self) -> bool:
        return self.amount > 0

    @property
    def is_negative(self) -> bool:
        return self.amount < 0

    # -- comparison ------------------------------------------------------
    def __eq__(self, other: object) -> bool:
        # Returns False across currencies rather than raising, so Money stays
        # usable as a dict key and in sets. Ordering DOES raise (see below).
        if not isinstance(other, Money):
            return NotImplemented
        return self.currency == other.currency and self.amount == other.amount

    def __hash__(self) -> int:
        return hash((self.currency.code, self.amount))

    def __lt__(self, other: "Money") -> bool:
        self._same_currency(other, "compare")
        return self.amount < other.amount

    def __le__(self, other: "Money") -> bool:
        self._same_currency(other, "compare")
        return self.amount <= other.amount

    def __gt__(self, other: "Money") -> bool:
        self._same_currency(other, "compare")
        return self.amount > other.amount

    def __ge__(self, other: "Money") -> bool:
        self._same_currency(other, "compare")
        return self.amount >= other.amount

    # -- rendering -------------------------------------------------------
    def __str__(self) -> str:
        # __str__ MUST NOT raise: it runs inside audit-log records and error
        # messages, where an exception would mask the safety event being
        # reported. Quantizing under the ambient default context (28 digits)
        # would raise InvalidOperation for very large values, so use the
        # financial context and fall back to the exact digits if even that is
        # too narrow.
        quantum = Decimal(1).scaleb(-self.currency.precision)
        try:
            with decimal.localcontext(FINANCIAL_CONTEXT):
                shown = self.amount.quantize(quantum, rounding=ROUND_HALF_EVEN)
        except decimal.DecimalException:
            shown = self.amount
        return f"{shown} {self.currency.code}"

    def __repr__(self) -> str:
        return f"Money('{self.amount}', {self.currency.code})"


@dataclass(frozen=True, slots=True, eq=False, order=False)
class Quantity:
    """An amount of a tradable asset.

    May be negative to represent a short position. Order construction requires
    a strictly positive quantity; direction is carried by the order side, never
    by the sign of the quantity.
    """

    amount: Decimal
    asset: str

    def __init__(self, amount: object, asset: str, *, max_scale: int = 8) -> None:
        if not isinstance(asset, str) or not asset:
            raise TypeError("asset must be a non-empty str")
        if not isinstance(max_scale, int) or isinstance(max_scale, bool):
            raise TypeError("max_scale must be an int")
        if not 0 <= max_scale <= _MAX_WORKING_SCALE:
            raise ValueError(f"max_scale must be between 0 and {_MAX_WORKING_SCALE}")
        value = to_decimal(amount, field="quantity")
        if _scale_of(value) > max_scale:
            raise PrecisionError(
                f"quantity {value} exceeds max scale {max_scale} for {asset}"
            )
        object.__setattr__(self, "amount", value)
        object.__setattr__(self, "asset", asset)

    @classmethod
    def zero(cls, asset: str) -> "Quantity":
        return cls(0, asset)

    def _same_asset(self, other: "Quantity", op: str) -> None:
        if not isinstance(other, Quantity):
            raise TypeError(f"{op} requires another Quantity")
        if other.asset != self.asset:
            raise CurrencyMismatch(f"cannot {op} {self.asset} and {other.asset}")

    def __add__(self, other: "Quantity") -> "Quantity":
        self._same_asset(other, "add")
        with decimal.localcontext(FINANCIAL_CONTEXT):
            return Quantity(self.amount + other.amount, self.asset, max_scale=_MAX_WORKING_SCALE)

    def __sub__(self, other: "Quantity") -> "Quantity":
        self._same_asset(other, "subtract")
        with decimal.localcontext(FINANCIAL_CONTEXT):
            return Quantity(self.amount - other.amount, self.asset, max_scale=_MAX_WORKING_SCALE)

    def __neg__(self) -> "Quantity":
        return Quantity(-self.amount, self.asset, max_scale=_MAX_WORKING_SCALE)

    def __abs__(self) -> "Quantity":
        return Quantity(abs(self.amount), self.asset, max_scale=_MAX_WORKING_SCALE)

    def floor_to_step(self, step: object) -> "Quantity":
        """Round DOWN to a multiple of ``step``.

        Always rounds down so that a lot-size adjustment can never increase
        exposure beyond what a risk check already approved.
        """
        s = to_decimal(step, field="step")
        if s <= 0:
            raise ValueError("step must be positive")
        with decimal.localcontext(FINANCIAL_CONTEXT):
            steps = (self.amount / s).to_integral_value(ROUND_DOWN)
            return Quantity(steps * s, self.asset, max_scale=_MAX_WORKING_SCALE)

    @property
    def is_zero(self) -> bool:
        return self.amount == 0

    @property
    def is_positive(self) -> bool:
        return self.amount > 0

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, Quantity):
            return NotImplemented
        return self.asset == other.asset and self.amount == other.amount

    def __hash__(self) -> int:
        return hash((self.asset, self.amount))

    def __lt__(self, other: "Quantity") -> bool:
        self._same_asset(other, "compare")
        return self.amount < other.amount

    def __le__(self, other: "Quantity") -> bool:
        self._same_asset(other, "compare")
        return self.amount <= other.amount

    def __gt__(self, other: "Quantity") -> bool:
        self._same_asset(other, "compare")
        return self.amount > other.amount

    def __ge__(self, other: "Quantity") -> bool:
        self._same_asset(other, "compare")
        return self.amount >= other.amount

    def __str__(self) -> str:
        return f"{self.amount} {self.asset}"

    def __repr__(self) -> str:
        return f"Quantity('{self.amount}', {self.asset!r})"


@dataclass(frozen=True, slots=True, eq=False, order=False)
class Price:
    """A price expressed in a quote currency.

    Prices carry more precision than the quote currency's cash precision,
    because a unit price may legitimately be finer than the smallest cash
    amount (e.g. 0.00001234 USDT per token).
    """

    amount: Decimal
    currency: Currency

    def __init__(self, amount: object, currency: Currency, *, max_scale: int = 12) -> None:
        if not isinstance(currency, Currency):
            raise TypeError("currency must be a Currency")
        value = to_decimal(amount, field="price")
        if value <= 0:
            raise ValueError(f"price must be strictly positive, got {value}")
        if _scale_of(value) > max_scale:
            raise PrecisionError(f"price {value} exceeds max scale {max_scale}")
        object.__setattr__(self, "amount", value)
        object.__setattr__(self, "currency", currency)

    def notional(
        self, quantity: Quantity, *, rounding: str = ROUND_UP
    ) -> Money:
        """Cash value of ``quantity`` at this price.

        Defaults to :data:`ROUND_UP` on the absolute value, which is the
        conservative direction for risk: a notional used in an exposure check
        is never understated by rounding.
        """
        if not isinstance(quantity, Quantity):
            raise TypeError("notional requires a Quantity")
        with decimal.localcontext(FINANCIAL_CONTEXT):
            raw = self.amount * abs(quantity.amount)
            value = Money.rounded(raw, self.currency, rounding=rounding)
        return -value if quantity.amount < 0 else value

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, Price):
            return NotImplemented
        return self.currency == other.currency and self.amount == other.amount

    def __hash__(self) -> int:
        return hash((self.currency.code, self.amount))

    def __lt__(self, other: "Price") -> bool:
        if not isinstance(other, Price) or other.currency != self.currency:
            raise CurrencyMismatch("cannot compare prices in different currencies")
        return self.amount < other.amount

    def __gt__(self, other: "Price") -> bool:
        if not isinstance(other, Price) or other.currency != self.currency:
            raise CurrencyMismatch("cannot compare prices in different currencies")
        return self.amount > other.amount

    def __str__(self) -> str:
        return f"{self.amount} {self.currency.code}"

    def __repr__(self) -> str:
        return f"Price('{self.amount}', {self.currency.code})"


def total(amounts: Iterable[Money], currency: Currency) -> Money:
    """Sum ``amounts``, returning zero in ``currency`` when empty."""
    result = Money.zero(currency)
    for item in amounts:
        result = result + item
    return result
