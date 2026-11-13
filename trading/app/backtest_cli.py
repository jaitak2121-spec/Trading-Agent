"""Command-line entry point for the isolated historical backtester."""

from __future__ import annotations

import argparse
from decimal import Decimal

from ..core.config import RiskConfig
from ..core.money import Money, USDT
from ..strategy.examples import MovingAverageCrossover
from .backtest import (
    BacktestConfig,
    HistoricalBacktester,
    load_historical_candles,
    run_contiguous_segment_backtests,
    write_backtest_report,
)


def parser() -> argparse.ArgumentParser:
    command = argparse.ArgumentParser(
        description="Run a research-only Moving Average Crossover historical backtest"
    )
    command.add_argument("dataset", help="authenticated CoinSwitch candle JSON")
    command.add_argument("output", help="destination for the reproducible JSON report")
    command.add_argument("--segment-report", help="optional independent contiguous-segment report")
    command.add_argument("--starting-cash", default="10000")
    command.add_argument("--fee-bps", default="10")
    command.add_argument("--slippage-bps", default="2")
    command.add_argument("--spread-bps", default="2")
    command.add_argument("--risk-fraction", default="0.005")
    command.add_argument("--max-order-notional", default="1000")
    command.add_argument("--max-position-notional", default="1000")
    command.add_argument("--max-gross-exposure", default="1000")
    command.add_argument("--max-daily-loss", default="100")
    command.add_argument("--execution-timing", choices=("next_open", "next_close"), default="next_open")
    command.add_argument("--fast", type=int, default=10)
    command.add_argument("--slow", type=int, default=30)
    command.add_argument("--atr-period", type=int, default=14)
    command.add_argument("--stop-atr-multiple", default="2")
    command.add_argument("--reward-multiple", default="2")
    return command


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    candles, hashes = load_historical_candles(args.dataset)
    config = BacktestConfig(
        starting_cash=Decimal(args.starting_cash),
        fee_bps=Decimal(args.fee_bps),
        slippage_bps=Decimal(args.slippage_bps),
        spread_bps=Decimal(args.spread_bps),
        execution_timing=args.execution_timing,
    )
    risk = RiskConfig(
        base_currency=USDT,
        max_order_notional=Money(args.max_order_notional, USDT),
        max_position_notional=Money(args.max_position_notional, USDT),
        max_gross_exposure=Money(args.max_gross_exposure, USDT),
        max_daily_loss=Money(args.max_daily_loss, USDT),
        risk_fraction_per_trade=Decimal(args.risk_fraction),
    )
    strategy = MovingAverageCrossover(
        fast=args.fast,
        slow=args.slow,
        atr_period=args.atr_period,
        stop_atr_multiple=args.stop_atr_multiple,
        reward_multiple=args.reward_multiple,
        symbols=[config.symbol],
    )
    result = HistoricalBacktester(strategy, config=config, risk=risk).run(
        candles, dataset_hashes=hashes
    )
    destination = write_backtest_report(args.output, result.report)
    if args.segment_report:
        segment_report = run_contiguous_segment_backtests(
            candles,
            strategy_factory=lambda: MovingAverageCrossover(
                fast=args.fast, slow=args.slow, atr_period=args.atr_period,
                stop_atr_multiple=args.stop_atr_multiple,
                reward_multiple=args.reward_multiple, symbols=[config.symbol],
            ),
            config=config, risk=risk, dataset_hashes=hashes,
        )
        write_backtest_report(args.segment_report, segment_report)
    metrics = result.metrics
    print(f"report: {destination}")
    print(f"report sha256: {result.report['report_sha256']}")
    print(f"trades: {metrics['trade_count']}")
    print(f"total return: {metrics['total_return']}")
    print(f"maximum drawdown: {metrics['maximum_drawdown']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
