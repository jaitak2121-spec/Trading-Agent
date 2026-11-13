"""Deterministic safety tests for the research-only historical backtester."""

from __future__ import annotations

import datetime as dt
import json
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path

from trading.app.backtest import (
    BacktestConfig,
    BacktestFold,
    ChronologicalBacktestPlan,
    HistoricalBacktester,
    load_historical_candles,
    run_contiguous_segment_backtests,
    run_development_walk_forward,
    write_backtest_report,
)
from trading.core.clock import UTC
from trading.core.config import RiskConfig
from trading.core.marketdata import Candle
from trading.core.money import Money, Price, Quantity, USDT
from trading.strategy import MovingAverageCrossover, Signal, SignalDirection, SignalStrategy

SYMBOL = "BTC/USDT"
ASSET = "BTC"


def risk(*, fraction: str = "0.01", daily: str = "10000") -> RiskConfig:
    return RiskConfig(
        base_currency=USDT,
        max_order_notional=Money("100000", USDT),
        max_position_notional=Money("100000", USDT),
        max_gross_exposure=Money("100000", USDT),
        max_daily_loss=Money(daily, USDT),
        risk_fraction_per_trade=Decimal(fraction),
    )


def bars(values, *, start=None, gap_after=None):
    opened = start or dt.datetime(2026, 1, 1, tzinfo=UTC)
    output = []
    for index, value in enumerate(values):
        if gap_after is not None and index == gap_after + 1:
            opened += dt.timedelta(minutes=1)
        close = Decimal(str(value))
        output.append(
            Candle(
                symbol=SYMBOL,
                open_time=opened,
                interval_seconds=60,
                open=Price(close, USDT),
                high=Price(close + Decimal("2"), USDT),
                low=Price(max(Decimal("0.01"), close - Decimal("2")), USDT),
                close=Price(close, USDT),
                volume=Quantity("1", ASSET),
                source="test",
            )
        )
        opened += dt.timedelta(minutes=1)
    return output


class Scripted(SignalStrategy):
    name = "scripted-backtest"

    def __init__(self, plan):
        self.plan = plan
        self.tick = 0

    def generate(self, context):
        direction = self.plan.get(self.tick)
        tick = self.tick
        self.tick += 1
        if direction is None:
            return []
        price = context.price(SYMBOL)
        assert price is not None
        kwargs = {}
        if direction is SignalDirection.LONG:
            kwargs = {
                "stop_loss": Price(price.amount - Decimal("10"), USDT),
                "take_profit": Price(price.amount + Decimal("100"), USDT),
            }
        return [
            Signal(
                strategy_name=self.name,
                signal_id=f"scripted:{tick}:{direction.value}",
                symbol=SYMBOL,
                direction=direction,
                reference_price=price,
                as_of=context.as_of,
                rationale="deterministic test",
                **kwargs,
            )
        ]


