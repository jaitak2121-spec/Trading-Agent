# Final completion audit and delivery plan

Audit date: 2026-11-11

## Final software handoff package — 0.3.3rc3 — 2026-11-13

Audited RC2 source/artifact/log hashes and checksums successfully. Replaced the
outdated README and added a clean Mac installation/operator guide and final
release checklist. README is wheel metadata, so the new documentation received
an explicit RC3 identity; prior wheels/evidence remain untouched. No trading or
risk behavior changed. **2,393 full-suite / 31 focused tests passed, zero skips**.
Offline isolated install, snapshot/readiness, expected inactive health, clean
shutdown and STOPPED restart passed with unchanged virtual cash and zero orders.
Wheel rebuilds match byte-for-byte; final operator bundle files/checksums verified.
Existing account/application, latch, BTC/cash, credentials, rejected models and
protected data were not accessed or changed. Engineering package verification is
complete; CoinSwitch precision rules, fresh metadata and separately authorized
account recovery/resume/observation remain blocking. Profitability is unproven.
[Final package summary, evidence boundaries and exact remaining gates](FINAL_SOFTWARE_RELEASE.md).

## Final release-engineering review — 0.3.3rc2 — 2026-11-13

Fixed two confirmed defects: CLI abbreviations could evade launcher exact-option
restrictions, and the release drill could select an unrelated newer wheel. Main
CLI now requires full flags; packaging selects the declared version exactly.
Added two deterministic regressions. **105 focused / 2,393 full-suite checks
passed, zero skips**; installed startup/readiness/shutdown/crash recovery passed.
Two offline rc2 builds are byte-identical and all 131 Python modules match source.
No further confirmed defects found in the scoped release paths. No actual account
opened/restarted/recovered/resumed, credentials access, orders or risk changes.
CoinSwitch precision/fresh metadata and authorized real-account observation remain
blocking. Engineering RC verification is complete; stable operational acceptance
and profitability are not established.
[Final artifact hashes, release notes, evidence and next gates](RELEASE_NOTES_0_3_3_RC2.md).

## Release readiness candidate 0.3.3rc1 — 2026-11-13

Replaced launcher database-existence gating with owned-process/origin startup
readiness; learning never starts after an unconfirmed probe. Separated collection
readiness from trading health, sanitized snapshot/health failures, and closed
dashboard/CLI startup cleanup gaps. Corrected package/distribution identity and
built a distinct **0.3.3rc1** candidate twice with identical bytes.
**103 focused / 2,391 full-suite tests passed, zero skips**; installed temporary
startup/readiness/shutdown/crash recovery passed. Initial full-suite architecture
failure was corrected by placing the HTTP probe at the existing HTTP boundary,
not by weakening tests. No actual-account launch/recovery/resume, orders,
credentials access, risk weakening or research rerun. Exchange precision semantics
and separately authorized operational validation remain blocking.
[Verified artifact, reproduction commands and release gates](RELEASE_READINESS_0_3_3_RC1.md).

## Paper release / shutdown completion — 2026-11-13

Fixed exception-safe post-worker cleanup and scheduler child cancellation; retained
database ownership on worker-stop timeout. Five new regressions; **25 focused /
2,382 full-suite tests passed, zero skips**. Isolated installed-wheel startup,
shutdown, crash recovery and paused restart passed. Built a distinct 0.3.2 safety
candidate without replacing old wheels or deploying to the account.

Read-only account integrity passed: 2,444 audit records, one order, original
0.01399178 BTC and 8,998.80000791 USDT; Emergency Stop/latch unchanged. Port 8766
refused connections: current feed/dashboard liveness is not verified. No restart,
recovery, resume, credentials use, orders or protected-data access occurred.
The CoinSwitch precision clarification remains external and blocking; no new
profitability hypothesis justified repeating research. [Release evidence,
prerequisites and exact operator workflow](PAPER_COMPLETION_RELEASE_CHECKLIST.md).

## Full paper-order path verification — 2026-11-13

Quantity/limit semantics remain unresolved after rechecking authoritative docs;
prepared a concise CoinSwitch request without sending it. Closed independent
execution gaps: metadata/state checks now guard initial and resting paper fills,
direct gateway orders receive exact market/precision/notional validation, and
generic recovery-only access to a genuinely bound account defaults fill-disabled.
Core risk checks/limits remain unchanged. Added three deterministic regressions;
**258 focused / 2,377 full-suite tests passed, zero skips.** Actual-account
SQLite/audit integrity, venue/ledger agreement and cash/fill accounting passed
read-only review. Emergency Stop, latch and 0.01399178 BTC remain preserved, with
one existing order and no post-stop fills or new exit-plan events. No reload,
recovery, resume, live execution, credentials access or new research in this pass.
[Order-path findings and exact next operator action](PAPER_ORDER_PATH_AUDIT.md).

## Authoritative metadata acquisition / safety hardening — 2026-11-13

User-authorized signed GET obtained genuine BTC/USDT c2c1 rules: notional min=5,
max=30,000 USDT; precision base=5, quote=2, **limit=2**. Captured exact response,
timestamp and hashes without credentials. Existing parser correctly rejects the
undocumented nonzero adjustment; independent minimum base quantity remains
unverified. Five-decimal base formatting implies a potential 0.00000178 BTC
residual from the current holding. No values or dust were invented/overwritten.

Hardened future real-paper readiness: missing/unverified metadata blocks execution;
exact minimum-notional products cannot be rounded upward into acceptance. Added
scoped GET reader and six tests. **116 focused / 2,374 full-suite tests passed,
zero skips.** Active account, latch and position remain untouched; no reload,
recovery, resume or orders. Metadata blocker is narrowed, not falsely resolved.
[Evidence, changed files and required CoinSwitch clarification](EXCHANGE_METADATA_VERIFICATION.md).

## Exit coverage / accounting review without recovery — 2026-11-13

Added six deterministic cases for partial-fill replay, minimum tails, blocked
restart, inactive-state gates, metadata expiry and stale-mark dashboard behavior.
**126 focused and 2,368 full-suite tests passed, zero skips.** No production code
changed. Read-only actual-account inspection verified SQLite/audit integrity,
fill/cash conservation, marked-equity/P&L identities and persisted venue/position
agreement. Emergency Stop and BTC inventory remain intact; no recovery, control
call or test order touched the account. Verified exchange metadata and manual
operator review remain required before execution validation on the open position.
[Exact metadata, backup and recovery checklist](PAPER_OPERATOR_RECOVERY_CHECKLIST.md).

## Position-reducing exit hotfix — 2026-11-13

