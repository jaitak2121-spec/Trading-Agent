# Profitability research continuation after 0.3.0

2026-11-12. **No candidate qualified or entered paper trading.** Three new,
preregistered development studies distinguish ranking information from usable
payoffs, calibration uncertainty and event timing. This is executed research,
not another readiness claim. Original experiments were verified, not rerun.

## Prior evidence and priorities

Read the complete [eight-study findings](ML_PROFITABILITY_RESEARCH_REPORT.md)
and independently rebuilt its verified registry: 285 original models, 542
outer-fold evaluations, no qualifiers. Original registry file SHA-256 remains
`a1cf2860add0f308ab19942df2cb0df7456b1e7e1b2b901de0a30aae7b456e46`.

The strongest unresolved question was whether failed trading policies masked
information in frozen forecasts. Next came payoff calibration and settlement
timing: the previous nested studies used daily midnight decisions despite
lagged funding/flow publication. Another arbitrary price-indicator grid would
not answer either question. Independent on-chain publication history was also
investigated without credentials.

## 1. Frozen forecasts: information versus market drift

Protocol: `artifacts/research-registry/frozen-signal-information-plan.json`.
Results: `artifacts/frozen-signal-information-results/`.

Reproduced the original study-2 forecasts and exact training-row hashes without
refitting any model. Studied all six learner/horizon combinations. Outcomes are
non-overlapping on a fixed UTC epoch grid; correlations are centered within each
of 16 evaluation periods to remove pooled bull/bear level differences. Used
10,000 circular score shifts within periods and whole-period bootstrap samples,
seed 0. Six-test correction is local to this diagnostic; it does **not** erase
the selection history of previous research. Circular-shift stationarity and
period-bootstrap assumptions are limitations, not a confirmatory guarantee.

| Frozen family | Horizon | Outcomes | Within-period IC | Shift p | Six-test p |
|---|---:|---:|---:|---:|---:|
| Ridge | 24h | 224 | 0.21644 | 0.01130 | 0.06779 |
| Tree | 24h | 224 | 0.00696 | 0.91671 | 1 |
| Logistic payoff | 24h | 224 | **0.24443** | **0.00370** | **0.02220** |
| Ridge | 72h | 64 | 0.22556 | 0.28677 | 1 |
| Tree | 72h | 64 | 0.36116 | 0.00920 | 0.05519 |
| Logistic payoff | 72h | 64 | 0.04825 | 0.79042 | 1 |

The logistic 24h period-bootstrap 95% IC interval is [0.11056, 0.34659]. Its
raw no-stop IC is 0.24250: stop placement does not explain this ranking result.
The matched 48h momentum control's net IC is -0.03135. Conditional regime/score
tercile cells are saved, including sample counts and descriptive fee break-even
levels. Tercile cuts and volatility regime medians use training rows only.
Cells are exploratory descriptions, not newly validated entry rules.

Information does not establish a trading edge. The same 224 opportunities have
mean net stop return -0.00841% versus +0.19177% before fees with the same fills.
No-fill cases receive no fictitious fee credit. Raw no-stop comparisons change
the outcome definition and are diagnostic only.

## 2. Stage 9: independent prior payoff calibration

Protocol: `artifacts/research-registry/calibrated-expectancy-stage9-plan.json`.
Results: `artifacts/calibrated-expectancy-stage9-results/`.

One new candidate: existing 24h logistic payoff learner. For each 30-day outer
period, train on the prior 365-day window excluding its final 90 days. Calibrate
on those 90 days' independent daily forecasts; do not refit after calibration.
Use nonnegative affine payoff calibration and 1,000 three-day moving-block
bootstrap fits. Blocks never cross missing daily observations. Require at least
60 independent calibration outcomes and no score extrapolation. Entry requires
the 5th-percentile conditional-mean estimate above a fixed 28bp additional stress
cost allowance. No outer threshold selection or learned regime gating.

The existing simulator retains stops, sizing/risk caps, fresh equity each fold,
base 10/2/2bp fee/slippage/spread, stress 20/5/4bp and delayed next-close stress.
One matched uncalibrated control uses exactly the same fitted learner and
decision times. It is an ablation, not another selected candidate.

| Policy | Base mean 30d return | Stress | Delayed stress | Trades per scenario |
|---|---:|---:|---:|---:|
| Calibrated uncertainty gate | 0% | 0% | 0% | **0** |
| Matched uncalibrated control | -0.02018% | -0.04826% | -0.04121% | 71 |

