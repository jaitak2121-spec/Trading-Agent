# Architecture

A modular monolith. This document describes what exists on disk today, not a
plan. Where something is deliberately absent, it says so.

Stage 1 (the safety kernel) is complete. Stage 2 (the trading platform on top of
it) is in progress; sections marked **Stage 2** grow as subsystems land, and the
counts in §3 and §7 are re-measured at the end of the stage rather than after
every batch.

Referenced from `trading/__init__.py`.

---

## 1. Scope of Stage 1

Stage 1 is **the dependency-independent safety core and nothing else**. The
whole package imports only the Python standard library. There is:

- no network I/O — no HTTP client, no socket, no exchange client
- no database — no PostgreSQL, no ORM, no migration
- no web framework — no FastAPI, no React, no HTTP route
- no third-party dependency of any kind, including in the test suite
- no live trading; `TradingMode.LIVE` exists and is guarded, but no adapter
  behind it talks to a real venue

That is a constraint, not an accident. Every one of those additions is a way
for behaviour to change under the safety core's feet. Building the core first,
and proving it holds while nothing else is present, means a later stage can
attach FastAPI or PostgreSQL at a named seam without any question of whether
the safety properties still hold.

`tests/test_core_purity.py` enforces this mechanically rather than by
convention — see §4.

**What Stage 1 does ship:** the full safety chain, exercised end to end against
an in-process venue that misbehaves on purpose. Thirteen numbered invariants,
each enforced in code and asserted in tests. Stdlib `unittest` only,
95%+ statement coverage.

---

## 2. Layers and the dependency arrow

Four layers. The arrow points one way, always inward.

```
                    ┌─────────────────────────────────────────┐
                    │        trading.strategy                 │
                    │   decides WHAT to trade,                │
                    │   never WHETHER it may                  │
                    └──────────────────┬──────────────────────┘
                                       │ returns inert OrderIntents
                                       ▼
   ┌───────────────────────────────────────────────────────────────────┐
   │                        THE PURE KERNEL                            │
   │                                                                   │
   │   ┌─────────────────────────┐      ┌──────────────────────────┐   │
   │   │     trading.core        │─────▶│     trading.ports        │   │
   │   │  safety controls,       │      │  abstract interfaces     │   │
   │   │  value types, gateway   │      │  the kernel calls out    │   │
   │   └─────────────────────────┘      │  through                 │   │
   │              ▲                     └──────────┬───────────────┘   │
   │              └────────────────────────────────┘                   │
   │             (the two halves may import each other)                │
   └───────────────────────────────────┬───────────────────────────────┘
                                       │ implemented by
                                       ▼
                    ┌─────────────────────────────────────────┐
                    │        trading.adapters                 │
                    │   the ONLY layer allowed to know        │
                    │   about infrastructure                  │
                    └─────────────────────────────────────────┘
```

**`trading.core` and `trading.ports` together form the pure kernel.** Ports are
part of the kernel, not infrastructure: they are the interfaces the kernel calls
outward through, and they hold no implementation. The two halves may import each
other — `gateway.py` imports `ports.broker`, and `ports.repository` imports
`core.orders` — but neither may import an adapter or a strategy.

**Adapters import inward and are never imported inward.** Nothing in
`trading.core` or `trading.ports` names `trading.adapters`. A concrete broker is
handed to the gateway at construction time as a `BrokerPort`; the kernel never
knows which one it got.

**Strategies import the kernel but cannot reach a venue.** `trading.strategy`
may import `trading.core` and `trading.ports`, but no strategy module imports a
concrete adapter or the gateway. `test_core_purity.py` checks both directly.

---

## 3. Module inventory

### `trading.core` — the safety kernel (4 161 statements)

