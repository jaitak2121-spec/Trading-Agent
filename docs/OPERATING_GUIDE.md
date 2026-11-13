# Trading Agent operating guide

The supported product is local automated paper trading plus offline research.
Current source/release handoff: `EXECUTION_COST_READINESS_REPORT.md` (version 0.2.3).
The final wheel is in `dist/cost-ready-0.2.3/`; earlier distributions are retained.
Real-money execution is absent. There is currently no approved predictive ML
model. The default strategy is a research baseline, not a proven profitable
system. All recorded research and rejected artifacts must be retained.

Future V2 fitting and paper deployment now require hash-bound passing source-
integrity evidence. Missing legacy provenance cannot be bypassed by editing an
old model. Native five-minute research remains separate from the one-minute ML
feature schema. The new offline exchange contract has no HTTP order client and
cannot be selected as a live broker through this launcher.
New V1 models also bind one-minute cadence; legacy artifacts without that
provenance cannot be approved by adding fields to their frozen files.

Version 0.2.0 adds V2 paper inference, operational monitoring alerts, opt-in
desktop notifications, funded spot fills and exclusive database writer ownership.

## Install and launch on macOS

Python 3.11+ is required. Python 3.11 is available on the development Mac; its
setuptools installation also supports release builds.

```bash
cd "/Users/jaitak/Documents/Trading app"
python3.11 -m venv .venv
.venv/bin/python -m pip install .
./scripts/start-paper.command
```

Open `http://127.0.0.1:8766/`. The launcher now uses genuine CoinSwitch BTC/USDT
public data, a separate USDT paper database and the bounded learning scheduler.
It assumes 10bp fees per fill plus 2bp slippage and crosses observed bid/ask.
See [the v0.3.2 report](REAL_MARKET_PAPER_0_3_2.md) for state paths, readiness,
learning safeguards and direct CLI commands. The legacy `--dashboard` option
still selects synthetic DEMOUSD and must not be used for market evidence.
Starting capital is
configurable with `--cash`; an existing database restores its cash instead of
resetting it. `--fee-bps 10` assumes 0.10% per fill, independent of slippage.
These are paper assumptions, not a current account-fee guarantee. Use `--slippage-bps` for an adverse
fill assumption. The example profile enables all runtime risk policies.
Composed spot accounts refuse fills exceeding cash plus fees and sells exceeding
held quantity. No implicit borrowing or shorts are created.

Omit `--paper-controls` for a read-only dashboard. When enabled, controls require
a per-server token, localhost Host and matching Origin. The token remains in the
local page, is not persisted, and is never an exchange credential. Remote
control listeners are refused. The dashboard is not a multi-user security
boundary against software already running as the Mac account.

Pause and Stop bot keep the dashboard open but prevent further bot trading.
Resume requires a reason. Emergency stop latches the kill switch. Closing the
terminal with Ctrl-C shuts down the worker, stream, dashboard and SQLite.
SIGTERM follows the same cleanup path. Startup never silently resumes a saved
paused/stopped bot. After inspecting recovery, add `--resume-paper` explicitly.

Add `--local-notifications` to opt in to macOS desktop failure and monitoring
notifications. Delivery is bounded and failure is audited. It is off by default
and depends on OS permission; no real notification was sent in verification.

Emergency release is deliberately an operator action. Stop the application,
then use the local API on the same database to release the switch and move the
emergency bot to paused, with an audited reason. Do not release an emergency
until its cause is understood. For example, in a Python session:

```python
from trading.app.composition import build_paper_stack
stack = build_paper_stack(sqlite_path="paper-state.sqlite3")
stack.kill_switch.release(stack.operator_id, reason="operator resolved the incident")
stack.bot.transition("paused", stack.operator_id, reason="operator verified recovery")
stack.persistence.close()
```

Then restart with `--resume-paper`. A second writer using the same canonical
database path is refused by an OS file lock. Read-only backups/monitoring remain
available. Lock files may remain after shutdown; their existence is not the
lock state. Do not delete them while a writer runs.
An emergency may also need open-order reconciliation; releasing
the latch does not bypass other gates.

## Genuine CoinSwitch market data

The public-data mode does not use account credentials or exchange orders. Use
fresh captured market constraints or all reviewed manual market constraints as
described in README. A current capture file is required in this example:

```bash
.venv/bin/python -m trading.app --coinswitch-symbol BTC/USDT \
  --coinswitch-exchange c2c1 --coinswitch-market-info-file ./trade-info.json \
  --paper-controls --fee-bps 10 --state-db ./coinswitch-paper.sqlite3
```

