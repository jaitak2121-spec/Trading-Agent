"""A reference signal strategy, so the interface is proven rather than described.

One strategy, kept simple on purpose. Its job here is not to make money: it is to
demonstrate that a real decision can be expressed as a :class:`Signal` without the
strategy knowing anything about position size, account equity, order routing, or
permission -- and to be something the sizing, advisory, and paper-trading
subsystems can be tested against end to end.

The properties worth copying into a real strategy:

* **Deterministic signal ids.** The id is derived from the strategy, symbol, bar
  time, and direction, so re-deciding on the same closed bar produces the same id
  and therefore the same idempotency key downstream. A process that restarts and
  re-runs the last bar must not create a second order (INVARIANT 12), and that
  property starts here -- a random or timestamp-based id defeats it before the
  registry is ever consulted.
* **No decision without complete data.** Missing bars, a stale quote, or a flat
  market all produce no signal rather than a guess.
* **Every signal carries its evidence.** The indicator readings that justified it
  travel with it, so an operator reading advisory output or an audit record can
  reconstruct the reasoning rather than take it on trust.
"""

from __future__ import annotations

import decimal
from decimal import Decimal
from typing import Sequence

from ..core.money import FINANCIAL_CONTEXT, ROUND_DOWN, Price, to_decimal
from .context import MarketContext
from .indicators import atr, sma
from .signals import Signal, SignalDirection, SignalStrategy

__all__ = ["MovingAverageCrossover"]

#: A spread this wide means the book is thin enough that the mid is a poor guide
#: to what an order would actually pay. Not a refusal -- a warning the operator
#: sees, because thin books are normal in some markets and a hard block would be
#: wrong there.
WIDE_SPREAD_BPS = Decimal("50")

#: The finest level a :class:`~trading.core.money.Price` can express. A stop at
#: or below this is not a stop, it is a rounding artefact.
SMALLEST_PRICE = Decimal("1E-12")


