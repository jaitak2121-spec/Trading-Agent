# Handoff

Written 2026-08-25, on branch `stage-1-completion`, with the working tree clean at
commit `3664991`. Every number in this file was measured against that commit, not
recalled. Where a claim comes from an operator instruction rather than from the
repository, it says so — a new session should be able to tell the difference.

Read this, then read `docs/ARCHITECTURE.md` (525 lines) and `docs/SAFETY.md`
(551 lines) in full before changing anything. This file is a map; those two are
the territory.

---

## 1. What this is, and how it is shaped

A trading system that must eventually support two capabilities that are kept
architecturally separate:

1. **Advisory mode** — analyze market data, generate signals, size positions,
   propose entries/exits/stops/targets, explain the reasoning and the risk. It
   must be structurally incapable of placing an order, not merely discouraged
   from it by the UI.
2. **Live / autonomous mode** — execution through one chokepoint, only after
   every authorization, risk, sizing, reconciliation and mode check passes.

Live trading is **off** and **unimplemented**. There is no network code, no
database, no broker credential path, and no third-party dependency anywhere in
the project — including the tests. Python 3.13.7, stdlib only.

### Layers

Five packages, in dependency order. The arrows are the only legal direction.

```
trading.core     <-- the pure kernel: money, orders, risk, gateway, audit, ...
trading.ports    <-- abstract interfaces (BrokerPort, QuoteFeedPort, repository)
trading.adapters <-- concrete implementations (memory/, paper/) + lifecycle.py,
                     the poller, which *calls* the kernel rather than serving it
trading.strategy <-- signal generation; proposes, never executes
trading.advisory <-- leaf. Nothing under trading/ imports it back.
```

`trading/core` and `trading/ports` are a **mechanically enforced** pure kernel:
`tests/test_core_purity.py` (47 tests) walks the import graph with `ast` and
also runs fresh-interpreter subprocess probes, so a forbidden import fails the
suite rather than merely violating a convention. This is not a style rule. Do
not add an import to `core/` or `ports/` without reading that test file first.

### The chokepoint

`ExecutionGateway.submit` (`trading/core/gateway.py`, 1139 lines) is the single
path to execution. It runs ten gates in a fixed order:

```
1 authorization  2 kill_switch  3 circuit_breakers  4 trading_mode
5 live_authorization  6 duplicate_order  7 reconciliation  8 risk
9 token  10 execution
```

Three possible outcomes, and the difference between them is the safety model:

| Outcome | Meaning | Idempotency key |
|---|---|---|
| REFUSED | A gate said no | Released, if the refusal happened before the broker was touched |
| EXECUTED | Known, final | SETTLED |
| UNKNOWN | We do not know what the venue did | **Not released.** Blocks the whole system until an operator reconciles |

**There is no retry anywhere in `gateway.py`.** That absence is deliberate: a
retry is how you end up long twice. Do not add one.

---

## 2. Completed stages and commits

Thirteen commits, oldest last. Branch `main` sits at `ed024c2`; all Stage 1 and
Stage 2 work is on `stage-1-completion`. Stages 2H–2K are **not** in this list —
they are uncommitted, and §9 lists them.

| Commit | Date | Stage |
|---|---|---|
| `519406b` | 2026-09-21 | Stage 2G: a fill that happens after the ack can still find its way home |
| `26625d1` | 2026-09-20 | Stage 2G: a cancel that reconciles what the venue actually did |
| `c5ff7a5` | 2026-08-27 | Fix unknown order resolution lifecycle |
| `3664991` | 2026-08-25 | Stage 2F: a paper venue that fills against the book it can see |
| `629d78c` | 2026-08-25 | Stage 2E: advice an operator can read, and cannot accidentally submit |
| `22ffb35` | 2026-08-24 | Stage 2D: a signal acquires a size, or a reason it has none |
| `f4682f5` | 2026-08-24 | Stage 2D: realized P&L reaches the daily-loss limit |
| `568e1ad` | 2026-08-24 | Stage 2C: cost basis, realized P&L, and equity over one ledger |
| `dedbe6b` | 2026-08-24 | Stage 2B: signal generation with no execution or sizing surface |
| `aaf0096` | 2026-08-23 | Stage 2A: normalized market data and structural staleness handling |
| `a6dfe1b` | 2026-08-23 | Stage 1: architecture and safety docs, resolve_unknown FILLED coverage |
| `539d63a` | 2026-08-23 | Stage 1: ports/adapters, execution chokepoint, and invariant coverage |
| `ed024c2` | 2026-08-22 | checkpoint: Stage 1 progress |

Stage 2D took two commits: the daily-loss wiring landed first, position sizing
second.

---

## 3. Where the project is right now

**Stages 2H–2K are implemented in the working tree and not yet committed.** The
committed history stops at Stage 2G (`519406b`). Everything from there through
2K is staged on `stage-1-completion` as uncommitted work; §9 lists exactly which
files. Stage 2I, 2J and 2K are described below, and Stages 2I–2L were the
work orders this handoff was resumed under.

**Stage 2H — persistence and restart recovery.** The repository seam is real and
the kernel is injected through it: `trading/ports/repository.py` (orders,
positions), `trading/ports/reservation_repository.py` and
`trading/ports/recovery.py`, implemented in-memory by
`trading/adapters/persistence/` and driven by
`trading/adapters/recovery.RestartRecoveryCoordinator`. **Nothing is written to
a disk or a database, so state still does not survive a real process exit.** What
is tested is that a stack *rebuilt* around surviving repositories keeps
INVARIANT 5's block and INVARIANT 12's refusal
(`tests/test_restart_recovery_e2e.py`).

Defect 11 below (§7) was repaired as part of this pass: the persistence package
now exports `OrderStoreAdapter` under the name `OrderStore`, and the adapter
shares the wrapped store's `_lock` as well as its dicts.

