# Paper application completion — 2026-11-13

## Current result

Implemented genuine-feed paper composition, capped exits, metadata gates,
persistent accounting/recovery, dashboard origin/status labels and controlled
experience-learning jobs already exist. Reused them; no new trading strategy,
research grid, live adapter activation or model promotion was added.

This pass fixed two lifecycle gaps:

- Once the paper worker has stopped, bot-transition or feed-stop exceptions no
  longer skip SQLite cleanup. A worker-stop timeout deliberately retains the
  database and permits a subsequent stop attempt; never close beneath a writer.
- Scheduler shutdown now cancels/reaps its bounded research child instead of
  waiting for the full research timeout. Cancellation is recorded as stopped,
  not a failed experiment. Partial research files cannot authorize deployment.

Changed implementation/tests: `trading/app/runtime.py`,
`trading/adapters/process.py`, `trading/app/research_jobs.py`,
`tests/test_app_runtime.py`, `tests/test_research_jobs.py`.

## Evidence and limits

| State | Evidence |
|---|---|
| Tested | 25 focused tests; 2,382 full-suite tests passed, zero skips. Five new shutdown/cancellation regressions. |
| Packaged and exercised | Isolated installed-wheel CLI, localhost dashboard, clean shutdown, crash inventory recovery, paused restart and explicit fixture-only resume passed. |
| Preserved account | Read-only SQLite/audit-chain integrity passed: 2,444 records, one order, cash 8,998.80000791 USDT, BTC 0.01399178; Emergency Stop and kill latch retained. |
| Current runtime | GET `http://127.0.0.1:8766/api/snapshot` refused connection during this pass. No running dashboard/feed is verified now; no restart attempted. |
| Not operationally verified | Actual position's complete capped exit after recovery; accepted current exchange metadata; sustained operation of the newly packaged source. |
| Not established | Profitable strategy or independent ML qualification. No claim follows from fixture P&L. |

Release drill uses **temporary synthetic fixtures only**; its balances are not
the genuine account or profitability evidence. Prior genuine feed/MA entry is
documented in the existing v0.3.2 report; it does not establish current liveness.

Evidence:

- `artifacts/paper-completion-2026-11-13/account-readonly.json`
- `artifacts/paper-completion-2026-11-13/verification.json`
- `artifacts/paper-release-2026-11-13.json`
- Wheel: `dist/paper-safety-2026-11-13/safety_first_trading_app-0.3.2-py3-none-any.whl`
- Wheel SHA-256: `74706ada7586374ea080a32596f09e3259316c78094986490bbcafd4bf7c971a`

This is a **0.3.2 safety candidate**, not a new tagged release or installation in
the active account. Older 0.3.2 wheels lack later fixes; compare hashes, not merely
the version string. The old artifacts were retained.

## Remaining gates / exact next action

1. Send the already prepared [CoinSwitch clarification request](COINSWITCH_RULE_CLARIFICATION_REQUEST.txt)
   with its non-secret evidence below. It has **not been sent** by the agent.
   Keep Emergency Stop latched. Effective LIMIT quantity/tick semantics for
   `precision.limit=2` and independent minimum BTC quantity remain unresolved.
2. Obtain authoritative formulas and valid/invalid worked examples. Implement
   only the confirmed interpretation and tests; obtain fresh authenticated
   metadata under a separately authorized read if required. The retained capture
   is correctly refused even before expiry; do not edit it into acceptance.
3. Review costs, current open inventory, dust and pending-order status; preserve
   the potential 0.00000178 BTC tail. Never round up, erase or force an exit.
4. Obtain explicit operator authorization for recovery/restart. Follow the
   [existing backup and recovery procedure](PAPER_OPERATOR_RECOVERY_CHECKLIST.md)
   using the original USDT database. Backup is non-overwriting and WAL-aware.
   Inspect its integrity and SHA-256 before recovery. A refused dashboard alone
   does not prove the writer/scheduler stopped; verify ownership first.
5. Restore **PAUSED**, with kill-release separately audited, accepted unexpired
   metadata and fresh genuine quotes/candles. Verify venue/ledger/portfolio
   agreement and absence of UNKNOWN/pending orders. No automatic resume.
6. Only after separate resume authorization validate a legitimate MA exit,
   sequential capped continuation, fills, fees, residual and dashboard accounting.
   Keep live exchange execution disabled. No synthetic trades in this database.

### Support evidence to include

The draft quotes the exact GET response and UTC capture time. Attach the sanitized
capture `artifacts/metadata-verification-2026-11-13/btc-usdt-c2c1-trade-info.json`,
not headers, request signatures, Keychain data or environment dumps.

Response-body SHA-256:
`0e3208b2505fbe981a8c6c24cb4e774d9452f87446e4b9bea732ef8a4e384202`

Capture-file SHA-256:
`60d26c240a93a02ee0dab6167d210a6f130097f1b0522eac35119142bcab9246`

## Reproducible verification / future launch

Verification does not require starting or recovering the genuine account:

```sh
cd "/Users/jaitak/Documents/Trading app"
.venv/bin/python -m unittest tests.test_app_runtime tests.test_research_jobs -q
.venv/bin/python -m unittest discover -s tests -t . -q
.venv/bin/python -m trading.app --help
git diff --check
```

After gates 1–4 only, use the existing Mac launcher with a newly supported,
reviewed capture (set this variable to its actual path; no credentials needed):

```sh
cd "/Users/jaitak/Documents/Trading app"
./scripts/start-paper.command --coinswitch-market-info-file "$REVIEWED_CAPTURE"
```

Do **not** add `--resume-paper`. Verify restored PAUSED status, original balance
and position, genuine origin/fresh timestamps, rule expiry and ML status in
`http://127.0.0.1:8766`. The launcher owns the hourly learning scheduler; insufficient
contiguous experience is waiting, not trained/approved. A weak/held candidate is
not deployed. Ctrl-C in that launcher's terminal shuts down owned processes;
shutdown errors/timeouts require checking writer ownership before a new start.
Never delete an active lock or force-reset the database.

No new profitability experiment was run: the existing completed studies provide
no qualified candidate and no new independently justified hypothesis was supplied
by this operational pass. Rejected models, registry and protected data were not
opened or changed. The next milestone is authoritative rule resolution followed
by separately authorized, paused recovery and controlled genuine-paper validation.
