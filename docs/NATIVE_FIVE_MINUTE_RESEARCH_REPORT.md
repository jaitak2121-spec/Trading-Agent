# Native five-minute development experiment — 2026-11-12

**All six candidates rejected for advancement.** This completed experiment does
not establish profitability. No model, production strategy or threshold was changed.

## Genuine data and completeness

Using the existing authenticated GET-only historical adapter, downloaded
CoinSwitch `c2c1` BTC/USDT native five-minute candles for **2025-05-06 00:00 UTC
through 2025-11-02 00:00 UTC, end exclusive**. This predates the existing 365-day
research source and avoids protected confirmation and ML Segment 7 observations.
Historical Keychain credentials were passed only to the GET downloader process;
no credential values, signed headers or account data were stored.

The initial two-day native-five-minute probe passed with **576/576** candles.
The full acquisition returned **51,837/51,840**, with no duplicates and one
gap: **August 14 at 12:40, 12:45 and 12:50 UTC**. Its integrity remains **FAIL**.
All 36 response bodies, requested ranges, statuses, counts and hashes were saved
under `artifacts/native-5m-development-acquisition/`. They permit hash-checked
replay after interruption. No missing price was fabricated or filled.

Parent: `artifacts/btc-usdt-5m-2025-05-06_2025-11-02-development.json`
SHA-256: `e7aca21f39fac23a2340ae0c22d5bad8a06bdbd063573055adcc3336579eda2f`.

Coverage-only derivation produced two separate datasets. Each explicitly records
the failed parent's hash and original requested range. Their declared requested
ranges were narrowed openly to the genuine contiguous rows; **the parent was
not repaired, relabeled PASS or overwritten**. Both derivatives independently
pass full expected-timestamp, ordering, duplicate, OHLCV and hash validation.

| Complete derivative | Requested UTC range, end exclusive | Candles | File SHA-256 |
|---|---|---:|---|
| `artifacts/native-5m-complete-runs/run-1.json` | May 6 00:00–August 14 12:40 | 28,952 | `84ded88a6bc41d3885e4e024cd7f7eedcd3f12abfd052072bec16dc7dadfd162` |
| `artifacts/native-5m-complete-runs/run-2.json` | August 14 12:55–November 2 00:00 | 22,885 | `562b4807bc82ac3ba7d6c1a00b376074f296f6e997f7a9bfae8e968f43ea8a60` |

This new native dataset does not recover the four original missing one-minute
May 7 candles. Five-minute candles are genuine venue aggregates, not an inferred
one-minute path. The existing one-minute ML feature pipeline was not repurposed
or fitted on them.

## Frozen protocol

Plan: `config/native-5m-development-plan-v2.json`
file SHA-256: `42cc537976279c6d9c9aeec05faa19dd5beca56a26bbe38116b667e22913c801`.

Six fixed existing implementations: MA crossover, momentum, mean reversion,
volatility-filtered momentum, breakout and trend pullback. Parameters were not
optimized. Versioned code/source hashes, costs, fold boundaries, selection rule
and acceptance criteria were frozen before performance evaluation.

Each complete run has rolling **30-day indicator warm-up / 15-day validation**
windows, stepped forward 15 days: four validation folds in run 1, three in run 2.
Validation windows do not overlap. Signals use closed native five-minute bars;
fills occur at the next native bar's open. Stops/targets use conservative
stop-first handling when both are touched. **No one-minute execution accuracy is
claimed.** No positions or indicator windows cross the missing-data gap.

These rule-based strategies have no fitted parameters: warm-up is historical
indicator context, not supervised model training. Each validation fold starts
with fresh 10,000 USDT equity. Position/order/gross exposure caps are 1,000 USDT,
the realized daily-loss budget is 100 USDT, and stop-risk fraction is 0.5%.

