"""One supervised cycle of an ingestion-driven paper trading service.

The service is intentionally synchronous: ``poll_once`` is the complete unit of
work and a future process supervisor may schedule it.  It composes existing
components without creating a second execution path; every order still reaches
the venue only through :class:`ExecutionGateway`.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping

from ..adapters.lifecycle import LifecyclePoller, PollReport
from ..adapters.reconciliation import VenueSweepReport
from ..adapters.marketdata import (
    CandleIngestReport,
    CandleIngestor,
    FeedState,
    InMemoryCandleStore,
    IngestReport,
    MarketDataIngestor,
)
from ..core.errors import SafetyViolation
from ..core.marketdata import MarketSnapshot
from ..core.money import Price, Quantity
from ..core.orders import OrderIntent, OrderSide, OrderType
from ..strategy import (
    MarketContext,
    Signal,
    SignalDirection,
    SignalRunner,
    SignalSizer,
    SignalStrategy,
)
from .composition import PaperStack
from .order_policy import MarketableLimitPolicy
from .report import OrderOutcome

__all__ = ["PaperServiceCycle", "RealtimePaperService"]

_ENTRY_SIDE = {
    SignalDirection.LONG: OrderSide.BUY,
    SignalDirection.SHORT: OrderSide.SELL,
}


@dataclass(frozen=True, slots=True)
class PaperServiceCycle:
    at: str
    quotes: IngestReport
    candles: CandleIngestReport
    lifecycle: PollReport
    reconciliation: VenueSweepReport
    signals: tuple[str, ...] = ()
    outcomes: tuple[OrderOutcome, ...] = ()
    skipped_reason: str | None = None
    ready: bool = False

    @property
    def healthy(self) -> bool:
        return (
            self.skipped_reason is None
            and self.quotes.healthy
            and self.candles.healthy
            and self.lifecycle.healthy
        )

    def as_details(self) -> dict[str, object]:
        return {
            "at": self.at,
            "quotes": self.quotes.as_details(),
            "candles": self.candles.as_details(),
            "lifecycle": self.lifecycle.as_details(),
            "reconciliation": self.reconciliation.as_details(),
            "signals": list(self.signals),
            "outcomes": [outcome.as_details() for outcome in self.outcomes],
            "skipped_reason": self.skipped_reason,
            "ready": self.ready,
        }


class RealtimePaperService:
    """Ingest, synchronize, decide, size and submit one safe cycle at a time."""

    def __init__(
        self,
        stack: PaperStack,
        strategy: SignalStrategy,
        *,
        quote_ingestor: MarketDataIngestor,
        candle_ingestor: CandleIngestor,
        candles: InMemoryCandleStore,
        instruments: Mapping[str, str],
        order_policy: MarketableLimitPolicy | None = None,
    ) -> None:
        self._stack = stack
        self._quotes = quote_ingestor
        self._candles = candle_ingestor
        self._store = candles
        self._instruments = dict(instruments)
        if order_policy is not None and not isinstance(
            order_policy, MarketableLimitPolicy
        ):
            raise TypeError("order_policy must be a MarketableLimitPolicy or None")
        self._order_policy = order_policy
        self._runner = SignalRunner(
            strategy, identity=stack.strategy_id, audit=stack.audit, clock=stack.clock
        )
        self._sizer = SignalSizer(stack.risk)
        self._lifecycle = LifecyclePoller(
            gateway=stack.gateway, orders=stack.orders, identity=stack.system_id
        )
        self._cycles = 0
        self._last: PaperServiceCycle | None = None

    @property
    def cycles(self) -> int:
        return self._cycles

    @property
    def last_cycle(self) -> PaperServiceCycle | None:
        return self._last

    def poll_once(self) -> PaperServiceCycle:
        prior_candle_counts = {
            symbol: len(self._store.candles(symbol))
            for symbol in self._store.symbols()
        }
        quote_report = self._quotes.poll_once()
        candle_report = self._candles.poll_once()
        reconciliation = self._stack.reconciliation_coordinator.sweep()
        # Synchronize old exposure before sizing a new decision.
        lifecycle = self._lifecycle.poll_once()
        at = self._stack.clock.now()

        reason = None
        if (
            self._order_policy is not None
            and not self._order_policy.metadata_valid_at(at)
        ):
            reason = "venue market metadata is stale"
        if reason is None:
            reason = self._cycle_block(
                quote_report, candle_report, lifecycle, reconciliation
            )
        if reason is not None:
            return self._record(
                PaperServiceCycle(
                    at=at.isoformat(), quotes=quote_report, candles=candle_report,
                    lifecycle=lifecycle, reconciliation=reconciliation,
                    skipped_reason=reason, ready=False,
                )
            )

        newly_closed = {
            symbol
            for symbol in set(candle_report.accepted)
            if any(
                candle.is_complete(at)
                for candle in self._store.candles(symbol)[
                    prior_candle_counts.get(symbol, 0) :
                ]
            )
        }
        if not newly_closed:
            return self._record(
                PaperServiceCycle(
                    at=at.isoformat(), quotes=quote_report, candles=candle_report,
                    lifecycle=lifecycle, reconciliation=reconciliation,
                    skipped_reason="no new closed candle", ready=False,
                )
            )

        quotes = {
            symbol: quote
            for symbol in self._stack.feed.symbols()
            if (quote := self._stack.feed.quote(symbol)) is not None
        }
        snapshot = MarketSnapshot(as_of=at, quotes=quotes)
        marks = snapshot.mark_prices(self._stack.staleness)
        try:
            equity = self._stack.portfolio.equity(marks)
        except SafetyViolation as exc:
            return self._record(
                PaperServiceCycle(
                    at=at.isoformat(), quotes=quote_report, candles=candle_report,
                    lifecycle=lifecycle, reconciliation=reconciliation,
                    skipped_reason=str(exc), ready=False,
                )
            )

        context = MarketContext(
            as_of=at,
            equity=equity,
            snapshot=snapshot,
            positions=self._stack.portfolio.snapshot(),
            history={symbol: self._store.candles(symbol) for symbol in self._store.symbols()},
            policy=self._stack.staleness,
        )
        signals = [
            signal
            for signal in self._runner.generate(context)
            if signal.symbol in newly_closed
        ]
        outcomes = tuple(self._execute(signal, equity=equity, marks=marks) for signal in signals)
        ready = self._stack.startup.ok and self._stack.monitor.ready()
        return self._record(
            PaperServiceCycle(
                at=at.isoformat(), quotes=quote_report, candles=candle_report,
                lifecycle=lifecycle, reconciliation=reconciliation,
                signals=tuple(s.signal_id for s in signals),
                outcomes=outcomes, ready=ready,
            )
        )

    def _cycle_block(
        self,
        quotes: IngestReport,
        candles: CandleIngestReport,
        lifecycle: PollReport,
        reconciliation: VenueSweepReport,
    ) -> str | None:
        if not self._stack.startup.ok:
            return "startup recovery or reconciliation did not complete cleanly"
        if reconciliation.blocked:
            return "venue-wide reconciliation reported a blocking finding"
        if lifecycle.failed:
            return "order lifecycle synchronization failed"
        if lifecycle.refused:
            return "order lifecycle synchronization was refused"
        if quotes.state != FeedState.CONNECTED.value:
            return f"quote feed is {quotes.state}"
        if candles.state != FeedState.CONNECTED.value:
            return f"candle feed is {candles.state}"
        if not quotes.healthy:
            return "quote ingestion reported rejected data or a sequence gap"
        if not candles.healthy:
            return "candle ingestion reported rejected data or a bar gap"
        return None

    def _execute(
        self, signal: Signal, *, equity, marks: Mapping[str, Price]
    ) -> OrderOutcome:
        if signal.direction.is_entry:
            side = _ENTRY_SIDE[signal.direction]
            asset = self._instruments.get(signal.symbol)
            if asset is None:
                return OrderOutcome.not_sized(
                    signal_id=signal.signal_id, symbol=signal.symbol, side=side.value,
                    reason=f"no base asset configured for {signal.symbol!r}",
                )
            sized = self._sizer.size(signal, equity=equity, asset=asset)
            if not sized.is_tradeable:
                return OrderOutcome.not_sized(
                    signal_id=signal.signal_id, symbol=signal.symbol, side=side.value,
                    reason=sized.reason or "risk sizer declined",
                )
            quantity = sized.quantity
        else:
            held = self._stack.portfolio.snapshot().get(signal.symbol)
            if held is None or held.is_zero:
                return OrderOutcome.not_sized(
                    signal_id=signal.signal_id, symbol=signal.symbol, side="",
                    reason="exit signal arrived while flat",
                )
            side = OrderSide.SELL if held.amount > 0 else OrderSide.BUY
            quantity = Quantity(abs(held.amount), held.asset)

        order_options: dict[str, object] = {}
        if self._order_policy is not None:
            quote = self._stack.feed.quote(signal.symbol)
            if quote is None:
                return OrderOutcome.not_sized(
                    signal_id=signal.signal_id,
                    symbol=signal.symbol,
                    side=side.value,
                    reason="cannot construct protected limit without a quote",
                )
            try:
                quantity, limit_price = self._order_policy.prepare(
                    side, quote, quantity
                )
            except (TypeError, ValueError, ArithmeticError) as exc:
                return OrderOutcome.not_sized(
                    signal_id=signal.signal_id,
                    symbol=signal.symbol,
                    side=side.value,
                    reason=f"cannot construct protected limit: {exc}",
                )
            order_options = {
                "order_type": OrderType.LIMIT,
                "limit_price": limit_price,
            }

        intent = OrderIntent(
            strategy_id=signal.strategy_name,
            signal_id=signal.signal_id,
            symbol=signal.symbol,
            side=side,
            quantity=quantity,
            **order_options,
        )
        result = self._stack.gateway.submit(
            intent, proposer=self._stack.strategy_id, mark_prices=marks
        )
        return OrderOutcome.from_result(
            signal_id=signal.signal_id, symbol=signal.symbol, side=side.value,
            quantity=quantity, result=result,
        )

    def _record(self, cycle: PaperServiceCycle) -> PaperServiceCycle:
        self._cycles += 1
        self._last = cycle
        return cycle