Sixteen folds; 14 model/calibrator pairs. Two folds lacked sufficient independent
calibration observations and correctly remained cash. The 14 available periods
had only 70–89 calibration outcomes. Nine affine point fits had zero slope.
Of 405 outer forecasts, 11 were outside the calibration score range. The largest
lower-bound estimate among the other 394 was only **6.83bp**; none cleared 28bp.
Ten lower bounds exceeded zero, which still did not justify cost-stressed entry.
These results were not used to relax the gate.

The calibrated policy failed trade-count, positive-period, benchmark-excess and
positive-interval criteria. Its cash return is not an edge. The control had
7/16 positive base periods, 5/16 stressed periods and negative average benchmark
excess. Its selection-adjusted daily-account-return intervals cross zero:
base [-0.77835, +0.36020]bp/day, stress [-0.92881, +0.26207], delayed
[-0.89037, +0.34404]. Used 100,000 bootstrap replicates and preregistered stage-9
alpha 0.00001953125; extreme tails have limited Monte Carlo resolution.

Across independent control accounts, base net P&L was -32.28459 USDT with
32.48051 USDT fees: before-fee P&L retained spread/slippage and was only
+0.19592 USDT. Stressed before-fee P&L was already negative. Thus fees consume
the tiny base gross result, but cheaper fees alone do not establish robust edge.
These are sums across reset accounts, not a compounded deployed portfolio.

## 3. Stage 10: settlement timing and incremental information

Protocol: `artifacts/research-registry/event-information-stage10-plan.json`.
Results: `artifacts/event-information-stage10-results/`.

Fixed ridge L2=1, training-only scaling and prior 365-day purged training.
Price control: 12h return, 48h volatility, trend strength. Auxiliary additions:
settled funding 7-day z-score, lagged 24h aggressor imbalance and 6h-minus-24h
imbalance. Inputs remain informational Binance records; execution prices remain
genuine CoinSwitch candles. No funding income credited to spot holdings.

For 3/6h horizons, compare 02/10/18 UTC settlement decisions to 00/08/16 clock
controls; for 24h compare 02 to 00 UTC. Funding must be at least two hours old
at settlement decisions, allowing recorded millisecond timestamp jitter; flow
retains its complete-hour plus one-hour lag. Outcomes do not overlap within a
schedule. The compared schedules are correlated, not independent replications.

| Horizon / schedule | Outcomes | Price IC | Auxiliary IC | Auxiliary corrected p | Auxiliary MSE improvement over price |
|---|---:|---:|---:|---:|---:|
| 3h / clock | 1,439 | 0.04835 | 0.06325 | 0.19558 | +4.82e-8 |
| 3h / settlement | 1,440 | 0.02758 | 0.02327 | 1 | -6.72e-8 |
| 6h / clock | 1,439 | 0.04412 | 0.05517 | 0.45475 | +6.08e-8 |
| 6h / settlement | 1,424 | 0.01144 | -0.01139 | 1 | -3.47e-7 |
| 24h / clock | 463 | 0.09594 | 0.11753 | 0.40916 | +1.06e-6 |
| 24h / settlement | 464 | 0.06056 | 0.03497 | 1 | -3.38e-6 |

192 fitted model artifacts: 12 fixed information configurations × 16 periods.
No information test survived the preregistered 12-test correction. Auxiliary
information worsened MSE relative to the price control at all three settlement
horizons. This rejects the proposed timing repair in these data; it is not a
proof about sub-hour event signals, other features or future periods.
Validated auxiliary coverage was 12,565/12,565 eligible source/schedule/horizon
rows before outer-fold restriction; this includes training and reused horizon
views, not 12,565 independent evaluation outcomes. Saved every outer forecast,
fold identifier, timestamp, outcome, model hash and preprocessing parameters.

## Independent source investigation and statistical power

Coin Metrics Community API probe returned HTTP 403, exact response hash retained
in `artifacts/onchain-availability-probe.json`; the reason is unverified.
The publisher's [public archive documentation](https://github.com/coinmetrics/data)
describes daily CSV archives and CC BY-NC 4.0 licensing. Uncredentialed official
GitHub metadata requests succeeded, but querying BTC CSV commit history before
2025-03-01 returned an empty list. Latest returned BTC CSV commit was dated
2026-05-24. No historical CSV observations were downloaded or fitted. A current
revised archive cannot be represented as a verified publication-time vintage.
Evidence: `artifacts/onchain-public-archive-probe/`. This does not establish that
no provider or alternate archive offers suitable vintages. Commercial use also
requires compatible licensing. No API credentials were accessed for these checks.

