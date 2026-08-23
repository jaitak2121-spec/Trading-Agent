"""Indicators: pure functions over closed candles.

Every one is a plain function of a bar sequence -- no state, no clock, no
configuration object. That is not minimalism for its own sake: an indicator that
remembers anything between calls behaves differently on the second run over the
same data, which makes a backtest and a live session disagree for reasons nobody
can find.

Three rules hold throughout:

* **``Decimal`` only.** Never ``float``. An indicator is arithmetic on prices, and
  binary floating point on prices is how a stop lands one tick off (INVARIANT 8).
* **Insufficient data returns ``None``, never a partial answer.** A 20-period mean
  of 12 bars is not a 20-period mean, and a strategy handed one would size a real
  position off it. ``None`` forces the caller to decide.
* **Input is validated, not trusted.** Unordered or mixed-currency bars raise
  rather than produce a number that looks plausible.

The smoothing choices are stated where they are made. There is no single correct
definition of ATR or RSI, and an undocumented variant is a number nobody can
reproduce.
"""

from __future__ import annotations

import decimal
from decimal import Decimal
from typing import Sequence

from ..core.marketdata import Candle
from ..core.money import FINANCIAL_CONTEXT, Price

__all__ = [
    "atr",
    "ema",
    "highest_high",
    "lowest_low",
    "rsi",
    "sma",
    "true_range",
]


def _validated(candles: Sequence[Candle], period: int) -> tuple[Candle, ...]:
    """Check the bars are usable, and that ``period`` is a period."""
    if isinstance(period, bool) or not isinstance(period, int):
        raise TypeError("period must be an int")
    if period <= 0:
        raise ValueError("period must be positive")
    bars = tuple(candles)
    for bar in bars:
        if not isinstance(bar, Candle):
            raise TypeError(f"expected Candle, got {type(bar).__name__}")
    if bars:
        currencies = {bar.close.currency for bar in bars}
        if len(currencies) != 1:
            raise ValueError(
                "candles mix currencies "
                f"{sorted(c.code for c in currencies)}; an indicator over them "
                "would be adding unlike numbers"
            )
        times = [bar.open_time for bar in bars]
        if times != sorted(times):
            raise ValueError(
                "candles are not in chronological order; an indicator over them "
                "would be meaningless rather than merely wrong"
            )
    return bars


def sma(candles: Sequence[Candle], period: int) -> Price | None:
    """Simple mean of the last ``period`` closes, or ``None`` if too few bars."""
    bars = _validated(candles, period)
    if len(bars) < period:
        return None
    window = bars[-period:]
    with decimal.localcontext(FINANCIAL_CONTEXT):
        total = sum((bar.close.amount for bar in window), Decimal(0))
        mean = total / Decimal(period)
    return Price.rounded(mean, window[-1].close.currency)


def ema(candles: Sequence[Candle], period: int) -> Price | None:
    """Exponential mean, seeded with the SMA of the first ``period`` bars.

    Seeding with an SMA rather than the first close is the conventional choice and
    the more stable one: seeding from a single bar lets one outlier dominate the
    early values, and a strategy that starts trading immediately would trade that
    artefact.

    Uses the standard ``2 / (period + 1)`` smoothing factor.
    """
    bars = _validated(candles, period)
    if len(bars) < period:
        return None
    currency = bars[-1].close.currency
    with decimal.localcontext(FINANCIAL_CONTEXT):
        seed = sum((bar.close.amount for bar in bars[:period]), Decimal(0)) / Decimal(
            period
        )
        multiplier = Decimal(2) / (Decimal(period) + Decimal(1))
        value = seed
        for bar in bars[period:]:
            value = (bar.close.amount - value) * multiplier + value
    return Price.rounded(value, currency)


def true_range(current: Candle, previous: Candle | None = None) -> Decimal:
    """The bar's true range.

    ``max(high - low, |high - prev_close|, |low - prev_close|)``. The two gap
    terms are the point: on a gap open the plain high-low range understates how
    far price actually travelled, and a stop placed off it would be far too tight.

    With no previous bar it degrades to high - low, which is the best available
    answer rather than a guess.
    """
    if not isinstance(current, Candle):
        raise TypeError("current must be a Candle")
    with decimal.localcontext(FINANCIAL_CONTEXT):
        span = current.high.amount - current.low.amount
        if previous is None:
            return span
        if not isinstance(previous, Candle):
            raise TypeError("previous must be a Candle or None")
        if previous.close.currency != current.close.currency:
            raise ValueError("candles mix currencies")
        prior_close = previous.close.amount
        return max(
            span,
            abs(current.high.amount - prior_close),
            abs(current.low.amount - prior_close),
        )


def atr(candles: Sequence[Candle], period: int) -> Decimal | None:
    """Average true range over ``period`` bars, as a bare ``Decimal``.

    A ``Decimal`` and not a ``Price``: a range is a distance, not a level, and
    typing it as a price invites someone to compare it against one.

    Needs ``period + 1`` bars, because the first true range needs a prior close.
    Uses a **simple mean** of the last ``period`` true ranges rather than Wilder's
    smoothing -- it depends only on the visible window, so the same bars always
    give the same answer regardless of how much history preceded them. Wilder's
    version carries an unbounded dependency on the start of the series, which
    makes a live value and a backtest value differ forever.
    """
    bars = _validated(candles, period)
    if len(bars) < period + 1:
        return None
    ranges = [
        true_range(bars[i], bars[i - 1]) for i in range(len(bars) - period, len(bars))
    ]
    with decimal.localcontext(FINANCIAL_CONTEXT):
        return sum(ranges, Decimal(0)) / Decimal(period)


def rsi(candles: Sequence[Candle], period: int) -> Decimal | None:
    """Relative strength index over ``period`` bars, in ``[0, 100]``.

    Simple means of gains and losses over the window, matching :func:`atr`'s
    reasoning about reproducibility. Needs ``period + 1`` bars.

    When there are no losses in the window the ratio is undefined, and the answer
    is ``100`` -- the limit as losses approach zero. Returning ``None`` there
    would conflate "maximally overbought" with "not enough data", which are
    opposite instructions to a strategy.
    """
    bars = _validated(candles, period)
    if len(bars) < period + 1:
        return None
    window = bars[-(period + 1) :]
    with decimal.localcontext(FINANCIAL_CONTEXT):
        gains, losses = Decimal(0), Decimal(0)
        for earlier, later in zip(window, window[1:]):
            change = later.close.amount - earlier.close.amount
            if change > 0:
                gains += change
            else:
                losses += -change
        if losses == 0:
            return Decimal(100) if gains > 0 else Decimal(50)
        relative_strength = (gains / Decimal(period)) / (losses / Decimal(period))
        return Decimal(100) - (Decimal(100) / (Decimal(1) + relative_strength))


def highest_high(candles: Sequence[Candle], period: int) -> Price | None:
    """Highest high over the last ``period`` bars. For breakout logic."""
    bars = _validated(candles, period)
    if len(bars) < period:
        return None
    window = bars[-period:]
    best = window[0].high
    for bar in window[1:]:
        if bar.high > best:
            best = bar.high
    return best


def lowest_low(candles: Sequence[Candle], period: int) -> Price | None:
    """Lowest low over the last ``period`` bars. For stop placement."""
    bars = _validated(candles, period)
    if len(bars) < period:
        return None
    window = bars[-period:]
    worst = window[0].low
    for bar in window[1:]:
        if bar.low < worst:
            worst = bar.low
    return worst