| Module | What it owns |
|---|---|
| `errors.py` | The exception hierarchy. `SafetyViolation` is the root of everything that means "refused"; each subclass names the invariant it defends. |
| `clock.py` | `Clock` / `SystemClock` / `ManualClock`. Time is an injected input, never ambient. `monotonic_seconds()` is separate from `now()` so a backwards wall-clock jump cannot shorten a cooldown. |
| `money.py` | `Money`, `Price`, `Quantity`, `Currency`. Decimal-only, precision-strict, explicit rounding direction. |
| `secrets.py` | `Secret` containment and `Redactor` scrubbing. |
| `audit.py` | Hash-chained append-only audit log, redacted at the boundary, fail-closed. |
| `authz.py` | Role/action matrix and `ExecutionToken` capability tokens. |
| `config.py` | `TradingConfig` and `RiskConfig`. Live trading requires two independent signals. |
| `modes.py` | `TradingMode` and its complete transition table. |
| `killswitch.py` | Latching kill switch with an out-of-band file trigger. |
| `breaker.py` | Named circuit breakers, CLOSED / OPEN / HALF_OPEN. |
| `orders.py` | `OrderIntent`, `Order`, the order state machine, `OrderStore`. |
| `dedupe.py` | `IdempotencyRegistry` — a state machine over keys, not orders. |
| `reconciliation.py` | `PositionLedger` and the `ReconciliationGate` (UNKNOWN + mismatch + staleness). |
| `portfolio.py` | **Stage 2.** `Portfolio`, `Position`, `FillEffect` — cash, cost basis, realized/unrealized P&L, and equity, layered over one `PositionLedger`. An unknown basis reports as unknown. |
| `risk.py` | `RiskEngine` and `RiskApproval`. Approvals are single-use capabilities. |
| `sizing.py` | `PositionSizer`. Rounds down, always; verifies the result rather than trusting the arithmetic. |
| `gateway.py` | **`ExecutionGateway` — the one place an order can leave this system.** |
| `marketdata.py` | **Stage 2.** `Quote`, `Candle`, `MarketSnapshot`, `StalenessPolicy`, and `FreshMarkPrices` — the bridge that turns a stale quote into an absent mark price. |

`trading/core/__init__.py` re-exports nothing. Callers import from the specific
module, so an import line says which control is in play.

### `trading.ports` — interfaces only (137 statements)

`BrokerPort`, `MarketDataPort`, `QuoteFeedPort`, `OrderRepositoryPort`,
`PositionRepositoryPort`. Every one is abstract and every abstract method body is
`...` or `pass` — `test_ports.py` asserts that by AST, so a port cannot quietly
acquire logic. It also pairs every exported port with a concrete implementation,
so a port nothing satisfies cannot be added.

The repository ports declare their conformance with `ABCMeta.register()` rather
than by inheritance:

```python
# trading/ports/repository.py
OrderRepositoryPort.register(OrderStore)
PositionRepositoryPort.register(PositionLedger)
```

`register()` is used because `trading.ports.broker` imports `trading.core.orders`
at runtime, so having `OrderStore` name `OrderRepositoryPort` as a base class
would close an import cycle. Registering from the ports side works because that
module already depends on `trading.core`, and the arrow still points one way.

`register()` buys the `issubclass` relationship but enforces no method, so it is
not the whole guarantee. `test_ports.py` checks that every abstract method
exists on each implementation *with a compatible signature* — strictly stronger
than inheritance, which would notice a missing method but not a changed one.

### `trading.adapters.memory` — the only adapters (287 statements)

`SimulatedBroker`, `StaticMarketData`, and (Stage 2) `InMemoryQuoteFeed`.
Deterministic, offline, clock injected. The broker can be scripted to reject, to
answer `UNCERTAIN`, or to raise after the request has left; the quote feed can
freeze, go dark, deliver ticks out of order, or stamp them in the future. The
failure modes that matter are the ones that are hard to reach against a real
venue.

### `trading.strategy` — proposals only (666 statements)

`Strategy`, `StrategyRunner`, `MarketView` — Stage 1's form, whose entire output
is a list of `OrderIntent`. Stage 2 adds a parallel form: `SignalStrategy` and
`SignalRunner` produce `Signal` objects that carry direction, stop, target, and a
required rationale but **no quantity**, because sizing depends on equity and stop
distance and is a risk decision rather than a strategy one. `MarketContext`
widens what a strategy may see to the timestamped market and filters it on the
way out; `indicators.py` holds pure `Decimal` functions over closed bars. Neither
runner holds a gateway. See §6.

---

## 4. What is enforced mechanically

`tests/test_core_purity.py` (43 tests) parses every source file's AST and
checks the layering rather than trusting review. It asserts, among other things:

- `trading.core` imports only stdlib, `trading.core`, and `trading.ports`
- `trading.ports` imports only stdlib and `trading.core`
- no kernel module imports `trading.adapters`, `trading.strategy`, or `tests`
- no strategy module imports a concrete adapter or the gateway
- every import in the package resolves to stdlib or first-party — no third-party
  anywhere, **including the test suite**
- no dependency manifest declares a requirement
- no module imports `socket`, `http`, `urllib`, `requests`, or `httpx`, by any
  name or alias
- the kernel uses no dynamic import machinery (`importlib`, `__import__`) and
  no dynamic code execution (`eval`, `exec`, `compile`)
- **importing the whole kernel pulls in no adapter and nothing third-party**, and
  imports cleanly with no installed packages reachable on `sys.path`
- importing the kernel opens no socket and reads no environment secret
- every port module is abstract
- the layering graph is acyclic: no pair of units imports both ways
- each kernel module imports cleanly on its own

The last group are runtime checks in a subprocess, not AST checks. An AST scan
proves nobody *wrote* the import; running the import proves nothing pulls it in
transitively.

---

## 5. The path an order takes

```
  Strategy.propose(MarketView)          returns OrderIntent — inert, no permission
            │
            ▼
  StrategyRunner.propose(...)           audits the proposal; refuses a strategy
            │                           carrying an execution surface
            ▼
  ExecutionGateway.submit(intent, proposer=..., mark_prices=...)
            │
            ├─ 1  authorization       may this principal propose?        INV 3
            ├─ 2  kill_switch         latched stop engaged?              INV 10
            ├─ 3  circuit_breakers    any breaker open?
            ├─ 4  trading_mode        does the mode allow execution?     INV 2, 11
            ├─ 5  live_authorization  if LIVE: config + mode agree?      INV 1, 2
            ├─ 6  duplicate_order     claim the idempotency key          INV 12
            ├─ 7  reconciliation      no UNKNOWN order, no mismatch      INV 5, 6
            ├─ 8  risk                RiskApproval covering all limits   INV 4, 7
            ├─ 9  token               mint ExecutionToken, persist order INV 3
            └─ 10 execution           broker.place_order(token=...)      INV 3, 12
            │
            ▼
  ExecutionResult:  EXECUTED  |  REFUSED  |  UNKNOWN
```

The ordering is load-bearing, not stylistic. Cheap absolute stops precede
expensive evaluation, so a halted system does no risk arithmetic. Idempotency is
claimed *before* the risk check, so a duplicate is rejected without consuming
rate-limit budget. Risk approval is the last gate before the token exists, so a
token can never exist without a complete approval behind it — which is what
makes INVARIANT 4 structural rather than a matter of statement ordering.

`ExecutionGate.ORDER` holds the sequence as data, and `test_gateway.py` trips
several gates at once and asserts the reported failure names the earliest.

---

## 6. Design decisions worth knowing

**The gateway is the only actor.** Every other kernel component answers a
question; the gateway is the only one that *acts*. It is the only module that
holds a `BrokerPort` and the only caller of `mint_execution_token()`. Even code
that smuggles in a broker reference cannot use it, because
`BrokerPort.place_order` requires an `ExecutionToken` as a keyword-only argument
with no default.

**Permissions are capabilities, not booleans.** A function returning `True`
invites a caller to ignore it. `RiskApproval` and `ExecutionToken` are
single-use, bound to one order, and carry a TTL. There is no path to execution
that does not consume one, and no way to obtain one without having passed the
checks. This is the same reasoning in both places: make the safe path the only
path, rather than the documented one.

**Decimal-only money, enforced structurally.** Every constructor and arithmetic
operand routes through `to_decimal()`, which raises `TypeError` on `float` *and*
on `bool`. There is no code path that accepts a binary float. `bool` is rejected
even though it is an `int` subclass, because `Money(True, USD)` is far more
likely to be a bug than an intent.

**Rounding names its direction.** Exposure rounds up; order size rounds down.
Both are explicit at every call site, because the safe direction differs by use
and a default would be wrong half the time.

**Time is injected.** Every control that depends on time takes a `Clock`. Tests
use `ManualClock` and move time without sleeping, so time-dependent safety
behaviour is deterministic rather than flaky. `ManualClock.set_wall_clock()` can
move the wall clock *backwards* without moving the monotonic clock — that exists
so tests can prove cooldowns and staleness checks do not depend on wall clock.

