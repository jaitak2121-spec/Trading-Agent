# Paper exit verification and operator recovery — 2026-11-13

Release engineering update: [0.3.3rc2 final candidate and release gates](RELEASE_NOTES_0_3_3_RC2.md).
Final documentation/install package: [0.3.3rc3 handoff](FINAL_SOFTWARE_RELEASE.md).
This does not authorize any recovery step below or resolve the venue-rule blocker.

Latest verification: [order-path audit and prepared CoinSwitch request](PAPER_ORDER_PATH_AUDIT.md).
The source now guards direct orders and resting fills at the broker boundary;
generic recovery-only composition of a real-market account cannot authorize fills.
No active runtime state was changed. Resolve the support questions before using
any recovery command below.

**Update: do not execute recovery yet.** The authorized CoinSwitch GET returned
5/30,000 USDT notional bounds and precision base=5, quote=2, **limit=2**. The
nonzero limit adjustment is not sufficiently specified by official docs, and no
independent minimum base quantity was returned. The captured response is correctly
rejected. Missing/unverified metadata now blocks future genuine-paper execution.
See [authoritative evidence and the exact outstanding questions](EXCHANGE_METADATA_VERIFICATION.md).
The five-decimal quantity format also exposes a potential 0.00000178 BTC residual;
do not erase it or round the current position upward. Steps below remain a future
operator procedure, conditional on resolving these rules and separate approval.

**Preparation only. No recovery, kill release, resume, cancellation or order was
performed.** No production code changed in this verification pass; six isolated
deterministic tests were added to the existing seven exit tests.

## Verified / still unverified

- Focused suite: **126 passed**. Full suite: **2,368 passed, zero skips**.
- Added full-service venue-minimum tail splitting and dashboard fee/P&L checks;
  repeated cumulative partial-fill replay without duplicate charges; all inactive
  states blocking pending exits; blocked-plan restart and unauthorized re-arm
  refusal; metadata expiry blocking the next chunk; stale marks showing unknown
  equity/unrealized P&L rather than a fabricated number.
- Read-only SQLite integrity/audit-chain inspection passed. Actual account cash
  equals initial capital plus recorded fill cash deltas. Dashboard equity equals
  cash plus marked holdings and initial capital plus realized/unrealized P&L,
  within 0.00000001 USDT. Persisted paper-venue BTC inventory matches the position
  ledger and recorded basis. Entry fees are realized expenses; the displayed
  -1.00019980 USDT realized P&L is not a closed-position trading loss.
- Actual account remains emergency-stopped, kill switch engaged, holding
  **0.01399178 BTC**. One buy and no subsequent fills or new exit-plan events.
  Reconciliation freshness is stale while inactive; collection continues.
- Evidence: `artifacts/exit-safety-review-2026-11-13/readonly-account-audit.json`
  and `tests.json`. No test trades entered that account. Actual complete chunked
  liquidation after recovery remains **untested on this position**. Partial
  exchange execution, queues and fees were tested through controlled paper/mock
  behavior, not authenticated exchange orders. No profitability claim.

## Required exchange metadata

Obtain current, independently checked rules for **BTC/USDT, c2c1**, in USDT:

| Required value | Existing capture translator / configuration |
|---|---|
| Exact market/exchange | Capture `symbol`, `exchange`; matching `data.c2c1["BTC/USDT"]` |
| Price precision / valid price increment | `precision.quote` → price decimals; manual `--limit-price-decimals` |
| BTC quantity precision / valid lot increment | `precision.base` → `10^-base` step; manual `--quantity-step` |
| Minimum order value | `quote.min` → USDT minimum; manual `--min-order-notional` |
| Maximum order value | `quote.max` → USDT maximum; manual `--max-order-notional` |
| Additional limit precision rule | `precision.limit`; current translator accepts **zero only**, rejects unsupported adjustments |
| Provenance / freshness | `coinswitch-trade-info-capture/v1`, source `/trade/api/v2/tradeInfo`, UTC `captured_at`, `response_body_base64`, `response_sha256`; default maximum age 24h |

