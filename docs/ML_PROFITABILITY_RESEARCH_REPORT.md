# Executed ML net-expectancy research — 0.3.0

Date: 2026-11-12. **Outcome: no candidate met the frozen qualification criteria.**
This was an implementation and experimentation cycle, not a readiness audit:
eight studies, 38 outer candidate policies/algorithms, 542 outer-fold evaluations,
1,545 cost/execution-scenario ledgers, 285 saved trained-model artifacts and
5,141 independently reconciled simulated trade records. No model was promoted.

The available verified evidence is insufficient to identify a statistically
credible positive net edge for these long-only spot strategies. This does not
prove that no profitable crypto strategy exists. Additional searches on the same
consumed folds would not substitute for new information or independent evidence.

## What failed before, and what changed

The original one-minute logistic model had near-random ranking and down/flat
prediction collapse in the previously recorded validation/robustness results.
Its three short price features and next-candle direction label did not optimize
payoff magnitude, execution timing, costs, or selective entry. The later V2
direction experiment expanded features but still targeted classification.
The consumed V2 confirmation observations were not reopened for this work.

The frozen six-rule native-five-minute experiment lost under both base and
stressed costs. Its frozen fills also lost before fees while retaining modeled
spread/slippage. Consequently, lower fees alone were not a credible repair.
Short horizons, frequent turnover, narrow price moves and unstable regime
relationships were investigated through materially different experiments below.

Implemented/reused:

- Cost-adjusted trade-outcome labels instead of merely predicting up/down.
- Training-only standardized ridge regression, the existing logistic trainer
  generalized to conditional win/loss payoffs, shallow regression trees,
  deterministic three-cluster regime expectancy, and boosted return trees.
- A separate versioned causal feature schema with explicit native cadence.
  Frozen V1/V2 schemas and artifacts were not replaced.
- 1/6/24-hour tests on native five-minute bars, then 24/72-hour hourly tests,
  and nested selection extending to seven-day holding periods.
- ETH relative-market context, settled funding and public aggressor-flow inputs.
- Cash/no-trade decisions, a fixed trend-filter comparison, volatility stops,
  conviction scaling and inner-selected entry buffers/sizing within existing caps.
- Automatic fitting, serialization, forecasting, realistic-cost simulation,
  comparison, rejection and a hash-verified registry/dashboard summary.