Fixed oversized exits with sequential compliant chunks; core risk checks/caps
remain unchanged. Audit-backed requests restore remaining exits across restart;
open/partial orders prevent duplicate submissions. Exact downward lot rounding,
minimum-tail allocation, visible dust, fee/inventory checks and one-time blocked
states prevent overselling or repeated refusal loops. Operator re-arm never
releases Emergency Stop or submits an order. **170 focused and 2,362 full-suite
tests passed, zero skips.** The current account remains emergency-stopped with
its kill switch engaged; no restart, resume, live order or post-stop fill.
Source changes are limited to paper service/order policy and focused tests.
[Hotfix details and deployment limits](PAPER_EXIT_HANDLING_FIX.md).

## Genuine-market paper integration — 0.3.2, 2026-11-13

The default launcher now runs real CoinSwitch BTC/USDT data and the existing
paper engine on port 8766 with separate USDT state, observed spread, explicit
10bp fees/2bp slippage, unchanged risk gates and the bounded learning scheduler.
Database provenance prevents synthetic P&L or unverified legacy accounts entering
genuine views/learning. Dashboard shows result provenance, quotes, costs and
scheduler/readiness. Fixed actual worker dispatch and shutdown-property defects.
Excluded startup/reconnect partial-minute trade buckets; preserved the initial
pre-guard verification separately without training on it. No old account reset.

115 focused tests and **2,355 full-suite tests passed, zero skips**. Native
contract fixtures verify feed → candles → MA → risk → paper fills/accounting →
persistence/dashboard/restart; actual public feed independently shows advancing
quotes and two validated complete candles at the evidence snapshot. No genuine
fill or model-profitability claim. The genuine service and hourly learner are
running; ML correctly awaits sufficient genuine experience and remains unapproved.
Packaged/source-matched v0.3.2. Details and exact launch commands:
[genuine-market completion report](REAL_MARKET_PAPER_0_3_2.md).

Remaining: adequate forward experience, independently qualified net edge and
extended paper evidence. Rejected models/protected data remain untouched; live
orders and automatic model promotion remain disabled.

## Integrated ML lifecycle — 0.3.1, 2026-11-12

Closed actual integration gaps: durable hash-checked closed candles and restart
history; collection/outcome maturation while paused without strategy or gateway
calls; bounded scheduled learning from contiguous collected experience; chronological
evaluation, automatic weak-model rejection and a deployment hold pending independent
cost-aware qualification; persistent new-data cooldown and artifact verification;
explicit pre-signal monitoring safety pause; collection/learning dashboard visibility.
Reused existing approval, explicit rollback, risk/accounting and disabled native
exchange execution contract. No production model was promoted or live order enabled.

**2,350 tests passed, zero skips**. Synthetic approved-model integration verifies
inference → risk → paper fill → fees/accounting → restart; no genuine model fitting
or profitability result is claimed. Public operational check: 65 supervised paused
collection passes, one genuine validated candle, zero orders/errors. Restart restored
history and remained stopped. Packaged v0.3.1 with 131 source-matched Python modules
and isolated CLI verification. Exact artifacts, commands, changed components and
limits: [ML lifecycle completion report](ML_LIFECYCLE_COMPLETION_0_3_1.md).

Remaining evidence blockers: no independently qualified profitable model, insufficient
new forward experience/elapsed validation, unchanged historical gaps, and unverified
CoinSwitch sandbox/permissions/live qualification. Operationally verified collection
and synthetic paper lifecycle are distinct from an extended genuine-model paper trial.

## Research continuation after 0.3.0 — 2026-11-12

Executed three new preregistered development studies; see the full
[continuation findings](ML_RESEARCH_CONTINUATION_REPORT.md). Frozen-forecast
diagnostics found exploratory 24h logistic-payoff ranking information (IC
0.24443, local six-test p 0.02220), but this reused development result is not
independent profitability evidence. Prior-only payoff calibration produced no
cost-stressed eligible entries over 16 periods; a matched control lost with 71
trades per scenario. A fixed funding-settlement timing study fitted 192 models
and found no information test surviving 12-test correction. Settlement auxiliary
inputs worsened MSE versus price controls at all three horizons.

Added 206 saved models in total, 96 execution ledgers and 213 independently
reconciled trade-scenario records. Verified the original eight studies without
rerunning their experiments. New source modules: `signal_information.py`,
`calibrated_expectancy.py`, `event_information.py`; three corresponding test
files and `scripts/verify_research_continuation.py`. New registry:
`artifacts/research-registry/post-v030-continuation-registry.json`.
**2,341 tests passed, zero skips**; 13 new deterministic tests. Original model,
dataset and registry hashes were preserved; complete development sources,
chronology, source snapshots and new ledgers verified. No production behavior,
credentials, actual orders, protected evaluation observations or promotion.

Independent on-chain sourcing hit an API HTTP 403 and no pre-March-2025 BTC CSV
publication vintage in the publisher's queried public archive history. No such
data was claimed downloaded. Available sample sizes/calibration stability and
execution costs still prevent a credible net-edge claim. Next concrete milestone:
prospective synchronized depth/trade/quote information collection with audited
receive times, then preregistered information testing on independent periods;
or obtain verified, appropriately licensed historical publication vintages.
This research tooling is source-only beyond the retained 0.3.0 release wheel.

## Active ML profitability research — 0.3.0, 2026-11-12

Implemented and executed eight research studies rather than another readiness
audit: **38 outer candidate policies/algorithms, 542 outer-fold evaluations,
1,545 cost/execution ledgers, 285 saved trained models and 5,141 independently
reconciled simulated trade records**. No strategy met the frozen qualification
criteria. The research outcome is insufficient available evidence for a credible
positive net edge, not a profitable application delivery.

Added cost-adjusted trade-outcome labels; causal native-cadence features; ridge,
payoff logistic, shallow/boosted tree and regime models; 1-hour through seven-day
holds; BTC/ETH context; actual public funding/aggressor information; cash decisions;
and inner-only model, horizon, entry-buffer and risk-sizing selection. All use
the existing simulator, existing risk caps and real CoinSwitch execution candles.
No production strategy/risk behavior or actual exchange-order transport changed.

Acquired genuine development history with raw-response provenance. Original
incomplete parents remain FAIL, including unavailable early 2021 history and a
partial Binance hourly record. Used independently complete, openly scoped
derivatives only; no gap interpolation or price replacement. Raw/native record
matching verified nine acquisitions and 23 CoinSwitch derivatives. ETH execution
prices were never replaced by BTC or Binance prices. Funding is an information
feature, never fictional spot income.

Some 72-hour BTC candidates had positive development averages, but confidence
bounds crossed zero and all failed benchmark/stability/trade-count requirements.
Nested optimized BTC policies produced gains concentrated in one period; the
ETH-market extension also failed. No threshold was tuned on outer observations.
No confirmation, protected Segment 7, rejected V1/V2 model or original missing
candle dataset was modified or repurposed. No unsupported model was promoted
into paper execution and no approved extended paper trial was started.

**2,328 tests passed, zero skips**. Added 28 research tests plus a release-content
test. Independent ledger audits found no entry over the 1,000-USDT cap and no
candidate realized daily loss beyond 100 USDT. Registry integrity, model/data
hashes, source snapshots and chronology were verified. Compilation and whitespace
checks passed. Research summaries now load read-only in the existing dashboard,
with approval always None.

