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
from .order_policy import MarketableLimitPolicy,bounded_exit_quantity
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
        self._strategy = strategy
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
        self._prediction_monitor = None
        self._monitor_alerts = {}
        self._notification = None
        self._runtime_alert = None
        self._exit_plans={}
        for record in stack.audit.records():
            if record.action=='paper.exit_requested':self._exit_plans[record.details['symbol']]=dict(record.details)
            elif record.action=='paper.exit_completed':self._exit_plans.pop(record.details['symbol'],None)
            elif record.action=='paper.exit_blocked' and record.details['symbol'] in self._exit_plans:
                self._exit_plans[record.details['symbol']]['blocked']=True

    @property
    def cycles(self) -> int:
        return self._cycles

    @property
    def last_cycle(self) -> PaperServiceCycle | None:
        return self._last

    @property
    def strategy_name(self) -> str:
        return self._runner.strategy_name

    def poll_once(self) -> PaperServiceCycle:
        with self._stack.bot.lock:
            self._stack.bot.require_running()
            try:
                return self._poll_running()
            except BaseException:
                self._stack.kill_switch.engage(
                    self._stack.system_id, reason='paper cycle failed; inspection required'
                )
                self._notify_operator('Paper cycle failed. Trading is blocked; inspect the local dashboard.')
                raise

    def collect_only(self):
        """Keep observations/outcomes current during pause; no strategy or gateway."""
        with self._stack.bot.lock:
            if self._stack.bot.state=='running':
                return self.poll_once()
            quotes=self._quotes.poll_once();candles=self._candles.poll_once()
            at=self._stack.clock.now()
            self._resolve_predictions(at)
            return self._record(PaperServiceCycle(at=at.isoformat(),quotes=quotes,candles=candles,
                lifecycle=PollReport(),reconciliation=VenueSweepReport(at=at.isoformat(),error='not swept while inactive'),
                skipped_reason='paper bot '+self._stack.bot.state+'; collection only',ready=False))

    def _poll_running(self) -> PaperServiceCycle:
        prior_candle_times = {
            symbol: self._store.last(symbol).open_time
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
            market=self._order_policy.market
            reason = ('verified venue market metadata is missing or unverified'
                if market is None or market.valid_until is None else 'venue market metadata is stale')
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
                candle.is_complete(at) and (symbol not in prior_candle_times or candle.open_time>prior_candle_times[symbol])
                for candle in self._store.candles(symbol)
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
        self._stack.risk.observe_equity(equity)
        self._resolve_predictions(at)
        self._finish_exits()
        if self._prediction_monitor is not None:
            monitoring=self._prediction_monitor()
            if (monitoring.get('alert_policy') or {}).get('on_alert')=='pause' and monitoring['alerts']:
                self._stack.bot.transition('paused',self._stack.operator_id,
                    reason='ML monitoring safety halt; explicit operator review required')
                return self._record(PaperServiceCycle(at=at.isoformat(),quotes=quote_report,candles=candle_report,
                    lifecycle=lifecycle,reconciliation=reconciliation,skipped_reason='ML monitoring safety halt',ready=False))
        for symbol in newly_closed:
            closed = [bar for bar in self._store.candles(symbol) if bar.is_complete(at)]
            if closed:
                self._stack.risk.observe_candle(closed[-1])
        signals = [
            signal
            for signal in self._runner.generate(context)
            if signal.symbol in newly_closed
        ]
        # Continue only a previously audited exit; never invent an entry.
        for symbol,plan in tuple(self._exit_plans.items()):
            if symbol not in newly_closed or plan.get('blocked'):continue
            if any(s.symbol==symbol and not s.direction.is_entry for s in signals):continue
            quote=self._stack.feed.quote(symbol)
            if quote is not None:
                signals.append(Signal(strategy_name=plan['strategy'],signal_id=plan['signal_id'],symbol=symbol,
                    direction=SignalDirection.EXIT,reference_price=quote.mid,as_of=at,
                    rationale='continue audited position exit using a compliant chunk'))
        prediction = getattr(self._strategy, 'last_prediction', None)
        if prediction is not None:
            from ..core.audit import AuditCategory, AuditOutcome
            self._stack.audit.record(
                AuditCategory.SIGNAL, 'ml.prediction', outcome=AuditOutcome.INFO,
                actor=self._stack.strategy_id.principal_id, details=prediction,
            )
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

    def _resolve_predictions(self, at):
        import datetime as dt
        from decimal import Decimal
        from ..core.audit import AuditCategory, AuditOutcome
        records = self._stack.audit.records()
        resolved = {r.details['prediction_id'] for r in records if r.action == 'ml.outcome'}
        for record in records:
            if record.action != 'ml.prediction' or 'prediction_id' not in record.details:
                continue
            prediction = record.details
            if prediction['prediction_id'] in resolved:
                continue
            opened = dt.datetime.fromisoformat(prediction['bar_open_time'])
            horizon_seconds = prediction['horizon_seconds']
            if type(horizon_seconds) is not int or horizon_seconds not in {60,300,900,1800}:
                raise SafetyViolation('persisted prediction horizon is invalid')
            bars = {bar.open_time:bar for bar in self._store.candles(prediction['symbol'])}
            required = [bars.get(opened + dt.timedelta(seconds=offset))
                        for offset in range(60, horizon_seconds+1, 60)]
            if any(bar is None or bar.interval_seconds != 60 or not bar.is_complete(at) for bar in required):
                continue
            next_bar = required[-1]
            label = int(next_bar.close.amount > Decimal(prediction['reference_close']))
            self._stack.audit.record(AuditCategory.SIGNAL, 'ml.outcome', outcome=AuditOutcome.INFO,
                actor=self._stack.system_id.principal_id,
                details={'prediction_id':prediction['prediction_id'], 'label':label,
                         'up_probability':prediction['up_probability'],
                         'threshold':prediction['threshold'],
                         'model_sha256':prediction['model_sha256'],
                         'target_close_time':next_bar.close_time.isoformat()})

    def _execute(
        self, signal: Signal, *, equity, marks: Mapping[str, Price]
    ) -> OrderOutcome:
        if signal.direction.is_entry:
            if signal.symbol in self._exit_plans:
                return OrderOutcome.not_sized(signal_id=signal.signal_id,symbol=signal.symbol,side='',reason='position exit remains pending or blocked')
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
            self._finish_exits()
            held = self._stack.portfolio.snapshot().get(signal.symbol)
            if held is None or held.is_zero:
                return OrderOutcome.not_sized(
                    signal_id=signal.signal_id, symbol=signal.symbol, side="",
                    reason="exit signal arrived while flat",
                )
            side = OrderSide.SELL if held.amount > 0 else OrderSide.BUY
            quantity = Quantity(abs(held.amount), held.asset)
            plan=self._exit_plans.get(signal.symbol)
            if plan is not None and plan.get('blocked'):
                return OrderOutcome.not_sized(signal_id=signal.signal_id,symbol=signal.symbol,side=side.value,
                    reason='audited exit is blocked; operator review required')
            if plan is None:
                plan={'symbol':signal.symbol,'strategy':signal.strategy_name,'signal_id':signal.signal_id,
                      'side':side.value,'blocked':False}
                self._exit_event('paper.exit_requested',plan)
                self._exit_plans[signal.symbol]=plan
            if plan['side']!=side.value:raise SafetyViolation('pending exit position changed direction')
            if any(order.symbol==signal.symbol for order in self._stack.orders.open_orders()):
                return OrderOutcome.not_sized(signal_id=signal.signal_id,symbol=signal.symbol,side=side.value,
                    reason='existing open or uncertain order must resolve before another exit')
            try:
                quote=self._stack.feed.quote(signal.symbol)
                limit=self._order_policy.limit_price(side,quote) if self._order_policy is not None and quote is not None else None
                valuation=marks.get(signal.symbol)
                if valuation is None:raise ValueError('fresh exit valuation unavailable')
                quantity=bounded_exit_quantity(quantity,ceiling=self._stack.risk.config.max_order_notional,
                    valuation_price=valuation,limit_price=limit,
                    market=self._order_policy.market if self._order_policy is not None else None)
            except (ValueError,TypeError,ArithmeticError) as exc:
                self._block_exit(plan,str(exc))
                return OrderOutcome.not_sized(signal_id=signal.signal_id,symbol=signal.symbol,side=side.value,reason=str(exc))

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
                if not signal.direction.is_entry:self._block_exit(plan,str(exc))
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
            signal_id=(signal.signal_id if signal.direction.is_entry else
                plan['signal_id']+':exit-chunk:'+str(sum(o.intent.signal_id.startswith(plan['signal_id']+':exit-chunk:')
                    and o.symbol==signal.symbol for o in self._stack.orders.all_orders()))),
            symbol=signal.symbol,
            side=side,
            quantity=quantity,
            **order_options,
        )
        result = self._stack.gateway.submit(
            intent, proposer=self._stack.strategy_id, mark_prices=marks
        )
        if not signal.direction.is_entry:
            if not result.is_executed:self._block_exit(plan,result.reason or result.outcome)
            self._finish_exits()
        return OrderOutcome.from_result(
            signal_id=signal.signal_id, symbol=signal.symbol, side=side.value,
            quantity=quantity, result=result,
        )

    def _exit_event(self,action,details,actor=None):
        from ..core.audit import AuditCategory,AuditOutcome
        self._stack.audit.record(AuditCategory.ORDER,action,outcome=AuditOutcome.INFO,
            actor=actor or self._stack.system_id.principal_id,details=details)

    def retry_exit(self,symbol,principal,*,reason):
        """Operator re-arms a blocked plan while inactive; never submits/resumes."""
        from ..core.authz import Action,authorize
        authorize(principal,Action.RELEASE_KILL_SWITCH)
        with self._stack.bot.lock:
            if self._stack.bot.state=='running':raise SafetyViolation('pause before reviewing an exit retry')
            if not isinstance(reason,str) or not reason.strip():raise ValueError('exit review requires a reason')
            plan={**self._exit_plans[symbol],'blocked':False,'review_reason':reason}
            self._exit_event('paper.exit_requested',plan,actor=principal.principal_id)
            self._exit_plans[symbol]=plan

    def _block_exit(self,plan,reason):
        if not plan.get('blocked'):
            self._exit_event('paper.exit_blocked',{'symbol':plan['symbol'],'reason':reason})
            plan['blocked']=True

    def _finish_exits(self):
        positions=self._stack.portfolio.snapshot()
        for symbol,plan in tuple(self._exit_plans.items()):
            held=positions.get(symbol)
            if (held is None or held.is_zero) and not any(o.symbol==symbol for o in self._stack.orders.open_orders()):
                self._exit_event('paper.exit_completed',{'symbol':symbol,'signal_id':plan['signal_id']})
                del self._exit_plans[symbol]

    def _record(self, cycle: PaperServiceCycle) -> PaperServiceCycle:
        from ..core.audit import AuditCategory, AuditOutcome
        issue=cycle.skipped_reason if cycle.skipped_reason != 'no new closed candle' else None
        if issue != self._runtime_alert:
            self._stack.audit.record(AuditCategory.SYSTEM,'paper.runtime_alert',
                outcome=AuditOutcome.INFO,actor=self._stack.system_id.principal_id,
                details={'status':'active' if issue else 'recovered','reason':issue})
            if issue:
                self._notify_operator('Paper bot requires review. Inspect the local dashboard.')
            self._runtime_alert=issue
        if self._prediction_monitor is not None:
            monitoring=self._prediction_monitor()
            active={(alert['code'],alert.get('feature')):alert for alert in monitoring['alerts']}
            new=set(active)-set(self._monitor_alerts)
            for key in sorted(set(active) | set(self._monitor_alerts), key=str):
                if (key in active) == (key in self._monitor_alerts):
                    continue
                self._stack.audit.record(AuditCategory.SYSTEM,'ml.monitor_alert',
                    outcome=AuditOutcome.INFO,actor=self._stack.system_id.principal_id,
                    details={'model_sha256':monitoring['model_sha256'],'code':key[0],
                             'feature':key[1],'status':'active' if key in active else 'recovered'})
            self._monitor_alerts=active
            if new:
                self._notify_operator('Paper model monitoring requires review. No model was changed.')
        if self._stack.persistence is not None:
            import json
            from ..adapters.http.dashboard import OperatorDashboard
            # Reuse the same valuation rule as the operator view.
            view = OperatorDashboard(self._stack, type('Idle', (), {'status': lambda _: None})())
            portfolio = view._portfolio_state(self._stack.portfolio.open_positions())
            state = self._stack.persistence
            with state.transaction():
                state.connection.execute(
                    'INSERT INTO equity_snapshots(payload) VALUES(?)',
                    (json.dumps({'at': cycle.at, **portfolio}),),
                )
        self._cycles += 1
        self._last = cycle
        return cycle

    def _notify_operator(self,message):
        if self._notification is None:
            return
        delivered=False
        try:
            delivered=bool(self._notification(message))
        except Exception:
            pass
        if not delivered:
            from ..core.audit import AuditCategory, AuditOutcome
            self._stack.audit.record(AuditCategory.SYSTEM,'operator.notification_failed',
                outcome=AuditOutcome.ERROR,actor=self._stack.system_id.principal_id,
                details={'delivery':'local desktop','message':'operator notification could not be delivered'})