**Audit before effect.** Callers record an intended action before performing it.
A crash then leaves evidence of intent, which is exactly what reconciliation
needs. Recording after the fact would lose the only case that matters.

**Fail closed everywhere.** A sink that raises propagates. A kill switch whose
state cannot be determined reports ENGAGED. A missing mark price is a risk
violation, not a skipped check. "I don't know" never resolves to "carry on".

**A signal carries no quantity.** *(Stage 2.)* `Signal` names a direction, a
stop, a target, and a rationale, and there is nowhere on it to put a size.
Sizing depends on account equity and the distance to the stop, neither of which a
strategy should be reasoning about — a strategy that sizes its own orders can
breach a limit before the risk engine ever sees it. `SignalStrategy` therefore
rejects sizing-shaped attribute names (`size`, `position_size`, `quantity_for`,
`size_order`) at class-definition time, alongside the execution-shaped ones. The
coherence rules are part of the same reasoning: a long whose stop sits at or
above its reference price is refused at construction, because a zero or negative
risk-per-unit would make the sizer's division produce an unbounded position from
a plausible-looking number.

**An unknown cost basis is unknown, not zero.** *(Stage 2.)* `Portfolio` layers
cash and cost basis over one `PositionLedger` rather than keeping a second
quantity book, because two local views of a position disagreeing is the failure
INVARIANT 6 exists to catch and there is no reason to inflict it on ourselves.
When positions are adopted from a venue we learn the quantity and not what was
paid, so `Position.average_entry_price` is `None` and `unrealized_pnl` returns
`None` — reporting zero would price an adopted long as pure profit, which is the
most expensive lie available to tell a loss limit. The basis also
*self-invalidates*: the portfolio remembers which quantity its basis describes, so
any direct ledger write (`set_position`, `adopt_broker_positions`, a future
persistence adapter) leaves the two disagreeing and the basis reads as unknown,
without the writer needing to know the portfolio exists. `equity()` still answers
in that state, because valuation needs a mark and not a basis — but it raises
rather than skipping a symbol it cannot mark, for the same reason
`RiskLimit.MARK_PRICE_AVAILABLE` treats a missing mark as a breach. Every
rounding decision in the module is pessimistic: a buy's cash outflow rounds up, a
sale's inflow rounds down, a long's market value rounds down, a short's rounds up,
and a P&L figure rounds toward a loss.

**No retries.** A retry after an uncertain outcome is the single most dangerous
thing a trading system can do, so there is no retry anywhere in `gateway.py`.
An uncertain answer produces `UNKNOWN` and stops the system.

---

## 7. Tests

Stdlib `unittest` only. There is no pytest, no `requirements.txt`, and no
`pyproject.toml` declaring a dependency — `test_core_purity.py` asserts that too.

```bash
python3 -m unittest discover -s tests -t .
```

1 212 tests, ~4 s, 96.1% statement coverage (5 253 statements, 203 missed).

| Module | Tests | Covers |
|---|---:|---|
| `test_marketdata.py` | 95 | Quote/candle validation, staleness, the frozen-feed refusal |
| `test_gateway.py` | 89 | Each gate's refusal, and the chain ordering |
| `test_invariants_end_to_end.py` | 79 | All thirteen invariants on a wired system |
| `test_dedupe_reconciliation.py` | 75 | Idempotency keys, UNKNOWN, mismatch, staleness |
| `test_orders.py` | 75 | Order state machine, `OrderStore` |
| `test_risk.py` | 75 | Every limit, and the reducing-order waiver |
| `test_signals.py` | 69 | Signal coherence, `MarketContext`, `SignalRunner`, the reference strategy |
| `test_adapters.py` | 66 | `SimulatedBroker`, `StaticMarketData`, `InMemoryQuoteFeed` |
| `test_money.py` | 57 | Decimal discipline, precision, rounding |
| `test_portfolio.py` | 57 | Cost basis, realized/unrealized P&L, equity refusal, basis self-invalidation |
| `test_strategy.py` | 53 | `Strategy`, `StrategyRunner`, the execution tripwire |
| `test_config.py` | 46 | Two-signal live authorization, env and TOML loading |
| `test_killswitch_breaker.py` | 46 | Latching, asymmetric authority, breaker states |
| `test_audit.py` | 43 | Hash chain, redaction, identifier survival |
| `test_core_purity.py` | 43 | The layering rules in §4 |
| `test_indicators.py` | 42 | Insufficient-data `None`, gap-aware true range, input validation |
| `test_sizing.py` | 42 | Round-down, verify-the-result |
| `test_secrets.py` | 37 | Containment and scrubbing |
| `test_authz.py` | 34 | Role matrix, token single-use and TTL |
| `test_modes.py` | 34 | Transition table |
| `test_clock.py` | 33 | `SystemClock` and `ManualClock` |
| `test_ports.py` | 22 | Ports abstract; implementations conform by signature |