The capture must match the exact symbol/exchange, checksum and allowed age.
Do not invent its precision/minimums. Feed loss, stale marks, gaps, expired
metadata, inconsistent recovery and risk refusals prevent new entries.
Public WebSocket reliability is subject to the external service; the previous
four-hour qualification is historical evidence, not a current connectivity SLA.

macOS Keychain remains the credential source for separately invoked historical
GET acquisition. No credentials belong in TOML, command arguments, logs or Git.
Paper and research commands do not need withdrawal or trading permissions.

## Read-only research and cost review

For cost analysis, `--research-report` also accepts the explicit
`artifacts/native-5m-frozen-fill-cost-diagnostics.json` or native walk-forward
results file. Hash verification is required and display never approves or
deploys a strategy. See `EXECUTION_COST_READINESS_REPORT.md` for the read-only
spread capture and prepared, separately authorized fee-lookup workflow.
An authorized lookup on 2026-11-12 verified current BTC/`c2c1` effective fees at
10bp maker/taker, matching the research base assumption. The snapshot is private;
it does not prove historical fees or authorize orders, and no automatic account
lookup is enabled on paper startup.

These research costs are in USDT across independent historical folds, not the
current demo account's USD P&L or a new profitability forecast.

## Choose a new paper balance

To try another starting balance, create a separate account. Existing accounts,
orders and P&L history are never reset. This is virtual money, not a deposit.

```bash
.venv/bin/python -m trading.app.paper_account create \
  --state-db ./paper-10000-usd.sqlite3 --cash 10000 --currency USD
```

Creation does not start a feed, run a strategy or place orders. The installed
distribution also exposes this command as `trading-paper-account create`.
The initialized
database has private file permissions, an audited starting balance and a paused
bot. Invalid balances, excessive currency precision, existing paths/symlinks and
existing SQLite sidecars are refused. Initialization and integrity checks finish
before the file is published; publication cannot overwrite a competing account.

For a synthetic demo using this USD account:

```bash
.venv/bin/python -m trading.app --config config/paper.example.toml \
  --dashboard --paper-controls --fee-bps 10 --state-db ./paper-10000-usd.sqlite3
```

It remains paused until explicit Resume with a reason, or until launched with
`--resume-paper`. Starting an existing account with a different `--cash` restores
its saved capital and balance; it does not add funds. Use the same quote currency
on restart. `--currency USDT` is for `/USDT` paper markets and `--currency INR`
for `/INR`; USD is the synthetic demo currency. Quote currency is not conversion
between currencies. Account setup does not change runtime risk limits or approve
a strategy; the chosen launch configuration still supplies those controls.

There is no mid-session balance editor or reset button. Use a new database for
an independent trial and retain the old one for comparison and audit. Existing
backup/inspection commands below also work with these accounts.

## Backup and restore

The backup command uses SQLite's backup API, including committed WAL pages.
It verifies SQLite integrity and the audit hash chain before publishing. It
never overwrites a target. Keep the resulting SHA-256 with the backup.

```bash
.venv/bin/python -m trading.app.maintenance inspect ./paper-state.sqlite3
.venv/bin/python -m trading.app.maintenance backup ./paper-state.sqlite3 ./backup-001.sqlite3
.venv/bin/python -m trading.app.maintenance restore ./backup-001.sqlite3 ./restored.sqlite3 \
  --sha256 FULL_BACKUP_HASH_FROM_THE_BACKUP_OUTPUT
```

Restore to a new filename while the application is stopped, inspect it, and
start the application against that filename. Review recovered state before
explicit resume. Schema 1 upgrades additively to schema 2 without deleting
orders/accounting/audit rows; unknown schemas are refused. Back up before an
upgrade. Legacy open portfolios without saved simulated venue inventory still
require the exact audited legacy recovery workflow in README.

## Research and models

Historical download, validation, segmented preparation, training, independent
evaluation and paper inference remain separate commands. Run `--help` for the
existing historical and ML CLI. Genuine datasets must pass integrity checks
before training. The original rejected model, consumed confirmation evidence
and untouched final Segment 7 must not be repurposed or overwritten.

The new fixed strategy comparison is reproducible and development-only:

```bash
.venv/bin/python -m trading.app.strategy_research config/strategy-development-plan.json \
  ./artifacts/fixed-strategy-development-comparison.json
```

It accepts only the already authorized development backtest source and its
recorded hash. It compares fixed MA, momentum, mean-reversion and
volatility-filtered momentum rules. It does not optimize parameters or approve
a winner. Empty validation folds report insufficient data. The initial run had
two empty validation folds; all candidates lost money in the usable fold.