class ExecutionTest(unittest.TestCase):
    def engine(self, plan, **config):
        return HistoricalBacktester(
            Scripted(plan),
            config=BacktestConfig(
                starting_cash=Decimal("10000"),
                fee_bps=Decimal(str(config.pop("fee_bps", "0"))),
                slippage_bps=Decimal(str(config.pop("slippage_bps", "0"))),
                spread_bps=Decimal(str(config.pop("spread_bps", "0"))),
                **config,
            ),
            risk=risk(),
        )

    def test_signal_executes_at_next_open_not_signal_close(self):
        candles = bars([100, 150, 150])
        result = self.engine({0: SignalDirection.LONG}).run(candles)
        trade = result.report["trades"][0]
        self.assertEqual(trade["entry_price"], "150")
        self.assertEqual(trade["entry_time"], candles[1].open_time.isoformat())

    def test_fees_spread_and_slippage_are_charged(self):
        result = self.engine(
            {0: SignalDirection.LONG, 1: SignalDirection.EXIT},
            fee_bps="10", spread_bps="10", slippage_bps="5",
        ).run(bars([100, 100, 100]))
        metrics = result.metrics
        self.assertLess(Decimal(str(metrics["realized_pnl"])), 0)
        self.assertGreater(Decimal(str(metrics["fee_total"])), 0)
        self.assertGreater(Decimal(str(metrics["slippage_total"])), 0)

    def test_position_size_uses_stop_risk_budget(self):
        result = self.engine({0: SignalDirection.LONG}).run(bars([100, 100]))
        # 1% of 10,000 divided by a 10 USDT stop distance.
        self.assertEqual(Decimal(result.report["trades"][0]["quantity"]), Decimal("10"))

    def test_next_close_timing_is_causal_and_configurable(self):
        candles = bars([100, 150, 175])
        result = self.engine(
            {0: SignalDirection.LONG}, execution_timing="next_close"
        ).run(candles)
        self.assertEqual(result.report["trades"][0]["entry_price"], "150")
        self.assertEqual(result.report["trades"][0]["entry_time"], candles[1].close_time.isoformat())

    def test_risk_notional_limit_caps_position(self):
        limited = RiskConfig(
            base_currency=USDT,
            max_order_notional=Money("50", USDT),
            max_position_notional=Money("50", USDT),
            max_gross_exposure=Money("50", USDT),
            max_daily_loss=Money("100", USDT),
            risk_fraction_per_trade=Decimal("0.01"),
        )
        engine = HistoricalBacktester(
            Scripted({0: SignalDirection.LONG}),
            config=BacktestConfig(fee_bps=Decimal(0), slippage_bps=Decimal(0), spread_bps=Decimal(0)),
            risk=limited,
        )
        result = engine.run(bars([100, 100]))
        self.assertEqual(result.report["trades"][0]["quantity"], "0.5")


class GapAndRiskTest(unittest.TestCase):
    def test_gap_cancels_pending_entry_and_resets_history(self):
        result = HistoricalBacktester(
            Scripted({0: SignalDirection.LONG}), config=BacktestConfig(), risk=risk()
        ).run(bars([100, 100, 100], gap_after=0))
        self.assertEqual(result.metrics["trade_count"], 0)
        self.assertEqual(result.report["dataset"]["gap_count"], 1)

    def test_gap_flattens_open_position_at_first_real_price(self):
        candles = bars([100, 100, 80, 80], gap_after=1)
        result = HistoricalBacktester(
            Scripted({0: SignalDirection.LONG}),
            config=BacktestConfig(fee_bps=Decimal(0), slippage_bps=Decimal(0), spread_bps=Decimal(0)),
            risk=risk(),
        ).run(candles)
        trade = result.report["trades"][0]
        self.assertEqual(trade["exit_reason"], "data_gap")
        self.assertEqual(trade["exit_price"], "80")

    def test_stop_wins_when_stop_and_target_touch_same_bar(self):
        candles = bars([100, 100, 100])
        candles[1] = Candle(
            symbol=SYMBOL, open_time=candles[1].open_time, interval_seconds=60,
            open=Price("100", USDT), high=Price("250", USDT), low=Price("80", USDT),
            close=Price("100", USDT), volume=Quantity("1", ASSET), source="test",
        )
        result = HistoricalBacktester(
            Scripted({0: SignalDirection.LONG}),
            config=BacktestConfig(fee_bps=Decimal(0), slippage_bps=Decimal(0), spread_bps=Decimal(0)),
            risk=risk(),
        ).run(candles)
        self.assertEqual(result.report["trades"][0]["exit_reason"], "stop")

    def test_daily_loss_limit_refuses_a_new_entry(self):
        result = HistoricalBacktester(
            Scripted({0: SignalDirection.LONG, 2: SignalDirection.LONG}),
            config=BacktestConfig(fee_bps=Decimal(0), slippage_bps=Decimal(0), spread_bps=Decimal(0)),
            risk=risk(daily="50"),
        ).run(bars([100, 100, 80, 80, 80]))
        self.assertEqual(result.metrics["trade_count"], 1)
        self.assertEqual(result.report["risk_refusals"], 1)


