# Public observation quality gate — 0.2.5

Completed an automatic research quality check for the timing and outage defects
identified during 0.2.4. No new network request, Keychain access, model fitting,
paper/live order, strategy change or protected dataset access was performed.

## Implemented

`trading/app/depth_costs.py` now emits version 2 reports with an explicit
`research_observation_quality` result. Existing reports and captures are retained.
Static sweep statistics remain diagnostic when quality fails. Live qualification
remains false even for a capture that passes this limited research check.

Fixed policy for subsequent observations:

- Maximum five seconds between received snapshots, including startup/tail silence.
- Maximum two seconds of wall/monotonic clock divergence.
- Requested duration covered on both clocks; timezone-aware boundaries required.
- No feed-error polls or reconnect attempts; combined observer must pass.
- Consistent observer market identity and snapshot counts.
- Nondecreasing receive and exchange timestamps; receive times inside boundaries.
- At least two fresh unique books; no stale snapshots.

These are conservative research acceptance rules, not a CoinSwitch service
guarantee. Applying them to the old capture is retrospective diagnosis, not a
preregistered successful experiment. They do not change strategy acceptance rules
or imply that five-second-old liquidity is safe to trade.

Added a hash-required offline command. Output collisions and protected input paths
are rejected before reading, including symlinks pointing to protected filenames.
No public collection or private account request is triggered by offline analysis.

## Actual result

Reassessed the unchanged original capture:
`artifacts/public-depth-capture-2026-11-12.json`, SHA-256
`b68b662a78673808c10f44a43635479bcb0feb7b162382542a39c1ac58311b51`.

**Research quality FAIL**, command exit 2. Reasons: clock divergence, two
feed-error polls, a reconnect, excessive receive gap and combined observer failure.
Wall span: 563.737132 seconds; monotonic observation: 120.002265 seconds;
divergence: 443.734867 seconds; maximum receive gap: 506.978 seconds.
The underlying cause of clock divergence remains unverified.

New derived report: `artifacts/public-depth-capture-2026-11-12.quality-v2.json`,
file SHA-256 `9628998d058d42a5e2008ab3875d7edfc16a4dbadb721b1ce36562413e6ddfb5`.
The original source file hash was independently rechecked after assessment.

```sh
.venv/bin/python -m trading.app.depth_costs artifacts/public-depth-NEW-quality.json \
  --input artifacts/public-depth-capture-2026-11-12.json \
  --sha256 b68b662a78673808c10f44a43635479bcb0feb7b162382542a39c1ac58311b51
```

Use a new output path. Exit 2 is the expected failure result for this capture.

## Verification and delivery

Focused suite: 76 passed. Final full suite: **2,295 passed, zero skips**.
Five additional test methods exercise valid synthetic continuity, clock
divergence, startup/tail/middle gaps, reversed/out-of-bound timestamps, missing or
invalid metadata, identity/count mismatches, offline hashes and protected symlinks.
Synthetic passing fixtures are test evidence only, not genuine feed qualification.
Offline package, local dashboard and synthetic crash/restart drills passed.
117 packaged modules match source; dependency and isolated import checks passed.

Installed wheel:
`dist/quality-ready-0.2.5/safety_first_trading_app-0.2.5-py3-none-any.whl`.
SHA-256 `0636a2e648276a18e6037cf2169448e2a0cf2323c4bb087049b355aaa3e8da91`.
Release evidence: `artifacts/release-0.2.5-quality-ready-verification.json`.

Changed: `trading/app/depth_costs.py`, `tests/test_depth_costs.py`,
`trading/app/release.py`, `pyproject.toml`, completion/operating documentation,
and the new derived quality report. Existing unrelated working-tree changes were
preserved. Compilation and whitespace checks passed.

## Remaining work

No new genuine capture passed this milestone. Next, preregister and execute
multi-period public observations on an awake, clock-stable host; assess every
period independently, retaining failures rather than selecting only good windows.
Do not replace historical costs with these current-book observations.
No strategy has qualified for profitability; missing original candles and
unverified exchange sandbox access/permissions remain blockers. Live execution
remains disabled; no model promotion or threshold optimization was performed.
