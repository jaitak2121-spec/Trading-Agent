# Local application completion — 0.2.0

Date: 2026-11-12

The remaining local paper/research implementation gaps identified after the
0.1 release have been closed. This does not make real-money execution supported
or establish a profitable model. The current rejected models remain ineligible.

## Completed

- V2 paper inference supports the frozen 3/18-feature schemas and 5/15/30-minute
  horizons. It reuses the offline feature generator, fixed 240-candle context,
  sampling alignment and training-only scaling. No runtime fitting occurs.
- Future frozen exports carry symbol, exchange and earliest-inference metadata.
  Old artifacts missing provenance are refused, not modified. Previously
  completed confirmation evidence must be accepted, internally consistent and
  hash-bound to the exact model. No confirmation observations are opened.
- Registry registration, explicit approval, rejection, archival, rollback and
  per-inference revocation checks work with V1 and V2. The current rejected V2
  artifact was not retrofitted, retrained, evaluated or promoted.
- Prediction outcomes require every intervening minute through the frozen
  horizon. Missing prices remain missing. Shared strategy context now returns
  only the latest contiguous same-interval history and rejects duplicate times,
  preventing both MA and ML indicators from spanning a data gap.
- Explicit monitoring policies set rolling windows, sample minimums, feature
  drift limits and calibration margins. Headless cycles persist active/recovered
  alert transitions. The dashboard and standalone monitor use the same report.
  Alerts do not change predictions, thresholds, approvals or hard risk limits.
- Optional macOS desktop notifications deliver generic runtime/monitor alerts
  with a bounded, fixed command and no exchange credentials. Failed delivery is
  audited. Delivery is disabled unless `--local-notifications` is supplied.
- Composed spot-paper accounts cannot buy beyond cash including rounded fees
  or sell more than held. Checks use the actual fill quantity/price, including
  partial fills. No implicit borrowing or unfunded short is created.
- Canonical database paths have exclusive OS writer ownership on macOS/Unix.
  A second writer fails safely; read-only inspection/monitoring/backups remain
  available. Ownership is released on close or process exit. WAL, shared-memory
  and ownership sidecars are ignored by Git.
- Bounded workers were repaired for macOS, which rejected the previous address-
  space limit. Parent RSS supervision kills over-budget workers and fails closed
  if memory inspection is unavailable. CPU/wall-time limits remain. Measurements
  and enforcement details are recorded in worker reports.
- A real non-fitting monitor child was executed with temporary synthetic model
  fixtures, native process supervision and no training/orders. This goes beyond
  the previous mocked worker checks. Shipped job examples remain disabled.
- An isolated `.venv` was created on this workspace. The declared historical
  signing dependency was installed there; no credentials were read or system
  Python changed. No application or OS login service was started persistently.

## Verification

Both commands passed **2,209 tests, zero skips**, with localhost/process
inspection permission:

```bash
python3 -m unittest
.venv/bin/python -m unittest
```

The isolated environment uses Python 3.11.15 and cryptography 50.0.2 (with cffi
2.1.1 and pycparser 3.12). Core paper/research functionality remains stdlib-only;
cryptography is the optional historical-authentication dependency.

Compilation, whitespace checks and the macOS launcher syntax passed. Restricted
sandbox runs skip five HTTP tests and the native child-memory inspection test;
the full permitted runs executed them successfully. No failed check was hidden:
the native test exposed `RLIMIT_AS` failure before the supervisor was repaired.

The installed-wheel release drill passed startup, interruption, persisted
inventory recovery, explicit resume, shutdown and restart. The tested wheel is
`dist/safety_first_trading_app-0.2.0-py3-none-any.whl`, SHA-256:

```text
fe6f0f754a8a8735c8c94c47490079273baf9f8a56b90652327e828dd4fcbda4
```

That exact wheel was installed into the workspace `.venv` without fetching
additional package dependencies. `pip check` reported no broken requirements,
and the installed `.venv/bin/trading-paper --help` command passed. The previous
0.1 wheel remains separately available; it was not overwritten by this release.

Memory supervision is sampled roughly every 0.2 seconds plus measurement latency,
not an instantaneous OS cap; transient spikes between samples can occur. Other
Unix workers also retain their OS address-space limit. Unsupported/unavailable
supervision refuses work rather than silently dropping the memory budget.

Desktop notification delivery was mocked in tests. No real notification or OS
permission change was performed. No genuine model fitting, confirmation run,
market-data download, paper deployment of a model, or real-money order occurred.
Existing synthetic unit/release tests exercise virtual paper fills only.

## Main files

New: `trading/app/model_deployment.py`, `tests/test_v2_paper_strategy.py`,
`tests/test_model_monitor.py`, `tests/test_operator_alerts.py`,
`config/model-monitor.example.json`, `config/monitor-job.example.json`.

Updated: `app/model_strategy.py`, `app/model_registry.py`, `app/model_monitor.py`,
`app/ml_research.py`, `app/paper_service.py`, `app/runtime.py`, `app/__main__.py`,
`app/research_jobs.py`, `adapters/process.py`, `adapters/paper/broker.py`,
`adapters/persistence/sqlite.py`, `strategy/context.py`, runtime/job tests,
`.gitignore`, package version and operating/release documentation.

Previous worktree changes and research evidence were retained. No Git commit,
push or external message was sent.

## Remaining external boundary

The implementation is ready for documented local paper/research use. There is
no approved ML model with credible independent predictive evidence. A new model
requires a new authorized research protocol, not edits to rejected evidence.

Real-money qualification remains blocked by unverified non-production CoinSwitch
access and account permissions. No live-order transport was added. See
`LIVE_EXECUTION_READINESS.md`; production endpoints are not a substitute sandbox.

Desktop notification permission, enabling optional schedules, launch-at-login
and secure phone access are operator settings, not actions performed silently
by this release. External email/webhook delivery is not implemented or claimed;
the implemented notification channel is the local macOS desktop. The local
dashboard remains loopback-only, with controls explicitly enabled by the user.