**Stage 2I — venue-wide reconciliation.** `trading/adapters/reconciliation.py`
adds `ReconciliationCoordinator`, which reads the whole book the venue holds and
compares it against local state. The capability it needs is a new port:
`BrokerOrderInventoryPort` in `trading/ports/broker.py`, with a validated
`BrokerOrderSnapshot` value type and a `BrokerOrderStatus`. It is deliberately
*separate* from `BrokerPort`, so "can this venue be reconciled in full" is a
checkable fact rather than an exception discovered mid-sweep; a venue without it
yields one blocking `INVENTORY_UNAVAILABLE` finding instead of a partial sweep
that would look identical to a clean one.

The sweep detects: a local open order the venue does not hold; a venue order
with no local record (out-of-band, or lost in a crash window); broker-id,
symbol, side, quantity, and fill-regression contradictions; a terminal local
order the venue still holds open; duplicate venue records; and transport
failure. It **observes** — a matched, consistent order is handed to
`gateway.sync_order`, so booking, authorization and audit all stay in the
gateway — and it **never resolves an UNKNOWN**, reporting it instead. A clean
snapshot goes through `ReconciliationGate.reconcile`, so the latch and freshness
rules are unchanged.

**Stage 2J — the sandbox broker boundary.**
`trading/adapters/sandbox/` holds `SandboxBroker`, an adapter whose transport is
injected. It names no endpoint, holds no credential, and performs no I/O; it
exists so the translation from a venue's answers to a domain outcome can be
written and reviewed before there is anything to talk to. The rule it enforces:
an answer that cannot be fully read is `UNCERTAIN`. Timeouts, disconnects, rate
limits, unparseable bodies, unrecognised status words, overfills, and
float-valued money all become uncertainty, and **nothing is ever retried
automatically**. `tests/test_sandbox_broker.py` asserts the transport is called
exactly once per placement in every failure mode.

**Stage 2K — operational safety.** `trading/adapters/operations.py` adds
`OperationalMonitor` (a redacted, immutable status, plus a fail-closed
`ready` predicate) and `StartupCoordinator` (config validation, restart recovery
scan, optional initial sweep). The monitor holds no execution surface — it has
no `submit`, `cancel`, or `resolve_*` — and cannot authorize anything; it reports
that the system is un-ready and the gateway is what refuses. Startup never
enables live trading, submits an order, clears a mismatch, or resolves an
UNKNOWN. There is deliberately no web framework: the architecture has no inbound
adapter layer, so status is read in-process.

**Stage 2F — the paper venue.** It added `trading/adapters/paper/` — a
`PaperBroker` that is the honest counterpart to the deliberately hostile
`SimulatedBroker` in `trading/adapters/memory/`. It fills against a quote feed
with no randomness anywhere: a buy lifts the ask and a sell hits the bid,
`slippage_bps` moves the fill *against* the order in both directions, `depth`
caps what one placement can take (which is how partial fills first became
producible in this repository), a non-crossing limit rests, and a missing or
stale quote is a refusal rather than a guess.

**Stage 2G — the order lifecycle.** Four increments, each a whole capability
rather than a slice of one:

1. **Cancel reconciles what the venue actually did** (`26625d1`). `cancel` no
   longer trusts its own acknowledgement — it reads the venue's authoritative
   state afterwards, books a fill that won the race exactly once, and declares
   `CANCELED` only if the order is still open. A fill always beats a cancel.
2. **`ExecutionGateway.sync_order`** (`519406b`) — the lifecycle-sync entry
   point. A fill that happens after the ack now has a route into the portfolio
   that does not require an operator to resolve an `UNKNOWN`. Folded in with it:
   `resolve_unknown` now takes the gateway lock, which it had been missing
   despite being a fill-booking path (§7 item 8 below).
3. **`PaperBroker.fetch_order_state` re-evaluates** — the venue half, so
   `sync_order` has something true to read. Asking about a *resting* order
   re-runs the same `_decide` the placement used against the *current* quote, so
   a limit the market subsequently reaches fills on that poll. Nothing in this
   process fills in the background, so the poll is what moves a resting order
   forward. Bounded three ways: the fill reuses the stored `broker_order_id` (a
   different one is a contradiction `sync_order` refuses); a non-fill outcome —
   including a missing or stale quote — returns the stored record verbatim
   rather than the rejection `_decide` would produce, so a gap in the feed
   cannot disown a live resting order; and an order fills at most once, because
   `depth` is what one placement can take rather than a pool that refills.
4. **A periodic poller** — `trading/adapters/lifecycle.py`, `LifecyclePoller`.
   The schedule that was missing: it sweeps `OrderStore.open_orders()` and hands
   each order to `sync_order` under a `Principal` holding `Action.RECONCILE`
   (`Role.SYSTEM` holds it precisely so an unattended poller can). `poll_once()`
   is the whole behaviour and is synchronous; `start`/`stop` add only a daemon
   thread on an interval. It is a *driver*, not a kernel component, and holds no
   lifecycle opinion: selection is `open_orders()` (so terminal and `UNKNOWN`
   orders are unreachable without naming a state), authorization is the gateway's,
   and interpretation, booking and auditing are all `sync_order`'s. Its report
   keeps a gateway refusal apart from a failed venue read, and a failed read
   leaves the order untouched — never `UNKNOWN`, never finished.

No `amend` or `replace` exists anywhere under `trading/`. Nothing is
half-finished; there is no work-in-progress to pick up. The remaining 2G
questions are listed in §7 — nothing is blocked on code.

---

## 4. Remaining stages

> **Source note.** This ordered list comes from the operator's work orders given
> in conversation. It is **not recorded anywhere in the repository or Git
> history.** `docs/ARCHITECTURE.md` §8 ("Where Stage 2 attaches") describes the
> seams but does not enumerate stages. Treat the list as the plan of record and
> confirm scope with the operator if anything looks ambiguous.

Stages 2A–2F are done (§2 above), and 2G–2L are implemented in the working tree
(§3, §9). Status of each:

