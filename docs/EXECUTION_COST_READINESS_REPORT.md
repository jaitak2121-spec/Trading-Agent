# Execution-cost readiness — 0.2.3

Follow-up: [0.2.4 public depth diagnostic](PUBLIC_DEPTH_COST_REPORT.md) adds
visible-liquidity sweeps. Its two-minute combined feed observation **failed**
because no closed candle arrived; outages and reconnect evidence were retained.
No profitability or live qualification follows.

Date: 2026-11-12, user local date. This milestone improves the evidence used for
profitability research and future execution qualification. **It does not make
any strategy profitable or enable actual exchange orders.**

## Completed

- Added deterministic cost attribution over the existing frozen native-five-minute
  research report. Every trade and fold is checked against recorded P&L and fees.
  Inputs and outputs are hash-bound; existing results are not rewritten.
- Added a public quote sink to the existing finite CoinSwitch observer. It is
  optional and does not change existing collectors or production strategy logic.
  The capture checkpoints quotes every five seconds, stops the stream on callback
  failure, and never starts a broker. A hard kill may lose the last checkpoint
  fragment; existing capture files are not overwritten or automatically resumed.
- Added exact-decimal spread summaries with market, timestamp, crossed-quote,
  freshness and duplicate checks. No market-impact/slippage estimate is invented.
- Added an explicit, single-endpoint account-fee reader and strict snapshot parser.
  It reuses the existing reviewed HTTPS/signing boundary, accepts only a fixed
  GET fee endpoint and supported exchange identifiers, sends no balances/orders,
  follows no redirects and does not retry failed requests. Missing/invalid fees
  never default to zero. The same parser now validates the mocked native execution
  contract's funding fee reserve, including negative/malformed-rate refusal.
- Native research and cost-diagnostic reports now work with `--research-report`
  in the existing dashboard. Their hashes are verified and approval stays **None**.
  Displaying research never deploys a strategy or changes risk controls.

## Actual cost diagnosis

The original seven-fold experiment still rejects every candidate. The table
below sums seven independently funded validation periods, **not one compounded
account**. All amounts are USDT from the base-cost frozen executions.

| Strategy | Net P&L | Recorded fees | P&L before fees at the same fills |
|---|---:|---:|---:|
| MA crossover | -1,578.17 | 1,175.60 | -402.57 |
| Momentum | -1,313.39 | 1,005.69 | -307.70 |
| Mean reversion | -731.61 | 594.44 | -137.17 |
| Volatility-filtered momentum | -1,294.80 | 1,003.71 | -291.09 |
| Breakout | -151.97 | 61.91 | -90.06 |
| Trend pullback | -82.47 | 41.96 | -40.51 |

**Fee reduction alone cannot turn these recorded fill sequences profitable.**
This is an accounting conclusion, not a zero-cost counterfactual simulation:
pre-fee P&L still contains the original assumed spread and slippage. Different
costs could change sizing, risk refusals, fills and subsequent trades in a full
replay. We did not tune costs after observing losses to manufacture a winner.

The diagnostic also reports notional turnover, normalized net-edge deficit,
observed winning/losing payoffs and break-even flat fee where mathematically
possible. A payoff-based break-even win rate is a descriptive two-outcome
calculation using nonzero winning/losing payoffs, not a forecast or guarantee.
No candidate approval, model fitting or promotion occurs in this tool.

Cost report: `artifacts/native-5m-frozen-fill-cost-diagnostics.json`.
SHA-256: `ee8ce219f375df5710b627ca70bd11a327255ae49b38a897290da878e8dd9c7a`.
Original research report SHA-256 remains
`07cbc2a8e501c0001020d61691c7464b37a9fdf941888bc3f4718054d17d6ec8`.

## Genuine public spread capture

Executed a credential-free `c2c1` BTC/USDT observation for **120.0048/120 seconds**:

- 216 quote messages, 2 closed candle messages, 119 polls.
- Zero feed-error polls, zero reconnects; observation passed.
- 209 fresh unique quote samples; 7 exact duplicates excluded, zero stale samples.
- Observed receive-time span: 117,590 ms after stream startup.
- Median full quoted spread: **0.0013553442 basis points**.
- 95th-percentile full spread: **0.0013554157 basis points**.

These are event-weighted top-of-book statistics over a short current window.
Receive timestamps include observer polling; they are not wire-latency estimates.
No order-book depth, queue priority, fill price or market impact was measured.
The apparent narrow spread does not demonstrate executable profitability and
cannot replace unknown 2025 historical spreads. The old 2bp spread assumption
was not silently changed, and past experiments were not requalified.

Raw quote evidence and observation health:
`artifacts/public-spread-capture-2026-11-12.json`.
SHA-256: `f22d22942ad33e9d6c2194819ab6759eca09ba925cce55388fcd619a7aacd252`.
The summary was independently recomputed from the preserved samples.

## Account fees: authorized read completed

