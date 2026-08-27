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
trading.adapters <-- concrete implementations (memory/, paper/)
trading.strategy <-- signal generation; proposes, never executes
trading.advisory <-- leaf. Nothing under trading/ imports it back.
```

`trading/core` and `trading/ports` are a **mechanically enforced** pure kernel:
`tests/test_core_purity.py` (47 tests) walks the import graph with `ast` and
also runs fresh-interpreter subprocess probes, so a forbidden import fails the
suite rather than merely violating a convention. This is not a style rule. Do
not add an import to `core/` or `ports/` without reading that test file first.

### The chokepoint

`ExecutionGateway.submit` (`trading/core/gateway.py`, 684 lines) is the single
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

Ten commits, oldest last. Branch `main` sits at `ed024c2`; all Stage 1 and
Stage 2 work is on `stage-1-completion`.

| Commit | Date | Stage |
|---|---|---|
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

**Stage 2F is the most recent completed work.** It added `trading/adapters/paper/`
— a `PaperBroker` (401 lines) that is the honest counterpart to the deliberately
hostile `SimulatedBroker` in `trading/adapters/memory/`. It fills against a quote
feed with no randomness anywhere: a buy lifts the ask and a sell hits the bid,
`slippage_bps` moves the fill *against* the order in both directions, `depth`
caps what one placement can take (which is how partial fills first became
producible in this repository), a non-crossing limit rests, and a missing or
stale quote is a refusal rather than a guess.

**Stage 2G is next and has not been started.** Verified by grep: no `amend`,
`replace`, `sync`, or `lifecycle` production code exists anywhere under
`trading/`. Nothing is half-finished; there is no work-in-progress to pick up.

---

## 4. Remaining stages

> **Source note.** This ordered list comes from the operator's work orders given
> in conversation. It is **not recorded anywhere in the repository or Git
> history.** `docs/ARCHITECTURE.md` §8 ("Where Stage 2 attaches") describes the
> seams but does not enumerate stages. Treat the list as the plan of record and
> confirm scope with the operator if anything looks ambiguous.

Stages 2A–2F are done (§2 above). Remaining, in order:

- **2G — Order lifecycle.** ← next. Correct state transitions; partial fills;
  cancellation semantics; amendment/replacement *where the existing architecture
  permits it*; keeping filled quantity, remaining quantity, average execution
  price, portfolio state and audit state mutually consistent; repeated and
  duplicate lifecycle events safe and deterministic; invalid, stale,
  contradictory and impossible events handled defensively. **Explicitly out of
  scope: persistence, network/broker code, live trading, unrelated refactoring.**
- **2H — Persistence and restart recovery.** State survives process restart.
  `trading/ports/repository.py` already exists as the seam (85.0% covered — the
  least-exercised file in the project).
- **2I — Reconciliation.** Beyond the existing gate: periodic position and order
  reconciliation against a venue.
- **2J — Broker adapter interface.** A real adapter behind `BrokerPort`,
  deliberately isolated. Sandbox only.
- **2K — Monitoring, audit, operational safety.** Operator-facing surfaces.
- **2L — Tests for all critical paths.** Final sweep.

Live-trading preparation must, across these stages, address: duplicate-order
prevention, stale market data, broker/API timeouts, unknown order outcomes,
partial fills, rejections, disconnect/reconnect, process restart, position
mismatch, rate limits, kill switch, maximum order/position/notional/loss limits,
and auditability.

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
reading the source at `3664991`, and each is Stage 2G territory:

1. **`gateway.cancel()` never changes the order state**
   (`trading/core/gateway.py:610`). It authorizes, calls
   `broker.cancel_order`, and audits — but the `Order` stays
   ACCEPTED/PARTIALLY_FILLED. So it remains `is_open`, keeps consuming the
   `max_open_orders` budget at `trading/core/risk.py:686`, and reconciliation
   still believes it is live.
2. **No path exists for a later fill to reach the portfolio.** The only route
   from a venue-observed fill into the portfolio is operator `resolve_unknown`,
   which requires the order to be UNKNOWN. A resting limit order cannot become a
   fill. `docs/ARCHITECTURE.md` §8 names this as the Stage 2G seam and says
   plainly that driving a resting order to a fill belongs in core, not in an
   adapter.
3. **Latent double-booking in `resolve_unknown`** (`trading/core/gateway.py:654`).
   It applies `ack.filled_quantity` as an *increment*. `PARTIALLY_FILLED →
   UNKNOWN` is a legal transition, so an order with prior fills that later
   resolved would book the earlier fills twice. **Not reachable today** — the
   only route to UNKNOWN is from PENDING_NEW inside `_execute` — but it becomes
   reachable the moment a lifecycle sync can mark an open order UNKNOWN. Fix it
   in the same change that introduces that capability.
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
   definition. `OrderStore.open_orders()` is consumed at exactly one place,
   `trading/core/risk.py:686`.
8. **`gateway._lock` is a plain non-reentrant `threading.Lock`.** `submit` takes
   it; `cancel` and `resolve_unknown` do **not**. Any new lifecycle entry point
   needs a deliberate decision here, and must not deadlock against `submit`.
9. **No persistence.** Everything is in-memory; a restart loses all state.
   Stage 2H.
10. **The paper fill is an optimistic estimate of a live fill, always.** No
    fees, no market impact (`depth` is per-placement, not a depleting pool), no
    latency, no queue position, no uncertainty. That is a limitation of the
    approach and it is why the broker-sandbox step exists in the progression.

### One documentation defect, left unfixed on purpose

`docs/SAFETY.md` §7 rule 6 says **"1 155 tests in ~4 s"**. The real figure is
1462 tests in ~3 s. This handoff does not touch production code or other docs,
so the stale number is reported rather than silently corrected. Fix it in the
next stage that legitimately edits `SAFETY.md`.

---

## 8. Tests and coverage

Verified at `3664991` on 2026-08-25.

```bash
python3 -m unittest discover -s tests -t .
```

Result: **`Ran 1462 tests in 3.335s` / `OK`.**

Coverage, via the project's stdlib-`trace` script (the one embedded in
`docs/ARCHITECTURE.md` §7 — use it, not an ad-hoc approximation):

**TOTAL: 96.7% — 5992 statements, 199 missed.**

| Package | Statements | Missed |
|---|---|---|
| `trading/adapters` | 503 | 0 |
| `trading/advisory` | 397 | 2 |
| `trading/core` | 4216 | 172 |
| `trading/ports` | 137 | 14 |
| `trading/strategy` | 737 | 11 |
| `trading/__init__.py` | 2 | 0 |

Least-covered files: `core/config.py` 86.5%, `ports/repository.py` 85.0%,
`ports/broker.py` 88.9%, `core/money.py` 92.2%, `core/secrets.py` 92.2%,
`core/sizing.py` 92.6%, `strategy/context.py` 93.6%, `core/gateway.py` 95.6%.

At 100%: all of `adapters/`, plus `core/clock.py`, `core/errors.py`,
`core/marketdata.py`, `core/portfolio.py`, `strategy/base.py`,
`strategy/indicators.py`, `strategy/sizing.py`.

Size: 26 `test_*.py` files under `tests/`, plus `harness.py` and `__init__.py`
(28 tracked Python files there); 40 production `.py` files under `trading/`,
totalling 10907 lines. Largest: `core/risk.py` 752, `advisory/advisor.py` 707,
`core/gateway.py` 684, `core/orders.py` 589, `core/money.py` 555,
`core/marketdata.py` 511, `core/portfolio.py` 493, `core/config.py` 456.

`docs/ARCHITECTURE.md` §3 and §7 currently state these same figures and are
accurate. Keep them accurate.

---

## 9. Git working-tree status

At the time of writing, immediately before this file was created:

```
$ git status --short
(empty)
```

Clean. Branch `stage-1-completion` at `3664991`; `main` at `ed024c2`. No
stashes in play, no untracked files besides OS cruft covered by `.gitignore`
(which aggressively ignores anything credential-shaped — `.env`, `*.key`,
`*secret*`, `*token*`, `*credentials*`, and more; do not fight it, and never
commit anything that could authenticate against a real exchange).

The only change accompanying this handoff is this file itself, committed on its
own as a documentation commit.

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

## Appendix — Stage 2G design notes (proposal, NOT implemented)

Everything below is reasoning developed in a prior session while inspecting the
code. **None of it exists in the repository.** It is recorded here so the
analysis is not lost, and it is explicitly not a commitment: re-derive it
against the source before building on it, and discard any part the code
contradicts. The verified *facts* it rests on are all in §7 above.

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
