# Final software package — Trading-Agent 0.3.3rc3

## Release summary

This is the final **engineering-verified paper release candidate**, not a stable
operational acceptance or profitable trading product. RC3 changes documentation,
package identity and version-contract registration; RC2's trading/risk behavior
is unchanged. README is embedded in wheel metadata, so updated documentation was
assigned a new version rather than silently changing the frozen RC2 artifact.

| Status | Evidence / boundary |
|---|---|
| Implemented | Genuine public-feed paper composition, MA signals, risk-gated simulated fills, capped exits, SQLite recovery/accounting, dashboard and controlled ML lifecycle described in README. |
| Audited | RC2 release notes, artifact/source/log manifest and SHA256SUMS all matched before documentation changes. |
| Tested again | **2,393 full-suite tests passed, zero skips**; 31 focused package/dashboard/launcher tests passed. |
| Installed and exercised | Offline installation in a temporary environment; initial PAUSED dashboard, snapshot/readiness, expected inactive health 503, clean shutdown and STOPPED restart passed. Virtual balance stayed 1234.56 USD; no positions or orders. |
| Build verified | Two byte-identical RC3 wheels; all packaged Python modules match source; metadata contains the final README. |
| Existing account | No existing application restarted, account opened/recovered/resumed, Emergency Stop released, credentials accessed or orders submitted. |
| Not operationally verified | Sustained genuine BTC/USDT operation of the candidate or completion of the existing position's capped exit after recovery. |
| ML/profitability | **No established predictive edge, no qualified profitable strategy, no automatic deployment of an unqualified model.** |
| Live execution | Disabled; exchange sandbox/permissions and live trading are not qualified by this software package. |

The installed-dashboard evidence uses a deliberately separate synthetic account
that never runs a strategy. It is an installation check, not market validation.
The full suite uses isolated/mock execution fixtures, not the existing account.
No new experiments, training or protected-dataset inspection were performed.

## Package contents and integrity

`dist/Trading-Agent-0.3.3rc3.zip` contains:

- Final README and macOS installation guide.
- Frozen `wheels/safety_first_trading_app-0.3.3rc3-py3-none-any.whl`.
- Operator recovery/support documents, release checklist and selected research/
  architecture evidence documents; raw datasets and models are excluded.
- Conservative example configuration and existing Mac launcher.
- Test/build/installation evidence, build-tool versions and source hashes.
- `SHA256SUMS` for all included files except itself.

Excluded: all SQLite accounts/backups, credentials, local runtime configuration,
raw market data, protected datasets and model artifacts. The bundle is not a full
source checkout and does not contain the source test suite. Historical reports
retain their original evidence references; unavailable raw artifacts are not
recreated or represented as included.

Wheel SHA-256:
`e6df7b07b0115e135a0986390f11332dc3d98111255ec4952d9328280b9a5c11`

The ZIP has a sibling `.sha256` file. After extraction, run
`shasum -a 256 -c SHA256SUMS` before installation. See
[the Mac guide](MACOS_INSTALLATION.md) for exact offline commands and the safe
zero-order dashboard check. Do not substitute old wheels with the same general
project name.

The wheel uses `SOURCE_DATE_EPOCH=1794528000`; same source and recorded build tools
produce identical bytes. ZIP entries also use fixed timestamps/order/permissions;
identical frozen inputs reproduce the ZIP. Rerunning tests changes evidence logs,
so a newly assembled evidence bundle can legitimately have a different ZIP hash.

## Final release checklist

- [x] Prior RC notes, manifest, logs and checksums audited.
- [x] README describes implemented architecture/features and limitations.
- [x] Mac guide covers prerequisites, offline install, isolated dashboard,
      shutdown, troubleshooting and gated recovery.
- [x] Full tests, isolated installed dashboard and inactive restart pass.
- [x] Zero-order inspection account; actual BTC/USDT account untouched.
- [x] Reproducible wheel, source/version identity and complete bundle hashes checked.
- [x] PAPER remains default; no live orders, model promotion or profitability claim.
- [ ] CoinSwitch authoritative quantity/tick/minimum-quantity clarification.
- [ ] Confirmed rule implementation/tests and fresh accepted market metadata.
- [ ] Explicit operator recovery authorization, verified backup/reconciliation.
- [ ] Recovery into PAUSED; independently checked genuine feed/dashboard/accounting.
- [ ] Separate resume authorization and seven-day operational observation.
- [ ] Stable operational release designation after those gates; independent of any
      future profitability qualification.

## Exact remaining operational prerequisites

1. Submit the [prepared request](COINSWITCH_RULE_CLARIFICATION_REQUEST.txt). Obtain
   authoritative `precision.limit=2` semantics, valid/invalid examples, effective
   BTC step/price tick, minimum BTC quantity and residual handling. Never guess.
2. Implement/test only those confirmed rules. With appropriate read authorization,
   obtain fresh metadata and validate its source/hash/market/expiry. The existing
   unsupported capture remains rejected; do not edit it to pass.
3. Obtain explicit authorization before stopping/recovering/starting the existing
   account. Confirm writer/scheduler ownership, make a new WAL-aware verified
   backup, reconcile original cash/BTC/orders and preserve residual inventory.
4. Follow [the audited recovery procedure](PAPER_OPERATOR_RECOVERY_CHECKLIST.md):
   release Emergency Stop only under that authorization, restore PAUSED, then
   verify fresh genuine quotes/candles, rule validity, startup reconciliation,
   dashboard and learning status. No automatic resume.
5. Obtain separate resume approval and observe seven days: freshness/gaps,
   availability, rejection reasons, risk decisions, capped/partial exits, fees,
   cash/equity/P&L reconciliation, restart preservation and learning outcomes.
   Stop on duplicate/unauthorized orders, overselling, unknown orders, broken
   persistence/accounting or risk/metadata/freshness violations. Do not force a
   trade to finish validation. No natural exit means that portion stays unverified.

Engineering package verification is complete. Genuine operational acceptance is
still externally gated. Profitability and ML predictive value remain unproven.
