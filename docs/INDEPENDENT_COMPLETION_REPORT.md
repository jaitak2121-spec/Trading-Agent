# Independently achievable completion work — 0.2.2

Date: 2026-11-12. Scope: the nine requested completion areas, with actual work
beyond support-request preparation. **No profitability or live-readiness claim.**

| Requested area | Work completed and verified | Remaining boundary |
|---|---|---|
| Alternative candle source | Queried Binance's documented public Spot kline endpoint for the four exact May 7 timestamps. Three HTTP 200 requests returned four genuine observations; independently checked times, OHLCV and raw-response hashes. | Separate exchange; authoritative equivalence to CoinSwitch `c2c1` is unverified. No records were merged into CoinSwitch. |
| Integrity-aware research | New whole-range eligibility evidence recomputes expected coverage, ordering, duplicates, OHLCV/provenance and stored hashes; evidence itself is hashed. Backtest reports carry qualification eligibility; incomplete diagnostic inputs cannot qualify cost-research candidates. | Passing flags are trusted only with matching provenance and the relevant research protocol. Integrity does not prove an edge. |
| Eligible development research | Acquired 180 days of genuine native five-minute CoinSwitch data, preserved its three-candle gap as FAIL, derived two separately complete datasets, froze six candidates and seven non-overlapping walk-forward folds, evaluated base/stressed costs and baselines with selection-adjusted intervals. | All six lost money and failed acceptance. None selected. No confirmation opened or parameters tuned after results. |
| ML lifecycle | Propagated source-integrity evidence through newly generated V2 causal/development artifacts into future frozen models. Final fitting and paper approval reject missing/failed/mismatched provenance. Existing retraining workers, evaluation, serialization, registry approval/revocation/rollback and inference are reused. | No new eligible one-minute ML experiment was fitted. Native five-minute observations cannot silently replace the one-minute feature schema. Existing rejected models remain unchanged. |
| Live adapter interface | Implemented an abstract native Spot exchange port, immutable request/response shapes, disabled default, exact decimal JSON/signing-message bytes and a CoinSwitch contract broker over existing gateway machinery. | No HTTP order client, credential/signer integration, host selection or production wiring. Not venue-qualified. |
| Lifecycle/risk/recovery | Mocked gateway tests cover native LIMIT placement, risk/pause refusals, funding+fee reserves, stable client IDs, partial/delta fills, cancel-pending/terminal races, UNKNOWN/no resend, SQLite restart recovery, whole-wallet mismatch and incomplete inventory refusal. | Account-wide balances cannot prove per-strategy positions. Test account allocation and actual fill-fee reconciliation require exchange access. |
| External requirements | Found an official CSX paper endpoint and probed its unauthenticated health route; got 403. Documented its different API/authentication and narrowed the real access/permission blockers. | Account provisioning, IP authorization, API-surface choice, scope and compatible test access remain unverified. |
| Safe default | Existing PAPER composition is unchanged; new contract is not selected by any launcher and its default port refuses calls. No real or exchange-paper orders occurred. | Live activation remains a separate future implementation/qualification/user action. |
| Verification and handoff | Full suite, installed-wheel restart/dashboard drills, independent dataset/trade-ledger checks and completion documentation completed. | Future strategy research and authenticated venue qualification cannot be replaced by software test counts. |

## Data evidence