Register new eligible model artifacts only when separate research evidence
exists. These commands never fit, deploy or modify model files:

```bash
.venv/bin/python -m trading.app.model_registry --state-db ./models.sqlite3 list
.venv/bin/python -m trading.app.model_registry --state-db ./models.sqlite3 \
  register ./candidate.json ./evaluation.json --reason "reviewed frozen candidate"
.venv/bin/python -m trading.app.model_registry --state-db ./models.sqlite3 \
  transition MODEL_HASH approved_for_paper --reason "reviewed evidence" \
  --confirmation "APPROVE PAPER MODEL MODEL_HASH"
```

Approval reuses the existing frozen model/evaluation gate and exact confirmation.
Rejected registry identities cannot be promoted, including after archival.
An archived previously approved model can be restored only after passing the
same artifact integrity and evidence checks. Registry approval does not deploy.
V1 and V2 paper inference are supported. V2 requires complete frozen symbol,
exchange and earliest-inference metadata, passed hash-bound confirmation evidence,
and explicit approval. Existing artifacts lacking these fields refuse deployment
rather than being rewritten. The current rejected V2 model remains ineligible.
Do not retrofit its metadata or alter its rejected evidence.

V2 reuses the research feature generator, contiguous 240-candle warm-up, frozen
feature subset, and original 5/15/30-minute sampling stride. Runtime scaling is
never fitted. Every intermediate label candle must exist; gaps stay unresolved.

CLI paper deployment additionally requires `--model-registry-db` alongside the
existing model, evaluation and exact-confirmation arguments. Each inference
checks approval again, so revocation or artifact mutation fails the cycle closed.
There is no automatic promotion, threshold tuning or weight updating.

Forward paper prediction/outcome monitoring is read-only:

```bash
.venv/bin/python -m trading.app.model_monitor --state-db ./coinswitch-paper.sqlite3 \
  --model-sha256 MODEL_HASH --reference-model ./candidate.json \
  --policy config/model-monitor.example.json
```

Only observed horizon outcomes are resolved. Missing target/intermediate bars remain
unresolved. Reports include classification, Brier and optional feature drift
against training scales. They do not establish net trading profitability.
Use `--model-monitor-policy config/model-monitor.example.json` with an approved
paper model for the same policy in headless cycles and the dashboard. Window,
minimum sample count, feature mean/tail limits and Brier deterioration margin
are explicit operator settings. Active/recovered alerts are audited. No alert
changes weights, model-selection thresholds, approvals or hard risk limits.
The system does not train after individual trades. Further training must use
the development research pipeline and fresh independent evaluation. Scheduling
training or automatically changing models is not enabled.

## Phone access and automation

The Mac and application must remain running for the dashboard to work. The
loopback address is accessible on the Mac only. Phone access needs a separately
configured authenticated tunnel or reverse proxy with TLS; do not expose this
HTTP server publicly or port-forward it on a router. No system networking or
account security settings were changed by this implementation.

Startup/shutdown, supervised cycles, restart recovery and opt-in research jobs
are automated. The local launcher is `scripts/start-paper.command`; installation
of `.venv` is required first. Add `--paper-controls` or `--resume-paper` explicitly
when invoking it from a terminal. The launcher defaults to genuine BTC/USDT
paper mode on port 8766; legacy demo state is kept separate.

Bounded scheduled research and public collection are available but disabled:

```bash
.venv/bin/python -m trading.app.research_jobs config/research-job.example.json --once
.venv/bin/python -m trading.app.research_jobs config/collection-job.example.json --once
.venv/bin/python -m trading.app.research_jobs config/monitor-job.example.json --once
```

These shipped examples print disabled and perform no work. Copy a profile and
configure it before setting enabled true. Research requires file paths and
SHA-256 hashes for `development`, `walk_forward` and `preregistration`, each as
`{"path":"...","file_sha256":"..."}`, plus an exact `candidate` object from
the preregistration. The canonical preregistered protocol and source identity
must match. Protected paths refuse before reading, and development provenance
is verified before fitting. Outputs get unique filenames and never replace a
frozen/rejected model. There is no confirmation, deployment or promotion action.

