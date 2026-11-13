# Paper-order path audit — 2026-11-13

**No active account recovery, restart, kill release, resume, orders or cancellation.**
No new research, credentials access or authenticated API request in this pass.

## Rule evidence and blocker

Rechecked authoritative [Trade Info](https://api-trading.coinswitch.co/spot/reference/trade-info)
and [Exchange Precision](https://api-trading.coinswitch.co/spot/reference/exchange-precision)
documentation. They define decimal precision but still do not explain the
nonzero limit adjustment. The retained authenticated BTC/USDT c2c1 response has
base=5, quote=2, limit=2 and 5/30,000-USDT notional bounds. Effective limit-order
quantity increment/tick and any independent minimum base quantity remain
unresolved. The existing refusal stays intact; no values were guessed.

[Prepared concise support request](COINSWITCH_RULE_CLARIFICATION_REQUEST.txt).
It has not been sent. Original capture and hashes remain in
`artifacts/metadata-verification-2026-11-13/`. Do not relabel precision as a minimum
quantity or remove limit=2 from the capture to make it pass.

## Paths audited and gaps closed

| Path | Enforcement / verification |
|---|---|
| MA and approved-ML entry | Shared service → protected limit and metadata → gateway risk checks → broker fee/cash/inventory and fresh-quote checks |
| Strategy EXIT and continued chunks | Same gates plus current holding, cap, downward lot rounding, minimum-tail allocation, pending-order suppression and audited blocked state |
| Direct gateway request | Added broker-boundary validation of market/asset/currency, protected-limit requirement, exact lot/price precision and notional bounds; no silent rounding of an already-constructed intent |
| Resting order / lifecycle / unknown reconciliation | Added current metadata/state permission at the fill boundary. Expired metadata cannot produce a new simulated fill even though service lifecycle polling precedes its own metadata check. Previously observed fills still reconcile idempotently |
| Recovery-only composition | A persisted real-market account now defaults to fill-disabled in generic composition. Only the genuine runtime installs the metadata-ready callback and order validator. A recovery API cannot authorize fills merely by using generic paper helpers |
| Paused/stopped/emergency | Existing bot, latch and gateway guards retained; broker also checks fill permission before initial fill and reconsideration. Collection can continue without new execution |
| Offline/demo/native exchange mocks | Explicitly isolated from genuine-state provenance; no new strategy or live transport introduced |

Production changes: `trading/app/order_policy.py`, `runtime.py`, `composition.py`
and `trading/adapters/paper/broker.py`. The core gateway/risk limits, sizing caps,
fee accounting and real-money disablement were not weakened or changed.
The active process was not reloaded, so new guards are source fixes for a future
authorized restart. Existing wheels predate these fixes.

## Tests and actual-account review

Three necessary deterministic cases added: direct off-lot/off-tick/under-minimum
orders refuse; a resting limit remains unfilled after metadata expires despite
a fresh crossing quote; recovery-only composition cannot fill in a genuinely
bound account. Execution uses temporary fixtures only. A USDT denomination error
in the new recovery fixture was corrected before final verification.

Focused: **258 passed**. Full suite: **2,377 passed, zero skips**. Results are recorded in
`artifacts/paper-path-audit-2026-11-13/tests.json` and the completion audit.
Compilation and whitespace checks passed. Tests do not establish profitability.

Read-only account review: SQLite/audit-chain integrity passed; Emergency Stop
and kill switch remain engaged; one existing order; **0.01399178 BTC** preserved;
persisted paper venue matches position ledger; cash matches fill cash deltas;
no fills after Emergency Stop and no exit-plan events. Evidence:
`artifacts/paper-path-audit-2026-11-13/account-review.json`.
No synthetic fixtures entered the active account. Live execution remains disabled.

## Exact next operator action / future controlled procedure

1. **Send the prepared request to CoinSwitch and keep Emergency Stop engaged.**
   Do not run recovery while rule semantics are unresolved.
2. Obtain authoritative worked examples and minimum-quantity confirmation. Only
   then implement the supported interpretation, rerun tests and obtain a fresh
   capture via a separately authorized read if the retained capture has expired.
3. Review the existing position and residual explicitly. Five-decimal formatting
   alone would leave 0.00000178 BTC; no round-up or write-off is authorized.
4. Follow [the precise backup/recovery checklist](PAPER_OPERATOR_RECOVERY_CHECKLIST.md):
   gracefully stop the genuine launcher → verified non-overwriting SQLite backup
   → inspect backup/hash → matching original USDT database/profile and wall clock
   → separately approved audited recovery to PAUSED → start corrected source with
   accepted metadata, without automatic resume → inspect restored BTC/cash/orders
   and startup reconciliation.
5. Only after separate approval resume paper execution. The running cycle must
   refresh reconciliation before submitting anything. Observe a legitimate MA
   exit and capped continuation; do not force test trades into the real account.
   Pending partial orders and residual dust require review, not duplicate orders.

This closes independently fixable execution-path gaps; it does not resolve the
external rule ambiguity or claim live-position liquidation has been validated.
