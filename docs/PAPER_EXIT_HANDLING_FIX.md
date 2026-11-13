# v0.3.2 paper exit hotfix — 2026-11-13

Fixed the oversized position-exit rejection by splitting exits. **No risk check
or notional ceiling was waived. No current bot was restarted/resumed or sent
orders.** Real-money execution stays disabled.

- Exit quantities are recalculated from current holdings and rounded down to
  the venue step (8 decimals when metadata is absent). Each chunk respects the
  smaller of risk and venue order ceilings, using both the fresh valuation and
  protected limit price. Existing gateway/risk/fee/inventory checks still apply.
- When a split would leave a sub-minimum tail, the first chunk is reduced to
  leave a compliant final chunk. Untradeable dust stays visible; it is never
  rounded up, invented, discarded or silently written off. Venue metadata remains
  required to claim actual venue compatibility; NOT_CONFIGURED was not bypassed.
- One strategy EXIT creates an audited persistent exit request. Remaining chunks
  continue on subsequent closed-candle cycles, without another crossover. Entries
  for that symbol are held while an exit is pending or blocked.
- Open/partial/uncertain orders prevent another exit submission for the symbol.
  Existing lifecycle/reconciliation books cumulative fill deltas and fees. An
  unfilled partial remainder remains with its existing order; it is not blindly
  resubmitted. After that order is resolved/cancelled, remaining inventory is
  recalculated. Distinct sequential chunks have stable, persisted parent-linked
  identities; repeated requests cannot sell an already closed position.
- Dust, construction failures or gateway refusals block the plan once and record
  the reason, preventing automatic rejection loops. Operator-only
  `service.retry_exit(symbol, principal, reason=...)` can re-arm an existing plan
  while the bot is inactive, without submitting an order, releasing the kill
  switch or resuming trading. All gates still apply to a later attempt.
- Audit-backed exit requests, blocks and completions restore across restart.
  Emergency Stop continues to block strategy/execution; collection alone remains
  permitted. Pre-fix failed signals are not retroactively replayed.

Relevant source changes only: `trading/app/order_policy.py` and
`trading/app/paper_service.py`; new `tests/test_paper_exit_chunks.py` plus
completion documentation. Core risk, gateway, fee accounting and live execution
authorization were unchanged. No model or protected dataset changed.

Verification: **170 focused tests passed; full suite 2,362 passed, zero skips**.
Seven new tests cover capped long exits from one signal, fees/cash accounting,
partial fills and pending-order duplicate suppression, cancelled remainder,
precision/minimum/dust boundaries, emergency stop, insufficient inventory,
restart between chunks and restart with partial inventory. All order placement
and explicit recovery in tests used isolated synthetic fixtures only.
Compilation and whitespace checks passed.

Read-only verification of the actual account after testing: emergency-stopped,
kill switch engaged, one existing order, zero fills after the emergency, and
zero new exit-plan events. The running process still holds its pre-fix code;
loading this source fix requires a later authorized restart. Existing release
wheels predate this hotfix. The checkout launcher loads the corrected source.

Next step: review the current BTC position and exchange-rule metadata, then
perform explicit audited emergency recovery/restart only when authorized.
This change neither closes the current position nor grants permission to resume.