Omit `--once` for recurring execution at the configured interval. Workers have
wall-time/CPU limits and supervised memory budgets, inherit no exchange credentials, and
cannot execute arbitrary shell commands. A failed job prints a failure and does
not promote a model. macOS rejects `RLIMIT_AS`; the parent samples child RSS
about every 0.2 seconds plus measurement latency and kills over-budget workers.
This is sampled enforcement, not an instantaneous memory cap; short spikes may
occur between measurements. Other Unix workers also retain the OS address-space
limit. Unavailable memory inspection stops the worker. Resource reports record
wall time and observed memory. A real non-fitting monitor worker was verified
on this Mac with temporary fixtures; no genuine training job was activated.
SIGINT/SIGTERM stop scheduling; in-flight work remains bounded by timeout.
Public observation uses no credentials, persists complete candles incrementally,
rejects conflicting replays, and retains final qualification evidence. Collection
artifacts may have gaps and still require independent validation before training.

No OS launch-at-login job or external alert destination has been installed.
Configuring account services, notification recipients or networking remains a
separate operator decision; never schedule live activation or model promotion.

## Verify a release

```bash
python3.11 -m unittest
python3.11 scripts/release_drill.py --python python3.11
```

The release drill builds and installs a wheel in a temporary environment, starts
the local synthetic paper application, verifies health, interrupts an open
position, verifies restored inventory/paused state, explicitly resumes and
checks restart continuity. It never contacts an exchange order endpoint.

If port binding is denied by the execution environment, HTTP tests skip;
rerun them in an environment with loopback permission. If Python cannot import
`setuptools.build_meta`, use the documented Python 3.11 environment or install
the declared build requirements in an isolated build environment. Do not hide
failed checks or treat skipped HTTP tests as verified controls.

## Completion boundary

The local paper application, safety controls, offline training/evaluation,
basic model lifecycle, strategy comparisons, monitoring, bounded opt-in jobs,
backup and release workflow are usable. V2 deployment, configurable monitoring
alerts and optional desktop delivery are implemented. External email/webhook
destinations, launch-at-login, phone networking and live qualification require
operator/exchange setup. Dashboard audit/equity/dataset health views
are available; add `--research-report artifacts/fixed-strategy-development-comparison.json`
to show the explicit development comparison. This does not browse sealed files.
No system security setting was changed or live venue access assumed. No current ML model
or strategy is approved as having a credible profitable edge.
# Public depth diagnostic (0.2.4)

Run `.venv/bin/python -m trading.app.depth_costs artifacts/public-depth-NEW.json --seconds 120`
with a new output filename. This only observes public BTC/USDT c2c1 depth;
it uses a fixed 10bp fee scenario and makes no account or order requests.
An exit code of 2 means the observation did not qualify; inspect the raw
observer report even if depth statistics exist. Current measured results and
limitations: [public depth report](PUBLIC_DEPTH_COST_REPORT.md).
Final verified wheel: `dist/depth-ready-0.2.4-final/safety_first_trading_app-0.2.4-py3-none-any.whl`.

# Public observation quality gate (0.2.5)

Depth captures now require continuous timing/coverage and no feed errors or
reconnects before they are eligible for cost-observation research. A passing
research check never enables trading. Offline reassessment is available through
`python -m trading.app.depth_costs OUTPUT --input CAPTURE --sha256 EXACT_HASH`;
existing outputs and protected dataset paths are refused. Details and actual
failed evidence: [quality report](PUBLIC_OBSERVATION_QUALITY_REPORT.md).

# Observation timing correction (0.2.6)

Depth quality now compares wall and monotonic clocks over the same observation
window, excluding transport cleanup. Strict tolerances are unchanged. A genuine
single pilot passed; the original three-period cohort remains rejected.
Results, evidence and commands: [public cohort report](PUBLIC_DEPTH_COHORT_REPORT.md).
Installed wheel: `dist/timing-ready-0.2.6/safety_first_trading_app-0.2.6-py3-none-any.whl`.

# Cost-aware ML research (0.3.0)

Eight executed studies produced no qualified strategy. Read
[the research findings](ML_PROFITABILITY_RESEARCH_REPORT.md) before interpreting
positive development averages. The verified registry is
`artifacts/research-registry/net-expectancy-registry-v4.json`.
Final installed wheel:
`dist/expectancy-research-0.3.0-final/safety_first_trading_app-0.3.0-py3-none-any.whl`.

Pass that explicit file through the existing `--research-report` option to view
development summaries in the local dashboard. It does not approve or deploy a
model. To independently reverify the studies, use a new output filename:

```sh
.venv/bin/python -m trading.app.expectancy_registry \
  config/expectancy-research-registry-v3.json artifacts/research-registry/reverified-NEW.json
```

Historical replay refuses changed code/data. The latest ETH plan matches the
current implementation; older studies require their retained source snapshot.
Keep protected confirmation data and Segment 7 untouched. No research model is
approved for paper or live execution; existing PAPER defaults are unchanged.