class MovingAverageCrossover(SignalStrategy):
    """Long when the fast mean crosses above the slow one; exit when it crosses back.

    Stops are placed at a multiple of ATR below the reference price, so stop
    distance scales with realised volatility rather than being a fixed percentage
    that is far too tight in a fast market and far too loose in a quiet one. The
    target is expressed as a multiple of that risk, which means the position's
    reward-to-risk ratio is a property of the strategy configuration rather than
    an accident of where price happens to be.

    Long-only. Shorting is not a small extension -- it brings borrow, funding, and
    an unbounded loss profile -- so it is left out rather than half-implemented.
    """

    name = "ma-crossover"

    def __init__(
        self,
        *,
        fast: int = 10,
        slow: int = 30,
        atr_period: int = 14,
        stop_atr_multiple: Decimal | str = "2",
        reward_multiple: Decimal | str = "2",
        symbols: Sequence[str] | None = None,
    ) -> None:
        for label, value in (("fast", fast), ("slow", slow), ("atr_period", atr_period)):
            if isinstance(value, bool) or not isinstance(value, int):
                raise TypeError(f"{label} must be an int")
            if value <= 0:
                raise ValueError(f"{label} must be positive")
        if fast >= slow:
            raise ValueError(
                f"fast ({fast}) must be shorter than slow ({slow}); otherwise the "
                "crossover has no meaning"
            )
        self._fast = fast
        self._slow = slow
        self._atr_period = atr_period
        self._stop_multiple = to_decimal(stop_atr_multiple)
        self._reward_multiple = to_decimal(reward_multiple)
        if self._stop_multiple <= 0 or self._reward_multiple <= 0:
            raise ValueError("stop and reward multiples must be positive")
        self._symbols = tuple(symbols) if symbols is not None else None

    @property
    def required_bars(self) -> int:
        """Closed bars needed before this strategy will say anything.

        ``slow + 1`` for the previous crossover state, ``atr_period + 1`` for the
        first true range. Exposed so a caller can tell "no signal" from "not
        enough history yet" -- they look identical from the outside and mean very
        different things.
        """
        return max(self._slow + 1, self._atr_period + 1)

    def generate(self, context: MarketContext) -> list[Signal]:
        symbols = self._symbols if self._symbols is not None else context.tradable_symbols
        signals: list[Signal] = []
        for symbol in symbols:
            signal = self._for_symbol(context, symbol)
            if signal is not None:
                signals.append(signal)
        return signals

    def _for_symbol(self, context: MarketContext, symbol: str) -> Signal | None:
        quote = context.quote(symbol)
        if quote is None:
            return None  # stale or absent: not a decision we are able to make
        bars = context.candles(symbol)
        if len(bars) < self.required_bars:
            return None

        fast_now, slow_now = sma(bars, self._fast), sma(bars, self._slow)
        fast_before, slow_before = sma(bars[:-1], self._fast), sma(bars[:-1], self._slow)
        if None in (fast_now, slow_now, fast_before, slow_before):
            return None  # unreachable given required_bars, but do not assume it

        crossed_up = fast_before < slow_before and fast_now > slow_now
        crossed_down = fast_before > slow_before and fast_now < slow_now

        evidence = {
            f"sma_{self._fast}": str(fast_now.amount),
            f"sma_{self._slow}": str(slow_now.amount),
            "bar_open_time": bars[-1].open_time.isoformat(),
            "bars_available": str(len(bars)),
        }

        if crossed_down and not context.is_flat(symbol):
            return self._signal(
                context,
                symbol,
                SignalDirection.EXIT,
                quote.mid,
                rationale=(
                    f"the {self._fast}-period mean crossed below the "
                    f"{self._slow}-period mean, so the trend that justified the "
                    "position is no longer present"
                ),
                evidence=evidence,
                quote=quote,
            )

        if not crossed_up or not context.is_flat(symbol):
            return None

        volatility = atr(bars, self._atr_period)
        if volatility is None or volatility == 0:
            # A zero range means every bar in the window opened, closed, and
            # traded at one price. There is no volatility to place a stop
            # against, so there is no risk-defined position to take.
            return None

        reference = quote.mid
        with decimal.localcontext(FINANCIAL_CONTEXT):
            risk = volatility * self._stop_multiple
            stop_amount = reference.amount - risk
            target_amount = reference.amount + risk * self._reward_multiple
        if stop_amount <= SMALLEST_PRICE:
            # The volatility-derived stop sits at or below the smallest price
            # expressible. Clamping it to something positive would silently
            # invent a risk figure, so decline instead.
            return None
        # ROUND_DOWN on both levels is the direction that never flatters the
        # trade: it moves the stop further from entry, so risk-per-unit is never
        # understated, and the target closer, so reward is never overstated.
        stop = Price.rounded(stop_amount, reference.currency, rounding=ROUND_DOWN)
        target = Price.rounded(target_amount, reference.currency, rounding=ROUND_DOWN)
        if not stop < reference or not target > reference:
            # Rounding collapsed a level onto the reference price: the move is
            # finer than a price can express, so there is no risk-defined trade.
            return None

        evidence[f"atr_{self._atr_period}"] = str(volatility)
        return self._signal(
            context,
            symbol,
            SignalDirection.LONG,
            reference,
            rationale=(
                f"the {self._fast}-period mean crossed above the "
                f"{self._slow}-period mean while flat; stop placed "
                f"{self._stop_multiple}x ATR({self._atr_period}) below the mid, "
                f"target at {self._reward_multiple}x that risk"
            ),
            evidence=evidence,
            quote=quote,
            stop_loss=stop,
            take_profit=target,
        )

    def _signal(
        self,
        context: MarketContext,
        symbol: str,
        direction: SignalDirection,
        reference: Price,
        *,
        rationale: str,
        evidence: dict[str, str],
        quote,
        stop_loss: Price | None = None,
        take_profit: Price | None = None,
    ) -> Signal:
        warnings: list[str] = []
        if quote.spread_bps > WIDE_SPREAD_BPS:
            warnings.append(
                f"spread is {quote.spread_bps.quantize(Decimal('0.1'))} bps; the "
                "mid may overstate what this order would actually get filled at"
            )
        # No "unclosed bar" warning is needed here: context.candles() only ever
        # returns bars that had closed by context.as_of, so an in-progress bar
        # cannot have reached this point.
        return Signal(
            strategy_name=self.name,
            signal_id=self._signal_id(symbol, direction, context),
            symbol=symbol,
            direction=direction,
            reference_price=reference,
            as_of=context.as_of,
            rationale=rationale,
            evidence=evidence,
            warnings=tuple(warnings),
            stop_loss=stop_loss,
            take_profit=take_profit,
        )

    def _signal_id(
        self, symbol: str, direction: SignalDirection, context: MarketContext
    ) -> str:
        """Derived from the bar, not the wall clock.

        This is what makes a re-run on the same closed bar idempotent all the way
        down to the gateway's duplicate check.
        """
        bar_time = context.candles(symbol)[-1].open_time.isoformat()
        return f"{self.name}:{symbol}:{bar_time}:{direction.value}"
