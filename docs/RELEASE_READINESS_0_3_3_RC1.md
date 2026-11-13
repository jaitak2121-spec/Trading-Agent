# Paper release candidate 0.3.3rc1 — 2026-11-13

Engineering verification complete; **not cleared for existing-account recovery**.
No actual account was opened by the launcher, restarted, recovered or resumed.
No credentials, orders, BTC/cash changes, precision guesses or new research.

## Implemented

- Launcher waits up to 30 seconds for `/readyz` from its own child PID and the
  `real_market_paper` origin. A pre-existing SQLite file is not readiness. Failure,
  timeout, wrong listener/origin or unavailable snapshot prevents learning startup
  and invokes existing child cleanup. Fixed host/port prevent mismatched probes;
  custom profiles should use the direct CLI.
- `/readyz` requires successful startup reconciliation, a healthy worker with at
  least one completed pass and a serializable snapshot. PAUSED collection can be
  ready without permission to trade. `/healthz` retains stricter trading-health
  semantics: readiness does not bypass Emergency Stop, metadata, risk or freshness.
- Snapshot/readiness failures return sanitized HTTP 503. Health calculation
  failures also return 503. Thread-start failure releases the bound listener;
  CLI startup failure now runs application cleanup.
- Distribution and `trading.__version__` agree on **0.3.3rc1**. Release verification
  checks this identity and retains the existing required-module checks. Builds
  record a fixed `SOURCE_DATE_EPOCH=1794528000`; two offline builds match exactly.

Changed: `scripts/start-paper.command`, `scripts/release_drill.py`,
`trading/adapters/http/dashboard.py`, `trading/app/__main__.py`,
`trading/app/release.py`, `trading/__init__.py`, `pyproject.toml`,
`tests/test_paper_launcher.py`, `tests/test_app_dashboard.py`,
`tests/test_app_runtime.py`, `tests/test_app_release.py`, and completion documents.
Existing v0.3.2 account paths, balances, model artifacts and risk controls remain
unchanged; the candidate is not installed into an operating account.

## Verified

- **103 focused tests passed**, including architecture boundaries, delayed probe,
  wrong PID/origin, exited child, failed snapshots, paused readiness, listener
  cleanup and launcher success/failure with an existing temporary database.
- **2,391 full-suite tests passed, zero skips**; compilation and whitespace pass.
- Installed candidate: CLI, HTTP snapshot, child-specific readiness, paused new
  account, clean shutdown, crash inventory recovery, paused restart and explicitly
  resumed **temporary fixture** sessions passed. Synthetic drill P&L is not real
  account performance or evidence of profitability.
- Byte-identical rebuild; package modules match the source manifest.

Selected wheel:
`dist/paper-0.3.3rc1-verified/safety_first_trading_app-0.3.3rc1-py3-none-any.whl`

SHA-256: `1e2f8a817046204b3817f6b1a24f3406ba8810a1e4dd03fec8de943d15fc36dd`

Evidence: `artifacts/paper-0.3.3rc1-verified-release-drill.json` and
`artifacts/paper-0.3.3rc1-readiness-manifest.json`. The earlier
`dist/paper-0.3.3rc1` build predates the architecture correction and is superseded;
select the verified path/hash above. Existing v0.3.2 wheels were not overwritten.

## Reproduce without touching the actual account

```sh
cd "/Users/jaitak/Documents/Trading app"
.venv/bin/python -m unittest tests.test_paper_launcher tests.test_app_dashboard \
  tests.test_app_runtime tests.test_research_jobs tests.test_app_release \
  tests.test_core_purity -q
.venv/bin/python -m unittest discover -s tests -t . -q
.venv/bin/python scripts/release_drill.py --python .venv/bin/python \
  --dist /tmp/trading-033rc1-reproduction \
  --evidence /tmp/trading-033rc1-reproduction.json
git diff --check
```

Choose a new evidence path if it exists. Same source, Python/build-tool versions
and epoch are required for the recorded checksum. The drill only uses temporary
accounts; it does not launch the genuine BTC database or require exchange access.

## Remaining release checklist — in order

1. Obtain CoinSwitch's authoritative `precision.limit=2` quantity/tick formula,
   worked examples and independent minimum-quantity rule. The
   [prepared request](COINSWITCH_RULE_CLARIFICATION_REQUEST.txt) remains unsent.
2. Implement only the confirmed interpretation, rerun tests and acquire a fresh
   supported capture under appropriate read authorization. Do not alter the
   current rejected capture or accept manual guesses.
3. Obtain explicit recovery authorization. Verify actual writer/scheduler
   ownership, make and inspect a non-overwriting WAL-aware backup, then follow
   [the operator procedure](PAPER_OPERATOR_RECOVERY_CHECKLIST.md). Preserve the
   0.01399178 BTC holding and any residual. Recover to **PAUSED**, never automatic
   resume. Existing Emergency Stop must remain engaged until authorized recovery.
4. After separately authorized startup, verify genuine quote/candle timestamps,
   accepted unexpired metadata, restored balances/orders/positions, reconciliation,
   dashboard and scheduler state. `/readyz` is collection readiness; `/healthz`
   can correctly return 503 while paused or safety-blocked.
5. After separate resume approval, observe a legitimate MA position lifecycle,
   capped exits, partial/pending handling, fees/residual accounting and sustained
   monitoring. No forced test trades in the actual database. Check scheduler
   status/logs and experience readiness; awaiting data or held models are not
   successful training/deployment. Stop with Ctrl-C in the launcher terminal.
6. Only after operational acceptance, designate a stable release with a new
   immutable version/hash. Keep PAPER default and real exchange execution disabled.

**Still unverified:** sustained genuine operation of this candidate and actual
position exit after recovery. **Not established:** a qualified profitable model.
Current dashboard liveness was not re-probed in this pass; the previous report
recorded port 8766 unavailable. No new profitability hypothesis justified a rerun.
