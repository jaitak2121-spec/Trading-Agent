# CoinSwitch external qualification and activation boundary

Reviewed 2026-11-12. **Actual execution remains disabled. This checklist does not
enable it, authorize spending, or certify a profitable strategy.**

## Resolve the API surface first

The project's offline execution contract targets [CoinSwitch PRO Spot v2](https://api-trading.coinswitch.co/get-started/api-surfaces),
with `/trade/api/v2` routes, snake-case fields and `X-AUTH-*` authentication.
The [Spot authentication specification](https://api-trading.coinswitch.co/get-started/authentication)
uses method + decoded path/query + epoch milliseconds; JSON body is not included
in the modern epoch signature. Exact decimal body encoding and signing-message
preparation are mocked/tested; no signer, key or HTTP order client is shipped.

An additional official source was found: [CSX onboarding documentation](https://api-csx.coinswitch.co/)
links `https://sandbox-csx.coinswitch.co/` as its paper-trading endpoint. It uses
different `/api/v1` and `/api/v2` routes, `CSX-*` headers, camel-case order fields,
and a body-inclusive signature. The examples/header description use epoch seconds
while some prose describes milliseconds. This ambiguity needs authoritative
clarification. **Do not mix CSX and Spot v2 routes, signatures or credentials.**

Unauthenticated `GET https://sandbox-csx.coinswitch.co/api/v1/public/health/`
returned **403** from this machine. No credentials were sent. This establishes
neither usable account access nor that the sandbox is permanently unavailable.
Evidence: `artifacts/csx-sandbox-public-health-investigation.json`.
The previous broad statement that no official paper endpoint was documented is
superseded; compatible Spot v2 test access remains unverified.

## External requirements, not presently satisfied

- Confirm which API surface the account may use, whether the published CSX
  paper endpoint is currently supported, and whether a Spot v2-compatible test
  environment exists. Obtain official REST/private-stream URLs and the supported
  exchange/symbol list, not a guessed hostname or production substitute.
- Obtain separately provisioned test credentials and the account's actual
  permission/scope record. Existing historical GET success proves neither
  order permission nor CSX compatibility. Never reuse production keys on a new
  host without explicit exchange authorization and verified provisioning.
- Restrict permissions to the required balance/order/fee reads and trading.
  Do not request withdrawals or fund-transfer permissions. Separate test/live
  and historical credential services, revoke test credentials after qualification,
  and verify actual IP allowlisting. Do not spoof forwarding headers or bypass a
  403 with proxies. Configure the real authorized egress address with the exchange.
- Verify funded test balances, reset procedure and clear separation from real
  funds. Confirm fee assets, maker/taker discounts, TDS/tax cash flows and actual
  fill settlement from exchange trade records. Read-only account fees have not
  been queried in this milestone.
- Establish exclusive account/subaccount allocation or a verifiable allocation
  ledger. The [Portfolio API](https://api-trading.coinswitch.co/spot/reference/portfolio)
  describes account-wide holdings and locked amounts, not positions owned by one
  strategy on `c2c1`. The adapter intentionally refuses to manufacture those
  positions. Whole-account reconciliation cannot establish strategy ownership.

## Internal architecture delivered without access

`trading/adapters/coinswitch/execution_contract.py` provides immutable request /
response types, an abstract exchange port, a disabled default port, native
Spot request construction and a broker reusing the existing safety gateway.
It includes scoped LIMIT-only metadata checks, exact numeric JSON, conservative
fee-reserved funding checks, stable exchange-scoped UUID client IDs, bounded
placement bursts, validated native status translation and cumulative-fill handling.

There are no sockets, base host, HTTP order calls, credential provider, signing
key, live launcher, account-funding action or automatic production selection.
A scripted test implementation is the only exercised exchange port. Production-
quality behavior still needs independent venue qualification; mocks alone do not
make this a production-ready live adapter.

The [native order API](https://api-trading.coinswitch.co/spot/reference/create-order)
documents LIMIT orders, client IDs and expiry. Metadata, minimums and actual
permission must be verified before any future placement. The implementation does
not silently round invalid size/price. A lost placement response remains UNKNOWN;
no write is automatically retried. Stable client IDs are rebuilt from durable
local orders after restart. Missing/truncated inventories or 404 are not treated
as proof that no order exists.

The [cancel API](https://api-trading.coinswitch.co/spot/reference/cancel-order)
can return cancellation still in flight. This adapter always requires a following
authoritative order lookup before retiring the order. Partial fills are applied
as cumulative-to-delta changes, even when the remainder becomes canceled/expired.
The [order list](https://api-trading.coinswitch.co/spot/reference/list-orders)
has limited closed history; full pages are refused as potentially truncated.
An old unresolved order therefore stays blocked rather than being guessed away.

## Required qualification before any live implementation is activated

1. Implement the selected, independently verified **test-only** transport behind
   the existing port. Enforce an approved test host and credential scope; no
   runtime boolean may redirect a paper database or paper account into production.
2. Validate signature vectors, clock-drift refusal, numeric wire encoding,
   precision/minimums, fee assets and authenticated permissions in that environment.
3. Test placement, full/partial fills, cancel/fill races, expiration, duplicate
   client IDs, rejected/malformed responses, throttling, disconnects and timeouts.
   Preserve unknown outcomes and never retry an ambiguous write automatically.
4. Prove wallet, strategy allocations, all open orders, limited history and fill
   fees reconcile against durable records. Test overfills, foreign orders and
   missing identity. Position disagreement must block new exposure.
5. Exercise crashes before/after submission/fill persistence and test operator
   UNKNOWN resolution without duplicate placement. Paused/stopped/kill-switched
   bots and breached risk limits must produce no exchange writes.
6. Review dependency/security boundaries, key storage/revocation, audit redaction,
   TLS/host validation, bounded HTTP timeouts, aggregate rate limits, monitoring
   and emergency cancellation policy. Do not force-liquidate automatically.
7. Only after independent review, create separate live configuration, balances,
   credentials, database and deliberate activation. The user must specify the
   capital/order/exposure/loss limits and authorize activation separately. Default
   startup remains PAPER; missing evidence must refuse activation.

Even a fully qualified execution adapter would not prove profitable decisions.
Every current research candidate remains rejected; no model/strategy has been
promoted for real capital. No exchange message or order was sent by this milestone.