Prefer a verified capture supplied with `--coinswitch-market-info-file PATH`.
Do not invent values, relabel another market or modify an unsupported rule to
make validation pass. Manual constraints must all be supplied together with
reviewed price precision; they remain UNVERIFIED without capture freshness.
Also review fee/slippage settings (currently 10bp/2bp per fill); bid/ask spread
is observed, not added twice. No credentials were accessed or metadata fetched
in this pass. Current NOT_CONFIGURED warning does not establish venue compliance.

## Precise backup and recovery checklist — operator action only

1. Review the open BTC position, the single filled buy, absence of pending or
   UNKNOWN orders, the exit fix and venue rules. Preserve the current account;
   do not use the USD demo recovery example or reset balances.
2. When explicitly authorized, stop the genuine launcher cleanly with Ctrl-C in
   its terminal. This preserves the emergency state and position. Stop its
   scheduler too; the launcher normally owns that cleanup. Confirm port 8766 and
   the account writer have stopped. Never remove an active `.lock` file.
3. Make a **new, non-overwriting** backup using the existing SQLite backup API:

   ```sh
   cd "/Users/jaitak/Documents/Trading app"
   .venv/bin/python -m trading.app.maintenance backup \
     artifacts/real-paper-btc-usdt-v032.sqlite3 \
     artifacts/backups/btc-usdt-before-recovery-2026-11-13-001.sqlite3
   .venv/bin/python -m trading.app.maintenance inspect \
     artifacts/backups/btc-usdt-before-recovery-2026-11-13-001.sqlite3
   ```

   Record the printed SHA-256 and successful integrity result. Choose a new suffix
   if the destination exists. This includes committed WAL pages; a plain copy of
   only the main `.sqlite3` file while running is not the backup procedure.
   Do not restore an old backup over the account to erase trades or its latch.
4. Only after explicit operator approval, use the existing operator API on the
   **original database**, matching the USDT profile and wall clock:

   ```python
   from trading.app.composition import build_paper_stack
   from trading.core.clock import SystemClock
   from trading.core.config import TradingConfig
   from trading.core.money import Money, USDT
   profile = TradingConfig.from_toml_file("config/paper-usdt.example.toml")
   stack = build_paper_stack(
       sqlite_path="artifacts/real-paper-btc-usdt-v032.sqlite3",
       starting_cash=Money("10000", USDT), risk=profile.risk,
       clock=SystemClock(), fee_bps=10, slippage_bps=2)
   try:
       assert stack.startup.ok
       assert stack.bot.state == "emergency_stopped"
       stack.kill_switch.release(stack.operator_id, reason="OPERATOR: record the reviewed incident resolution")
       stack.bot.transition("paused", stack.operator_id, reason="OPERATOR: recovery reviewed; remain paused")
   finally:
       stack.persistence.close()
   ```

   Existing balances restore; the nominal starting amount does not reset them.
   Do not execute this code while the running application owns the database.
5. Start corrected source **without** automatic resume, using the verified capture:

   ```sh
   ./scripts/start-paper.command \
     --coinswitch-market-info-file artifacts/trade-info-btc-usdt-c2c1.json
   ```

   The capture must actually exist and pass checks. Confirm PAUSED, exact restored
   BTC/cash/orders, live quote timestamps, full-candle warm-up, valid metadata and
   clean startup reconciliation. An old snapshot is not a fresh venue sweep.
   Paused collection intentionally does not continuously reconcile, so freshness
   can expire; the running cycle must sweep successfully before any order.
6. Resume through local controls **only after separate operator authorization**.
   All risk gates still apply. No pre-fix failed EXIT is retroactively replayed;
   the next legitimate strategy EXIT starts the capped plan. Pending/partial
   orders must resolve before another chunk; dust/refusals require review.
   `service.retry_exit(...)` only re-arms an existing blocked plan while inactive;
   it does not create a missing plan, release the kill switch or close inventory.

**Exact next action:** obtain/review the BTC/USDT c2c1 rule capture and the backup
plan. Keep the current Emergency Stop engaged. Execution recovery and real-position
exit validation remain gated on that manual review; real-money trading stays disabled.