- **2G — Order lifecycle.** **Landed.** See §3.
- **2H — Persistence and restart recovery.** **Landed, with one honest caveat.**
  The repository seam is now real and the kernel is injected through it:
  `trading/ports/repository.py` (orders, positions) plus
  `trading/ports/reservation_repository.py` and `trading/ports/recovery.py`
  (new in 2H), implemented in-memory by `trading/adapters/persistence/` and
  driven by `trading/adapters/recovery.RestartRecoveryCoordinator`. The caveat:
  **nothing is written to a disk or a database, so state still does not survive
  a real process exit.** What is tested is that a stack *rebuilt* around
  surviving repositories keeps INVARIANT 5's block and INVARIANT 12's refusal
  (`tests/test_restart_recovery_e2e.py`). Durability itself is the remaining
  work, and it is a database adapter, not a kernel change.
- **2I — Reconciliation.** **Landed.** Venue-wide order *and* position
  reconciliation via `ReconciliationCoordinator`, with a new
  `BrokerOrderInventoryPort` and validated `BrokerOrderSnapshot` observations.
  See §3 and §7 items 15–16.
- **2J — Broker adapter interface.** **Landed as a mock/transport boundary
  only.** `trading/adapters/sandbox/SandboxBroker` translates a venue's answers
  into domain outcomes behind an injected transport. **No sandbox endpoint has
  been verified and no credentials exist**, so no network implementation was
  written. This is the explicit stop boundary — §7 item 14 lists what must be
  supplied first.
- **2K — Monitoring, audit, operational safety.** **Landed.**
  `trading/adapters/operations.py`: a redacted `OperationalStatus` with a
  fail-closed `ready` predicate, and `StartupCoordinator` for configuration
  validation, recovery scan and initial sweep. See §7 item 17.
- **2L — Tests for all critical paths.** **Landed.** 1875 tests, 96.5% coverage;
  figures in §8.
- **Not started: real durability, a live or sandbox broker connection, and any
  inbound adapter (HTTP/UI).** These are the three things between this repository
  and a small live deployment, in that order.

Live-trading preparation must, across these stages, address: duplicate-order
prevention, stale market data, broker/API timeouts, unknown order outcomes,
partial fills, rejections, disconnect/reconnect, process restart, position
mismatch, rate limits, kill switch, maximum order/position/notional/loss limits,
and auditability. **Only process restart and durability remain unaddressed** —
every other item has code and tests behind it now.

The intended deployment progression is
**Backtest → Advisory → Paper Trading → Broker Sandbox → Small Live Deployment.**
Nothing in this repository is past "Paper Trading".

---

## 5. Invariants and constraints that must never be violated

Thirteen numbered invariants, greppable by number in both source and tests.
The full table with rationale is `docs/SAFETY.md` §1; do not rely on this
summary alone when touching safety code.

| # | Invariant |
|---|---|
| 1 | `LIVE_TRADING` defaults to FALSE |
| 2 | No order executes while live trading is disabled |
| 3 | Strategies propose; they never execute |
| 4 | Risk approval precedes execution — and it is a *capability*, not a boolean |
| 5 | An UNKNOWN order blocks all new orders |
| 6 | A position mismatch blocks |
| 7 | Loss, exposure and rate limits cannot be bypassed |
| 8 | Money is `Decimal` only — never `float` |
| 9 | Secrets never reach logs |
| 10 | The kill switch works |
| 11 | Mode transitions are controlled |
| 12 | Duplicate submission is prevented, or escalated to UNKNOWN |
| 13 | Audit happens before the effect, not after |

Two rules `docs/ARCHITECTURE.md` states must survive every later stage:

- **The kernel stays stdlib-only.**
- **Nothing bypasses `ExecutionGateway.submit`.**

### Hard constraints from the operator, still in force

- Do not connect to real-money execution. Build interfaces and safety
  boundaries so live execution can be added later without redesigning the core.
- Do not weaken, bypass, or duplicate a safety check to make execution easier.
- Do not claim the system is safe for real money because tests pass. Passing
  tests are not a production-readiness argument.
- Advisory and execution stay architecturally separate.
- Stage 1 is complete. Do not redo it, do not over-polish it, and do not
  redesign working Stage 1 architecture unless the repository proves a change is
  necessary.

### Two tests that constrain how you may extend the gateway

Both will fail if you add surface area casually. Neither is a nuisance test —
each encodes an invariant about the shape of the chokepoint.

- `tests/test_gateway.py:839` — `test_submit_is_the_only_public_way_to_execute`
  asserts the gateway's public callables are exactly
  `{"submit", "cancel", "resolve_unknown"}`. A new public method must be a
  deliberate, argued change to that set.
- `tests/test_gateway.py:765` — `test_the_declared_chain_matches_the_gates_in_use`
  asserts `set(ExecutionGate.ORDER)` equals the set of upper-case string
  constants on `ExecutionGate`. Lifecycle-stage labels therefore belong in a
  *separate* namespace, not bolted onto `ExecutionGate`.

---

## 6. Design decisions, and why

These are the ones that are load-bearing — reversing any of them without
understanding the reason will reintroduce a specific failure.

**Money.** `Decimal` throughout, under an explicit `FINANCIAL_CONTEXT`.
`Price.rounded(...)` / `Money.rounded(...)` default to `ROUND_HALF_EVEN`.

**`Price` implements only `<` and `>`.** No `<=` or `>=`. "At or better" must be
spelled `not a > b`. The reason: a limit sitting exactly on the executable price
*does* cross, and a naive `>` / `<` silently drops that boundary. `Quantity`
does have all four comparisons, but `__lt__`/`__gt__`/`__le__`/`__ge__` raise
`CurrencyMismatch` on an asset mismatch while `__eq__` merely returns `False`.

**Risk approval is a capability, not a flag.** The gateway mints a single-use
`ExecutionToken`; `place_order` consumes it before doing anything else. A caller
without gateway-minted authority cannot reach a venue at all, which is what makes
INVARIANT 3 structural.

