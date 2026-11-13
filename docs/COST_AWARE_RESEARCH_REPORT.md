# Cost-aware development research — 2026-11-12

**Outcome: all six hypotheses rejected for advancement. No profitable strategy
has been demonstrated.** Nothing was activated in paper or live trading.

## Implementation and preregistration

Added research-only breakout and trend-pullback rules on 5-, 15- and 30-minute
completed bars. Reused `HistoricalBacktester`, Decimal accounting, position caps,
daily loss checks, protective stops and next-minute-open fills. A research strategy
can supply an optional decision cadence; strategies without it keep their existing
backtest behavior. Production strategy composition and order transport are unchanged.

Aggregation requires exact UTC-aligned, complete minute buckets. Missing minutes
are never filled. The strategy sees only completed bars and the latest contiguous
history. Stops and targets are still checked on every original minute, including
minutes without a new strategy decision. Intrabar ambiguity uses the existing
stop-first convention, not an invented price path.

The fixed rules use a rising 50-bar SMA trend and ATR(14). Breakout enters above
the previous 20 highs, exits below the previous 10 lows, and uses a fixed 2-ATR
stop. Pullback enters on a close crossing above SMA(20) while SMA(20)>SMA(50),
exits below SMA(50), and uses a 2-ATR stop / 4-ATR target. Entry additionally
requires 2 ATR / price >= 0.0052: twice the assumed base round-trip cost of 26bp.
No parameters were adjusted after the results.

Frozen plan: `config/cost-aware-development-plan.json`.

- Plan file SHA-256: `54dd5f1863c4f711b7136670c4794de7ac7c77ed4c8c66d4d0d5848022123f49`
- Canonical plan SHA-256: `c1f02c67de47af03d77a193036f2f3a308467ea701c656c4164d16c3ef43ad60`
- Result: `artifacts/cost-aware-development-results.json`
- Result file SHA-256: `91579b91ffc73e5f8cff26a3ba07e1b4b31075c9035a296b28454aec4874e78b`

The plan binds implementation hashes, source hash, candidate definitions, risk
configuration, costs, period boundaries, coverage policy and acceptance criteria.
The execution command refuses changes to these bindings and refuses an existing
output path. The result contains all 60 independent period/candidate/cost ledgers,
equity curves, trades, benchmarks and metrics. Code and source hashes were verified
again after evaluation.

## Data and execution assumptions

Only the authorized July–September development source was opened:
`artifacts/backtest-btc-usdt-1m-2026-07-05_2026-09-03-development-recovered.json`.
Its SHA-256 remains
`2c36a429413621ca33788fd627999658c9afccc98c4f184b994c05d9cbd5a871`.
The original full-range completeness failure remains: 56,399/86,400 candles,
30,001 missing timestamps and 21 contiguous runs. No acquisition completeness
claim has changed.

Period selection used coverage alone: at least 4,320 consecutive minutes, followed
by a fixed 1,560-minute warm-up. Five runs qualify, providing 31,513 evaluated
minutes (about 21.9 days), with separate 10,000 USDT accounts, fresh history and
no positions carried between runs. Run numbers below belong exclusively to this
development source; development run 7 is **not** the protected ML Segment 7.

| Development run | Trading starts UTC | Ends exclusive UTC | Evaluated minutes |
|---|---|---|---:|
| 1 | July 6 05:44 | July 9 09:00 | 4,516 |
| 4 | July 30 14:26 | August 1 23:57 | 3,451 |
| 5 | August 3 01:58 | August 6 11:43 | 4,905 |
| 7 | August 7 13:51 | August 11 04:42 | 5,211 |
| 20 | August 24 17:25 | September 3 01:15 | 13,430 |

Maximum order, position and gross exposure: 1,000 USDT. Per-entry stop-risk
budget: 0.5% of equity, capped by remaining 100 USDT daily realized loss budget.
These are simulator limits; fees, stop gaps and adverse prices can exceed a
nominal stop-loss budget. This is not a certification of every runtime risk policy.

Base assumptions per side: 10bp fee, 2bp slippage, 2bp full spread (half charged
per side). Stress: 20bp fee, 5bp slippage, 4bp full spread. These are disclosed
research assumptions, **not verified account-specific CoinSwitch fees**. Taxes,
TDS cash-flow effects, latency, market depth and partial fills are not modeled.

## Results

Returns are the equal-weight arithmetic mean of five independently funded period
returns, not a compounded account return or an annualized forecast. A zero-trade
period remains a zero return in that mean.

