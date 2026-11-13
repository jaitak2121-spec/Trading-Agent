# Public collection cohort and timing correction — 0.2.6

Executed the fixed three-period protocol in
`config/public-depth-cohort-2026-11-12.json`, recorded before collection. Each
period requested 120 seconds, with 30-second pauses; BTC/USDT c2c1 public data
only, fixed 100/500/1,000 USDT midpoint sizes and 10bp fee scenario. No new fee
lookup, Keychain access, paper/live orders or protected observation access.

macOS `caffeinate -i` requested idle-sleep inhibition for the finite collector;
an active assertion was observed. It does not guarantee protection from forced
sleep, host suspension or clock adjustments. Exact causes of large timing
discontinuities remain unverified.

## Original cohort: rejected, retained unchanged

| Period | Snapshots / fresh unique | Closed candle messages | Feed-error polls | Reconnect attempts | Combined observer | Original quality gate |
|---|---:|---:|---:|---:|---|---|
| 1 | 94 / 94 | 1 | 3 | 2 | FAIL | FAIL |
| 2 | 218 / 218 | 2 | 0 | 0 | PASS | FAIL |
| 3 | 218 / 218 | 2 | 0 | 0 | PASS | FAIL |

No stale quotes or exact duplicates were excluded in any period. All three
predefined windows were retained, with no replacement attempts or pooled cost
estimate. The original cohort remains rejected under its fixed protocol.
Windows were nearby and correlated; they do not represent independent market
regimes or establish realized fill quality, profitability or live readiness.

Period 1 spanned 5,930.216 seconds of wall time versus 120.010 monotonic seconds;
its maximum receive gap was 5,878.486 seconds. Periods 2 and 3 each spanned about
125 wall seconds versus 120 monotonic seconds, with maximum gaps about 5.83
seconds. Wall pauses between periods were 1,595.597 and 30.210 seconds. Therefore
the requested idle-sleep inhibition did not establish a clock-stable collection
environment for the whole cohort.

Artifact directory: `artifacts/public-depth-cohort-2026-11-12/`.
Each period has its raw capture, diagnostic report and execution log. The
execution manifest binds the original analyzer code hash and protocol hash.
Independent verification regenerated all reports using that implementation,
checked source/report hashes, period ordering, actual wall separation, market,
cost settings, quality policy, exit status and safety flags.
Verification file SHA-256:
`dde43f4b581b347939c459762fb319398a9ae1ec6ea09a23be81315bc107f2c7`.
The protocol file SHA-256 is
`5a71c80a4715e8248e0125c163b20cdc0769ab1b720fdae040c233c253fb0d8f`.

## Integration defect found and fixed

The original gate compared the observer's elapsed monotonic time, captured before
`stream.stop()`, with wall time ending after transport cleanup. The bounded
five-second shutdown wait could falsely appear to be clock divergence and tail
silence. This explains a measurement defect consistent with the last two windows;
it does not retroactively certify those windows, which lack the exact markers.
It also cannot explain period 1's much larger discrepancy.

The observer now emits nanosecond wall start/end markers for the observation
itself, with the end captured before cleanup. The depth gate uses those same
boundaries on both clocks and retains outer capture span separately. Partial,
invalid or out-of-bound markers fail closed. Old captures keep a clearly labeled
legacy timing basis; no missing historical end markers are invented. The clock
tolerance remains two seconds and maximum receive gap remains five seconds.

New depth reports use schema `public-visible-depth-costs/v3`, with timing schema
`public-depth-quality/v2`. Default strategy, risk and order behavior is unchanged.

## Separate prospective pilot after correction

Recorded `config/public-depth-timing-pilot-2026-11-12.json` before one additional
120-second measurement regression pilot. This was a separately labeled test,
not a replacement window in the original cohort. No repeated pilot attempts.

**Pilot research quality PASS**, command exit 0: 216 fresh unique snapshots,
zero duplicates/stale samples, two closed candle messages, 119 polls, zero
feed errors or reconnects. Monotonic observation lasted 120.004446 seconds;
wall observation lasted 120.004179 seconds. Clock divergence was 0.000267 seconds
(about 0.27ms), maximum receive gap 1.2091 seconds. Outer capture wall span was
125.013472 seconds, demonstrating why cleanup must be tracked separately.
The strict two-second/five-second limits were not changed.

Artifacts:

- `artifacts/public-depth-timing-pilot-2026-11-12.json`, raw SHA-256
  `8c197728b96337b23782e0648b1f751a7adae9390a6f7bd8620a3534630a35f6`.
- `artifacts/public-depth-timing-pilot-2026-11-12.costs.json`, report SHA-256
  `03897b9934fe2c2662ed45a9b7ccc62d8c11556bdd79fb37adff99eb45471bb6`.
- `artifacts/public-depth-timing-pilot-2026-11-12.verification.json`, independent
  source/report/plan/code hash and boundary checks, with deterministic report
  regeneration. All receive timestamps lie within the exact observation window.

Per-side median modeled cost including the explicit 10bp fee remained about
10.00069bp at the three fixed sizes. No actual fills were obtained. The short
pilot validates collection/timing in this window; it does not establish
multi-period eligibility, persistent feed reliability, profitability or trading
permissions. The original cohort stays rejected and its verification hash was
checked again after the fix.

Reproduce a future pilot with a new filename:

```sh
/usr/bin/caffeinate -i .venv/bin/python -m trading.app.depth_costs \
  artifacts/public-depth-NEW-pilot.json --seconds 120
```

Do not overwrite or automatically replace failed observations. For offline
reassessment use `--input CAPTURE --sha256 EXACT_HASH` with a new report path.

## Tests and release

Before correction: 43 relevant tests and all 2,295 tests passed. After correction:
**47 focused tests; 2,299 full-suite tests passed, zero skips**. Four new tests
verify that cleanup does not extend observation time, true clock divergence still
fails, invalid markers cannot fall back to legacy timing, and received messages
outside observation boundaries are rejected. Fixtures remain synthetic test data.

Offline wheel, local dashboard and synthetic crash/restart drills passed; all
117 packaged modules match source. Installed version 0.2.6, dependency and
isolated import checks passed. Wheel:
`dist/timing-ready-0.2.6/safety_first_trading_app-0.2.6-py3-none-any.whl`;
SHA-256 `5984394941c8e471ed9bcfbd1ba95eeb02d4709058d6eebf81121c555f0084d9`.
Release evidence: `artifacts/release-0.2.6-timing-ready-verification.json`.

Changed source: `trading/app/coinswitch_observation.py`,
`trading/app/depth_costs.py`, `trading/app/release.py`, `pyproject.toml`.
Tests changed: `tests/test_coinswitch_observation.py`, `tests/test_depth_costs.py`.
Added fixed cohort/pilot protocols, genuine public evidence and documentation.
Existing unrelated changes, rejected models and sealed artifacts were preserved.

Compilation and whitespace checks passed. No dependencies were installed;
the verified local wheel was installed offline with `--no-index --no-deps`.

## Remaining and next milestone

Next: preregister a new cohort with the corrected measurement boundaries,
separated across genuinely different periods on a stable host. Retain all
outcomes and require every predefined window to pass; do not reuse these short
snapshots as historical execution costs or as model-selection data.
No strategy has qualified for profitability. Original missing candles remain
unrecovered, CoinSwitch sandbox/account permissions remain unverified, and actual
live execution stays disabled. No model fitting, promotion or threshold tuning
was performed.