**Asymmetric authority in the permission matrix.** `Action` has exactly ten
members and there is **no `AMEND_ORDER`**. No role holds both `CANCEL_ORDER` and
`PROPOSE_ORDER`: `STRATEGY` proposes, `OPERATOR` and `EXECUTION_GATEWAY` cancel.
A cancel/replace therefore requires **two principals**. Granting one role both
permissions would make autonomous re-pricing possible — that is a permission
matrix change, and it must not be made silently.

**The idempotency key's state machine is asymmetric.** `release_unsent` is legal
only from RESERVED, i.e. only when nothing left the process. Once SUBMITTED, a
key can never be freed — it settles or goes UNKNOWN. `SETTLED → frozenset()`:
a settled reservation has no successor at all. `UNKNOWN → frozenset()` too; the
only exit is the explicitly named `resolve_unknown`, so reconciliation is
greppable in the audit log.

**Latching.** A detected mismatch stays latched until an operator clears it. The
system does not un-notice a problem because the next poll looked fine.

**Fail closed.** Every ambiguous answer resolves toward refusal. Notably: the
paper venue never returns UNCERTAIN and never raises, because a local arithmetic
problem escalating to a system-wide UNKNOWN block would be a self-inflicted
outage.

**Stage 2F, specifically:**
- *No fee field.* The fill price is the only number the cost basis — and
  therefore the daily-loss limit — ever reads. A separate fee field would be
  money no risk control can see, which is worse than no fee model. Use
  `slippage_bps`.
- *`slippage_bps >= 10_000` is refused at construction.* At 100% a sell price
  goes to zero, `Price` raises, `place_order` throws, and the gateway reads that
  as UNKNOWN — a config typo escalating into a system-wide block. Caught where
  it is a configuration error instead.
- *Depth asset mismatch is checked before `min(available, ordered)`*, for the
  same reason.
- *A partially-filled order keeps its FILLED ack record on cancel*, so
  `fetch_order_state` cannot forget that quantity changed hands.
- *`fetch_order_state` is a read with an effect* (2G increment 3). It re-decides
  a resting order, so it can move the venue's position. Two boundaries follow
  from that and are easy to trip over: `fetch_positions` does **not** re-decide
  anything, so a reconciliation sweep reading the venue's book cannot fill an
  order by looking; and `gateway.cancel`'s post-cancel fetch cannot either,
  because `cancel_order` has already popped the resting record by then.

**The `Advice → OrderIntent` bridge deliberately does not exist.** The advisory
layer holds no `OrderIntent` at all. That is what makes advisory mode
structurally non-executing rather than conventionally non-executing. Do not add
that bridge as a convenience.

**Coverage uses stdlib `trace`,** not `coverage.py`, because there are no
third-party dependencies. The script is embedded verbatim in
`docs/ARCHITECTURE.md` §7. Two gotchas: discovery and import must happen *inside*
the traced function, and `trace` does **not** honour `# pragma: no cover`.

---

## 7. Known limitations and deferred work

`docs/SAFETY.md` §5 is the authoritative list of honest limitations. Read it. In
addition, these are verified gaps in the current code — each was confirmed by
reading the source at `3664991`, and each is Stage 2G territory. Items 1, 2, 3
and 8 have since been **closed**; they are kept here, struck through, because
the reasoning in each is what the fix had to satisfy.

1. ~~**`gateway.cancel()` never changes the order state.**~~ **Closed by
   `26625d1`.** It authorized, called `broker.cancel_order`, and audited — but
   the `Order` stayed ACCEPTED/PARTIALLY_FILLED, so it kept consuming the
   `max_open_orders` budget and reconciliation still believed it was live.
   `cancel` now reads the venue's state afterwards and settles the order.
2. ~~**No path exists for a later fill to reach the portfolio.**~~ **Closed by
   `ExecutionGateway.sync_order` plus a re-evaluating
   `PaperBroker.fetch_order_state`.** The only route from a venue-observed fill
   into the portfolio used to be operator `resolve_unknown`, which requires the
   order to be UNKNOWN. A resting paper limit the market later reaches now fills
   on the next poll and reaches the portfolio, the cost basis and the daily-loss
   ledger through `sync_order`. The scheduling half is closed too:
   `LifecyclePoller` now calls it on an interval — see §3 increment 4.
3. ~~**Latent double-booking in `resolve_unknown`.**~~ **Closed by `c5ff7a5`.**
   It applied `ack.filled_quantity` as an *increment*, so an order with prior
   fills that later resolved would have booked them twice. Both venue-state
   paths now go through `_apply_fetched_state`, which treats the venue's answer
   as a cumulative snapshot and applies only the delta — which is also what
   makes a repeated `sync_order` free.
4. **No amend / replace.** `OrderIntent` is frozen and content-addressed, so a
   changed quantity is a different idempotency key and therefore a different
   order. Cancel-then-resubmit through the full chain is the only path the
   architecture permits.
5. **`OrderState.EXPIRED` is in the transition table but unreachable.** Grep
   confirms `EXPIRED` appears only in `orders.py`. Nothing in the `AckOutcome`
   vocabulary reports expiry, and time-in-force is not modelled.
6. **`AckOutcome` has only ACCEPTED / REJECTED / FILLED / UNCERTAIN.** There is
   no CANCELED and no EXPIRED. Any lifecycle work has to interpret the four it
   has, or argue for extending the port.
7. **`Order.remaining_quantity` has no production consumers** outside its own
   definition. `OrderStore.open_orders()` now has two: `trading/core/risk.py:686`
   counts them against `max_open_orders`, and
   `trading/adapters/lifecycle.py:186` sweeps them. Note what that second one
   means — `is_open` is now load-bearing for *safety*, not just for a limit: it
   is the single reason the poller cannot reach a terminal or `UNKNOWN` order.
   Widening it would silently widen what the poller touches.
8. ~~**`gateway._lock` is a plain non-reentrant `threading.Lock`** that `cancel`
   and `resolve_unknown` do not take.~~ **Closed.** All four public entry
   points — `submit`, `cancel`, `sync_order`, `resolve_unknown` — now take it,
   each via a thin `with self._lock:` wrapper around a `_*_locked` body, which
   is what keeps the non-reentrant lock from being taken twice on one path.
   Every path that can move the portfolio is serialised against every other,
   so a `submit` weighing its limits cannot read a position mid-change.
