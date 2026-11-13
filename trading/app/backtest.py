"""Deterministic, research-only historical backtesting.

The engine deliberately does not use an execution gateway: it has no broker,
credentials, network transport, or live-order method.  It reuses the production
``SignalStrategy`` interface while maintaining a small, explicit simulation
ledger for fees and historical fills.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import random
from dataclasses import asdict, dataclass
from decimal import Decimal, ROUND_DOWN, localcontext
from pathlib import Path
from typing import Mapping, Sequence

from ..core.clock import UTC
from ..core.config import RiskConfig
from ..core.marketdata import Candle, MarketSnapshot, Quote
from ..core.money import FINANCIAL_CONTEXT, Money, Price, Quantity, USDT, to_decimal
from ..strategy.context import MarketContext
from ..strategy.signals import Signal, SignalDirection, SignalStrategy

__all__ = [
    "BacktestConfig",
    "BacktestFold",
    "BacktestResult",
    "BacktestTrade",
    "ChronologicalBacktestPlan",
    "HistoricalBacktester",
    "load_historical_candles",
    "run_development_walk_forward",
    "run_contiguous_segment_backtests",
    "write_backtest_report",
]


def _decimal(value: object, name: str) -> Decimal:
    number = to_decimal(value, field=name)
    if number < 0:
        raise ValueError(f"{name} must not be negative")
    return number


def _canonical_hash(value: object) -> str:
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()


def _text(value: Decimal | None) -> str | None:
    return None if value is None else str(value)


@dataclass(frozen=True, slots=True)
class BacktestConfig:
    """Execution and account assumptions for one reproducible simulation."""

    symbol: str = "BTC/USDT"
    asset: str = "BTC"
    starting_cash: Decimal = Decimal("10000")
    fee_bps: Decimal = Decimal("10")
    slippage_bps: Decimal = Decimal("2")
    spread_bps: Decimal = Decimal("2")
    lot_step: Decimal = Decimal("0.00000001")
    execution_timing: str = "next_open"
    liquidate_at_end: bool = True

    def __post_init__(self) -> None:
        if not self.symbol or not self.asset:
            raise ValueError("symbol and asset must be non-empty")
        for name in ("starting_cash", "fee_bps", "slippage_bps", "spread_bps", "lot_step"):
            value = _decimal(getattr(self, name), name)
            object.__setattr__(self, name, value)
        if self.starting_cash <= 0 or self.lot_step <= 0:
            raise ValueError("starting_cash and lot_step must be positive")
        if self.execution_timing not in {"next_open", "next_close"}:
            raise ValueError("execution_timing must be 'next_open' or 'next_close'")
        if not isinstance(self.liquidate_at_end, bool):
            raise TypeError("liquidate_at_end must be a bool")

    def as_details(self) -> dict[str, object]:
        return {
            "symbol": self.symbol,
            "asset": self.asset,
            "starting_cash": str(self.starting_cash),
            "fee_bps": str(self.fee_bps),
            "slippage_bps": str(self.slippage_bps),
            "spread_bps": str(self.spread_bps),
            "lot_step": str(self.lot_step),
            "execution_timing": self.execution_timing,
            "liquidate_at_end": self.liquidate_at_end,
        }


@dataclass(frozen=True, slots=True)
class BacktestTrade:
    entry_time: str
    exit_time: str
    quantity: str
    entry_price: str
    exit_price: str
    entry_fee: str
    exit_fee: str
    slippage_cost: str
    net_pnl: str
    net_return: str
    exit_reason: str
    signal_id: str


@dataclass(frozen=True, slots=True)
class BacktestResult:
    report: Mapping[str, object]

    @property
    def metrics(self) -> Mapping[str, object]:
        value = self.report["metrics"]
        assert isinstance(value, Mapping)
        return value


@dataclass(frozen=True, slots=True)
class BacktestFold:
    """A development-only chronological fold; training bars only warm indicators."""

    fold_id: str
    train_start_ms: int
    train_end_ms: int
    validation_start_ms: int
    validation_end_ms: int

    def __post_init__(self) -> None:
        if not self.fold_id:
            raise ValueError("fold_id must be non-empty")
        if not (
            self.train_start_ms < self.train_end_ms
            <= self.validation_start_ms < self.validation_end_ms
        ):
            raise ValueError("fold boundaries must be chronological and non-overlapping")


@dataclass(frozen=True, slots=True)
class ChronologicalBacktestPlan:
    """Boundaries that keep development folds outside the sealed test period."""

    development_start_ms: int
    development_end_ms: int
    sealed_test_start_ms: int
    sealed_test_end_ms: int

    def __post_init__(self) -> None:
        if not (
            self.development_start_ms < self.development_end_ms
            <= self.sealed_test_start_ms < self.sealed_test_end_ms
        ):
            raise ValueError("development and sealed-test boundaries must be chronological")

    def require_development_fold(self, fold: BacktestFold) -> None:
        if not (
            self.development_start_ms <= fold.train_start_ms
            and fold.validation_end_ms <= self.development_end_ms
        ):
            raise ValueError(f"fold {fold.fold_id} crosses outside the development period")


def load_historical_candles(
    path: str | Path, *, symbol: str = "BTC/USDT"
) -> tuple[list[Candle], dict[str, str]]:
    """Load a genuine CoinSwitch candle artifact, allowing real market gaps."""
    source_path = Path(path)
    raw = source_path.read_bytes()
    dataset = json.loads(raw, parse_float=str)
    if not isinstance(dataset, Mapping):
        raise ValueError("historical dataset must be an object")
    if dataset.get("schema") != "coinswitch-public-candles/v1":
        raise ValueError("unsupported historical dataset schema")
    if not (
        dataset.get("public_data_only") is True
        and dataset.get("credentials_used") is True
        and dataset.get("live_trading") is False
        and dataset.get("training_only") is True
    ):
        raise ValueError("historical dataset safety markers are missing")
    source = dataset.get("source")
    if not isinstance(source, Mapping) or not (
        source.get("provider") == "CoinSwitch"
        and source.get("access") == "authenticated-read-only"
    ):
        raise ValueError("dataset is not authenticated CoinSwitch historical data")
    if str(dataset.get("symbol", "")).upper() != symbol.upper():
        raise ValueError("historical dataset symbol does not match backtest")
    rows = dataset.get("candles")
    if not isinstance(rows, list) or not rows:
        raise ValueError("historical dataset contains no candles")

    candles: list[Candle] = []
    seen: set[int] = set()
    previous = -1
    for row in rows:
        if not isinstance(row, Mapping):
            raise ValueError("historical candle must be an object")
        opened = int(str(row["open_time"]))
        if opened in seen or opened <= previous:
            raise ValueError("historical candles must be unique and strictly chronological")
        seen.add(opened)
        previous = opened
        interval = int(str(row["interval_seconds"]))
        candle = Candle(
            symbol=symbol,
            open_time=dt.datetime.fromtimestamp(opened / 1000, tz=UTC),
            interval_seconds=interval,
            open=Price(str(row["open"]), USDT),
            high=Price(str(row["high"]), USDT),
            low=Price(str(row["low"]), USDT),
            close=Price(str(row["close"]), USDT),
            # Venue volume precision is data, not an order lot size.  Preserve
            # it rather than applying BTC's execution precision to observations.
            volume=Quantity(str(row["volume"]), symbol.split("/")[0], max_scale=18),
            source=str(row.get("source", "coinswitch-rest-candles")),
        )
        candles.append(candle)
    candle_rows = [dict(row) for row in rows]
    hashes = {
        "source_file_sha256": hashlib.sha256(raw).hexdigest(),
        "candle_sha256": _canonical_hash(candle_rows),
    }
    from .dataset_eligibility import assess_dataset
    eligibility=assess_dataset(dataset)
    hashes['qualification_eligible']='true' if eligibility['eligible_for_strategy_qualification'] else 'false'
    hashes['eligibility_evidence_sha256']=_canonical_hash(eligibility)
    stored = dataset.get("integrity")
    if isinstance(stored, Mapping) and stored.get("candle_sha256") not in (None, hashes["candle_sha256"]):
        raise ValueError("stored candle hash does not match dataset content")
    return candles, hashes


class HistoricalBacktester:
    """Long-only deterministic simulator for existing signal strategies."""

    def __init__(
        self,
        strategy: SignalStrategy,
        *,
        config: BacktestConfig | None = None,
        risk: RiskConfig | None = None,
    ) -> None:
        if not isinstance(strategy, SignalStrategy):
            raise TypeError("strategy must implement SignalStrategy")
        self.strategy = strategy
        self.config = config or BacktestConfig()
        self.risk = risk or RiskConfig(
            base_currency=USDT,
            max_order_notional=Money("1000", USDT),
            max_position_notional=Money("1000", USDT),
            max_gross_exposure=Money("1000", USDT),
            max_daily_loss=Money("100", USDT),
        )
        if self.risk.base_currency != USDT:
            raise ValueError("BTC/USDT backtests require USDT risk limits")

    def run(
        self,
        candles: Sequence[Candle],
        *,
        dataset_hashes: Mapping[str, str] | None = None,
        trade_start_ms: int | None = None,
        trade_end_ms: int | None = None,
    ) -> BacktestResult:
        if not candles:
            raise ValueError("backtest requires at least one candle")
        if not any((trade_start_ms is None or int(c.open_time.timestamp()*1000) >= trade_start_ms)
                   and (trade_end_ms is None or int(c.open_time.timestamp()*1000) < trade_end_ms)
                   for c in candles):
            raise ValueError('backtest has no observations in its trading window')
        self._validate_candles(candles)
        cfg, risk = self.config, self.risk
        cash = cfg.starting_cash
        quantity = Decimal(0)
        entry_price = entry_fee = entry_slippage = Decimal(0)
        entry_time: dt.datetime | None = None
        entry_signal = ""
        stop = target = None
        pending: Signal | None = None
        history: list[Candle] = []
        trades: list[BacktestTrade] = []
        curve: list[dict[str, str]] = []
        total_fees = total_slippage = realized = Decimal(0)
        peak = cfg.starting_cash
        max_drawdown = Decimal(0)
        exposed = gaps = risk_refusals = 0
        daily_pnl: dict[dt.date, Decimal] = {}

        def in_scope(candle: Candle) -> bool:
            stamp = int(candle.open_time.timestamp() * 1000)
            return (trade_start_ms is None or stamp >= trade_start_ms) and (
                trade_end_ms is None or stamp < trade_end_ms
            )

        def executable(base: Decimal, side: str) -> tuple[Decimal, Decimal]:
            sign = Decimal(1) if side == "buy" else Decimal(-1)
            spread_price = base * (Decimal(1) + sign * cfg.spread_bps / Decimal(20_000))
            filled = spread_price * (Decimal(1) + sign * cfg.slippage_bps / Decimal(10_000))
            return filled, abs(filled - spread_price)

        def close_position(base: Decimal, when: dt.datetime, reason: str) -> None:
            nonlocal cash, quantity, total_fees, total_slippage, realized
            if quantity == 0 or entry_time is None:
                return
            fill, slip_per_unit = executable(base, "sell")
            notional = quantity * fill
            fee = notional * cfg.fee_bps / Decimal(10_000)
            slippage = quantity * slip_per_unit
            cash += notional - fee
            cost = quantity * entry_price + entry_fee
            pnl = notional - fee - cost
            realized += pnl
            daily_pnl[when.date()] = daily_pnl.get(when.date(), Decimal(0)) + pnl
            total_fees += fee
            total_slippage += slippage
            basis = quantity * entry_price + entry_fee
            trades.append(
                BacktestTrade(
                    entry_time=entry_time.isoformat(), exit_time=when.isoformat(),
                    quantity=str(quantity), entry_price=str(entry_price), exit_price=str(fill),
                    entry_fee=str(entry_fee), exit_fee=str(fee),
                    slippage_cost=str(entry_slippage + slippage), net_pnl=str(pnl),
                    net_return=str(pnl / basis if basis else Decimal(0)),
                    exit_reason=reason, signal_id=entry_signal,
                )
            )
            quantity = Decimal(0)

        def enter(signal: Signal, base: Decimal, when: dt.datetime) -> None:
            nonlocal cash, quantity, entry_price, entry_fee, entry_slippage
            nonlocal entry_time, entry_signal, stop, target, total_fees, total_slippage, risk_refusals
            if quantity != 0 or signal.direction is not SignalDirection.LONG or signal.stop_loss is None:
                risk_refusals += 1
                return
            fill, slip_per_unit = executable(base, "buy")
            stop_amount = signal.stop_loss.amount
            target_amount = signal.take_profit.amount if signal.take_profit else None
            if fill <= stop_amount or (target_amount is not None and fill >= target_amount):
                risk_refusals += 1
                return
            equity = cash
            loss_today = min(daily_pnl.get(when.date(), Decimal(0)), Decimal(0))
            remaining_daily = risk.max_daily_loss.amount + loss_today
            if remaining_daily <= 0:
                risk_refusals += 1
                return
            risk_budget = min(
                equity * risk.risk_fraction_per_trade * signal.conviction,
                remaining_daily,
            )
            per_unit = fill - stop_amount
            caps = (
                risk_budget / per_unit,
                risk.max_order_notional.amount / fill,
                risk.max_position_notional.amount / fill,
                risk.max_gross_exposure.amount / fill,
                cash / (fill * (Decimal(1) + cfg.fee_bps / Decimal(10_000))),
            )
            raw = min(caps)
            sized = (raw / cfg.lot_step).to_integral_value(rounding=ROUND_DOWN) * cfg.lot_step
            if sized <= 0:
                risk_refusals += 1
                return
            notional = sized * fill
            fee = notional * cfg.fee_bps / Decimal(10_000)
            slippage = sized * slip_per_unit
            cash -= notional + fee
            quantity, entry_price, entry_fee, entry_slippage = sized, fill, fee, slippage
            entry_time, entry_signal = when, signal.signal_id
            stop, target = stop_amount, target_amount
            total_fees += fee
            total_slippage += slippage

        previous: Candle | None = None
        for candle in candles:
            scoped = in_scope(candle)
            is_gap = previous is not None and candle.open_time != previous.close_time
            if is_gap:
                gaps += 1
                pending = None
                history = []
                if quantity and scoped:
                    close_position(candle.open.amount, candle.open_time, "data_gap")

            if cfg.execution_timing == "next_open" and pending is not None and scoped:
                if pending.direction is SignalDirection.EXIT:
                    close_position(candle.open.amount, candle.open_time, "signal")
                else:
                    enter(pending, candle.open.amount, candle.open_time)
                pending = None

            # Protective levels are checked only after the candle opens.  If both
            # are touched, the stop wins: the conservative ordering is deterministic.
            if quantity and scoped:
                exposed += 1
                if candle.open.amount <= stop:  # type: ignore[operator]
                    close_position(candle.open.amount, candle.open_time, "stop_gap")
                elif target is not None and candle.open.amount >= target:
                    close_position(candle.open.amount, candle.open_time, "target_gap")
                elif candle.low.amount <= stop:  # type: ignore[operator]
                    close_position(stop, candle.close_time, "stop")  # type: ignore[arg-type]
                elif target is not None and candle.high.amount >= target:
                    close_position(target, candle.close_time, "target")

            if cfg.execution_timing == "next_close" and pending is not None and scoped:
                if pending.direction is SignalDirection.EXIT:
                    close_position(candle.close.amount, candle.close_time, "signal")
                else:
                    enter(pending, candle.close.amount, candle.close_time)
                pending = None

            history.append(candle)
            required = getattr(self.strategy, "required_bars", None)
            if isinstance(required, int) and required > 0:
                history = history[-required:]
            if scoped:
                quote = self._quote(candle)
                equity = cash + quantity * candle.close.amount
                due=getattr(self.strategy,'decision_due',None)
                signals=[]
                if due is None or due(candle):
                    context = MarketContext(
                        as_of=candle.close_time,
                        equity=Money.rounded(equity, USDT),
                        snapshot=MarketSnapshot(candle.close_time, {cfg.symbol: quote}),
                        positions={cfg.symbol: Quantity(quantity, cfg.asset)},
                        history={cfg.symbol: tuple(history)},
                    )
                    signals = self.strategy.generate(context)
                pending = signals[0] if signals else None
                if pending is not None and pending.symbol != cfg.symbol:
                    raise ValueError("strategy emitted a signal for an unexpected symbol")
                peak = max(peak, equity)
                drawdown = (peak - equity) / peak if peak else Decimal(0)
                max_drawdown = max(max_drawdown, drawdown)
                curve.append({"time": candle.close_time.isoformat(), "equity": str(equity)})
            previous = candle

        if cfg.liquidate_at_end and quantity:
            last = candles[-1]
            close_position(last.close.amount, last.close_time, "end_of_test")
            if curve:
                curve[-1]["equity"] = str(cash)
                peak = max(peak, cash)
                max_drawdown = max(max_drawdown, (peak - cash) / peak if peak else Decimal(0))

        ending_equity = cash + quantity * candles[-1].close.amount
        metrics = self._metrics(
            [bar for bar in candles if (
                trade_start_ms is None or int(bar.open_time.timestamp()*1000) >= trade_start_ms
            ) and (trade_end_ms is None or int(bar.open_time.timestamp()*1000) < trade_end_ms)] or candles,
            curve, trades, cfg.starting_cash, ending_equity,
            realized, quantity * (candles[-1].close.amount - entry_price),
            max_drawdown, total_fees, total_slippage, exposed,
        )
        report: dict[str, object] = {
            "schema": "trading-agent-backtest/v1",
            "research_only": True,
            "strategy_qualification_allowed": bool(dataset_hashes and dataset_hashes.get('qualification_eligible')=='true' and gaps==0),
            "live_trading": False,
            "strategy": {"name": self.strategy.name, "class": type(self.strategy).__name__,
                         'configuration':getattr(self.strategy, 'configuration', {})},
            "configuration": cfg.as_details(),
            "risk_limits": {
                "max_order_notional": str(risk.max_order_notional.amount),
                "max_position_notional": str(risk.max_position_notional.amount),
                "max_gross_exposure": str(risk.max_gross_exposure.amount),
                "max_daily_loss": str(risk.max_daily_loss.amount),
                "risk_fraction_per_trade": str(risk.risk_fraction_per_trade),
            },
            "dataset": {
                **dict(dataset_hashes or {}),
                "first_open_time": candles[0].open_time.isoformat(),
                "last_close_time": candles[-1].close_time.isoformat(),
                "candle_count": len(candles),
                "gap_count": gaps,
                "trade_start_ms": trade_start_ms,
                "trade_end_ms": trade_end_ms,
            },
            "causality": {
                "decision_time": "closed candle",
                "earliest_execution": cfg.execution_timing,
                "gap_policy": "cancel queued signal, flatten at first genuine post-gap open, reset history",
                "ambiguous_stop_target": "stop_first",
            },
            "risk_refusals": risk_refusals,
            "metrics": metrics,
            "trades": [asdict(trade) for trade in trades],
            "equity_curve": curve,
        }
        report["report_sha256"] = _canonical_hash(report)
        return BacktestResult(report)

    def _quote(self, candle: Candle) -> Quote:
        half = self.config.spread_bps / Decimal(20_000)
        mid = candle.close.amount
        return Quote(
            symbol=self.config.symbol,
            bid=Price.rounded(mid * (Decimal(1) - half), USDT),
            ask=Price.rounded(mid * (Decimal(1) + half), USDT),
            as_of=candle.close_time,
            source="historical-backtest",
        )

    def _validate_candles(self, candles: Sequence[Candle]) -> None:
        previous = None
        seen = set()
        for candle in candles:
            if candle.symbol != self.config.symbol:
                raise ValueError("candle symbol does not match backtest configuration")
            if candle.open_time in seen or (previous and candle.open_time <= previous.open_time):
                raise ValueError("candles must be unique and strictly chronological")
            seen.add(candle.open_time)
            previous = candle

    def _metrics(
        self, candles, curve, trades, starting, ending, realized, unrealized,
        drawdown, fees, slippage, exposed,
    ) -> dict[str, object]:
        total_return = ending / starting - Decimal(1)
        elapsed = candles[-1].close_time - candles[0].open_time
        duration_days = Decimal(elapsed.days * 86400 + elapsed.seconds) / Decimal(86400)
        cagr = None
        if duration_days >= 30 and ending > 0:
            with localcontext(FINANCIAL_CONTEXT):
                cagr = (ending / starting) ** (Decimal(365) / duration_days) - Decimal(1)
        daily: dict[str, Decimal] = {}
        for point in curve:
            daily[str(point["time"])[:10]] = Decimal(str(point["equity"]))
        daily_values = list(daily.values())
        days = list(daily)
        return_runs = [[]]
        for index in range(1, len(daily_values)):
            contiguous = (dt.date.fromisoformat(days[index])-dt.date.fromisoformat(days[index-1])).days == 1
            if not contiguous or not daily_values[index-1]:
                if return_runs[-1]:
                    return_runs.append([])
                continue
            return_runs[-1].append(daily_values[index]/daily_values[index-1]-1)
        return_runs = [run for run in return_runs if run]
        returns = [value for run in return_runs for value in run]
        sharpe = None
        if len(returns) >= 2:
            mean = sum(returns, Decimal(0)) / len(returns)
            variance = sum((value - mean) ** 2 for value in returns) / (len(returns) - 1)
            if variance > 0:
                with localcontext(FINANCIAL_CONTEXT):
                    sharpe = mean / variance.sqrt() * Decimal(365).sqrt()
        pnls = [Decimal(trade.net_pnl) for trade in trades]
        profits = sum((p for p in pnls if p > 0), Decimal(0))
        losses = abs(sum((p for p in pnls if p < 0), Decimal(0)))
        returns_per_trade = [Decimal(trade.net_return) for trade in trades]
        sortino = None
        if returns:
            downside = sum((min(r, Decimal(0)) ** 2 for r in returns), Decimal(0)) / len(returns)
            if downside > 0:
                sortino = (sum(returns, Decimal(0)) / len(returns)) / downside.sqrt() * Decimal(365).sqrt()
        turnover = sum((Decimal(t.quantity) * (Decimal(t.entry_price) + Decimal(t.exit_price))
                        for t in trades), Decimal(0)) / starting
        cost = self.config
        entry = candles[0].open.amount * (1 + (cost.spread_bps / 2 + cost.slippage_bps) / 10000)
        exit_price = candles[-1].close.amount * (1 - (cost.spread_bps / 2 + cost.slippage_bps) / 10000)
        hold_return = exit_price / entry * (1 - cost.fee_bps / 10000) / (1 + cost.fee_bps / 10000) - 1
        confidence = None
        if len(returns) >= 10:
            generator = random.Random(0)
            block = max(2, int(len(returns) ** 0.5))
            samples = []
            for _ in range(200):
                sampled = []
                while len(sampled) < len(returns):
                    run = generator.choices(return_runs, weights=[len(r) for r in return_runs])[0]
                    length = min(block, len(run))
                    index = generator.randrange(len(run)-length+1)
                    sampled.extend(run[index:index+length])
                samples.append(sum(sampled[:len(returns)], Decimal(0))/len(returns))
            samples.sort()
            confidence = {'method':'moving_block_bootstrap', 'seed':0, 'replicates':200,
                          'block_days':block, 'lower':str(samples[4]), 'upper':str(samples[194]),
                          'caveat':'descriptive daily mean; not a profitability or selection test'}
        regimes = {}
        for trade in trades:
            entered = dt.datetime.fromisoformat(trade.entry_time)
            history = []
            for bar in reversed(candles):
                if bar.close_time > entered:
                    continue
                if history and bar.close_time != history[-1].open_time:
                    break
                history.append(bar)
                if len(history) == 31:
                    break
            regime = 'insufficient_contiguous_history'
            if len(history) == 31:
                trend = history[0].close.amount/history[-1].close.amount-1
                regime = 'uptrend' if trend > Decimal('0.005') else ('downtrend' if trend < Decimal('-0.005') else 'sideways')
            bucket = regimes.setdefault(regime, {'trade_count':0, 'net_pnl':Decimal(0)})
            bucket['trade_count'] += 1
            bucket['net_pnl'] += Decimal(trade.net_pnl)
        for bucket in regimes.values():
            bucket['net_pnl'] = str(bucket['net_pnl'])
        return {
            "starting_equity": str(starting), "ending_equity": str(ending),
            "total_return": str(total_return), "cagr": _text(cagr),
            "maximum_drawdown": str(drawdown), "sharpe_ratio": _text(sharpe),
            "realized_pnl": str(realized), "unrealized_pnl": str(unrealized),
            "trade_count": len(trades),
            "win_rate": str(Decimal(sum(p > 0 for p in pnls)) / len(pnls)) if pnls else None,
            "profit_factor": str(profits / losses) if losses else ("Infinity" if profits else None),
            "average_trade_return": str(sum(returns_per_trade, Decimal(0)) / len(returns_per_trade)) if returns_per_trade else None,
            "fee_total": str(fees), "slippage_total": str(slippage),
            "exposure_fraction": str(Decimal(exposed) / len(curve)) if curve else "0",
            'sortino_ratio': _text(sortino),
            'closed_trade_turnover': str(turnover),
            'cash_benchmark_return': '0',
            'buy_hold_benchmark_return_after_costs': str(hold_return),
            'benchmark_caveat': 'full-period unlevered hold; assumes continuous ownership across data gaps',
            'daily_mean_return_95_block_bootstrap':confidence,
            'entry_regime_performance':regimes,
            'regime_definition':'causal 30-bar return; +/-0.5% fixed trend thresholds; reset at gaps',
        }


def run_development_walk_forward(
    candles: Sequence[Candle],
    *,
    strategy_factory,
    folds: Sequence[BacktestFold],
    config: BacktestConfig,
    risk: RiskConfig,
    dataset_hashes: Mapping[str, str],
    plan: ChronologicalBacktestPlan,
) -> dict[str, object]:
    """Run fixed-strategy validation folds; accepts no confirmation/test input."""
    results = []
    for fold in folds:
        plan.require_development_fold(fold)
        selected = [
            candle for candle in candles
            if fold.train_start_ms <= int(candle.open_time.timestamp() * 1000) < fold.validation_end_ms
        ]
        if not selected:
            raise ValueError(f"fold {fold.fold_id} contains no candles")
        validation = [c for c in selected if fold.validation_start_ms <= int(c.open_time.timestamp()*1000) < fold.validation_end_ms]
        if not validation:
            results.append({'fold':asdict(fold), 'status':'insufficient_validation_data', 'result':None})
            continue
        result = HistoricalBacktester(strategy_factory(), config=config, risk=risk).run(
            selected,
            dataset_hashes=dataset_hashes,
            trade_start_ms=fold.validation_start_ms,
            trade_end_ms=fold.validation_end_ms,
        )
        results.append({"fold": asdict(fold), "result": result.report})
    report: dict[str, object] = {
        "schema": "trading-agent-walk-forward/v1",
        "scope": "development_only",
        "confirmation_accessed": False,
        "test_accessed": False,
        "chronology": asdict(plan),
        "dataset_hashes": dict(dataset_hashes),
        "folds": results,
    }
    report["report_sha256"] = _canonical_hash(report)
    return report


def run_contiguous_segment_backtests(
    candles: Sequence[Candle],
    *,
    strategy_factory,
    config: BacktestConfig,
    risk: RiskConfig,
    dataset_hashes: Mapping[str, str],
) -> dict[str, object]:
    """Backtest every genuine contiguous segment with fresh independent equity."""
    if not candles:
        raise ValueError("segment backtest requires candles")
    segments: list[list[Candle]] = [[candles[0]]]
    for candle in candles[1:]:
        if candle.open_time != segments[-1][-1].close_time:
            segments.append([])
        segments[-1].append(candle)
    results = []
    for index, segment in enumerate(segments, 1):
        strategy = strategy_factory()
        warmup = getattr(strategy, "required_bars", 0)
        rows = [
            {
                "open_time": candle.open_time.isoformat(),
                "open": str(candle.open.amount), "high": str(candle.high.amount),
                "low": str(candle.low.amount), "close": str(candle.close.amount),
                "volume": str(candle.volume.amount),
            }
            for candle in segment
        ]
        details: dict[str, object] = {
            "segment_id": index,
            "first_open_time": segment[0].open_time.isoformat(),
            "last_close_time": segment[-1].close_time.isoformat(),
            "candle_count": len(segment),
            "segment_sha256": _canonical_hash(rows),
            "warmup_bars": warmup,
        }
        if isinstance(warmup, int) and len(segment) < warmup:
            details["status"] = "insufficient_warmup"
            details["result"] = None
        else:
            details["status"] = "evaluated"
            details["result"] = HistoricalBacktester(
                strategy, config=config, risk=risk
            ).run(segment, dataset_hashes=dataset_hashes).report
        results.append(details)
    report: dict[str, object] = {
        "schema": "trading-agent-contiguous-segment-backtests/v1",
        "scope": "development_only",
        "fresh_equity_per_segment": True,
        "confirmation_accessed": False,
        "test_accessed": False,
        "dataset_hashes": dict(dataset_hashes),
        "segment_count": len(results),
        "segments": results,
    }
    report["report_sha256"] = _canonical_hash(report)
    return report


def write_backtest_report(path: str | Path, report: Mapping[str, object]) -> Path:
    """Atomically persist a deterministic JSON report."""
    destination = Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_suffix(destination.suffix + ".tmp")
    temporary.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    temporary.replace(destination)
    return destination