Final evidence and full findings:
[ML profitability research report](ML_PROFITABILITY_RESEARCH_REPORT.md),
`artifacts/research-registry/net-expectancy-registry-v4.json`, and
`artifacts/research-registry/acquisition-provenance-verification.json`.
Old plans/results/source snapshots remain retained for exact historical replay.

Installed final 0.3.0 offline wheel, with all 126 Python modules byte-matched to
source and installed dependency/isolated-import checks passed. Local dashboard
and synthetic recovery drills passed. Wheel SHA-256:
`fa5aac9d9645aa98d22760fe8cc0a602608628ca8aa50b15c235b1f234217170`.
Release: `dist/expectancy-research-0.3.0-final/`;
verification: `artifacts/release-0.3.0-expectancy-research-final-verification.json`;
completion: `artifacts/research-registry/ml-expectancy-research-completion.json`.

Remaining: the verified information sets do not identify a credible edge; new
point-in-time information or genuinely independent forward evidence is needed
before another selection/paper-deployment claim. Existing missing candles and
exchange-access/permission blockers are not declared resolved. Actual live
execution remains disabled. This is the mission's insufficient-evidence outcome,
not a claim that every future market or research idea is impossible.

## Genuine public cohort and timing-boundary correction — 0.2.6, 2026-11-12

Executed the predefined three-period public BTC/USDT depth cohort under requested
macOS idle-sleep inhibition. Retained every outcome: 94, 218 and 218 fresh unique
snapshots. Original research quality **FAIL in all three periods**; no pooled
cost estimate or replacement windows. Period 1 had large clock divergence,
feed errors and reconnects; periods 2/3 passed the combined feed observer but
failed the then-current timing gate. Source/report/protocol/code hashes and
deterministic report regeneration were verified before any implementation change.

Found and corrected a measurement mismatch: monotonic observation ended before
transport shutdown while wall observation previously ended afterward. Added exact
pre-cleanup wall boundaries; validation now compares the same interval on both
clocks. Kept the strict two-second clock tolerance and five-second gap limit.
Invalid/partial markers fail closed; original cohort evidence remains unchanged.
The much larger period-1 clock discrepancy is still unexplained.

A separately preregistered single pilot **passed**: 216 fresh snapshots, two
closed candle messages, zero feed errors/reconnects, 120.004 seconds observation,
0.27ms clock divergence, 1.209-second maximum receive gap. This supports the
measurement fix for one window; it does not overturn the failed cohort or
establish trading profitability/readiness. No account request, credentials,
orders, protected observation access, training, tuning or model promotion.

**47 focused tests and 2,299 full-suite tests passed, zero skips**. Compilation,
whitespace, offline release/local dashboard/synthetic recovery drills passed.
Installed source-matching 0.2.6 wheel, SHA-256
`5984394941c8e471ed9bcfbd1ba95eeb02d4709058d6eebf81121c555f0084d9`.
Detailed files, protocol/data hashes, results and next milestone:
[public cohort report](PUBLIC_DEPTH_COHORT_REPORT.md).

Next: independently preregister observations across distinct periods using the
corrected timing boundaries. No profitable strategy has qualified, missing
original candles remain unrecovered, exchange permissions remain unverified,
and actual live execution stays disabled.

## Automatic public-observation quality gate — 0.2.5, 2026-11-12

Completed strict timing/coverage/outage checks and a hash-required offline
assessment command. New depth report schema explicitly separates diagnostic
statistics from research eligibility. Missing timing metadata, wall/monotonic
divergence, startup/tail gaps, reversed timestamps, observer inconsistencies,
stale quotes and outages cannot silently qualify. Sealed-path protection also
resolves symlinks before any file read.

Reassessed the unchanged 0.2.4 public capture: **FAIL**, five independent rejection
reasons, 443.735-second clock divergence and a 506.978-second receive gap. No new
collection, credentials, paper/live orders, model changes or protected observation
access. Existing static statistics do not establish fill quality or profitability.

Verification: **2,295 full-suite tests passed, zero skips**; offline package and
synthetic recovery drills passed. Installed 0.2.5 wheel, SHA-256
`0636a2e648276a18e6037cf2169448e2a0cf2323c4bb087049b355aaa3e8da91`.
Details, exact command, file list, evidence hashes and next milestone:
[observation quality report](PUBLIC_OBSERVATION_QUALITY_REPORT.md).
No profitability, data-recovery or exchange-access blocker has been declared
resolved. Next: fixed multi-period public collection on an awake, clock-stable
host with independent quality assessment of every period.

## Public depth diagnostics — 0.2.4, 2026-11-12

Added optional full-depth preservation to the existing public stream, bounded
read-only capture, exact-decimal static buy/sell sweeps, insufficient-liquidity
refusal, hash-bound reports and explicit observer failure disclosure. Default
production behavior, risk controls and strategy/model approval are unchanged.

Fixed 120-second BTC/USDT c2c1 capture received 96 snapshots, 93 fresh unique;
two feed-error polls and one successful reconnect. No closed candle arrived:
**combined observer FAIL**, preserved without weakening its criteria. All
100/500/1,000 USDT midpoint sizes fit observed top liquidity. Median modeled
fee-inclusive cost was about 10.00068bp per side; this is not a fill guarantee,
historical cost replacement, profitability evidence or live qualification.

Timing verification also found 563.737 seconds of wall time versus 120.002
monotonic observer seconds and a 506.978-second receive gap. Cause unverified;
this was not a continuous two-minute wall-time qualification. Separate hashed
timing evidence is preserved alongside the capture.

Independent arithmetic checked all 576 directional sweeps, along with capture,
report and protocol hashes. No new account lookup or credential resolution;
reused only the previously verified 10bp fee scenario. No orders, training,
promotion, sealed observation access or protected artifact changes.

Full suite: **2,290 passed, zero skips**. Offline release/recovery drill passed;
117 packaged modules match source. Installed final 0.2.4 wheel, SHA-256
`9bc3f7a3ebcf77d7cb1e5d0cdc37e25f0d724735173ae4be9f55f74e823091e6`.
Details, files, artifacts, limits and exact next milestone:
[public depth report](PUBLIC_DEPTH_COST_REPORT.md).

Remaining: no qualified profitable strategy; original missing candles remain
unrecovered; sandbox account access/permissions and actual lifecycle validation
are unverified. Live execution stays disabled. Next: preregister longer public
depth/outage observations across periods before changing research cost assumptions.

## Authorized current-fee verification — 2026-11-12

After explicit user authorization, executed one signed GET to the Spot v2
`tradingFee` endpoint for `c2c1`. HTTP 200; effective BTC maker/taker fees were
both **10bp (0.10%)**, captured at 10:51:44 IST. Exact authorized Keychain services
were used with values hidden and injected only into the reader process. No
balance requests, order requests, trading-permission changes or live activation.

