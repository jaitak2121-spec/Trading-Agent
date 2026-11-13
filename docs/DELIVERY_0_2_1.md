# Local paper application delivery — 0.2.1

Date: 2026-11-12

Delivered and verified for local paper trading and offline research. This is
not a profitable-strategy certification or a live-execution release.

## Run the app

From the repository, using the installed workspace environment:

```sh
.venv/bin/trading-paper-account create \
  --state-db ./my-paper.sqlite3 --cash 10000 --currency USD
.venv/bin/trading-paper --config config/paper.example.toml \
  --dashboard --paper-controls --fee-bps 10 --state-db ./my-paper.sqlite3
```

Open `http://127.0.0.1:8765/`. The new account starts paused. Resume deliberately
with a reason. An existing account is restored, never reset by changing `--cash`.
The create command refuses existing account files. USD here selects the labeled
synthetic demo, not genuine market-performance evidence. Use the
`OPERATING_GUIDE.md` procedure for CoinSwitch public-data paper sessions and
validated venue metadata; no exchange order transport is involved.

The macOS `scripts/start-paper.command` launcher remains available. Pass
`--paper-controls` to opt into local controls; otherwise the dashboard is
read-only. Optional jobs/notifications remain opt-in. No account balance was
chosen for the user and no persistent bot was started during this delivery.

## Included capabilities

- Automated paper strategy evaluation, risk-approved orders, fees and slippage.
- Cash, positions, realized/unrealized P&L, equity, drawdown and audit records.
- Persistent accounts, saved simulated venue inventory, recovery and deduplication.
- Audited pause, resume, stop and emergency stop; explicit restart resumption.
- Local dashboard, bounded monitoring and optional authenticated local controls.
- Immutable model identity, evidence-gated paper approval, rejection and rollback.
- Offline causal datasets, training/evaluation, walk-forward research and backtests.
- GET-only historical acquisition, gap detection, targeted recovery and checkpoints.
- Verified database backup/restore and the new paused-account setup command.

Existing ML functionality is usable with eligible artifacts, but **there is no
approved model or proven profitable strategy**. Rejected V1/V2 models and six
failed cost-aware hypotheses retain their status.

## Release verification

- Full source suite: **2,238 passed, zero skips**, Python 3.11.15, with localhost
  and native worker access. Focused account/release tests: **15 passed**.
- Built offline without dependency fetching. Verified package metadata,
  entrypoints and inclusion of the current account/research modules. No dataset,
  model artifact or credentials are bundled in the wheel.
- Isolated packaged-wheel drill passed CLI help, custom 1,234.56 USD account
  creation, paused state and saved balance, HTTP dashboard health, abrupt
  interruption with inventory, inventory recovery, explicit resume, clean shutdown
  and persistent restart drill. Only temporary synthetic fixtures were used.
- The isolated environment lacked ensurepip, so the existing wheel-extraction
  fallback was exercised. It initially exposed an invalid shebang when the
  interpreter path contained spaces. The corrected quoted shell launcher and a
  regression test now pass. No failed verification was hidden.
- That exact wheel was also installed via pip into the workspace `.venv` with
  `--no-index --no-deps`. `pip check` passed; both installed CLI help commands
  passed. Isolated Python (`-I`) confirmed version 0.2.1 and imported the account
  module from installed `site-packages`, not the checkout.
- Compilation, macOS launcher syntax and whitespace checks passed.

Wheel: `dist/safety_first_trading_app-0.2.1-py3-none-any.whl`

SHA-256:

```text
c1992cae60902e46a7d70b6540d5b11236d2b696b13ec4591a7ef10fcdeff7a5
```

Checksum sidecar: same filename plus `.sha256`.
Installed-wheel evidence: `artifacts/release-0.2.1-installed-verification.json`.
Previous 0.1.0 and 0.2.0 distributions were preserved.

Main delivery changes: version/console entrypoint in `pyproject.toml`, current
module/entrypoint checks in `trading/app/release.py`, offline build/install and
custom-account verification in `scripts/release_drill.py`, release regression
tests in `tests/test_app_release.py`, README and completion/operating documentation.
The earlier account/research implementations are now included in the distribution.

## Remaining boundaries

The older 180-day acquisition remains stopped on four unresolved May 7, 2025
minutes. No complete extension dataset, new model or performance result was
invented. Research can continue only after recovery/integrity review or a newly
approved gap-safe protocol. Protected confirmation and ML Segment 7 were not
opened. No rejected model or historical evidence was changed.

Real-money execution is absent. Verified exchange test-environment availability
and independent live-path qualification remain external requirements. There is
no guarantee of paper or live profit. The UI is a local browser dashboard, not
a hosted or native mobile service. Secure remote access, external email/webhook
delivery and OS login scheduling are not configured by this release.

No Git history was rewritten or pushed. Existing unrelated working-tree changes
were retained. This handoff closes the current local paper/research packaging
and usability work; further strategy research and any live execution are separate
milestones, not silently enabled features.
