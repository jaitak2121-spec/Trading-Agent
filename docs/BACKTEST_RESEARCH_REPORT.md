# Moving Average Crossover backtest research report

Status: development-only research simulation. No paper or live orders were sent.

## Scope and safeguards

The backtester reuses the existing `MovingAverageCrossover` signal strategy without changing it. A signal is formed only after a candle closes and is filled no earlier than the next candle open (the configured timing for this run). It has no broker, execution gateway, credentials, or live-order method.

The source range ends at `2026-09-03T03:44:00Z`, exclusive of the locked confirmation period. Neither the previously evaluated confirmation artifact nor Segment 7 was loaded. This result is development evidence only and is not suitable for model or strategy promotion.

Missing candles are never filled. At each observed gap the simulator cancels a queued signal, closes an existing position at the first genuine post-gap open, and resets strategy history and indicator warm-up. If a candle touches both stop and target, the stop is applied first.

## Genuine dataset

- Artifact: `artifacts/backtest-btc-usdt-1m-2026-07-05_2026-09-03-development.json`
- Requested range: 2026-07-05 03:44 UTC through 2026-09-03 03:44 UTC
- Returned candles: 56,399 of 86,400 expected
- Duplicate candles: 0
- Observed gaps: 20
- Candle SHA-256: `261172f176bb2717f3a153eb6789ad749850c998482ff8f7d19be3bc65617866`
- Source-file SHA-256: `20023a0c37bec03343a7ffd3b2eaf18df43a869d92578eb47cf479d3a9b6dceb`
- Acquisition integrity: **FAIL** because CoinSwitch omitted 30,001 requested timestamps

The failed acquisition verdict is preserved. The backtest is permitted to operate on discontinuous genuine observations only because its gap policy is explicit and tested; this does not make the historical dataset complete.

## Configuration

- Strategy: existing Moving Average Crossover (fast 10, slow 30, ATR 14, stop 2 ATR, reward 2 times risk)
- Starting equity: 10,000 USDT
- Risk per entry: 0.5% of current equity
- Maximum order/position/gross exposure: 1,000 USDT
- Maximum daily loss: 100 USDT (new entries are refused after exhaustion; exits remain allowed)
- Fee: 10 bps per fill
- Assumed spread: 2 bps
- Slippage: 2 bps per fill
- Execution: next candle open
- End-of-test position: liquidated at the final genuine close

## Development result

- Ending equity: 7,483.028009950409767495497727 USDT
- Total return: -25.1697%
- Realized PnL: -2,516.971990049590232504502263 USDT
- Maximum drawdown: 25.1697%
- Closed trades: 1,010
- Win rate: 6.9307%
- Profit factor: 0.0356842
- Average net trade return: -0.249049%
- Exposure: 18.4081%
- Fees: 2,019.013768091681155400050989 USDT
- Slippage: 403.8027498521173468483409861 USDT
- Daily-return Sharpe ratio: -45.6179
- Annualized CAGR: -82.8616%
- Risk refusals: 92

The CAGR and Sharpe values are mechanically annualized from a short, incomplete period and should not be treated as stable estimates. The result is plainly negative after the stated costs. It provides no evidence of profitability.

## Reproducibility

- JSON report: `artifacts/backtest-ma-crossover-development-report.json`
- Internal canonical report SHA-256: `b36bb8e6d8faaff28a673af0f5861f66806ab787f366c49acaab0d81824df6ea`
- Report-file SHA-256: `8c5be379c52929ffa4d11d4b16253e3d22ec191b8334708ae2bc72147ca15761`

Re-run the fixed configuration with:

```shell
python3 -m trading.app.backtest_cli \
  artifacts/backtest-btc-usdt-1m-2026-07-05_2026-09-03-development.json \
  artifacts/backtest-ma-crossover-development-report.json \
  --starting-cash 10000 --fee-bps 10 --slippage-bps 2 --spread-bps 2 \
  --risk-fraction 0.005 --max-order-notional 1000 \
  --max-position-notional 1000 --max-gross-exposure 1000 \
  --max-daily-loss 100 --execution-timing next_open
```

The implementation also provides deterministic development-only walk-forward folds. Training bars warm the unchanged indicators; orders are allowed only in the later validation interval. The API deliberately accepts no confirmation or test artifact parameter.

## Verification

- Focused backtest tests: 12 passed
- Full application suite: 2,149 passed, 4 skipped
- Covered: next-bar execution, fees, spread, slippage, stop-risk sizing, notional caps, daily-loss enforcement, stop/target ambiguity, data gaps, strategy integration, deterministic folds, hashes, and atomic report persistence

## Remaining work

Before any out-of-sample conclusion, acquire a more complete development dataset, preregister fixed strategy/cost assumptions, run the development walk-forward folds, and reserve a genuinely untouched test period. Confirmation data must remain unavailable until a separate explicit authorization permits one fixed final evaluation.