| Candidate | Closed trades, base / stress | Base mean net return | Stress mean net return | Positive base periods |
|---|---:|---:|---:|---:|
| Breakout 5m | 8 / 8 | -0.03828% | -0.08304% | 1/5 |
| Breakout 15m | 13 / 13 | -0.10813% | -0.18078% | 1/5 |
| Breakout 30m | 12 / 12 | -0.14373% | -0.21071% | 0/5 |
| Trend-pullback 5m | 2 / 2 | -0.02340% | -0.03457% | 0/5 |
| Trend-pullback 15m | 13 / 13 | -0.12400% | -0.19662% | 0/5 |
| Trend-pullback 30m | 14 / 14 | -0.10248% | -0.18074% | 0/5 |

Base returns by development run, in percent:

| Candidate | 1 | 4 | 5 | 7 | 20 |
|---|---:|---:|---:|---:|---:|
| Breakout 5m | +0.0247 | 0 | -0.0142 | 0 | -0.2020 |
| Breakout 15m | +0.0403 | -0.0799 | -0.0164 | 0 | -0.4847 |
| Breakout 30m | -0.1696 | -0.0891 | -0.1341 | 0 | -0.3259 |
| Trend-pullback 5m | 0 | 0 | 0 | 0 | -0.1170 |
| Trend-pullback 15m | -0.0712 | -0.1095 | -0.0199 | -0.0938 | -0.3256 |
| Trend-pullback 30m | 0 | -0.0759 | -0.0780 | -0.1343 | -0.2241 |

All failed the positive-return, positive-period and minimum-30-trade criteria.
Moving-block bootstrap intervals were correctly withheld for fewer than 30 trades.
The implemented bootstrap uses blocks of up to 10 trades within individual
periods, 2,000 replicates, seed 0, and Bonferroni adjustment over six candidates.
It cannot establish an edge from these sparse observations.

All exceeded full-allocation buy-and-hold returns on average, but still lost money
and failed the zero-return cash baseline. The hold benchmark allocates 100% while
the strategy caps exposure near 10%; relative outperformance in falling markets
therefore does not establish skill. Smaller losses than earlier experiments are
also not a controlled improvement comparison: evaluation windows and exposure differ.

## Verification

- Focused mechanics/backtester tests: **23 passed** (10 new tests).
- Full package-aware suite: **2,219 passed, zero skips** using Python 3.11.15.
- Independent verification of all **60** saved ledgers: trade P&L reconciles to
  realized P&L and ending-minus-starting equity; trade fees reconcile to fees;
  trade counts and entry/exit boundaries match.
- Compilation and `git diff --check` passed.
- An initial discovery invocation without `-t .` produced relative-import errors;
  corrected package-aware discovery passed. An initial new test expected an
  intrabar stop for a gap-open fixture; corrected to the existing `stop_gap` result.

Commands used:

```sh
.venv/bin/python -m trading.app.cost_research prepare config/cost-aware-development-plan.json
.venv/bin/python -m trading.app.cost_research run config/cost-aware-development-plan.json --output artifacts/cost-aware-development-results.json
.venv/bin/python -m unittest tests.test_cost_research tests.test_backtest -q
.venv/bin/python -m unittest discover -s tests -t . -q
```

The preparation and execution commands now intentionally refuse to overwrite
their existing artifacts. Do not change the plan or rerun with new parameters to
find a winner against these results.

Files added: `trading/strategy/cost_aware.py`, `trading/app/cost_research.py`,
`tests/test_cost_research.py`, the plan, results and this report. Updated existing
backtester decision cadence and `docs/FINAL_COMPLETION_AUDIT.md`. Unrelated prior
working-tree changes were preserved. No rejected model, protected observation,
production trading strategy, credential configuration or live-order path was changed.

## Next milestone

Do not promote any candidate or promise paper/live profit. First acquire a longer,
continuous, genuinely unseen development extension with explicit protected-date
exclusions, or start authorized public-data-only forward collection. Review
coverage and verify actual fee assumptions before preregistering another bounded
experiment. Repeated experiments on this explored source increase overfitting risk.

Only a future qualifying frozen candidate should advance to separately authorized
new confirmation and forward paper observation (planning minimum: 60 days and 100
closed trades, not sufficient by itself). Existing confirmation and protected ML
Segment 7 remain untouched. Positive simulations would still not guarantee live
results or eliminate capital-loss risk.