The 24h independent opportunity outcomes have sample standard deviation 2.8343%.
A simple Gaussian independent-sample calculation for a 10bp mean, 80% power and
two-sided 5% level gives approximately **6,306 independent outcomes**:
`((1.96 + 0.8416) * 0.0283433 / 0.001)^2`. This is an illustrative optimistic
scale calculation, not a universal sample-size requirement. Serial dependence,
multiple searches, selective entries, regime shifts and execution uncertainty
can require substantially more. Hundreds of overlapping hourly labels do not
create hundreds of independent 24h outcomes. Seventy to 89 calibration days
cannot tightly resolve a small payoff under these conditions.

## Integrity, tests and reproducibility

New verified registry:
`artifacts/research-registry/post-v030-continuation-registry.json`, file SHA-256
`6afd6f57e5b1ffc3fbe391ccdc41fd98a98e82a0090a55c47ae0f47ebf4d3855`.
It hashes every new output and plan, verifies complete source datasets and saved
models, freezes source snapshots, checks learner/calibration chronology and
independently reconciles all **96 new execution ledgers / 213 trade-scenario
records** using the existing Decimal accounting/risk audit. No notional-cap or
realized daily-loss breach found. Added **206 models** (14 calibration-study and
192 information-screen models); diagnostic model fits are not trading policies.

Result content hashes:

- Frozen information: `77d5d52fa9621af1ca384e25d5c8399cbfd7789da9a08d9afd61e0ff1a3ec4e0`.
- Stage 9: `dbb92adfe3697fbe7bc5ea875199c18ffc677bfc0c22539ffe3e1647ecede207`.
- Stage 10: `92adcf24efcdd17f0077de171124a52e3722becd7b157b9ffcc4d03fe3ddd3be`.

Focused suite: **88 passed**. Full package-aware suite: **2,341 passed, zero
skips**. Thirteen new tests cover synthetic planted information controls, period
centering, deterministic inference, non-overlapping labels, exact timing,
calibration chronology, gaps, provenance and extrapolation refusal. A first
focused command named two nonexistent test modules; corrected to the existing
consolidated tests before experiment execution. No passing result was invented.

Changed source: `trading/app/signal_information.py`,
`trading/app/calibrated_expectancy.py`, `trading/app/event_information.py`,
their three matching test files, `scripts/verify_research_continuation.py`, this
report and links/status notes in README/completion audit. No dependencies added,
production strategy changed or release wheel rebuilt. These additions are source
research tooling beyond the retained 0.3.0 wheel.

Replay in a new output directory (existing destinations are refused):

```sh
.venv/bin/python -m trading.app.signal_information run \
  artifacts/research-registry/frozen-signal-information-plan.json \
  --output artifacts/frozen-information-REPLAY-NEW
.venv/bin/python -m trading.app.calibrated_expectancy run \
  artifacts/research-registry/calibrated-expectancy-stage9-plan.json \
  --output artifacts/calibration-REPLAY-NEW
.venv/bin/python -m trading.app.event_information run \
  artifacts/research-registry/event-information-stage10-plan.json \
  --output artifacts/event-information-REPLAY-NEW
.venv/bin/python scripts/verify_research_continuation.py \
  artifacts/research-registry/continuation-REVERIFY-NEW.json
.venv/bin/python -m unittest discover -s tests -t . -q
```

Plans bind code/data hashes. Replaying verifies reproducibility; it does not add
independent evidence. Original rejected artifacts, consumed confirmation,
Segment 7 and reserved March–April 2025 confirmation remain untouched. No genuine
paper or live orders, model promotion, threshold tuning on outer data, or change
to production execution. Full tests include synthetic execution fixtures only.

## Outcome and next concrete opportunity

The observed ranking signal is the strongest new lead, but prior payoff
calibration is unstable and fails to support cost-stressed entries. Event timing
does not rescue funding/flow information. This distinguishes some weak conditional
information from the much stronger claim of executable, statistically credible
net expectancy. Available evidence remains insufficient for that claim.

Further grids over the same price/funding/flow history have diminishing evidential
value. The next concrete opportunity is a **new prospectively timestamped
microstructure dataset**: existing public depth capture plus synchronized quotes,
native trades and measured receive times, with gaps/staleness audited. Predefine
a low-dimensional liquidity/flow hypothesis, collect contiguous periods across
regimes, and test information before fitting a policy. The current depth tooling
provides a starting point; it does not provide historical queues or actual fills.
Alternatively obtain provider-licensed point-in-time network/news vintages.
Both supply information or independent periods missing here. Do not reopen the
reserved confirmation merely to search for another rule. No candidate is eligible
for extended paper validation until frozen unseen net-performance criteria pass.