Base per-side costs: 10bp fee, 2bp slippage, 2bp full spread (half per side).
Stress: 20bp fee, 5bp slippage, 4bp full spread. Account-specific fees remain
unverified assumptions. Compare cash and fixed 10%-capital buy-and-hold, with
the remaining 90% in cash. It is a fixed-allocation benchmark, not an exact
match to each strategy's varying exposure.

Preregistered development acceptance requires at least six folds; 100 closed
trades under each cost scenario; five positive folds under each scenario;
positive mean net and benchmark-excess returns; at most 10% drawdown per fold;
and a positive lower bound for mean trade P&L under both cost scenarios.
The latter uses 2,000 moving-block bootstrap replicates, blocks up to ten trades
within a fold, seed zero, and Bonferroni adjustment across six candidates.
Intervals assume exchangeable periods and sufficiently stationary trade blocks.
They are development diagnostics, not a live-profit guarantee.

An initial plan serialization check caught tuple/list drift **before evaluating
any strategy**. That plan was preserved. The corrected round-trip representation
has a regression test and the distinct `-v2` plan above. No acceptance threshold
was altered after viewing performance.

## Actual results

Returns below are arithmetic means of seven independently funded validation-fold
returns, not one compounded account or annual forecasts. Every candidate had
**zero positive validation folds** under both cost scenarios.

| Candidate | Base mean net return | Stress mean net return | Base / stress closed trades | Qualified |
|---|---:|---:|---:|---|
| MA crossover | -2.2545% | -4.5654% | 588 / 583 | No |
| Momentum | -1.8763% | -3.8861% | 503 / 503 | No |
| Mean reversion | -1.0452% | -2.2529% | 298 / 296 | No |
| Volatility-filtered momentum | -1.8497% | -3.8556% | 502 / 502 | No |
| Breakout | -0.2171% | -0.3408% | 31 / 31 | No |
| Trend pullback | -0.1178% | -0.2017% | 21 / 21 | No |

All computed trade-P&L confidence intervals have negative upper bounds. Trend
pullback has fewer than 30 trades and its interval is correctly withheld.
Breakout and pullback also fail the 100-trade minimum. No candidate was selected
for confirmation, fitting, deployment or capital allocation. The accepted-model
registry was not changed. No protected confirmation or final-test data was read.

Full fold-by-fold metrics, trade ledgers, costs, benchmarks, confidence intervals
and failed criteria: `artifacts/native-5m-development-results.json`.
File SHA-256: `07cbc2a8e501c0001020d61691c7464b37a9fdf941888bc3f4718054d17d6ec8`.
Independent verification: `artifacts/native-5m-development-verification.json`.
All **84** candidate/cost/fold ledgers reconcile trade P&L to ending-minus-starting
equity and fees to the trade-fee sums; trade boundaries and no-gap status pass.
Frozen source/code hashes were checked after execution.

Commands executed:

```sh
.venv/bin/python -m trading.app.native_research prepare config/native-5m-development-plan-v2.json
.venv/bin/python -m trading.app.native_research run config/native-5m-development-plan-v2.json \
  --output artifacts/native-5m-development-results.json
```

These commands now refuse to overwrite the existing plan/results. Derived files
are reused only if their canonical hashes match. Research commands require the
repository's explicit development artifacts; protected datasets are not bundled
or scanned. Do not keep adjusting these hypotheses to find a winner on this data.

## Limits and next research direction

This tests a different, older period and native cadence than the prior one-minute
experiment. Smaller losses are not proof of improvement in a controlled comparison.
Taxes/TDS cash-flow, depth, latency, queue priority and actual fill-fee settlement
are not modeled. Native intrabar ordering remains uncertain. Software tests and
honest rejection do not establish a profitable trading edge.

Next research needs a separately preregistered economic hypothesis, broader
regimes and genuinely independent confirmation/forward observations. None of
the rejected configurations should be tuned on confirmation. New ML fitting
requires validated data compatible with the frozen one-minute feature schema,
or a separately versioned and validated research protocol; this dataset is not
silently interchangeable with it.
