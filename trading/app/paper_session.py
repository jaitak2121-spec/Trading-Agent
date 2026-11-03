"""The paper-trading session: the loop that turns market data into orders.

This is the driver that was missing. Every piece it needs already existed -- a
strategy that emits :class:`~trading.strategy.signals.Signal` objects, a
:class:`~trading.strategy.sizing.SignalSizer` that turns a signal into a sized
quantity under the risk engine's limits, an
:class:`~trading.core.gateway.ExecutionGateway` that runs the safety chain, a
:class:`~trading.adapters.paper.broker.PaperBroker` that fills deterministically,
and a :class:`~trading.adapters.lifecycle.LifecyclePoller` that carries a later
fill home. What did not exist was anything that connected them. This does, and
nothing else: it holds no broker reference and mints no token, so an order's
only route to the venue is still ``gateway.submit``.

One frame at a time
===================

A session replays a sequence of :class:`MarketFrame` objects. Each frame is one
closed bar per symbol plus the quote in force as it closed. For each frame the
session:

1. advances the clock by the bar interval, so each bar has a distinct close time
   (and therefore a distinct, deterministic signal id downstream -- the property
   that keeps a replayed bar from creating a second order, INVARIANT 12);
2. publishes the quotes, appends the closed candles, and assembles a
   :class:`~trading.core.marketdata.MarketSnapshot` and a
   :class:`~trading.strategy.context.MarketContext`;
3. asks the strategy for signals;
4. sizes each entry under the risk engine and submits the result through the
   gateway; closes a position for each exit;
5. offers every still-open order to ``sync_order`` via the poller, so a resting
   limit the market has since reached is carried into the portfolio.

Because it replays a provided series bar by bar, the session drives a
:class:`~trading.core.clock.ManualClock` (the composition default). Real-time
operation against a live feed is a later milestone; this one is deterministic
replay, which is exactly what paper trading and backtesting both need.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping, Sequence

from ..adapters.lifecycle import LifecyclePoller
from ..core.marketdata import Candle, MarketSnapshot, Quote
from ..core.money import Price, Quantity
from ..core.orders import OrderIntent, OrderSide
from ..strategy import (
    MarketContext,
    Signal,
    SignalDirection,
    SignalRunner,
    SignalSizer,
    SignalStrategy,
)
from .composition import PaperStack
from .report import OrderOutcome, SessionReport

__all__ = ["MarketFrame", "PaperTradingSession", "SymbolBar"]

#: Entry direction to order side. ``EXIT`` has no intrinsic side -- it is sized
#: from the position held -- so it is handled separately, never through this map.
_ENTRY_SIDE: Mapping[SignalDirection, OrderSide] = {
    SignalDirection.LONG: OrderSide.BUY,
    SignalDirection.SHORT: OrderSide.SELL,
}


@dataclass(frozen=True)
class SymbolBar:
    """One symbol's closed OHLCV bar, plus the two-sided quote at its close.

    The quote and the bar are carried together because a strategy decides on the
    closed bar but an order fills against the live quote; keeping them in one
    object means a frame cannot describe a bar without the price it would trade
    at. All prices are :class:`~trading.core.money.Price` and the volume a
    :class:`~trading.core.money.Quantity`; the underlying
    :class:`~trading.core.marketdata.Candle` and
    :class:`~trading.core.marketdata.Quote` validate coherence (no crossed book,
    OHLC within range) on construction.
    """

    open: Price
    high: Price
    low: Price
    close: Price
    volume: Quantity
    bid: Price
    ask: Price


@dataclass(frozen=True)
class MarketFrame:
    """One time step of the replay: the bar that just closed for each symbol."""

    interval_seconds: int
    bars: Mapping[str, SymbolBar]


class PaperTradingSession:
    """Drives a strategy against the paper venue over a replayed market series."""

    def __init__(
        self,
        stack: PaperStack,
        strategy: SignalStrategy,
        *,
        instruments: Mapping[str, str],
        poll_resting: bool = True,
    ) -> None:
        """Wire a session around an already-built :class:`PaperStack`.

        ``instruments`` maps each tradable symbol to its base asset (e.g.
        ``{"BTCUSD": "BTC"}``). It is required and never guessed: no rule in this
        system splits a symbol into a base asset, and a wrong split would mislabel
        the quantity of every order. A signal for a symbol absent from this map is
        reported as un-sizeable rather than sized against a guess.
        """
        clock = stack.clock
        advance = getattr(clock, "advance", None)
        if not callable(advance):
            raise TypeError(
                "PaperTradingSession replays a series bar by bar and needs a clock "
                "it can advance (e.g. ManualClock, the composition default); "
                "real-time operation against a live clock is a later milestone"
            )

        self._stack = stack
        self._clock = clock
        self._advance = advance
        self._portfolio = stack.portfolio
        self._gateway = stack.gateway
        self._feed = stack.feed
        self._staleness = stack.staleness
        self._instruments = dict(instruments)
        self._poll_resting = poll_resting
        self._starting_cash = stack.portfolio.cash

        self._signal_runner = SignalRunner(
            strategy, identity=stack.strategy_id, audit=stack.audit, clock=clock
        )
        self._sizer = SignalSizer(stack.risk)
        self._poller = LifecyclePoller(
            gateway=stack.gateway, orders=stack.orders, identity=stack.system_id
        )

        self._history: dict[str, list[Candle]] = {}
        self._last_snapshot: MarketSnapshot | None = None
        self._outcomes: list[OrderOutcome] = []
        self._ticks = 0

    # -- the loop ---------------------------------------------------------

    def run(self, frames: Sequence[MarketFrame]) -> SessionReport:
        """Replay every frame in order and return the session's report."""
        for frame in frames:
            self.step(frame)
        return self.report()

    def step(self, frame: MarketFrame) -> None:
        """Process one market frame end to end."""
        open_time = self._clock.now()
        self._advance(frame.interval_seconds)
        close_time = self._clock.now()

        quotes: dict[str, Quote] = {}
        for symbol, bar in frame.bars.items():
            self._feed.publish(symbol, bar.bid, bar.ask)
            quote = self._feed.quote(symbol)
            if quote is not None:
                quotes[symbol] = quote
            candle = Candle(
                symbol=symbol,
                open_time=open_time,
                interval_seconds=frame.interval_seconds,
                open=bar.open,
                high=bar.high,
                low=bar.low,
                close=bar.close,
                volume=bar.volume,
                source="paper",
            )
            self._history.setdefault(symbol, []).append(candle)

        snapshot = MarketSnapshot(as_of=close_time, quotes=quotes)
        self._last_snapshot = snapshot
        marks = snapshot.mark_prices(self._staleness)
        equity = self._equity(marks)

        context = MarketContext(
            as_of=close_time,
            equity=equity,
            snapshot=snapshot,
            positions=self._portfolio.snapshot(),
            history={symbol: tuple(bars) for symbol, bars in self._history.items()},
            policy=self._staleness,
        )

        for signal in self._signal_runner.generate(context):
            self._handle_signal(signal, equity=equity, marks=marks)

        # Carry any still-open order forward. For market orders this is a no-op
        # (they are terminal once filled); it is what lets a resting limit the
        # market later reaches reach the portfolio, through the gateway.
        if self._poll_resting:
            self._poller.poll_once()

        self._ticks += 1

    # -- one signal -------------------------------------------------------

    def _handle_signal(
        self, signal: Signal, *, equity, marks: Mapping[str, Price]
    ) -> None:
        symbol = signal.symbol

        if signal.direction.is_entry:
            side = _ENTRY_SIDE[signal.direction]
            asset = self._instruments.get(symbol)
            if asset is None:
                self._outcomes.append(
                    OrderOutcome.not_sized(
                        signal_id=signal.signal_id,
                        symbol=symbol,
                        side=side.value,
                        reason=(
                            f"no base asset configured for {symbol!r}; cannot size "
                            "without one, and this system never guesses a base asset "
                            "from a ticker"
                        ),
                    )
                )
                return

            result = self._sizer.size(signal, equity=equity, asset=asset)
            if not result.is_tradeable:
                self._outcomes.append(
                    OrderOutcome.not_sized(
                        signal_id=signal.signal_id,
                        symbol=symbol,
                        side=side.value,
                        reason=result.reason
                        or f"risk sizer declined ({result.binding_constraint.value})",
                    )
                )
                return

            quantity = result.quantity
            intent = OrderIntent(
                strategy_id=signal.strategy_name,
                signal_id=signal.signal_id,
                symbol=symbol,
                side=side,
                quantity=quantity,
            )
        else:
            # EXIT: close whatever is held. Sizing is from the position, not the
            # risk budget -- an exit that refused to size would leave a position
            # open while reading as "nothing to do".
            held = self._portfolio.snapshot().get(symbol)
            if held is None or held.is_zero:
                return  # nothing to close; not an order and not worth reporting
            side = OrderSide.SELL if held.amount > 0 else OrderSide.BUY
            quantity = Quantity(abs(held.amount), held.asset)
            intent = OrderIntent(
                strategy_id=signal.strategy_name,
                signal_id=signal.signal_id,
                symbol=symbol,
                side=side,
                quantity=quantity,
            )

        result = self._gateway.submit(
            intent, proposer=self._stack.strategy_id, mark_prices=marks
        )
        self._outcomes.append(
            OrderOutcome.from_result(
                signal_id=signal.signal_id,
                symbol=symbol,
                side=side.value,
                quantity=quantity,
                result=result,
            )
        )

    # -- reporting --------------------------------------------------------

    def report(self) -> SessionReport:
        """Build the read-only report of everything the session has done so far."""
        marks: Mapping[str, Price] = {}
        if self._last_snapshot is not None:
            marks = self._last_snapshot.mark_prices(self._staleness)
        return SessionReport.build(
            portfolio=self._portfolio,
            marks=marks,
            status=self._stack.monitor.status(),
            outcomes=self._outcomes,
            ticks=self._ticks,
            starting_cash=self._starting_cash,
        )

    @property
    def outcomes(self) -> tuple[OrderOutcome, ...]:
        return tuple(self._outcomes)

    # -- internals --------------------------------------------------------

    def _equity(self, marks: Mapping[str, Price]):
        """Account equity at ``marks``, marking the legs we can price.

        Uses the portfolio's own ``equity`` when every open position is marked;
        otherwise marks the legs that have a price and leaves the rest at cost,
        so a momentarily missing quote degrades the figure rather than raising.
        """
        open_positions = self._portfolio.open_positions()
        if open_positions and all(p.symbol in marks for p in open_positions):
            return self._portfolio.equity(marks)
        equity = self._portfolio.cash
        for pos in open_positions:
            mark = marks.get(pos.symbol)
            if mark is not None:
                equity = equity + pos.market_value(mark)
        return equity
