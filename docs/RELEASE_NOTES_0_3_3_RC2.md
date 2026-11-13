# Trading-Agent 0.3.3rc2 — final engineering review

2026-11-13. Supersedes rc1; prior builds/evidence are retained. This is a verified
**paper-only release candidate**, not authorization to recover the existing account.

## Confirmed defects fixed

1. Argparse accepted abbreviated options such as `--state-d` despite the Mac
   launcher's exact-option restrictions. Main CLI now requires full option names;
   abbreviations fail before any account composition or network start.
2. The release drill selected whichever wheel had the newest modification time,
   potentially testing an unrelated version in an existing directory. It now
   selects exactly the version declared by `pyproject.toml` and refuses absence.

Two deterministic regression tests cover account/control abbreviations and a
newer unrelated wheel. No risk policy, metadata interpretation, trading strategy,
model or database behavior was changed. Package/distribution identity and release
contract checks now include **0.3.3rc2**.

Changed implementation: `trading/app/__main__.py`, `scripts/release_drill.py`,
`trading/app/release.py`, `trading/__init__.py`, `pyproject.toml`.
Tests: `tests/test_app_runtime.py`, `tests/test_app_release.py`.

## Exact status

| Category | Result |
|---|---|
| Implemented | Above fixes; retained rc1 startup readiness, snapshot/health failure handling and cleanup. |
| Tested | **105 focused checks; 2,393 full-suite tests passed, zero skips**; compilation and whitespace passed. |
| Operationally exercised in isolation | Installed CLI, dashboard/snapshot/readiness, clean shutdown, crash inventory recovery, paused restart, explicit temporary-fixture resume and restart drill passed. |
| Build verified | Two offline builds byte-identical; all 131 packaged Python modules match source; package/distribution version agrees. |
| Actual account | Not opened, restarted, recovered or resumed by this work. No orders, credentials, BTC/cash changes or safety release. |
| Remaining software findings | No additional confirmed defects in the reviewed release paths. This is not a guarantee against undiscovered defects. |
| Blocked | CoinSwitch precision semantics, fresh accepted metadata and separately authorized actual-account operational acceptance. |
| ML/profitability | Unproven; rejected artifacts unchanged and no research/model promotion performed. |

The release drill uses temporary synthetic accounts and ephemeral ports. Its P&L
is fixture output, not genuine trading performance. Current account/dashboard
liveness was not probed or changed in this review.

## Final candidate / reproducibility

Wheel: `dist/paper-0.3.3rc2/safety_first_trading_app-0.3.3rc2-py3-none-any.whl`

SHA-256: `0ff6339027e1ee74bdbe58a9bb384bfdf00a26a1e8c71cf7dc5d40ba03b13719`

Identical rebuild: `dist/paper-0.3.3rc2-rebuild/`.
Fixed epoch: `SOURCE_DATE_EPOCH=1794528000`.

Evidence: `artifacts/paper-0.3.3rc2-release-drill.json`,
`artifacts/paper-0.3.3rc2-final/manifest.json`, saved test/build logs and
`dist/paper-0.3.3rc2/SHA256SUMS`. The manifest records source hashes and build-tool
versions. No older artifacts, databases or backups were overwritten.

```sh
cd "/Users/jaitak/Documents/Trading app"
.venv/bin/python -m unittest tests.test_paper_launcher tests.test_app_dashboard \
  tests.test_app_runtime tests.test_research_jobs tests.test_app_release \
  tests.test_core_purity -q
.venv/bin/python -m unittest discover -s tests -t . -q
.venv/bin/python scripts/release_drill.py --python .venv/bin/python \
  --dist /tmp/trading-033rc2-reproduce \
  --evidence /tmp/trading-033rc2-reproduce.json
shasum -a 256 dist/paper-0.3.3rc2/*.whl
git diff --check
```

Use new output/evidence paths on subsequent runs. Reproducing the checksum requires
the same source, build tools and epoch. These tests do not launch the actual account.

## After CoinSwitch responds — exact gates

1. Verify the authoritative formula/worked examples for effective quantity step,
   tick and minimum BTC quantity, including `precision.limit=2` and residuals.
   Implement only the confirmed interpretation; add tests and rebuild/reverify.
2. Under appropriate read authorization obtain fresh metadata; retain raw
   provenance/hashes and require the capture to pass existing integrity/freshness
   validation. Do not edit the rejected capture to make it pass.
3. Obtain explicit recovery authorization. Verify writer/scheduler ownership,
   create/inspect a new WAL-aware backup, reconcile balances/inventory/orders and
   follow the [operator recovery checklist](PAPER_OPERATOR_RECOVERY_CHECKLIST.md).
   Preserve existing BTC and dust; release Emergency Stop **into PAUSED only**.
4. After authorized startup verify accepted metadata, advancing genuine quotes and
   candles, restored accounting, startup reconciliation, dashboard and learning
   status. Readiness is collection readiness, not trading permission.
5. Obtain separate resume approval, then perform the agreed seven-day observation
   and legitimate capped-exit validation. Stop on safety/accounting violations;
   never force trades or enable live execution.

**Conclusion:** independently achievable release-engineering verification is
complete for this candidate. A stable, operationally accepted CoinSwitch paper
release remains blocked; neither profitability nor actual-account exit completion
is established. Real-money execution remains disabled.