9. **Durability is now partial, and honest about which half.** *(Narrowed
   again.)* The kernel owns none of its state — orders, positions, and
   reservations are injected repositories. Two of the three now have **crash-safe
   on-disk adapters** in `trading/adapters/persistence/durable.py`:
   `DurableReservationRepository` and `DurablePositionLedger`, stdlib-only, each
   writing its whole collection with an atomic `os.replace` and failing *loud*
   (never silently empty) on a corrupt or unrecognised file. They are **opt-in**:
   the default wiring (`build_rig`, the e2e fixtures) stays in-memory, so nothing
   changes for existing code until an operator wires a path in.
   `tests/test_durable_persistence.py` proves — with real files and a genuine
   rebuild-from-disk, not a shared-object rebuild — that an `UNKNOWN` reservation
   (INVARIANT 5), a claimed key (INVARIANT 12), and the position ledger
   (INVARIANT 6 baseline) all survive a true process exit.

   **Orders are deliberately still not durable**, and that is the remaining gap.
   The gateway persists an order once with `orders.add(order)` at `DRAFT`, then
   advances it *in place* without calling back into the store, so a durable order
   store would capture only the `DRAFT` snapshot. Closing it needs either a
   gateway `update(order)` call at the write-before-send checkpoint or an
   on-mutation observer on `Order` — both change the execution chokepoint or the
   core order object, so both are **your call**, not a persistence-layer
   decision. Activating durable orders would let the recovery *scan* reclassify
   ambiguous orders after a real restart (today it reads an empty order store and
   would call every surviving venue order a ghost); the cost is a write on the
   hot submit path and a serialize/restore contract on `Order` that must not
   become a way to reconstruct an order in a state the transition table forbids.

   One consequence had to be handled for the partial state to be *coherent*: with
   durable reservations but non-durable orders, a restart leaves an `UNKNOWN`
   reservation whose order object is gone. `require_clean` sources its UNKNOWN
   block from the *order* store, which is now empty, so on its own it would let a
   new order through. The gateway therefore gained a fail-closed backstop,
   `_require_no_unknown_reservation`, called immediately after `require_clean`:
   if `dedupe.has_unknown()` it refuses at the RECONCILIATION gate. It can only
   *add* a refusal, never remove one; in the in-memory default it never fires
   before `require_clean` already has (order and reservation go UNKNOWN
   together); and it is what makes INVARIANT 5 survive a real restart. The
   operational monitor got the matching optional `dedupe` dependency so its
   `ready` predicate does not read `True` off an empty order store while the
   gateway is refusing — a `BlockedReason.UNKNOWN_RESERVATION`.
10. **The paper fill is an optimistic estimate of a live fill, always.** No
    fees, no market impact (`depth` is per-placement, not a depleting pool), no
    latency, no queue position, no uncertainty. A resting order is re-evaluated
    only when someone asks about it, and since `LifecyclePoller` is now what
    asks, "how long it waited" is a statement about the poll interval rather
    than about the market: fill timing is poll timing. That is a limitation of
    the approach and it is why the broker-sandbox step exists in the
    progression.
11. ~~**`persistence.OrderStore` is not an adapter — it is the kernel's own
    class.**~~ **Closed.** *(Found by coverage during Stage 2H Phase 5.)*
    `trading/adapters/persistence/order_store.py` imported
    `trading.core.orders.OrderStore` at module scope and then defined
    `OrderStoreAdapter` below it, and `__init__.py` re-exported the name
    unaliased, so it picked up the **import**, not the adapter. The verification
    command printed `True`:

    ```bash
    python3 -c "import trading.adapters.persistence as p, trading.core.orders as c; print(p.OrderStore is c.OrderStore)"
    ```

    It now prints `False`. The fix took the second of the two options this
    document recommended against — export the adapter — and the recommendation
    here was wrong for a reason worth recording: the argument for deleting the
    adapter was that nothing used `update()`, but the *architecture* argument
    runs the other way. The whole point of Stage 2H is that the kernel is
    injected with a *seam*, and injecting the kernel's own class through it
    leaves the seam decorative. So:

    - `order_store.py` imports the core class as `CoreOrderStore` and exports
      `OrderStoreAdapter`.
    - `__init__.py` sets `OrderStore = OrderStoreAdapter`, so existing callers
      keep working while the name now resolves to the adapter.
    - The wrapped-store constructor aliases `_lock` alongside `_by_id` and
      `_by_key`. It previously aliased the dicts but not the lock, so two
      wrappers around one store would have mutated shared dicts under different
      locks.
    - `tests/test_persistence.py` now pins the export identity, the
      adapter-is-not-the-core-class claim, the shared dicts **and** shared
      `_lock`, `update()`, and a missing-update `KeyError`. The file is at 100%
      coverage.