[CoinSwitch's fee reference](https://api-trading.coinswitch.co/spot/reference/trading-fee)
defines account-specific maker/taker rates, effective discounted rates and a
per-exchange GET endpoint. Decimal rates are converted to basis points exactly.
Capture time and schedule-update time are retained; a current schedule is not
proof of historical fees or future tier/discount stability.

At the initial 0.2.3 handoff the authorization request was pending; only mocked
fee tests had run. After the user's explicit approval, **one authenticated
`GET /trade/api/v2/tradingFee?exchange=c2c1` returned HTTP 200**, with no retries,
balance requests or orders. Capture time: **2026-11-12 10:51:44 IST**
(`2026-11-12T05:21:44.649Z`). Current effective BTC fees were **10bp maker and
10bp taker (0.10% each)**. Both credentials were resolved from the exact authorized
macOS Keychain services without displaying or persisting their values.

The verified schedule matches the experiment's **base 10bp per-side assumption**.
For all six strategies, applying the current rate to the same recorded base-case
notional reproduces the recorded fee totals within 0.00000001 USDT. This is a
fixed-fill comparison, not a new backtest: it neither verifies 2025 historical
fees nor changes sizing, executions, risk outcomes or candidate acceptance.
The 20bp stress case remains a deliberately higher cost assumption.

Private evidence, both mode 0600 and Git-ignored:

- `artifacts/private-fees/c2c1-BTC-2026-11-12.json` — captured BTC schedule,
  response hash, capture time, HTTP status and snapshot hash.
- `artifacts/private-fees/c2c1-BTC-2026-11-12-comparison.json` — independent
  snapshot verification and all six fixed-notional fee comparisons.

The initial `execution-cost-readiness-0.2.3-evidence.json` still records the
earlier pending state and was not rewritten. No credential values, headers or
complete private response bodies were saved in the new evidence.

The `capture-fees` CLI requires an explicit read-authorization flag and credentials
supplied only to its process. Credentials are not discovered automatically, shown
in terminal arguments, written to a profile or requested in chat. Use the existing
macOS Keychain services with a process-local secure launcher. Fee captures are
mode 0600, refuse overwrite, and the recommended `artifacts/private-fees/` directory
is ignored by Git. Only selected asset fee fields and response hashes are saved;
API keys, signatures, private keys and full private response bodies are not saved.

The successful fee read does not verify trading permission, balances, sandbox provisioning,
order lifecycle or profitability. The CSX paper host's prior public health 403,
different API surface and unverified account access remain documented in
`COINSWITCH_ACTIVATION_CHECKLIST.md`. No production order transport was added.

## Commands

Commands actually executed:

```sh
.venv/bin/python -m trading.app.execution_costs analyze \
  artifacts/native-5m-development-results.json \
  artifacts/native-5m-frozen-fill-cost-diagnostics.json \
  --sha256 07cbc2a8e501c0001020d61691c7464b37a9fdf941888bc3f4718054d17d6ec8
.venv/bin/python -m trading.app.execution_costs observe \
  artifacts/public-spread-capture-2026-11-12.json --seconds 120
```

Use a new filename for another run; completed evidence is never overwritten.
The explicit fee-read command was executed once after scoped authorization
(the actual snapshot filename includes `2026-11-12`):

```sh
.venv/bin/python -m trading.app.execution_costs capture-fees \
  artifacts/private-fees/c2c1-BTC.json --exchange c2c1 --base-asset BTC \
  --authorize-read-only-fee-lookup
```

That command was subsequently executed once through a secure process-local
Keychain launcher after authorization. It refuses missing process credentials
and does not reuse
a running paper application's environment or imply live-order authorization.

To inspect the diagnostics on a separate paused demo account:

```sh
.venv/bin/trading-paper-account create --state-db ./cost-review.sqlite3 --cash 10000
.venv/bin/trading-paper --config config/paper.example.toml --dashboard \
  --state-db ./cost-review.sqlite3 --fee-bps 10 \
  --research-report artifacts/native-5m-frozen-fill-cost-diagnostics.json
```

The first command refuses an existing account. The account stays paused; this
demo uses labeled synthetic prices and is not forward-market performance evidence.

## Verification and next step

- Focused cost/fee/observer/historical/native-contract checks: **46 passed**.
- Full suite: **2,279 passed, zero skips** (15 new tests versus 2,264).
- Installed-wheel account/dashboard/interruption/recovery/resume/restart drill passed.
- Final wheel: `dist/cost-ready-0.2.3/safety_first_trading_app-0.2.3-py3-none-any.whl`.
  SHA-256: `47d6bc358f035b6e1ddd69c41795f17f59f6147e5602560f812e058a5eabe5c5`.
  Evidence: `artifacts/release-0.2.3-cost-ready-verification.json`.
  Earlier release artifacts and intermediate builds were preserved.
- The exact final wheel is installed in `.venv`; isolated import/version and
  dependency checks passed. Compilation, launcher syntax and whitespace checks
  passed. Consolidated evidence: `artifacts/execution-cost-readiness-0.2.3-evidence.json`.
- All HTTP/header exception text is sanitized at the fee-reader boundary;
  regression tests verify that client errors cannot echo credential-like data.
- Cost results and raw-quote summaries reproduced independently. Research inputs,
  protected observations, rejected models, production strategies and risk limits
  were preserved. No new model or candidate was trained, selected or promoted.

Main files: added `trading/app/execution_costs.py`,
`trading/adapters/coinswitch/trading_fees.py`, `tests/test_execution_costs.py`,
`tests/test_trading_fees.py`; updated the observer's optional quote callback,
shared GET signing helper, native funding fee parser, dashboard report loader,
CLI help, release checks, version and documentation.

Next: collect longer public depth/spread observations to design prospective
execution assumptions. Any new strategy hypothesis must be separately frozen
and tested on eligible development data. Do not select thresholds from these
losses or declare a winner by removing costs from frozen fills. Real execution
still requires the external test-account and qualification steps already recorded.
