# ML lifecycle integration — 0.3.1

2026-11-12. Implementation completed and tested; **no profitable or independently
qualified ML model is claimed**. Reused existing training, inference, approval,
monitoring, paper gateway, accounting, risk, persistence and exchange-contract
implementations. No completed research experiment was repeated.

## Implemented and integrated

- Public CoinSwitch paper runtime now saves closed one-minute candles to SQLite.
  Each row has a SHA-256, exact OHLCV/source/timestamps, and a unique market/time
  key. Conflicting replays, corrupted recovery, incomplete bars and provenance
  mismatches fail closed. Full history stays durable; inference caches at most
  4,096 bars. Restored ingestion detects duplicates and restart gaps.
- Supervisor keeps collecting while paused/stopped/emergency-stopped, without
  invoking strategy generation, order synchronization or the execution gateway.
  Prediction outcomes can mature during pauses. Direct trading polls still
  require RUNNING. Collection failure remains visible and engages the kill switch.
- Added `experience_retraining` to the existing bounded job scheduler. It reads
  a consistent read-only SQLite snapshot, validates hashes and the latest
  contiguous run, splits chronologically, fits the existing logistic model on
  development data, and screens on later development observations. Internal
  preprocessing remains training-only. No windows or labels cross gaps.
- Dataset/model/evaluation artifacts and hashes are retained. Repeated snapshots
  do not refit; persistent new-data cooldown survives scheduler restart. Interrupted
  attempts are preserved and retries use new artifact directories. Weak candidates
  are rejected through the existing model registry; other candidates remain
  unapproved and held for independent cost-aware qualification. Classification
  alone cannot release this deployment hold. Scheduler workers inherit no API keys.
- Existing monitoring now supports explicit `on_alert: "pause"`. Drift or Brier
  deterioration pauses before new signals/orders, persists the transition and
  records alerts. The supplied monitor example selects this action; existing
  policies without it retain warning behavior. Restart never silently resumes.
  No model hot-swap or automatic liquidation is introduced. Open positions remain
  visible and require operator review while execution is paused.
- Dashboard/API expose durable collection counts/range, learning status,
  candidate identity, training/evaluation results, existing prediction/drift
  status, holdings/equity/P&L/fees, trades, risks, controls and research evidence.
- Existing rollback is retained: explicitly restore approval of a previously
  approved archived model, validate immutable artifacts, and restart with that
  model's exact approval phrase. Rejected models remain ineligible. Monitoring
  never chooses another model or silently resumes trading.
- Existing CoinSwitch injected execution contract already handles partial fills,
  idempotency, uncertain outcomes, reconciliation and risk checks. Mocked native
  contract tests passed. The default exchange port remains disabled; no signed
  trading transport, account access or live activation was added.

## Verification

Focused lifecycle/architecture/worker/native-contract/release suite: **91 passed**.
Final full package-aware suite: **2,350 passed, zero skips**. Added eight synthetic
experience/integration tests and one release-content test. Tests exercise a real
fit on synthetic fixture data, future-only evaluation, rejection/deployment hold,
cooldown/tamper checks, bounded durable storage, restart gaps/conflicts,
pause-only collection, pre-signal safety halt, dashboard visibility, and an
explicitly approved synthetic model through inference → sizing/risk → paper fill
→ fees/accounting → persisted position recovery. Synthetic approval fixtures
are not approvals or performance evidence for real research models.

Full-suite checks caught infrastructure import-boundary defects; these were fixed
by moving SQLite access into the existing persistence boundary and injecting the
dashboard status callback. A cooldown tamper-check defect was also fixed before
the final passing run. Compilation/CLI/whitespace checks passed.

Actual public-feed operational check, with bot explicitly paused:

- 65 supervised passes over about 65 seconds; one genuine BTC/USDT c2c1 candle.
- No worker error, no reconnect, no credentials, no orders or model deployment.
- Independent validation passed: one candle, zero duplicates/gaps, valid OHLCV.
- Restart restored the candle and clean startup; bot remained STOPPED.
- Evidence: `artifacts/ml-lifecycle-operational-2026-11-12/{report.json,
  restart-and-integrity.json,collected-candles.json,final-release.json}`.
