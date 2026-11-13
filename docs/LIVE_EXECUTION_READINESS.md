# Live execution readiness

## Independent offline execution work — 2026-11-12

`execution_contract.py` now implements native Spot requests over an abstract,
disabled-by-default exchange port. It reuses the gateway and sandbox broker,
with mocked identity/funding/fee/metadata, partial/cancel/UNKNOWN and durable
restart coverage. No HTTP order transport, live credentials or live application
wiring exists. Whole-wallet checks do not establish strategy-owned positions.

Correction to earlier discovery scope: [official CSX documentation](https://api-csx.coinswitch.co/)
links a paper endpoint, but for a different API surface. A credential-free health
probe returned 403; account access and Spot v2 compatibility remain unverified.
See `COINSWITCH_ACTIVATION_CHECKLIST.md` for exact external requirements and
activation stages. Existing historical credentials were not sent to that host.

All new development research is complete and rejected; no profitable strategy
has been demonstrated. Full suite: **2,264 passed, no skips**. Default PAPER
composition and all existing risk controls are unchanged. This section supersedes
older statements about the absence of any documented paper endpoint while
preserving their original review history below.

## Latest verification — 2026-11-12

**Blocked, not completed.** The operator confirmed that no written CoinSwitch
Spot sandbox/testnet confirmation has been obtained. The official API Surfaces,
authentication, Spot order and fee reference pages were reviewed again. An
official-domain search also did not establish a non-production Spot endpoint.
That is not proof that no private sandbox exists. No environment URL, permission
or test credential was invented, and production funds were not used as a substitute.

Current verified documentation:

- [API Surfaces](https://api-trading.coinswitch.co/get-started/api-surfaces)
  publishes Spot v2 at `https://coinswitch.co/trade/api/v2` and exchanges
  `coinswitchx`, `c2c1`, `c2c2`.
- [Authentication](https://api-trading.coinswitch.co/get-started/authentication)
  documents Ed25519 over method + decoded path/query + epoch, with key,
  signature and epoch headers. Existing historical GET authentication works;
  that does not establish permission to trade.
- [Create Order](https://api-trading.coinswitch.co/spot/reference/create-order)
  documents LIMIT orders, UUID client order IDs, expiry and 100 requests per
  ten seconds. Venue trade-info precision and minimums must be fetched and
  verified before any future placement. No placement was attempted.
- [Trading Fee](https://api-trading.coinswitch.co/spot/reference/trading-fee)
  documents account-specific maker/taker rates and discounts. Actual account
  fees remain unverified; research's 10bp/20bp assumptions are not claimed to
  be the user's fee schedule. Existing historical-only credentials were not
  repurposed to query accounts or fees.

The current broker contract tests exercise injected in-memory transports, not
CoinSwitch order execution. Partial fills, ambiguity/reconciliation and restart
tests cannot substitute for qualification against the real venue contract.
No order transport or automatic live activation was added in this milestone.

Required external action: obtain the official test-environment and permission
details using `COINSWITCH_SANDBOX_REQUEST.md`. That request was updated but not
sent. A reply must establish isolation, URLs, provisioning, order lifecycle and
limits before a test adapter can be implemented and independently qualified.
Withdrawal permission must never be requested. No secrets should be shared in chat.

Next internal sequence, once those details exist: venue-specific request/response
contract tests; isolated test-only transport; balances/orders/fees/positions and
UNKNOWN reconciliation; outage/partial-fill/cancel/duplicate/restart qualification;
security review. Real activation remains a separate deliberate user action. A
profitable strategy also remains unestablished and cannot be inferred from a
working order adapter.

Verification: 122 focused broker/recovery/historical tests passed; full suite
**2,238 passed, no skips**. No paper strategy, model or protected observations
were changed. Historical GET-only recovery was performed separately and remains
incomplete, as recorded in `DEVELOPMENT_EXTENSION_REPORT.md`.

## Previous review — retained for history

Status: unsupported and disabled. No authenticated order transport was added,
no account credentials were read during final completion work, and no real
orders were attempted. Paper and historical GET acquisition remain separate.

Documentation review on 2026-11-11 confirmed these primary sources:

- [API surfaces](https://api-trading.coinswitch.co/get-started/api-surfaces):
  spot production base and the three spot exchange identifiers are documented.
- [Authentication](https://api-trading.coinswitch.co/get-started/authentication):
  Ed25519 request signatures, API-key/signature/epoch headers, decoded query
  canonicalization, and clock drift requirements are documented.
- [Create order](https://api-trading.coinswitch.co/spot/reference/create-order):
  LIMIT orders, venue precision/minimum requirements, expiry, idempotency UUID
  and the published rate limit are documented.
- [Spot reference](https://api-trading.coinswitch.co/spot/reference/): order
  create/cancel/status/list and account/fee interfaces are listed.

No non-production spot base URL or sandbox access was verified in those pages.
That is an evidence limitation, not proof that CoinSwitch offers no private
test environment. Written venue confirmation remains needed. The prepared
request in `COINSWITCH_SANDBOX_REQUEST.md` was not sent.

Existing broker ports, gateway gates, UNKNOWN-order handling, reconciliation,
response translators and mocked contract tests provide the architectural
boundary. They do not verify real-account execution. A future live adapter
must be isolated from paper accounts/databases and must implement real balance,
order, cancel, status, fee and position reconciliation with contract/outage
tests. Trading permissions must exclude withdrawals. Secret values must come
only from the secure credential provider and never reach UI/logs/artifacts.

The remaining external blocker is a verified safe venue qualification path and
account-specific permissions. No production endpoint should be used as a
substitute sandbox. Live activation needs a separate explicit security and
operational review; it is not part of this paper-product release.