12. ~~**`sync_order` leaves its reservation in `SUBMITTED` after the order
    reaches a terminal state.**~~ **Closed.** *(Stage 2H Phase 5 finding.)*
    Every other path closed its key out: `_settle` calls `mark_settled`, and
    `_resolve_unknown_locked` calls the registry's `resolve_unknown`, which is
    the only legal exit from `UNKNOWN` (the transition table forbids
    `UNKNOWN -> SETTLED`, deliberately, so the exit is greppable). `_sync_locked`
    called neither, so an order a poll or an operator sync discovered to be
    `FILLED` or `REJECTED` ended with a terminal order and a still-`SUBMITTED`
    key, and `in_flight()` reported finished work as live.

    The fix is `ExecutionGateway._settle_reservation_if_terminal`, called at the
    end of both `_sync_locked` and `_cancel_locked`. It settles the order's
    reservation only when the order is terminal *and* the reservation is
    `RESERVED`/`SUBMITTED`. It is deliberately conservative:

    - **It does not weaken INVARIANT 12.** `SETTLED` is exactly as un-reusable as
      `SUBMITTED`: `reserve()` refuses a key that has a reservation in *any*
      state, and `SETTLED` has no successor in the transition table, so the key
      can never be freed for a duplicate. `SUBMITTED -> SETTLED` is a truthful
      relabelling of a key that is already permanently claimed; it frees nothing
      and permits no retry.
    - **It never touches UNKNOWN.** An UNKNOWN *order* is not terminal, so the
      first guard returns; and an UNKNOWN *reservation* is skipped explicitly, so
      leaving UNKNOWN remains `resolve_unknown`'s job (which also keeps the
      transition table legal, since `UNKNOWN -> SETTLED` is forbidden).
    - **It is idempotent.** `_advance` returns early when the target state equals
      the current one, so calling it on an already-`SETTLED` key is a no-op.

    `tests/test_restart_recovery_e2e.py` proves both paths: a crashed
    `PENDING_NEW` order recovered through `sync_order` settles its key and is
    still refused as a duplicate, and the same holds for a recovered order
    cancelled instead — where the reservation is asserted `SUBMITTED` *before*
    the cancel, so the settlement can only have come from the cancel path.
13. ~~**`RestartRecoveryCoordinator` reads `gateway._dedupe`.**~~ **Closed.** It
    needed reservation state to classify a `PENDING_NEW` order as ambiguous, and
    took it off the gateway's private attribute. It now reads
    `self._reservations.get(order.idempotency_key)` — the injected
    `ReservationRepositoryPort`, which is the same store the gateway's
    `IdempotencyRegistry` writes through. This is the same shared-instance
    contract the coordinator already relies on for `orders`, so a scan reads
    reservation state through the port instead of reaching into gateway
    internals, and no adapter under `trading/adapters/` accesses a gateway
    private attribute any more. Safety semantics are unchanged: an ambiguous
    `PENDING_NEW` + `SUBMITTED` order is still detected, and an UNKNOWN order is
    still reported (that branch keys off `order.state` and never reads a
    reservation).

### New in 2I–2K: limitations that are properties, not defects

14. **The sandbox adapter has never spoken to a venue.** `SandboxBroker` is a
    translation layer with an injected transport; no implementation of
    `SandboxTransport` in this repository performs I/O. Before a real exchange
    adapter can be written, all of the following must be supplied and
    independently verified: an official **sandbox** base URL, the supported
    products and account type, the authentication and request-signing
    specification, request/response schemas, an order-list endpoint (without one
    the venue cannot be reconciled in full — see 15), the exact status vocabulary
    and fill semantics, cancellation semantics, rate limits, and sandbox
    credentials. **No CoinSwitch sandbox endpoint or credential provisioning has
    been established.** Do not infer one from the public documentation, and do
    not point this adapter at a production host.
15. **A venue with no order-list endpoint cannot be fully reconciled, and the
    system says so rather than pretending.** `BrokerOrderInventoryPort` is
    separate from `BrokerPort` precisely so this is a checkable fact: the sweep
    emits one blocking `INVENTORY_UNAVAILABLE` finding, the monitor reports the
    system un-ready, and startup reports a blocked stage. A partial sweep that
    looked clean would be worse than none, because it manufactures exactly the
    confidence `ReconciliationGate.require_clean` exists to require.
16. **Reconciliation findings are reported, not repaired.** A missing order, a
    ghost order, an identity contradiction and a fill regression all block. The
    coordinator never adopts the venue's view, never retries a submission, and
    never resolves an `UNKNOWN`. The operator routes are
    `ReconciliationGate.clear_mismatch` and `adopt_broker_positions`, both of
    which require a reason and re-verify against a fresh snapshot.
17. **`OperationalMonitor` is a summary, not an enforcement point.** Every
    condition in its `ready` predicate is already enforced by the component that
    owns it. The monitor has no execution surface by construction, and
    `tests/test_operations.py::TestItCannotAct` asserts that absence — including
    `test_it_is_not_a_second_gate`, which shows the monitor reporting un-ready
    while the *gateway* is what refuses the order. There is no HTTP surface; the
    architecture has no inbound adapter layer, and adding one would put a network
    boundary on a system whose safety story is that it has none.
18. **Order durability is the remaining gap; reservations and positions are
    durable.** Superseding the earlier "no durability, still" note: item 9 now
    describes the split. Reservations and positions have crash-safe on-disk
    adapters; **orders do not**, because the gateway advances them in place with
    no store callback, so a durable order store would capture only the `DRAFT`
    snapshot. Until that is closed (an execution-path decision for the operator),
    the reconciliation sweep after a *real* restart reads an empty local order book
    and would report every venue order as a ghost — which is why the sweep is
    fail-*loud* about it rather than silently adopting the venue's book. The
    UNKNOWN block itself does survive a real restart regardless, via the durable
    reservation and the gateway's reservation-level backstop.

### One documentation defect, now fixed

`docs/SAFETY.md` §7 rule 6 said **"1 155 tests in ~4 s"** when this handoff was
written, and the real figure was 1462. It was reported rather than silently
corrected, because that handoff touched no production code or other docs. Stage
2G edits `SAFETY.md` legitimately, so the number is now current there.

---

## 8. Tests and coverage

Re-verified after Stages 2G increments 1–4 and all of 2H, 2I, 2J, 2K and the
2I–2K review pass (the sync/cancel reservation settlement, the recovery-port
read, and the persistence export regression test).

```bash
python3 -m unittest discover -s tests -t .
```

Re-verified after Stages 2G–2K, the 2I–2K review pass, and the durable-
persistence pass (durable reservation/position adapters, the gateway's
reservation-level UNKNOWN backstop, and the monitor's matching readiness check).

```bash
python3 -m unittest discover -s tests -t .
```

Result: **`Ran 1875 tests in 2.512s` / `OK`.**

Coverage, via the project's stdlib-`trace` script (the one embedded in
`docs/ARCHITECTURE.md` §7 — use it, not an ad-hoc approximation):