- Dataset file SHA-256:
  `8dc7956a19a3c8e567f229764eb8bb221654a88600a01b020f9a05f4764386a0`.

This short observation proves the new collection/recovery path, not extended
uptime, live ML execution, trading profitability or a sufficient learning dataset.
No genuine-data model was trained in this milestone.

Built wheel: `dist/ml-lifecycle-0.3.1-final/safety_first_trading_app-0.3.1-py3-none-any.whl`.
SHA-256: `d8225bf9d99d140eb6ab1e3e14abe5bd613066c3a0bca00ece8bac0f80455cee`.
All **131 Python modules** byte-match source; isolated wheel import/CLI passed.
No dependencies added. Previous wheels, rejected models and protected datasets
remain unchanged. Older hash-bound research replays require their frozen source
snapshots now that shared application source has advanced.

Changed components: persistence `candles.py`/`sqlite.py`, `candle_ingestor.py`,
application `experience_learning.py`, `research_jobs.py`, `runtime.py`,
`paper_service.py`, `supervisor.py`, `model_monitor.py`, `model_training.py`,
`__main__.py`, dashboard, release verifier/version, two test files, three example
configurations, README and completion documentation. Reverified all 285 original
registered models and 254 continuation artifacts against their recorded hashes.

## Run the integrated application

From the project directory, public data with automated **paper** execution using
the existing MA baseline; the baseline is not claimed profitable:

```sh
.venv/bin/python -m trading.app --coinswitch-symbol BTC/USDT --coinswitch-exchange c2c1 \
  --config config/paper-usdt.example.toml --cash 10000 \
  --state-db artifacts/paper.sqlite3 --paper-controls \
  --fee-bps 10 --slippage-bps 2 \
  --learning-reports artifacts/experience-learning \
  --research-report artifacts/research-registry/net-expectancy-registry-v4.json
```

Dashboard: `http://127.0.0.1:8765`. Pause controls keep collecting without trading.
Recovered bots need explicit resume. ML execution additionally requires existing
`--paper-model`, `--paper-model-evaluation`, `--confirm-paper-model`, and
`--model-registry-db` approval checks. No current rejected model should be supplied.
Use `--model-monitor-policy config/model-monitor.example.json` for the explicit
pause-on-alert policy once an independently qualified model exists.

Configure the learning job in a separate terminal (no credentials):

```sh
.venv/bin/python - <<'PY'
import json
from pathlib import Path
p = Path('artifacts/experience-job.local.json')
p.parent.mkdir(parents=True, exist_ok=True)
config = json.loads(Path('config/experience-job.example.json').read_text())
config['enabled'] = True
with p.open('x') as handle:
    json.dump(config, handle, indent=2)
PY
.venv/bin/python -m trading.app.research_jobs artifacts/experience-job.local.json
```

The job checks daily, with CPU/memory/time limits, at least 720 contiguous candles
for its first screen, a maximum 10,080-candle window, and at least 1,440 new genuine
candles before another fit. Insufficient data fails visibly and waits
for a later scheduled attempt; missing prices are never filled. Use a separate
model-registry database as in the example, avoiding the runtime's exclusive writer
ownership. Outputs and registry are durable. Nothing is automatically approved.

## Remaining blockers

1. No independently validated positive net-expectancy candidate exists. Current
   experience retraining is a chronological classification screen, not a complete
   economic qualification or an automatic production promotion mechanism.
2. New forward experience is currently one candle, below the 720-candle minimum.
   Extended public collection, adequate regimes, frozen unseen cost-aware
   validation and subsequent prospective paper evidence still require elapsed
   market time and a qualifying model. Do not reopen protected test data.
3. Historical missing candles and unsuitable point-in-time auxiliary history are
   unchanged. The latest [targeted research findings](ML_RESEARCH_CONTINUATION_REPORT.md)
   remain the research basis; no redundant study was run here.
4. CoinSwitch sandbox, permissions, authenticated execution and live operational
   qualification remain unverified. Actual live orders remain disabled.

Next evidence milestone: accumulate genuine contiguous forward experience and
timestamped microstructure inputs, then preregister independent economic
qualification. The application can collect, learn, reject and monitor safely;
it cannot manufacture the missing edge or elapsed validation period.