The literature supplied hypotheses, not performance evidence for this app:
[Liu and Tsyvinski's crypto-return study](https://www.nber.org/papers/w24877)
motivated longer-horizon and market-context tests. The multiple-search problem in
[Bailey et al.](https://www.davidhbailey.com/dhbpapers/backtest-prob.pdf) motivated
frozen protocols, disclosed failures, nested selection and stricter intervals.

## Genuine data actually acquired

CoinSwitch uses the documented authenticated GET-only
[`/trade/api/v2/candles`](https://api-trading.coinswitch.co/spot/reference/candles)
interface. Exact Keychain services were resolved only for the historical reader
process, using the current Mac account. No keys, secrets, signed headers,
balances or account order data were persisted. No additional account-fee lookup.

| New CoinSwitch parent | Requested UTC range, end exclusive | Actual / expected candles | Original integrity |
|---|---|---:|---|
| BTC, native 5m | 2024-01-01–2025-03-01 | 121,975 / 122,400 | FAIL; 425 missing, 162 internal gaps |
| BTC, native 1h | 2024-01-01–2025-03-01 | 10,198 / 10,200 | FAIL; two missing hours |
| ETH, native 1h | 2024-01-01–2025-03-01 | 10,198 / 10,200 | FAIL; two missing hours |
| BTC, native 1h | 2021-01-01–2024-01-01 | 14,609 / 26,280 | FAIL; early history unavailable and 13 internal gaps |
| ETH, native 1h | 2021-01-01–2024-01-01 | 11,242 / 26,280 | FAIL; early history unavailable and 10 internal gaps |

The actual older BTC response begins 2022-04-30 19:00 UTC. The two-day January
2021 probe returned zero candles. The source coverage does not establish that
missing timestamps had no trades. A mistaken probe assumption in the acquisition
approval description was corrected; no 2021 market observations were invented.

Original parents remain failed and unchanged. Coverage-only derivation created
explicit, separately complete requested ranges. Each derivative independently
passes expected coverage, OHLCV, ordering, duplication and source/hash checks.
No rolling feature, label, position or valuation bridges a gap. Short pieces
without sufficient warm-up are excluded openly. The original four missing
one-minute candles from earlier milestones remain unrecovered.

The original two complete May–November 2025 BTC five-minute runs were reused
only as development evidence in study 1. Later studies use separate pre-March
2025 hourly development sources. Reused development is not untouched confirmation.

Additional genuine public information:

- **4,560 BTCUSDT settled funding events**, 2021-01-01 through 2025-03-01,
  obtained through Binance's unsigned
  [funding-history endpoint](https://developers.binance.com/docs/derivatives/usds-margined-futures/market-data/rest-api/Get-Funding-Rate-History).
  Strict ordering, unique timestamps, coverage and eight-hour cadence with
  timestamp-jitter tolerance were validated. Each rate is unavailable to the
  model until one hour after settlement. Funding is information only: spot
  positions receive no funding income in any ledger.
- **25,559 BTCUSDT public hourly kline/flow records** from 2022-04-01 to
  2025-03-01, through the documented
  [spot kline interface](https://developers.binance.com/docs/binance-spot-api-docs/rest-api/market-data-endpoints).
  The expected count was 25,560. One record covers only 2,381,647ms, rather than
  an hour, starting 2023-03-24 12:00 UTC. The raw parent is FAIL. That partial
  record was quarantined byte-for-value; the 25,558 complete records retain two
  explicit unavailable hours. Only separately validated complete derivatives
  feed the models, with another hour of publication lag.

Binance data never replaced CoinSwitch execution candles. Venue equivalence was
not assumed. Funding/flow remain separate providers with explicit symbol, native
time semantics, source hashes and availability assumptions.

Independent provenance verification matched nine acquisitions to cached raw
response hashes/native records and matched 23 complete CoinSwitch derivatives
exactly to their original parent candle values:
`artifacts/research-registry/acquisition-provenance-verification.json`.

## Features, labels and execution

The 18 base features use completed consecutive native bars: returns at
1/3/12/48/288 bars; volatility at 12/48/288 bars; volume ratios at 12:48 and
48:288; moving-average relationships at those periods; candle range/body/wicks;
normalized trend strength and a volatility ratio. A 289-bar context is required.
Native seconds are recorded and checked: five-minute and hourly observations
cannot silently exchange feature meanings or enter the old one-minute model.

ETH context adds seven exact-time causal features: ETH 24/72-hour returns,
relative BTC/ETH 24-hour return, correlation, relative volatility, volume ratio
and breadth. Funding adds six lagged level/mean/change/age features. Order flow
adds six lagged aggressor imbalance, activity, trade-size and volume features.
Unavailable auxiliary windows produce no trade; qualification requires at least
95% outer feature coverage. No forward filling is permitted.

Training targets are per-unit net returns from the next native open to the
causal volatility stop or a fixed holding-period exit. Stop levels are computed
from information at decision time. Targets include both fees, spread and
slippage and match the existing simulator in deterministic parity tests.
Full potential label horizons are purged at training/validation boundaries,
even if the historical stop would have exited earlier.

Execution reuses `HistoricalBacktester` and `SignalStrategy`, with fresh 10,000
USDT per validation period. Existing 1,000-USDT order/position/exposure caps,
100-USDT daily-loss budget and 0.5% stop-risk budget remain in force. Full-risk
research settings use that existing budget, not the whole account. No leverage.

Base per-side assumptions: 10bp fee, 2bp slippage, 2bp full spread; about 26bp
round-trip at unchanged prices. Stress: 20bp fee, 5bp slippage, 4bp full spread;
about 54bp round-trip. Later studies additionally delay execution to the next
native close under stressed costs. Current verified BTC fees support the base
scenario, not historical or ETH fee guarantees. ETH fees and venue precision
remain unverified; the simulator's 1e-8 lot step is a research assumption.

No broker, gateway or exchange-order transport is invoked. Stored trades are
historical simulations. The first strategy decision follows the simulator's
trading-window start; a boundary forecast is not retroactively executed before
that point. Taxes/TDS, queues, hidden depth and actual fill settlement remain
unmodeled. Hourly bars cannot establish an intrahour execution path.

## Actual results

Returns below are arithmetic means of separately funded folds, not a compounded
account or annual forecast. The first three rows show representative candidates;
the registry contains **every** candidate, including no-trade and failed cases.
Studies 4–8 each test one adaptive outer algorithm; internal model/parameter
choices are made exclusively within prior data.

| Study | Candidate/algorithm | Folds × days | Base mean net return | Stress mean net return | Base trades | Positive base folds | Qualified |
|---|---|---|---:|---:|---:|---:|---|
| 1: cost-targeted 5m | 24h regression tree | 9 × 15 | -0.01872% | -0.09587% | 61 | 5 | No |
| 2: native hourly | 72h regression tree | 16 × 15 | +0.09088% | +0.06522% | 44 | 12 | No |
| 3: ETH context | 72h regression tree | 16 × 15 | +0.06509% | +0.03873% | 48 | 11 | No |
| 4: nested adaptation | Model + 24/72/168h selection | 16 × 30 | -0.00858% | -0.01068% | 5 | 0 | No |
| 5: funding context | Nested adaptive policy | 16 × 30 | -0.01270% | -0.01550% | 8 | 0 | No |
| 6: entry/sizing optimization | Nested boosted/model/buffer/risk policy | 16 × 30 | +0.04019% | +0.02730% | 11 | 1 | No |
| 7: aggressor flow | Nested 6/24/72h policy | 16 × 30 | +0.01035% | -0.01299% | 18 | 1 | No |
| 8: ETH trading market | Same nested process, ETH execution prices | 13 × 30 | -0.12319% | -0.13671% | 11 | 0 | No |

Every candidate failed the positive confidence-bound requirement. Several also
failed trade-count, fold-consistency or fixed 10%-capital hold comparisons.
Rejection does not depend solely on beating that benchmark. The benchmark is
not an exact match to each strategy's varying exposure.

For example, study 2's tree has a selection-adjusted **daily-account-return**
interval of about **[-0.94, +2.39] basis points/day**. Study 6's interval is
**[-0.83, +2.32]bp/day**; its positive average is concentrated in one outer fold.
These intervals are not intervals for the table's 15/30-day fold returns.

Cash/no-trade was selected in 15/16, 14/16, 13/16, 13/16 and 10/13 outer periods
in studies 4–8. The later selectors compared actual inner simulated net and
benchmark-excess returns under base, stressed and delayed-stress execution,
rather than selecting classifiers by accuracy. Study 6 searched 48 internal
settings per outer period; studies 7/8 searched 36 each. No outer threshold tuning.

## Regimes and remaining weaknesses

Exploratory, non-promotional diagnostics are saved in
`artifacts/research-registry/regime-diagnostics.json` for studies 1–7; study 8's
full entry-regime metrics remain in its fold records.

The hourly 72-hour tree's 16 uptrend-tagged trades contributed +145.36 USDT
across independent simulations, while 11 downtrend-tagged trades lost 18.31.
These are small, selected subgroups, not evidence for a new uptrend-only rule.
The preregistered trend-filter variants did not solve qualification. Regime tags
use the simulator's fixed 30-bar heuristic; early-window insufficient-history
tags do not imply that the model's warm-up was missing.

That tree forecast an average selected-trade net return of 3.15%, while the
realized average was 1.01%; ETH-context forecasts likewise overestimated selected
trade returns. This is consistent with unstable conditional payoffs and selection
bias, not proof of a single cause. Study 7's selected-trade mean forecasts were
much closer to outcomes, yet the portfolio still failed cost stress and
statistical consistency. Conditional calibration alone is not profitability.

Observed weaknesses include noisy price/volume information, overlapping labels
with fewer independent events than nominal rows, sparse selected trades, regime
drift, winner concentration and fee sensitivity. Risk limits kept simulated
losses contained but did not manufacture predictive information. None of these
results justifies removing costs, loosening risk, reusing protected tests, or
fitting a larger network until historical returns look good.

## Protocol, reproducibility and verification

Models fit only prior training rows; scaling is training-only. Initial folds
use 30-day training and 15-day validation. Hourly fixed studies use 60 days of
training plus native warm-up. Adaptive studies pool only gap-safe examples in
the prior 365 days, select within the last 90 inner days, refit the selected
model on prior development rows and evaluate on a separate 30-day outer period.

The new research used a predeclared sequential alpha budget of 0.01, spending
0.01/2^stage, with within-study candidate adjustment. Later intervals resample
whole validation periods and seven-day blocks. Replicates range from 10,000 to
200,000, seed zero. These are conditional development diagnostics; they do not
fully erase all prior adaptive research or establish market stationarity.

Frozen plans, source snapshots, data/feature/model/prediction hashes, preprocessing
parameters, fold outcomes, internal choices and rejection checks are retained.
The first stage-6 plan was superseded before fitting only to correct diagnostic
horizon names; candidate settings and acceptance criteria did not change.
Earlier model/data artifacts and registry versions were not rewritten.

Final registry: `artifacts/research-registry/net-expectancy-registry-v4.json`.
Its exact file SHA-256 is
`a1cf2860add0f308ab19942df2cb0df7456b1e7e1b2b901de0a30aae7b456e46`.
It verifies all frozen source snapshots and independently validates source/model
hashes, chronology, fees, P&L, ending equity and compounded daily-return accounting.
All 5,141 candidate trade-scenario records reconciled; none exceeded the existing
1,000-USDT entry cap or recorded a realized daily loss beyond 100 USDT.

Final regression suite: **2,328 passed, zero skips**. Added 28 research test
methods plus a release-content regression. Tests cover feature causality,
future perturbation, gaps, labels versus simulator fills, temporal purging,
native cadence, persistence, deterministic forecasts, auxiliary identities,
publication lags, partial-record quarantine, inner selection/cash fallback,
boosted/regime learning, sizing limits, independent ledgers and dashboard refusal
of promoted/modified research evidence. Compilation and whitespace checks passed.

Offline package, local dashboard and synthetic crash/restart drills passed.
All 126 packaged Python modules match source. The exact final wheel is installed
in `.venv`, with dependency and isolated-import checks passed:
`dist/expectancy-research-0.3.0-final/safety_first_trading_app-0.3.0-py3-none-any.whl`.
Wheel SHA-256:
`fa5aac9d9645aa98d22760fe8cc0a602608628ca8aa50b15c235b1f234217170`.
Release evidence:
`artifacts/release-0.3.0-expectancy-research-final-verification.json`.
Consolidated completion evidence:
`artifacts/research-registry/ml-expectancy-research-completion.json`.

No confirmation was opened: neither the old consumed confirmation, protected
Segment 7, nor the newly reserved March–April 2025 confirmation observations.
No live or genuine paper orders. Synthetic unit/release recovery drills only.

## Commands and application integration

The current ETH plan can be replayed in this unchanged workspace with a new output
directory. Older studies deliberately require their saved implementation snapshot;
their original code/data hash guards reject silently running changed code.

```sh
.venv/bin/python -m trading.app.eth_expectancy run \
  artifacts/research-registry/eth-expectancy-stage8-plan.json \
  --output artifacts/eth-research-repeat-NEW

.venv/bin/python -m trading.app.expectancy_registry \
  config/expectancy-research-registry-v3.json artifacts/research-registry/reverified-NEW.json

.venv/bin/python -m unittest discover -s tests -t . -q
```

The existing dashboard accepts the explicit final registry through
`--research-report artifacts/research-registry/net-expectancy-registry-v4.json`.
It checks the registry hash, displays bounded development summaries and always
returns `approved_strategy: None`. It does not deploy a model or read sealed data.

Research executors are `net_expectancy`, `hourly_expectancy`,
`cross_market_expectancy`, `adaptive_expectancy`, `funding_expectancy`,
`decision_expectancy`, `flow_expectancy` and `eth_expectancy`, all under
`trading.app`. Each supports `prepare`/`run` and refuses existing outputs.

Files added/changed: those eight executor/model modules, `expectancy_registry.py`,
`strategy_research.py`, `release.py`, `pyproject.toml`,
`tests/test_net_expectancy.py`, `tests/test_app_release.py`, registry specifications
in `config/`, README and completion/operating documentation. Genuine artifacts
and frozen source snapshots are under the explicit research directories above.
Existing production strategy behavior, risk configuration and live disablement
were preserved.

## Research outcome and what remains

This comprehensive cycle establishes insufficient available evidence for a
credible edge in the tested BTC/ETH spot information sets. It does not claim to
exhaust every possible market, dataset or future strategy. Further ordinary
parameter searches on these consumed folds would increase selection risk; they
would not create genuinely new validation evidence.

Well-defined remaining avenues are point-in-time historical depth/queue and
liquidity data, independently timestamped news/on-chain/attention signals,
survivorship-aware broader asset panels, or a new independently observed forward
period. These require information/validation beyond another fit on the same
price, volume, funding and aggregate-flow records. Derivative carry would require
separate verified margin/liquidation/fee accounting; it cannot be simulated by
crediting funding income to spot holdings.

The conditional requirement to deploy a legitimately validated candidate was
not met. Therefore no new research model entered paper execution and no approved
extended paper trial was started. Existing retraining/approval controls remain
in place; research weights are not automatically production-compatible or
approved. Live trading stays disabled. A new information-based research cycle,
then frozen unseen confirmation and prospective paper fills, is required before
any profitability or real-money readiness claim.