Independently verified profile hash, market identity, HTTP status, 0600 permissions
and Git exclusion. Current fees reproduce all six base-case fee ledgers at their
recorded notional; no rerun, strategy change or profitability requalification is
justified. The schedule is current-account evidence, not proof of 2025 historical
fees, fee settlement assets or trading capability. Snapshot and comparison are
under `artifacts/private-fees/`; no credential values or raw private response
body was persisted. Focused fee/cost tests: **13 passed**. No code changed, so
the prior full-suite result remains **2,279 passed, zero skips**.

This entry supersedes the authorization-pending state in the initial handoff
below. Historical evidence was retained rather than rewritten.

## Execution-cost preparation — 0.2.3, 2026-11-12

Added frozen-fill cost diagnostics, fresh public spread capture and strict
account-fee snapshot tooling. The diagnostic reconciles each trade/fold and
shows that all six base-cost fill sequences remain negative before fees.
This retains modeled spread/slippage and is not a zero-cost counterfactual.
No strategy, threshold, model approval or past result was changed.

Executed 120 seconds of genuine credential-free `c2c1` BTC/USDT observation:
216 quotes, 209 fresh unique samples, two candle messages, zero feed-error polls
or reconnects. Median full quoted spread was about 0.00135534bp. This short,
event-weighted top-of-book sample estimates neither market impact nor historical
spreads, and does not qualify a strategy or live execution.

The native execution contract now reuses the strict fee parser for funding
checks. The new account-fee reader is a fixed single-endpoint GET with no order,
balance, redirect or retry path. Its CLI requires explicit read authorization,
refuses existing files, and writes private profiles with mode 0600. The suggested
`artifacts/private-fees/` directory is Git-ignored. Actual user authorization is
pending: no Keychain credential was read and no account-fee request was made in
this milestone. Tests used mock credentials and responses only.

Dashboard `--research-report` now accepts hash-verified native walk-forward and
cost-diagnostic reports; `approved_strategy` remains None. Optional quote sinks
reuse the existing observer and stop cleanly on sink errors. Default PAPER,
production strategy/risk controls and disabled actual execution are unchanged.

Verification: **46 focused tests; 2,279 full-suite tests passed, zero skips**.
Corrected installed-release account/dashboard/interruption/recovery/resume/restart
checks passed. `EXECUTION_COST_READINESS_REPORT.md` records commands, data hashes,
actual results and limits. Final wheel is in `dist/cost-ready-0.2.3/` with
`artifacts/release-0.2.3-cost-ready-verification.json` as release evidence.
The exact final wheel is installed in `.venv`, with dependency/isolated import,
compilation, launcher and whitespace checks passed. Consolidated hashes/status:
`artifacts/execution-cost-readiness-0.2.3-evidence.json`.
Profitability remains unproven; CSX account/test access remains unverified.

## Independent completion and new research — 0.2.2, 2026-11-12

Completed additional engineering/research beyond support-request preparation.
`INDEPENDENT_COMPLETION_REPORT.md` maps all nine requested areas to implementation,
tests, evidence and remaining boundaries.

- Alternative public-source investigation found four genuine Binance candles,
  preserved separately and independently validated. Underlying `c2c1` equivalence
  is not established; CoinSwitch's original four missing candles remain missing.
- Added hash-bound full-range eligibility evidence, explicit complete-run
  derivation and qualification flags. Incomplete diagnostic datasets cannot
  qualify cost-research candidates; future V2 final fitting and paper approval
  require matching passing source-integrity provenance. No legacy model is patched.
- Acquired genuine native-five-minute CoinSwitch data: 51,837/51,840 candles,
  parent integrity FAIL. Two explicitly bounded complete derivatives (28,952 and
  22,885 candles) pass independently. No missing price or sub-minute path was inferred.
- Executed a new frozen six-candidate/seven-fold development experiment under
  base and stressed costs. All six lost money, with zero positive folds. All are
  rejected; none selected for confirmation/promotion. Independently reconciled
  all 84 fold/cost/candidate ledgers. Details/hashes: `NATIVE_FIVE_MINUTE_RESEARCH_REPORT.md`.
- Added a native Spot execution contract over injected I/O with a disabled
  default. No HTTP order client, signer/key, host, launcher selection or real
  execution was added. Tests cover risk/funding/metadata, exact numeric encoding,
  stable UUID identity, partial fills, cancel-pending races, UNKNOWN/no resend,
  SQLite restart, wallet mismatch and incomplete inventories.
- Found the official CSX paper-endpoint link; its credential-free public health
  route returned 403. It is a different API surface, not verified Spot v2 test
  access. This narrows/corrects earlier broader sandbox statements. Requirements
  and deliberate activation stages are in `COINSWITCH_ACTIVATION_CHECKLIST.md`.

Also fixed the legacy trainer accepting non-one-minute candles for one-minute
paper inference. New V1 artifacts bind their cadence; incompatible or missing
cadence refuses paper approval. No frozen model was edited.

Verification: **2,264 full-suite tests passed, zero skips**; corrected installed
wheel passed paper account/dashboard/interruption/recovery/resume/restart drills.
Final wheel/evidence are under `dist/final-0.2.2/` and
`artifacts/release-0.2.2-installed-verification-v3.json`. Earlier builds and evidence
were retained; architecture-rule and preregistration-serialization failures were
fixed without weakening tests or changing acceptance after results.
The exact final wheel is installed in `.venv`; dependency, compilation, launcher
and whitespace checks passed. Consolidated source/evidence hashes are recorded in
`artifacts/completion-0.2.2-evidence.json`.

Actual boundaries: no new genuine ML fitting (five-minute bars are incompatible
with the existing one-minute feature schema); no approved model/strategy; no
profitability or live-readiness claim. Original rejected models, confirmation,
ML Segment 7 and production risk/strategy settings remain untouched. Default
PAPER and disabled actual live execution are preserved.

## Profitability/live follow-up — 2026-11-12

Actual targeted historical recovery executed into a separate artifact: six HTTP
200 reads, zero returned/recovered candles. Four missing May 7 timestamps remain
consistently omitted; complete-dataset validation still fails. Original source
hash is unchanged; no data was fabricated, no training/evaluation/strategy
promotion occurred and no protected observations were opened.

Official CoinSwitch authentication, Spot, order and fee documentation was checked
again. The operator confirmed no sandbox/testnet confirmation. No authenticated
order adapter was added or tested against production funds. The external venue
qualification requirement is unresolved; profitable strategies are also
unestablished. Updated `LIVE_EXECUTION_READINESS.md`,
`COINSWITCH_SANDBOX_REQUEST.md` and `DEVELOPMENT_EXTENSION_REPORT.md` record exact
evidence, limits and next requirements. No support request was sent.