Coverage is measured with the stdlib `trace` module, since `coverage.py` would be
a third-party dependency. Do **not** read the figure off `trace --summary`: its
denominator is the set of lines it saw, so it reports 100% for files that are not
fully covered. Compare against the executable-line set instead:

```bash
python3 -c "
import io, trace, unittest, pathlib
tracer = trace.Trace(count=1, trace=0)
tracer.runfunc(lambda: unittest.TextTestRunner(stream=io.StringIO()).run(
    unittest.TestLoader().discover('tests', top_level_dir='.')))
seen = {}
for (f, n), _ in tracer.results().counts.items():
    seen.setdefault(f, set()).add(n)
total = missed = 0
for f in sorted(pathlib.Path('trading').rglob('*.py')):
    exe = {n for n in set(trace._find_executable_linenos(str(f))) if isinstance(n, int) and n > 0}
    if not exe: continue
    hit = exe & (seen.get(str(f.resolve()), set()) | seen.get(str(f), set()))
    total += len(exe); missed += len(exe) - len(hit)
    print(f'{100*len(hit)/len(exe):6.1f}%  {len(exe):5d} stmt  {len(exe)-len(hit):4d} miss  {f}')
print(f'TOTAL: {100*(total-missed)/total:.1f}%  ({total} statements, {missed} missed)')
"
```

The 203 missed statements are overwhelmingly unreachable-by-design: the `...`
bodies of abstract port methods (all six misses in `ports/repository.py`), and
defensive `raise TypeError` / `raise ConfigurationError` guards against argument
types the surrounding code already prevents. A handful are unexercised `__repr__`
and property accessors. Four statements carry `# pragma: no cover`; `trace` does
not honour the pragma, so they are counted as missed here.

`tests/harness.py` builds a fully wired system (`build_rig()`) with a
`ManualClock`, an `InMemoryAuditSink`, and a `SimulatedBroker`. Prefer it to
hand-wiring in new tests.

---

## 8. Where Stage 2 attaches

Every seam is already named. Nothing in `trading.core` changes.

| Addition | Attaches at | Notes |
|---|---|---|
| PostgreSQL persistence | `OrderRepositoryPort`, `PositionRepositoryPort` | The in-memory `OrderStore` and `PositionLedger` already satisfy both. Restoring quantities alone is safe but leaves the cost basis unknown, so the basis needs persisting too if P&L attribution is to survive a restart. |
| CoinSwitch REST client | `BrokerPort` | Must demand an `ExecutionToken`. `SimulatedBroker` is the reference. |
| Live price feed | `QuoteFeedPort` | Publish timestamped `Quote`s; wrap in `FreshMarkPrices` so staleness becomes a risk refusal. |
| FastAPI | A new inbound adapter under `trading.adapters` | Calls `ExecutionGateway.submit`; never bypasses it. |
| Durable audit | `AuditSink` | See "Tamper evidence is not tamper proofing" in `SAFETY.md`. |
| Real strategies | `trading.strategy` | Subclass `Strategy`; the runner's tripwire applies. |

Two rules survive into every later stage:

1. **`trading.core` and `trading.ports` stay stdlib-only.** If a change requires
   a third-party import in the kernel, the change is in the wrong layer.
2. **Nothing bypasses `ExecutionGateway.submit`.** A second path to
   `place_order` is not an optimisation; it is the loss of every invariant the
   chain provides.

See [SAFETY.md](SAFETY.md) for the invariants themselves and for an honest
account of what these controls do *not* protect against.