Binance observations are explicitly **reference-only**, not a training dataset
or a replacement for CoinSwitch. They use a different schema so the CoinSwitch
historical loader cannot accidentally accept them. The current official
[exchange identifier reference](https://api-trading.coinswitch.co/spot/concepts/exchange-identifiers)
does not establish a Binance mapping. Separate CSX conversion examples naming
Binance do not establish identical `c2c1` candle provenance or aggregation.

- `artifacts/may-7-alternative-source-investigation.json` — public request ranges,
  statuses, raw responses and hashes; SHA-256
  `79a8bee70efd922506dd63fedf70cdebaaf012490d9c00386480774b3cff1216`.
- `artifacts/may-7-binance-independent-observations.json` — four normalized,
  individually validated observations with native close-time semantics retained;
  SHA-256 `976e8898f4adf74debd039c69b2a9663a22a31c4af0f775e5ab2fd16a7026845`.
- The original CoinSwitch one-minute dataset still has its four omissions.
  Its original file hash and failed integrity status were preserved.

Native CoinSwitch five-minute acquisition contains **51,837/51,840** candles,
one gap, no duplicates. The two explicitly narrowed complete development runs
contain **28,952** and **22,885** candles. Their parent remains failed and
unchanged. No feature/label/window crosses the gap. See
`NATIVE_FIVE_MINUTE_RESEARCH_REPORT.md` for boundaries, hashes, costs, fold-level
artifact locations and limitations.

## Honest research outcome

Six existing strategies were evaluated on seven chronological validation folds
under two cost scenarios: **84** independent ledgers. Base mean fold returns
ranged from **-0.1178% to -2.2545%**; stress returns from **-0.2017% to -4.5654%**.
All had zero positive folds. All computed trade-P&L intervals have negative upper
bounds; the 21-trade pullback interval is withheld. No candidate qualifies.

Baseline comparisons, full fold metrics and trades are preserved in
`artifacts/native-5m-development-results.json`; independent cash/fee/boundary
checks are in `artifacts/native-5m-development-verification.json`.
No new supervised model was fitted, no approved strategy was created and no
confirmation or protected Segment 7 observation was opened.

The final integration review also fixed a legacy cadence mismatch: V1 training
previously accepted arbitrary uniform intervals while its paper strategy uses
one-minute bars. Training now explicitly refuses other cadences, newly trained
models record `interval_seconds: 60`, and paper approval refuses missing/wrong
cadence. Frozen artifacts remain byte-for-byte untouched; they are not upgraded
by silently inserting metadata. This is a compatibility/safety fix, not a new
model or a change to trading signals.

New integrity gates do not mutate prior artifacts or retroactively rewrite old
preregistrations/results. Old research can remain diagnostic evidence, but future
final V2 fitting/deployment requires the new bound source-integrity evidence.
Do not retrofit an old model to bypass this requirement.

## What is actually implemented for execution

The new CoinSwitch contract reuses `SandboxBroker`, the execution token boundary,
durable local orders and gateway cumulative-fill/UNKNOWN resolution. It adds
native request mapping and strict venue-response identity checks, not another
order ledger. No production endpoint can be reached through the default port.

The tests caught a genuine cancel-pending issue: a successful DELETE may be
followed by a still-working partial fill. The native broker now treats DELETE
as a request, requires the gateway's subsequent authoritative GET, and never
retires the remaining quantity merely because cancellation was acknowledged.

Whole-account available/blocked balances can be parsed and reconciled against
an explicit account ledger. They are intentionally not fabricated into strategy
positions. Position allocation and actual exchange fee settlement remain explicit
qualification requirements. This is a tested execution architecture, not a
finished authenticated trading service.

The official [CSX onboarding page](https://api-csx.coinswitch.co/) links a paper
endpoint. Its public health route returned 403 without credentials. This corrects
the earlier overly broad documentation statement; it does not verify usable
Spot v2-compatible access. `COINSWITCH_ACTIVATION_CHECKLIST.md` records API
differences, permissions, isolated credentials/accounts/databases and exact tests
required before any future activation. No support message was sent.

## Tests and release

- Full suite: **2,264 passed, zero skips**, Python 3.11.15 with localhost/native
  worker support. There are **26 additional regression tests** versus the prior
  2,238-test audit.
- Actual test failures were retained in the work record: initial new mocks used
  the wrong gateway call signature; these were corrected. Pending cancellation
  exposed the native acknowledgement problem and was fixed. A disallowed URL-
  utility import was removed, preserving the architecture rule. The initial
  preregistration tuple/list mismatch aborted before evaluating any candidate;
  a corrected distinct plan and serialization regression test were added.
- Final corrected installed-wheel drill passed custom paused-account creation,
  saved capital, CLI help, HTTP health, abrupt interruption/inventory recovery,
  explicit resume, clean shutdown and persistent restart using temporary
  synthetic accounts. No exchange order API was called.
- Final wheel: `dist/final-0.2.2/safety_first_trading_app-0.2.2-py3-none-any.whl`.
  SHA-256: `ad711ab5cd23a0b0666db46d83ee7b52a003bb6afa2df93cb4e0ac0d11bdaa2b`.
- Final installed-wheel evidence:
  `artifacts/release-0.2.2-installed-verification-v3.json`.
  Earlier build/evidence, original datasets and older releases were preserved.
- The exact final wheel was installed offline into `.venv`; `pip check` passed.
  Isolated Python verified the installed version and refusal of missing-integrity
  qualification. Compilation, launcher syntax and whitespace checks passed.
  Consolidated hashes and status: `artifacts/completion-0.2.2-evidence.json`.

## Main files changed

Added: `trading/adapters/coinswitch/execution_contract.py`,
`trading/app/dataset_eligibility.py`, `trading/app/complete_runs.py`,
`trading/app/native_research.py`, `tests/test_execution_contract.py`,
`tests/test_dataset_eligibility.py`, `tests/test_complete_runs.py`, the new
research plan, data/evidence artifacts and this milestone's documentation.

Updated: `trading/app/backtest.py`, `trading/app/cost_research.py`,
`trading/app/ml_research.py`, `trading/app/model_training.py`, `trading/app/model_deployment.py`,
`trading/adapters/coinswitch/__init__.py`, `trading/app/release.py`,
`tests/test_ml_research.py`, `tests/test_v2_paper_strategy.py`,
`tests/test_model_training.py`, `tests/test_model_registry.py`,
`tests/test_app_release.py`, `pyproject.toml`, completion/operating/readiness docs.
No production strategy selection, risk limit or actual live execution was changed.

## Next boundaries

All preregistered work in the new development experiment is finished and rejected;
there is no winner to confirm. Another research cycle needs a new economic
hypothesis and frozen protocol, not repeated tuning on these losses. New ML
retraining requires eligible schema-compatible genuine data and a defined
experiment; one-minute features must not be invented from five-minute aggregates.

The live work that remains depends on verified API-surface/test-account access,
permissions, allocation/fee semantics and independent venue qualification. Do
not send existing keys to CSX or substitute production funds for a test account.
The prepared checklist is actionable; authenticated execution remains absent.