Verification: **122 focused tests and 2,238 full-suite tests passed, no skips**.
The local paper release remains available; neither profitability nor live
readiness is marked complete. Existing risk controls remain unchanged.

## Packaged local delivery — 0.2.1, 2026-11-12

Current source is packaged, independently exercised and installed in `.venv`.
Added installed `trading-paper-account`, current wheel-module/entrypoint checks,
offline build/install, custom paused-account verification and persistent release
evidence. Fixed a real path-with-spaces bug in the fallback release launcher and
added regression coverage. Full suite: **2,238 passed, zero skips**. Packaged-wheel
startup, dashboard, interruption, recovery, resume and restart passed. Compilation,
launcher syntax, dependency consistency and whitespace checks passed.

`DELIVERY_0_2_1.md` records launch commands, release checksum, files and limits.
README now leads with actual supported capabilities and replaces an obsolete
240-candle milestone. Existing packages, accounts, rejected models and protected
data were preserved. Local paper/research delivery is verified; profitable
strategies, a completed older-data extension and real-money execution remain
unproven/incomplete, with no automatic activation or promotion.

## Safe paper-account setup — 2026-11-12

Added `python -m trading.app.paper_account create --state-db NEW_PATH --cash AMOUNT
--currency USD|INR|USDT`. It reuses existing portfolio, audit, bot state and SQLite
composition; no alternate accounting implementation was introduced. Initializes
privately, audits virtual capital, pauses the bot, closes SQLite, verifies database
and audit integrity, then publishes atomically without replacing another file.
Existing files/symlinks/sidecars, invalid cash and unsupported precision are
refused. Failed verification leaves no published account. Private permissions
and recovery preserve the selected starting balance; a restart never auto-resumes.

Files added: `trading/app/paper_account.py`, `tests/test_paper_account.py`.
Updated: `docs/OPERATING_GUIDE.md` and this audit. Verification: **24 focused
tests passed; 2,236 full-suite tests passed, zero skips**; CLI help, compilation
and whitespace checks passed. Tests cover persistence/restart, paused execution
refusal, explicit resume, concurrent publication, old-file/audit preservation,
failed integrity, currency/precision validation, private permissions and CLI
errors. Only temporary test accounts were created; user accounts were untouched.

Remaining: no mid-session cash injection, reset or dashboard balance editor;
independent trials use separate databases. No production strategy, live transport,
rejected model or protected dataset changed. Historical extension remains blocked
by four unresolved May 7 gaps; next research milestone is targeted genuine-candle
recovery into a separate artifact, followed by independent validation before
continuing acquisition. No evidence of a profitable strategy has been established.

## Older development acquisition — 2026-11-12

Added a fixed 180-day, GET-only daily checkpoint driver reusing the existing
downloader/validator, with hash checks and fail-closed restart. November 1, 2025
availability probe passed (1,440 candles). The May 6–November 2, 2025 extension
stopped on May 7: 1,436/1,440 candles, four unresolved missing minutes across
three gaps. HTTP 200 on both requests; no retries. Failed responses and request
hashes were preserved; no combined dataset, training, evaluation or orders.
Verification: 40 focused tests and **2,226 full-suite tests passed, no skips**;
independent candle checks, compilation and whitespace checks passed.
Protected data and rejected models remain untouched. See
`DEVELOPMENT_EXTENSION_REPORT.md` for evidence, hashes and exact next recovery step.

## Cost-aware development research — 2026-11-12

Implemented six preregistered slower research hypotheses using complete 5/15/30m
signals and the existing native-minute execution/risk simulator. Base and stressed
costs were tested across five independent contiguous development runs. All six
had negative mean net returns and insufficient trades; **none qualifies**. No
production strategy or model was activated. See `COST_AWARE_RESEARCH_REPORT.md`
for frozen hashes, period results, assumptions and reproduction commands.

Verification: 23 focused tests passed; full suite **2,219 passed, no skips**;
all 60 saved ledgers independently reconciled; compilation and whitespace checks
passed. Protected evaluation observations and rejected models remain untouched.
Software readiness does not establish profitable trading. Next: improve genuinely
unseen continuous data coverage and verify fee assumptions before another frozen
research experiment, not parameter tuning against these failed results.

## Local gap closure — 2026-11-12

Version 0.2.0 closes the later V2 deployment, monitoring-alert, optional desktop
delivery, writer ownership and spot funding gaps. See `LOCAL_COMPLETION_0_2.md`
for implementation, verification and remaining external boundaries. Both Python
test environments passed 2,209 tests with no skips. Existing rejected artifacts
and protected evaluation data were untouched. The remaining live-qualification
blocker cannot be resolved by using production account funds as a sandbox.

## Final paper-product integration status — 2026-11-11

The later M4–M6 integration is documented in `FINAL_RELEASE_REPORT.md` and
`OPERATING_GUIDE.md`; these updates supersede the original gap inventory below.
Delivered fixed development comparisons and explicit strategy approval process,
model registry/rejection/archival/rollback gates, per-inference revocation checks,
gap-safe forward prediction outcomes and descriptive drift/performance reports,
disabled-by-default resource-bounded research/public-collection jobs, schema
migration, verified backup/restore, local launcher, optional authenticated local
paper controls, and bounded dashboard equity/audit/dataset/research views.

Final verification: **2,185 tests passed, no skips** with localhost permission;
installed-wheel release/restart drill passed; compilation/whitespace checks
passed. No model was trained or promoted. The development comparison had two
empty validation folds and negative returns for every candidate in the usable
fold. No strategy was approved. All protected evaluation artifacts remain sealed.

Live qualification is externally blocked: reviewed CoinSwitch documentation
does not establish a verified non-production spot environment or account-specific
permissions. `LIVE_EXECUTION_READINESS.md` records the primary sources and exact
boundary. Authenticated order transport remains absent and live execution remains
disabled. No claim of whole-system live readiness or profitability is justified.

Operator-dependent setup remains: external alert destination, login service,
phone networking, and enabling/configuring any scheduled jobs. Future V2 paper
inference needs a validated adapter and an independently approved model. These
are explicit limits, not silently completed items. Ordinary local paper startup,
monitoring, controls, recovery, research, backup and release usage are documented
and usable now.

## M3 runtime risk policy — completed 2026-11-11

The existing RiskEngine now checks four additional policies before minting risk
approval. Every approval includes all four checks. Configuration, audit and the
dashboard expose their limits; no execution route was added.

- `max_drawdown_fraction`: fresh equity versus a durable peak, initialized at
  least at starting capital. Equality refuses new exposure. Hash-chained peak
  audit events survive restart. Missing equity refuses entry. This is current
  drawdown, not a permanent latch; recovering above the boundary permits entry.
- `max_positions`: distinct held symbols plus symbols reserved by open/pending
  orders and the proposed order. Multiple orders for one symbol use one slot.
- `trade_cooldown_seconds`: elapsed wall time since any persisted order for
  that symbol was last updated, including rejections. Future timestamps block.
  Equality permits entry. Restart retains the original order timestamps.