**TOTAL: 96.5% — 7989 statements, 277 missed.**

| Package | Statements | Missed |
|---|---|---|
| `trading/adapters` | 2094 | 51 |
| `trading/advisory` | 397 | 2 |
| `trading/core` | 4518 | 185 |
| `trading/ports` | 241 | 28 |
| `trading/strategy` | 737 | 11 |
| `trading/__init__.py` | 2 | 0 |

The durable-persistence pass added `adapters/persistence/durable.py` at 98.7%
(the only miss is a `# pragma: no cover` directory-`fsync` fallback), with
`tests/test_durable_persistence.py` (26 tests) covering the on-disk round trip,
the real restart, and every fail-loud branch. `adapters/operations.py` is 99.4%,
`adapters/recovery.py` 100.0%, `adapters/persistence/order_store.py` 100.0%.

Least-covered files: `ports/broker.py` 81.6% (the new inventory port's
abstract-method body and `BrokerOrderSnapshot` validation guards, unreachable-by-
design in the same way `ports/repository.py`'s are), `ports/repository.py` 85.0%,
`core/config.py` 86.5%, `adapters/sandbox/broker.py` 91.4%, `core/money.py`
92.2%, `core/secrets.py` 92.2%, `core/sizing.py` 92.6%, `strategy/context.py`
93.6%, `adapters/reconciliation.py` 94.4%, `core/dedupe.py` 94.8%. `core/gateway.py`
is at 97.0%; its misses are defensive refusal branches.

Nine of `adapters/lifecycle.py`'s misses are `_run`, and they are a **tooling
artefact, not dead code**: `trace.Trace` does not follow threads it did not
start, so the poller's worker body reads as missed. The same measurement with
`threading.settrace(tracer.globaltrace)` installed reports that file at 100.0%.
Do not "fix" those lines. See ARCHITECTURE.md §7.

At 100%: `adapters/memory/market_data.py`, `adapters/memory/quote_feed.py`,
`adapters/paper/`, `adapters/persistence/order_store.py`,
`adapters/persistence/reservation_repository.py`, `ports/recovery.py`,
`ports/reservation_repository.py`, `strategy/`, plus `core/clock.py`,
`core/errors.py`, `core/marketdata.py`, `core/portfolio.py`.

Size: 38 `test_*.py` files under `tests/`, plus `harness.py` and `__init__.py`
(40 Python files there); 52 production `.py` files under `trading/`, totalling
14616 lines. Largest: `core/gateway.py` 1139, `core/risk.py` 752,
`advisory/advisor.py` 707, `core/orders.py` 687, `adapters/sandbox/broker.py`
652, `adapters/operations.py` 604, `adapters/reconciliation.py` 584,
`core/money.py` 555, `core/marketdata.py` 511, `adapters/paper/broker.py` 511,
`core/portfolio.py` 493.

`docs/ARCHITECTURE.md` §3 and §7 currently state these same figures and are
accurate. Keep them accurate.

---

## 9. Git working-tree status

Branch `stage-1-completion` at `519406b`; `main` at `ed024c2`. **Dirty**, and
deliberately so — Stage 2G increments 3–4, Stages 2H–2K, the 2I–2K review pass,
and the durable-persistence pass are implemented and tested but not yet
committed, pending the operator's word:

```
$ git status --short
 M docs/ARCHITECTURE.md
 M docs/CLAUDE_HANDOFF.md
 M docs/SAFETY.md
 M tests/test_core_purity.py
 M tests/test_dedupe_reconciliation.py
 M tests/test_paper_broker.py
 M tests/test_ports.py
 M trading/adapters/__init__.py
 M trading/adapters/memory/broker.py
 M trading/adapters/paper/broker.py
 M trading/core/dedupe.py
 M trading/core/gateway.py
 M trading/ports/__init__.py
 M trading/ports/broker.py
?? tests/test_durable_persistence.py
?? tests/test_gateway_persistence.py
?? tests/test_lifecycle_poller.py
?? tests/test_operations.py
?? tests/test_persistence.py
?? tests/test_reconciliation_sweep.py
?? tests/test_restart_recovery.py
?? tests/test_restart_recovery_e2e.py
?? tests/test_sandbox_broker.py
?? trading/adapters/lifecycle.py
?? trading/adapters/operations.py
?? trading/adapters/persistence/
?? trading/adapters/reconciliation.py
?? trading/adapters/recovery.py
?? trading/adapters/sandbox/
?? trading/ports/recovery.py
?? trading/ports/reservation_repository.py
```

The suite is green at this state (`Ran 1875 tests` / `OK`), so the tree is
consistent, not half-edited. No stashes in play, no untracked files besides
those listed and OS cruft covered by `.gitignore` (which aggressively ignores
anything credential-shaped — `.env`, `*.key`, `*secret*`, `*token*`,
`*credentials*`, and more; do not fight it, and never commit anything that could
authenticate against a real exchange).

**A remote does exist, and some history has been pushed to it.** `origin` is
`https://github.com/jaitak2121-spec/Trading-Agent.git`, and `origin/stage-1`
holds this project's history through `c5ff7a5` plus a README-only commit
`e5103ba` that exists nowhere locally. The local branch also still tracks a
deleted `origin/stage-1-completion` (`git branch -vv` prints `gone`). An earlier
revision of this document asserted the opposite — that nothing had ever been
pushed — and that was false; it is corrected here rather than quietly dropped,
because a successor trusting it could have pushed believing the remote was
unobserved.

The standing instruction is unchanged and is about the future, not the past:
**do not push.** Nothing goes to a remote unless the operator asks for it in
those words.

---

## 10. Read this before you modify anything

1. **Read `docs/SAFETY.md` §7 ("Changing safety code") first.** It is six rules
   and it governs the rest.
2. **Inspect before you write.** Every earlier stage lost time to invented APIs.
   Concrete traps confirmed in this codebase:
   - There is **no `EUR`**. Currency constants are `USD`, `INR`, `USDT`, `BTC`.
   - `PnlLedger` exposes `.realized` and `.realized_loss` — **not**
     `.realized_today`.
   - `RiskEngine.remaining_loss_budget` is a **property**, not a method, and it
     can be `None`.
   - `Position.average_entry_price`, not `average_cost`.
   - `KillSwitch.engage(principal, *, reason)` — the principal is **positional**,
     not `actor=`.
   - `Quantity` renders 8 decimals, so a `"0.5"` expectation must be written
     `"0.50000000"`.
   - `SizingConstraint.RISK_FRACTION` is the member name;
     `'risk_fraction_per_trade'` is its value.
3. **Use `tests/harness.py`.** `build_rig(*, mode=TradingMode.PAPER,
   default_outcome=AckOutcome.FILLED, live_authorized=False, risk=None,
   max_staleness_seconds=300.0, token_ttl_seconds=30, clock=None, broker=None)`
   wires the entire system around one `ManualClock`, so tests move time
   explicitly and never sleep. `clock=` and `broker=` exist because a venue with
   its own quote feed must share the system clock: build the clock, build the
   venue, hand in both. Constants: `DEFAULT_PRICE = Price("50000", USD)`,
   `DEFAULT_QUANTITY = Quantity("0.001", "BTC")`, `SYMBOL = "BTCUSD"`,
   `ASSET = "BTC"`. The portfolio starts at `Money("1000000.00", USD)`. Four
   distinct principals — `strategy-1`, `risk-1`, `gateway-1`, `operator-1` —
   because several invariants are precisely about one of them being unable to do
   another's job. Do not collapse them.
4. **Two brokers exist and they have opposite jobs.**
   `adapters/memory/SimulatedBroker` is hostile — `script(ack,
   lands_at_venue=False)`, `raise_on_next()`, `set_venue_position()` — and it
   exists to prove the system survives a venue that lies. `adapters/paper/
   PaperBroker` is honest and deterministic. Test new behaviour against both.
5. **Test behaviour and safety-critical invariants**, not every trivial line.
   High-value integration and end-to-end tests, plus unit tests for core risk
   and safety logic. Run the focused tests first, then the full suite. Measure
   coverage with the project's tooling. **Investigate every new failure rather
   than weakening or deleting the test.**
6. **Working style the operator has asked for:** work incrementally; keep the
   diff minimal and production-quality; do not touch unrelated files; do not
   rewrite completed modules for style; do not skip ahead to the next stage; do
   not stop to ask for status confirmation when the next task is clear; review
   the complete diff for accidental changes before committing; run the full
   suite one final time; commit only after implementation, focused tests, full
   suite, coverage and documentation are all verified.
7. **Update `docs/ARCHITECTURE.md` and `docs/SAFETY.md` only where the
   implementation requires it,** and keep the documented test counts and
   coverage figures true.

---

## Appendix — Stage 2G design notes (proposal, mostly now implemented)

Everything below is reasoning developed in a prior session while inspecting the
code, before any of Stage 2G was built. **It is a proposal, not a description.**
Increments 1–4 have since implemented the cumulative-snapshot/delta bullet, the
notional back-computation, the defensive-refusal list, the cancel sequence, and
the UNCERTAIN/UNKNOWN rule — but not necessarily in the form written here, and
`trading/core/lifecycle.py` was never created (the interpretation lives in
`gateway.py`, and `trading/adapters/lifecycle.py` is the unrelated poller). Amend
/replace and `EXPIRED` remain untouched. Re-derive any bullet against the source
before building on it, and discard any part the code contradicts. The verified
*facts* it rests on are all in §7 above.

- **Read `fetch_order_state` as a cumulative snapshot** — the natural reading of
  a venue `GET /order/{id}` — and have the lifecycle layer apply only the
  **delta** between the venue's cumulative filled quantity and what the order
  has already booked. This makes repeated and duplicate lifecycle events safe
  *by construction*: a re-poll yields a zero delta, which is a no-op. The
  alternative, a fill-id registry, is not expressible in `BrokerAck` without
  adding a port field.
- **Back-compute the delta's price from notionals**
  (`(cumulative_notional − booked_notional) / delta_qty`). On a cumulative
  snapshot the ack's `fill_price` is an *average*; booking that average against
  the delta would misstate the cost basis, which feeds the daily-loss limit.
  `Order._notional_total` already holds the exact booked figure, so the delta
  arithmetic belongs on `Order`, under its own lock.
- **Ack interpretation belongs in a new pure `trading/core/lifecycle.py`,** not
  in `orders.py` — `orders.py` must not acquire a `trading.ports.broker` import.
  `ExecutionGateway` keeps the sole broker reference.
- **Refuse defensively; do not silently repair.** A regressed cumulative
  quantity, an overfill, a currency or asset change, a differing
  `broker_order_id`, a venue disowning an order we have booked fills against, and
  a fill arriving against a terminal order should each raise `SafetyViolation`
  (audited first, per INVARIANT 13) and leave the order untouched.
- **Cancel should be:** request the cancellation, read the venue's final state to
  book any fill that raced it, then declare CANCELED only if the order is still
  open. Ignore a "no record" answer during cancel — of course there is no record,
  we just cancelled it. A venue REJECTED answer to `cancel_order` must not change
  state.
- **Amend = cancel/replace through the full chain, with two principals** (see
  §6 on asymmetric authority). Refuse unless the old order reached a terminal
  state, and refuse an identical replacement intent.
- **A lifecycle sync that receives UNCERTAIN must mark only the *order*
  UNKNOWN** — never call `dedupe.mark_unknown`, because the reservation is
  already SETTLED and `SETTLED → frozenset()` would raise. `require_clean`
  consults `orders.unknown_orders()` first, so marking the order is sufficient to
  block the system. Correspondingly, `gateway.resolve_unknown` needs a guard so
  it only calls `dedupe.resolve_unknown` when the reservation really is UNKNOWN.
- **Leave `EXPIRED` unreachable** and document it. An operator declaring expiry
  without venue confirmation is exactly the "assume it's dead" shortcut this
  architecture refuses.
