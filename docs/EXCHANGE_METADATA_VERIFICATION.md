# BTC/USDT c2c1 metadata verification — 2026-11-13

**Authoritative response acquired; execution blocker remains unresolved.** No
recovery, kill-switch release, resume, orders or active-account update occurred.

## Verified evidence

CoinSwitch's documented `GET /trade/api/v2/tradeInfo` returned 401 without auth.
After specific user approval, one signed GET for `exchange=c2c1&symbol=BTC/USDT`
returned HTTP 200. Existing Keychain credentials were used only in that reader
process, never printed, logged, persisted or sent to another endpoint. An initial
local signing attempt was refused by the existing endpoint allowlist before any
network call; the scoped metadata route was then added explicitly.

Actual selected response:

```json
{"quote":{"min":"5","max":"30000"},"precision":{"base":5,"quote":2,"limit":2}}
```

| Item | Result |
|---|---|
| Minimum / maximum order notional | **5 / 30,000 USDT**, directly returned |
| Quantity decimal places | **5**; decimal quantum **0.00001 BTC** |
| Price decimal places | **2**; formatting quantum **0.01 USDT** |
| Effective limit-order tick/adjustment | **Unresolved: `precision.limit=2`** |
| Separate minimum BTC quantity | **Not returned or independently verified** |

[Official Trade Info documentation](https://api-trading.coinswitch.co/spot/reference/trade-info)
defines the precision and notional fields. [Exchange Precision documentation](https://api-trading.coinswitch.co/spot/reference/exchange-precision)
describes `limit` as an adjustment but provides no formula for applying nonzero
values. Decimal places do not justify guessing the effective limit tick. Neither
page supplies an independent minimum base quantity. Do not substitute rules from
Binance, another exchange, or the BTC/INR documentation example.

The existing translator deliberately refuses this response with
`non-zero limit precision adjustment is not safely specified`. That guard is
preserved. No field was edited to make it pass. The capture remains unusable for
execution even while fresh; a new request alone does not resolve the semantics.

Artifacts: `artifacts/metadata-verification-2026-11-13/` contains public/authenticated
status evidence, exact non-secret response, timestamped checksummed capture
`btc-usdt-c2c1-trade-info.json`, `verification-report.json`, test results and
account-preservation evidence. Capture time: **2026-11-13 04:24:30 IST**.
Response SHA-256:
`0e3208b2505fbe981a8c6c24cb4e774d9452f87446e4b9bea732ef8a4e384202`.
Capture-file SHA-256:
`60d26c240a93a02ee0dab6167d210a6f130097f1b0522eac35119142bcab9246`.
The existing capture expiry is 24 hours by default; refresh requires an explicit
authorized read, not automatic credential access.

## Implemented and tested

- Added an endpoint-restricted GET-only metadata reader over existing TLS/signing
  code: no orders, balances, redirects, credential discovery or blind retries.
- Existing capture ingestion verifies source, exact market, payload SHA-256,
  timestamp, future/stale dates and unsupported fields before runtime DB creation.
  Capture/response were persisted separately; active DB metadata was not changed.
- Real-market runtime now requires verified, time-bounded metadata. Missing and
  untimed/manual metadata block readiness and execution rather than merely warn.
  Generic offline/test policy defaults remain unchanged. Dashboard reasons now
  distinguish missing, unverified and expired metadata. Already-running code was
  not reloaded; these guards take effect only after an authorized restart.
- Minimum-order comparisons use exact price × quantity before Money rounding,
  preventing an under-minimum order from rounding upward into acceptance.
- Exit chunks retain risk/venue ceilings, downward quantity rounding and minimum
  tail rules. No risk cap was waived. Full native-feed fixtures now explicitly
  supply supported, time-bounded test metadata rather than relying on its absence.

**116 focused tests passed. Full suite: 2,374 passed, zero skips.** Six added tests
cover the observed unsupported shape, GET-only scope/refusal/secret redaction,
missing/unverified/stale/future metadata, exact minimum boundaries and real-market
readiness rejection. Existing exit/restart tests also passed. No genuine orders
or live-exchange execution tests occurred; no profitability result is claimed.
Changed files: reviewed GET adapter, order policy, runtime, dashboard, paper-service
metadata reason, focused metadata/policy/integration fixtures and documentation.
No models, protected observations or research artifacts were changed.

## Existing BTC position and exact next operator action

Under the returned five-decimal base precision, `0.01399178 BTC` decomposes into
`0.01399 BTC` plus **0.00000178 BTC** residual. This is arithmetic only, not an
approved exit size: effective limit rules and any independent minimum quantity
remain unresolved. The residual must remain recorded; never round inventory up,
erase dust, or claim the position can already be fully liquidated.

Keep Emergency Stop engaged. Ask CoinSwitch to confirm, for **BTC/USDT c2c1**:

1. The exact interpretation/formula of returned `precision.limit=2`, including
   effective price tick and any additional quantity/order-type adjustments.
2. Whether any independent minimum base quantity/lot filter applies, and its
   authoritative source; if only precision and notional apply, confirm that.
3. How sub-step residual inventory is handled. No write-off is authorized.

This request has **not** been sent. Do not call the order endpoint to discover
rules through trial and error. After authoritative clarification, implement the
confirmed rule, rerun focused/full tests and re-fetch a fresh capture if needed.
Only then follow the [backup/recovery checklist](PAPER_OPERATOR_RECOVERY_CHECKLIST.md):
clean shutdown → verified non-overwriting backup → matching USDT operator recovery
to PAUSED → restart with an accepted capture → position/reconciliation review →
separately authorized resume and controlled simulated execution. The current
capture cannot be used to bypass that prerequisite. Real-money execution remains
disabled and no unqualified ML model is promoted.