- `max_candle_range_fraction`: latest complete candle `(high-low)/close`.
  Equality refuses entry. Missing observations or observations older than 120
  seconds refuse entry; future/incomplete observations are rejected. Restart
  invalidates observations until fresh validated data arrives.

Strict position reductions without sign flips waive these four policies and
audit the waiver. Reversals do not qualify. All existing gates still apply.
Both traditional and ML signals receive identical risk checks.

Existing profiles preserve their behavior: optional limits default disabled,
cooldown defaults zero. `config/paper.example.toml` explicitly enables 10%
drawdown, 5 symbols, 60-second cooldown and 10% candle range. These are example
safety settings, not tuned strategy parameters.

Files changed: `trading/core/config.py`, `trading/core/risk.py`,
`trading/core/gateway.py`, `trading/app/composition.py`,
`trading/app/paper_service.py`, `trading/adapters/http/dashboard.py`,
`config/paper.example.toml`, new `tests/test_runtime_risk.py`, README,
ARCHITECTURE, SAFETY and this audit. Existing SQLite audit/order persistence
stores peaks and cooldown history; no duplicate state store was introduced.

Verification: focused policy/config/deployment tests passed. Full suite:
2,168 run, 2,164 passed, 4 environment-dependent socket skips. Compilation and
whitespace checks passed. Tests cover boundaries, missing equity, de-risking,
reversal refusal, pending capacity, cooldown restart, stale/missing volatility,
direct gateway refusal and invalid configuration.

Limits: wall-clock jumps forward can expire cooldown; clock synchronization
remains necessary. Candle range measures one bar, not rolling volatility or
cross-bar jumps; ingestion gap controls remain independent. These policies
check new submissions; accepted orders retain earlier approval and lifecycle
rules. Paused/emergency bots still prevent resting paper fills. There is no
automatic forced liquidation or permanent drawdown latch.

Exact next milestone: M4 development-only strategy comparison and explicit
approval evidence, with benchmarks, turnover, uncertainty and regime reports.
Sealed confirmation and Segment 7 remain protected.

## Runtime accounting milestone update — 2026-11-11

This update supersedes the runtime/persistence gaps in the original audit below.

Implemented: configurable integer paper fees (`--fee-bps`, 0–9999), conservative
cash rounding, net realized P&L and daily-loss accounting, persistent starting
capital and cumulative fees, cash checkpoints, normalized fill records, closure
audit events, per-cycle equity snapshots with persistent peak/drawdown, gross
exposure, and bot state in dashboard snapshots. Missing/stale marks produce
unknown equity rather than a fabricated valuation.

Existing durable audit records remain the authority for signals, risk decisions,
orders and controls. ML inference now records each available prediction,
including predictions producing no trade, with model hash, features, timestamp
and probability. No research artifact was loaded or altered.

New SQLite simulated venue inventory is independent of local portfolio state.
It preserves cumulative acknowledgements, order intents, positions and venue ID
sequence. Restart verifies inventory against saved fills, runs the existing
order/reservation recovery and reconciliation, and restores portfolio/risk
state. Legacy databases lacking venue inventory still block on open-position
mismatch; the audited legacy reset remains available. Invalid cash checkpoints
or inconsistent venue snapshots fail startup.

Bot controls expose running, paused, stopped and emergency_stopped states.
Transitions require an operator and a reason and are serialized against service
cycles and gateway submission. Paused/stopped bots cannot submit orders or fill
resting orders. Application shutdown persists stopped; emergency shutdown keeps
the emergency state. A crash from running recovers paused. Kill-switch state is
replayed from the durable audit. Emergency recovery requires explicit release,
transition to paused, and then resume. Unexpected cycle failures latch the switch.

Local Python controls: `app.pause(reason=...)`, `app.resume(reason=...)`,
`app.emergency_stop(reason=...)`; stop uses `app.stop()`. Recovered continuous
applications require explicit `--resume-paper`. HTTP remains read-only.

Example (synthetic stream, virtual funds):

```bash
python3 -m trading.app --dashboard --state-db ./paper-state.sqlite3 --fee-bps 10
# On a subsequent launch, after reviewing recovered state:
python3 -m trading.app --dashboard --state-db ./paper-state.sqlite3 --fee-bps 10 --resume-paper
```

Fee 10 means 0.10% per fill; it is an explicit simulation assumption, not a
verified CoinSwitch account fee. Fee zero is supported and must not be mistaken
for a realistic exchange-cost assumption. Existing spread/slippage still apply.
Realized P&L is net of fees charged when incurred, including opening fees;
unrealized P&L remains mark versus price basis, so fees are never counted twice.

Files changed for this milestone: `core/portfolio.py`, `core/gateway.py`,
`adapters/persistence/sqlite.py`, new `adapters/persistence/paper_broker.py`,
`adapters/paper/broker.py`, `adapters/http/dashboard.py`, `app/composition.py`,
new `app/paper_state.py`, `app/paper_service.py`, `app/model_strategy.py`,
`app/runtime.py`, `app/supervisor.py`, `app/__main__.py`, `app/restart_drill.py`,
new `tests/test_paper_runtime_state.py`, `tests/test_app_composition.py`,
`tests/test_paper_recovery.py`, this audit and README.

Verification: focused runtime/recovery tests plus the full suite: 2,161 run,
2,157 passed, 4 environment-dependent loopback skips. Tests cover fee round
trips, invalid rates, restored open inventory, no duplicate placement on restart,
paused gateway/service refusal, persistent equity, stopped/emergency recovery,
cash corruption refusal, stale marks and legacy recovery. Existing suites cover
partial fills, risk refusals, transactions, lifecycle and idempotency.

Remaining limitations: single-process SQLite ownership; no authenticated remote
control UI; simulated liquidity/latency assumptions remain; legacy databases
cannot reconstruct missing venue inventory. Fee totals lack historical backfill
for pre-fee records. Equity history is cycle-sampled, not tick-sampled. Existing
in-memory mirrors can diverge after failed SQLite writes; cycle failure latches
the kill switch and requires inspection/rebuild rather than retrying execution.

Exact next milestone: M3 risk policy — persist and enforce maximum drawdown,
simultaneous-position limits, cooldown and abnormal-volatility rules with
de-risking exceptions and restart/failure tests. Do not proceed to model
promotion or live transport as part of that milestone.

Status: the repository is a strong, safety-first automated **paper-trading and
research application**. It is not a production live-trading system, and no
existing model or strategy has evidence supporting a profitability claim.

This audit distinguishes source code, tests, operational evidence, and future
work. A module, command, or test name alone is not treated as proof that a
capability works end to end.

## 1. Current architecture

The application is a modular monolith with dependency boundaries enforced by
tests:

- `trading.core`: pure safety and accounting kernel. It contains exact decimal
  money types, modes, configuration, authorization, risk limits, order state,
  portfolio accounting, idempotency, reconciliation, circuit breakers, the
  kill switch, and the execution gateway. It has no venue transport.
- `trading.ports`: abstract broker, market-data, repository, recovery, and
  reservation contracts.
- `trading.strategy` and `trading.advisory`: causal signal generation, moving
  average crossover, indicators, sizing, and non-executing advice.
- `trading.adapters`: in-memory test doubles, the in-process paper broker,
  CoinSwitch public market data, authenticated GET-only historical acquisition,
  SQLite persistence, recovery/lifecycle adapters, and the loopback HTTP
  dashboard.
- `trading.app`: composition, supervised paper service, CLI/runtime, historical
  validation and recovery, segmented datasets, ML research/training/inference,
  backtesting, soak/restart/release drills, and reports.

The execution boundary is deliberate: strategies create inert intents; only the
gateway can execute them. Paper mode uses real public CoinSwitch market data
when configured, but orders terminate at `PaperBroker`. There is no authenticated
CoinSwitch live-order transport in the runnable application.

## 2. Implemented versus missing

| Area | Implemented and verified | Remaining or not verified |
|---|---|---|
| Market data | CoinSwitch public spot stream, reconnect/resubscribe, quote/candle normalization, stale/missing detection, candle integrity, GET-only historical downloader, resume/recovery evidence | Historical endpoint has material unavailable ranges; long-duration completeness cannot be assumed. Production alert delivery is absent. |
| Automated paper engine | Continuous supervised cycles, MA crossover and gated ML trial strategy, sizing, gateway risk checks, paper fills, lifecycle sync, idempotency, graceful shutdown | Paper fees are not charged by the runtime broker. Pause/resume is not an operator control. Broader multi-strategy orchestration is absent. |
| Portfolio and orders | Exact accounting, realized P&L, positions, lifecycle, partial fills, UNKNOWN handling, reconciliation | Durable equity curve, signal history, prediction history, and a normalized trade ledger are not separate persisted entities. |
| Risk | Order/position/gross exposure, daily loss, rate/open-order limits, stale marks, authorization, kill switch, reconciliation gate, circuit breakers | No integrated maximum-drawdown gate, maximum-position-count policy, trade cooldown, or abnormal-volatility gate. Emergency liquidation policy is intentionally absent. |
| Persistence and recovery | Transactional SQLite orders, reservations, positions, portfolio, audit, risk state; startup recovery and paper reset workflow | Schema migration tooling, user-facing backup/restore commands, and durable dashboard equity/drawdown history are missing. Live venue reconciliation cannot be verified without a live adapter. |
| Backtesting | Deterministic MA crossover, next-bar execution, fees/spread/slippage, equity/P&L/drawdown/trades, gap segmentation, reproducible hashes | No general strategy registry, Sortino/regime attribution, or approval workflow. Existing development result is negative and is not profitability evidence. |
| Dashboard | Loopback-only read-only UI/API, health, feed, metadata, mode/strategy, readiness, alerts, orders; now also marked equity, session drawdown, P&L, positions, filled history, risk/breakers, and ML paper status | No authenticated mutation API, pause/start controls, persisted charts, backtest browser, dataset-health page, or durable last-training/model registry view. Session peak resets on dashboard restart. |
| Packaging and operations | Python 3.11+ wheel, `trading-paper` entrypoint, strict TOML paper profile, signals, soak/restart/release drills | No macOS service/launcher, scheduled collection/research supervisor, external notification integration, database maintenance command, or release version beyond `0.1.0`. |
| Live execution | Mode gates, two-factor configuration guard, broker contracts, order translation/reconciliation primitives | **No live-order adapter and no operational verification. Live trading is not supported.** No account/balance API integration, venue fee reconciliation, sandbox qualification, or live incident runbook exists. |

## 3. Existing ML capabilities and gaps

Implemented:

- Genuine CoinSwitch OHLCV acquisition, strict validation, deterministic hashes,
  gap classification, and gap-safe segmented datasets.
- Causal v1 and versioned v2 feature generation. V2 supports 18 features and
  5-, 15-, and 30-minute horizons with non-overlapping sampling.
- Chronological splits, deterministic walk-forward folds, training-only
  preprocessing, logistic candidates, baselines, broad classification metrics,
  Brier score, and moving-block bootstrap evidence.
- Frozen model artifacts with provenance, configuration, feature schema,
  library version, and hashes.
- Explicit selection/confirmation safeguards and preservation of rejected
  models. The original BTC/USDT model was honestly rejected near random.
- Exact-hash-gated, paper-only inference integrated with the normal risk and
  execution gateway. An ML signal cannot bypass hard risk controls.

Not complete:

- There is currently no approved model with credible, independently confirmed
  predictive value. The correct operational status is `NO APPROVED ML MODEL`.
- No persistent model registry supporting approve/reject/archive/rollback as
  explicit state transitions.
- No production prediction/outcome store, drift monitor, calibration monitor,
  regime monitor, or post-deployment performance dashboard.
- No controlled scheduled retraining runner with resource limits. Research is
  command-driven, and promotion remains manual by design.
- No evidence that an ML-assisted strategy has positive net returns after costs.
  Classification metrics alone cannot establish that.

Protected evidence remains protected: rejected artifacts must not be rewritten,
and sealed Segment 7 must not be accessed without separate explicit authority.

## 4. Existing trading capabilities and gaps

The application can run an automated paper strategy against a synthetic stream
or genuine CoinSwitch public data. It evaluates only closed bars, sizes through
the existing risk system, sends orders through the gateway, simulates venue
fills, persists state, reconciles lifecycle state, and recovers after a clean
restart. It fails closed for stale data, reconciliation mismatch, UNKNOWN
orders, engaged kill switch, open breakers, and exceeded risk limits.

The current baseline is Moving Average Crossover. The historical development
backtest with realistic costs was negative. ML paper inference is available only
for an explicitly approved model/evaluation hash pair; it is not evidence of a
usable model today.

The application cannot place real orders. A future live adapter would still
need authenticated order/balance/cancel/status APIs, account permissions,
precision and minimum enforcement from current venue metadata, fee handling,
restart reconciliation, rate-limit behavior, outage tests, and an independently
verified sandbox or other safe qualification environment.

## 5. Current launch procedure

Requirements: macOS or another Python 3.11+ environment. Core paper operation
has no runtime third-party dependency. Authenticated historical acquisition uses
the optional `historical-data` dependency and credentials outside source control.

Install in a virtual environment:

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -e .
```

Start the durable local synthetic paper application:

```bash
.venv/bin/trading-paper --config config/paper.example.toml \
  --dashboard --state-db ./paper-state.sqlite3
```

Open `http://127.0.0.1:8765/`. Stop with `Ctrl-C`; shutdown is ordered and
graceful. This address is loopback-only. It is not directly reachable from a
phone. Binding publicly is intentionally refused; secure remote/mobile access
requires a separately reviewed authenticated tunnel or reverse proxy while the
Mac and application remain running.