class StrategyAndWalkForwardTest(unittest.TestCase):
    def test_existing_moving_average_strategy_runs_unchanged(self):
        prices = [110 - i for i in range(15)] + [96 + i * 2 for i in range(20)]
        strategy = MovingAverageCrossover(fast=3, slow=5, atr_period=3, symbols=[SYMBOL])
        result = HistoricalBacktester(strategy, config=BacktestConfig(), risk=risk()).run(bars(prices))
        self.assertGreaterEqual(result.metrics["trade_count"], 1)
        self.assertEqual(result.report["strategy"]["class"], "MovingAverageCrossover")

    def test_walk_forward_trades_only_inside_validation_and_is_reproducible(self):
        candles = bars([100] * 10)
        base = int(candles[0].open_time.timestamp() * 1000)
        fold = BacktestFold("fold-1", base, base + 3 * 60_000, base + 3 * 60_000, base + 8 * 60_000)
        plan = ChronologicalBacktestPlan(
            base, base + 8 * 60_000, base + 8 * 60_000, base + 10 * 60_000
        )
        kwargs = dict(
            candles=candles, strategy_factory=lambda: Scripted({3: SignalDirection.LONG}),
            folds=[fold], config=BacktestConfig(), risk=risk(),
            dataset_hashes={"source_file_sha256": "a" * 64, "candle_sha256": "b" * 64},
            plan=plan,
        )
        first = run_development_walk_forward(**kwargs)
        second = run_development_walk_forward(**kwargs)
        self.assertEqual(first, second)
        self.assertEqual(first["scope"], "development_only")
        self.assertFalse(first["confirmation_accessed"])
        self.assertFalse(first["test_accessed"])

        crossing = BacktestFold(
            "crossing", base, base + 7 * 60_000, base + 7 * 60_000, base + 9 * 60_000
        )
        with self.assertRaisesRegex(ValueError, "outside the development"):
            run_development_walk_forward(**{**kwargs, "folds": [crossing]})

    def test_contiguous_segments_use_fresh_equity_and_independent_warmup(self):
        report = run_contiguous_segment_backtests(
            bars(range(100, 113), gap_after=2),
            strategy_factory=lambda: MovingAverageCrossover(
                fast=2, slow=3, atr_period=2, symbols=[SYMBOL]
            ),
            config=BacktestConfig(), risk=risk(),
            dataset_hashes={"source_file_sha256": "a" * 64, "candle_sha256": "b" * 64},
        )
        self.assertEqual(report["segment_count"], 2)
        self.assertTrue(report["fresh_equity_per_segment"])
        self.assertEqual(report["segments"][0]["status"], "insufficient_warmup")
        self.assertEqual(report["segments"][1]["status"], "evaluated")
        self.assertEqual(
            report["segments"][1]["result"]["metrics"]["starting_equity"], "10000"
        )


class ArtifactTest(unittest.TestCase):
    def test_genuine_dataset_loader_accepts_gaps_and_checks_hash(self):
        candles = []
        for candle in bars([100, 101, 102], gap_after=0):
            candles.append({
                "symbol": SYMBOL,
                "open_time": int(candle.open_time.timestamp() * 1000),
                "close_time": int(candle.close_time.timestamp() * 1000),
                "interval_seconds": 60,
                "open": str(candle.open.amount), "high": str(candle.high.amount),
                "low": str(candle.low.amount), "close": str(candle.close.amount),
                "volume": "0.833680000000", "source": "coinswitch-rest-candles",
            })
        dataset = {
            "schema": "coinswitch-public-candles/v1", "symbol": SYMBOL,
            "public_data_only": True, "credentials_used": True,
            "live_trading": False, "training_only": True, "candles": candles,
            "source": {"provider": "CoinSwitch", "access": "authenticated-read-only"},
        }
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "candles.json"
            source.write_text(json.dumps(dataset))
            loaded, hashes = load_historical_candles(source)
            self.assertEqual(len(loaded), 3)
            self.assertEqual(len(hashes["source_file_sha256"]), 64)

            report = HistoricalBacktester(
                Scripted({}), config=BacktestConfig(), risk=risk()
            ).run(loaded, dataset_hashes=hashes).report
            output = write_backtest_report(Path(directory) / "report.json", report)
            self.assertEqual(json.loads(output.read_text())["report_sha256"], report["report_sha256"])


if __name__ == "__main__":
    unittest.main()