Start CoinSwitch-public-data paper mode only after supplying current validated
market constraints as documented in `README.md`:

```bash
.venv/bin/trading-paper --coinswitch-symbol BTC/USDT \
  --coinswitch-exchange c2c1 --dashboard \
  --state-db ./coinswitch-paper.sqlite3
```

No form of either command sends exchange orders.

## 6. Critical safety issues

1. Live execution must continue to be described as unsupported, not merely
   disabled. The repository contains live gates but no live transport.
2. Paper fills remain an optimistic approximation: there is no queue position,
   real liquidity depletion, or runtime fee debit. Research backtests do model
   configurable costs, but paper accounting and backtest accounting differ.
3. A rebuilt in-memory paper venue cannot truthfully reproduce its pre-crash
   inventory. Open-position restart therefore blocks until the explicit audited
   paper-only recovery procedure is used.
4. Dashboard drawdown added in this milestone is session-observability state,
   not a persisted risk gate. It must not be presented as durable risk history.
5. CoinSwitch historical responses have proven gaps. Missing candles must never
   be interpolated or silently crossed by features, labels, or backtests.
6. No current ML model or strategy has demonstrated a credible net edge. The
   application is operationally useful for paper research, not proven income.
7. Adding dashboard controls requires authentication, CSRF protection, explicit
   roles, audit records, and a separate execution-safe command surface. The
   existing observational server should not casually gain mutation authority.

## 7. Prioritized milestone plan

### M1 — Paper observability (implemented in this milestone)

Expose marked equity, realized/unrealized P&L, session peak/drawdown, filled
order history, risk limits, kill-switch/breaker state, and ML paper status in
the existing read-only dashboard. Preserve loopback binding and mutation refusal.

### M2 — Runtime accounting and controlled operation

Add conservative configurable paper fees; persist normalized signals,
predictions, fills/trades, and an equity series; implement pause/resume as an
audited application state that cannot bypass shutdown, risk, or reconciliation.
Keep the HTTP adapter read-only until a separately authenticated control design
is approved.

### M3 — Complete risk policy

Add fail-closed drawdown, simultaneous-position, cooldown, and abnormal-
volatility policies. Persist required state, define de-risking exceptions, and
exercise every policy before the gateway in restart and failure tests.

### M4 — Strategy research and approval

Generalize the backtest runner to registered strategies; add benchmark,
turnover, Sortino/regime attribution, uncertainty, and an immutable strategy
approval manifest. Research additional strategies only on development folds.
Keep final confirmation data sealed until one preregistered candidate qualifies.

### M5 — Controlled ML lifecycle

Add a model registry with immutable artifact identities and explicit transitions
for candidate, approved-for-paper, rejected, archived, and rolled-back. Persist
prediction context/outcomes, compute drift and calibration reports, and add a
resource-bounded research scheduler. Never self-promote.

### M6 — Operations and release completion

Add SQLite migrations, online-safe backup plus verified restore, log rotation,
optional local alerts, dataset/model health pages, macOS launch instructions,
troubleshooting, and a complete release/end-to-end drill.

### M7 — Disabled live architecture qualification

Only after current CoinSwitch trading API documentation and a safe sandbox or
equivalent are independently verified: implement a separate authenticated live
adapter behind the existing port, using mocks/contract fixtures first. Keep
live builds/config/state isolated and disabled. Real orders require a separate
security and operational review and explicit user authorization.

## 8. Files likely to need modification

The exact changes should remain milestone-scoped:

- M1: `trading/adapters/http/dashboard.py`, `tests/test_app_dashboard.py`,
  `README.md`, `docs/ARCHITECTURE.md`.
- M2: `trading/adapters/paper/broker.py`, `trading/core/portfolio.py`,
  `trading/adapters/persistence/`, `trading/app/paper_service.py`,
  `trading/app/supervisor.py`, `trading/app/__main__.py`, and focused tests.
- M3: `trading/core/config.py`, `trading/core/risk.py`, `trading/core/gateway.py`,
  SQLite risk persistence, configuration examples, and risk/recovery tests.
- M4: `trading/app/backtest.py`, `trading/app/backtest_cli.py`, strategy modules,
  research manifests, and backtest/strategy tests.
- M5: `trading/app/ml_research.py`, `trading/app/model_training.py`,
  `trading/app/model_strategy.py`, new persistence-backed registry/monitor
  modules, CLI wiring, and ML security/provenance tests.
- M6: SQLite schema/migration code, new backup CLI, process/release scripts,
  `pyproject.toml`, `README.md`, `docs/ARCHITECTURE.md`, and `docs/SAFETY.md`.
- M7: a new adapter package and contract fixtures only after venue verification;
  core safety behavior should not be rewritten.

## 9. Test strategy

Every milestone should use this order:

1. Pure unit tests for new state and invariants, with deterministic clocks and
   exact Decimal values.
2. Adapter contract tests for malformed, partial, duplicate, stale, uncertain,
   and interrupted behavior.
3. Persistence/restart tests, including transaction rollback and schema upgrade.
4. Integration tests proving strategy → sizing → risk → gateway → paper fill →
   persistence → dashboard, plus rejection paths.
5. ML/backtest leakage, gap, chronology, provenance, hash, and sealed-data tests.
6. Security tests proving default paper mode, credential redaction, loopback
   binding, mutation refusal, rejected-model refusal, and no accidental live path.
7. Full suite with the package-aware command `python3 -m unittest`.
8. Release/restart/soak drill where relevant. Network-dependent checks must be
   reported separately from deterministic tests.

Current milestone verification:

- Focused dashboard/runtime: 20 passed, 4 environment-dependent loopback tests
  skipped.
- Full package-aware suite: 2,153 passed, 4 skipped.
- `python3 -m unittest discover -s tests` is not the supported invocation: it
  imports files outside the `tests` package and causes 12 relative-import
  collection errors. This is a runner configuration issue, not a hidden pass.

## 10. Realistic definition of final completion

Software completion for this project means:

- a documented, reproducible macOS install and one-command paper launch;
- genuine public market data, automated paper execution, conservative runtime
  costs, persistent state/history, audited pause/stop, recovery, and complete
  risk policy;
- a responsive, accurate local dashboard and secure instructions for optional
  remote access;
- reproducible gap-safe strategy/ML research, immutable evidence, explicit
  approval/rejection, paper inference, monitoring, and rollback;
- migrations, backup/restore, operational alerts, end-to-end/release drills,
  and a passing deterministic test suite;
- a live adapter boundary that remains absent or disabled until it is safely
  implementable and independently qualified.

It does **not** mean guaranteed returns, a profitable model, or authorization to
trade real funds. Profitability requires new out-of-sample evidence after costs
and then cautious forward paper evidence; live readiness requires a separate
venue, security, reconciliation, and operational review.
