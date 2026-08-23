# Safety

The thirteen invariants, where each is enforced, how each is tested — and an
honest account of what these controls do **not** protect against.

Referenced from `trading/core/audit.py`, `trading/core/config.py`,
`trading/core/authz.py`, and `trading/core/secrets.py`.

> **Stage 1 has never sent an order to a real venue.** `TradingMode.LIVE` exists
> and every gate that guards it is implemented and tested, but no adapter behind
> it talks to a real exchange. Nothing in this document should be read as a claim
> that live trading has been validated against real money.

---

## 1. The invariants

Every invariant is numbered, and the number appears in the code that enforces it
and the test that asserts it. `grep -rn "INVARIANT 7" trading/ tests/` finds
everything relevant to a limit breach.

| # | Invariant | Enforced in | Asserted in |
|---:|---|---|---|
| 1 | `LIVE_TRADING` defaults to FALSE | `config.py`, `modes.py` | `test_config.py`, `TestInvariant1LiveIsOptIn` |
| 2 | No order executes while live trading is disabled | `modes.py`, `gateway.py` | `test_modes.py`, `TestInvariant2NoLiveWithoutAuthorization` |
| 3 | Strategy code cannot directly execute an order | `authz.py`, `ports/broker.py`, `strategy/base.py`, `gateway.py` | `test_authz.py`, `test_strategy.py`, `TestInvariant3StrategyCannotExecute` |
| 4 | Risk checks happen before execution | `risk.py`, `gateway.py` | `test_risk.py`, `TestInvariant4RiskPrecedesExecution` |
| 5 | An UNKNOWN order blocks new orders until reconciled | `reconciliation.py`, `orders.py` | `test_dedupe_reconciliation.py`, `TestInvariant5UnknownBlocks` |
| 6 | A local/venue position mismatch blocks new live orders | `reconciliation.py` | `test_dedupe_reconciliation.py`, `TestInvariant6MismatchBlocks` |
| 7 | Loss, exposure, and rate limits cannot be bypassed | `risk.py` | `test_risk.py`, `TestInvariant7LimitsHold` |
| 8 | Financial calculations use `Decimal`, never `float` | `money.py` | `test_money.py`, `TestInvariant8NoFloats` |
| 9 | Secrets never appear in logs | `secrets.py`, `audit.py` | `test_secrets.py`, `TestInvariant9SecretsStayOut` |
| 10 | The kill switch prevents new orders | `killswitch.py` | `test_killswitch_breaker.py`, `TestInvariant10KillSwitch` |
| 11 | Invalid trading-mode transitions are rejected | `modes.py` | `test_modes.py`, `TestInvariant11ModeTransitions` |
| 12 | Duplicate submission is prevented, or made explicitly UNKNOWN | `dedupe.py`, `orders.py` | `test_dedupe_reconciliation.py`, `TestInvariant12NoDuplicates` |
| 13 | Every safety decision is recorded before it takes effect | `audit.py` | `test_audit.py`, `TestInvariant13AuditPrecedesEffect` |

The classes in the right-hand column live in
`tests/test_invariants_end_to_end.py`, which asserts each invariant against a
**fully wired system** rather than a component in isolation. Those tests are
phrased in terms of what an outside observer saw — `broker.placement_count`,
`broker.duplicate_keys`, and the audit trail — because a refusal that satisfied
the gateway's own bookkeeping but still reached the venue is the failure mode
worth catching, and only an external observer can catch it.

---

## 2. The execution chain

`ExecutionGateway.submit` is the only path to a venue. It runs ten gates in a
fixed order and refuses at the first failure.

| # | Gate | Refuses when | Invariant |
|---:|---|---|---|
| 1 | `authorization` | the proposer may not propose orders | 3 |
| 2 | `kill_switch` | the latching stop is engaged | 10 |
| 3 | `circuit_breakers` | any named breaker is OPEN | — |
| 4 | `trading_mode` | the mode does not allow execution | 2, 11 |
| 5 | `live_authorization` | mode is LIVE but config does not authorise it | 1, 2 |
| 6 | `duplicate_order` | the idempotency key is already claimed | 12 |
| 7 | `reconciliation` | an UNKNOWN order exists, positions disagree, or the last clean reconciliation is stale | 5, 6 |
| 8 | `risk` | any limit would be breached, or a mark price is missing | 4, 7 |
| 9 | `token` | the order cannot be persisted before sending | 3 |
| 10 | `execution` | the broker refuses | 3, 12 |

**Why this order.** Cheap absolute stops precede expensive evaluation, so a
halted system does no risk arithmetic. Idempotency is claimed *before* the risk
check, so a duplicate is rejected without consuming rate-limit budget. Risk
approval is the last gate before the token exists, so **a token can never exist
without a complete approval behind it** — that is what makes INVARIANT 4
structural rather than a matter of statement ordering.

`ExecutionGate.ORDER` holds the sequence as data. `test_gateway.py` trips several
gates at once and asserts the reported failure names the earliest one, so the
ordering is verified rather than merely written down.

### Three outcomes, never a silent one

| Outcome | Order state | Idempotency key | New orders |
|---|---|---|---|
| **REFUSED** | unchanged | released — a corrected order may reuse it | still accepted |
| **EXECUTED** | reflects the broker's answer | SETTLED | still accepted |
| **UNKNOWN** | `UNKNOWN` | `UNKNOWN`, **not** released | **blocked system-wide** |

`UNKNOWN` is produced when the broker answers `UNCERTAIN` *or* raises. The key is
deliberately not released: we cannot prove the venue never saw it.

**There is no retry anywhere in `gateway.py`.** A retry after an uncertain
outcome is the single most dangerous thing a trading system can do — it is how
you end up long twice. An uncertain answer stops the system and waits for a
human.

---

## 3. Recurring safety patterns

These five ideas appear throughout the kernel. Recognising them makes the code
much faster to read.

### Fail closed

"I don't know" must never resolve to "carry on trading".

- A kill switch whose state cannot be determined (an I/O error probing the
  trigger file) reports **ENGAGED**.
- A missing mark price is a **risk violation** (`RiskLimit.MARK_PRICE_AVAILABLE`),
  not a skipped check. Exposure cannot be computed without prices.
- An audit sink that raises **propagates**. A caller that cannot audit must
  refuse to act rather than act unobserved.
- A malformed `TRADING_LIVE` value raises `ConfigurationError` rather than being
  coerced. "Unparseable" must never resolve to "enabled".
- `NaN` and `Infinity` are rejected at construction, so a poisoned value cannot
  propagate through a risk check and compare `False` against every limit.

### Latching

A transient condition that trips a control does not un-trip itself when the
condition clears. A human decides when trading resumes.

- **Kill switch** — stays engaged until an operator explicitly releases it.
- **Position mismatch** — stays latched even after the venue agrees again.
  Agreement returning is not evidence the earlier gap was harmless. Clearing
  re-verifies against a fresh snapshot rather than taking the operator's word for
  it.
- **`HALTED` mode** — exits only to `DISABLED`, never straight back to `LIVE`.
  Leaving `HALTED` requires a conscious step through `DISABLED`, which forces
  re-arming.

### Asymmetric authority

Stopping is never gated on privilege; starting always is.

| Action | Who may |
|---|---|
| Engage the kill switch | every operational role, including `STRATEGY` |
| Release the kill switch | `Role.OPERATOR` only |
| Halt the mode machine | reachable from every state in one step |
| Enter `LIVE` | `OPERATOR`, and only from `PAPER`, and only with config authorisation |
| Clear a position mismatch | `Role.OPERATOR` only, with a fresh clean snapshot |
| Resolve an UNKNOWN order | `Role.OPERATOR` only |

### Capabilities, not booleans

A function returning `True` invites a caller to ignore it. A capability does not.

- **`RiskApproval`** — single-use, bound to one idempotency key, short TTL, and
  it must name *every* required check (`covers_all_limits()`). `RiskEngine` is
  the only thing that can produce one; the gateway is the only thing that
  consumes one.
- **`ExecutionToken`** — single-use, bound to one order, carries an expiry, and
  mintable only by a principal holding `Role.EXECUTION_GATEWAY`.
  `BrokerPort.place_order` demands one as a keyword-only argument with no
  default, so an adapter cannot be driven without it. Because tokens are
  single-use, a leaked token cannot be replayed.

Separation of duties is checked at construction, not per order: a gateway wired
with an identity that could also approve risk refuses to be built. No role in
`PERMISSIONS` currently holds both `EXECUTE_ORDER` and `APPROVE_ORDER`, so that
guard is unreachable today — which is why the property is *also* asserted against
the matrix itself, in both `test_gateway.py` and `test_risk.py`. The guard exists
to catch a future edit to `PERMISSIONS`; the tests catch it at the source.

### Audit before effect

Callers record an intended action *before* performing it. A crash then leaves
evidence of intent, which is exactly what reconciliation needs in order to ask
the venue the right question. Recording after the fact would lose the only case
that matters.

Refusals are audited too, so a refused mode transition and a rejected order both
leave a trace.

---

## 4. Two state machines that carry the weight

### Idempotency keys (INVARIANT 12)

The registry is a state machine over *keys*, not orders. A key is claimed before
anything is sent, and its lifecycle records how far the submission got.

```
(new) --reserve--> RESERVED --mark_submitted--> SUBMITTED --mark_settled--> SETTLED
                      |                            |
                release_unsent               mark_unknown
                      |                            |
                   (freed)                      UNKNOWN
```

The asymmetry is the whole point. `release_unsent` is legal only from `RESERVED`
— i.e. when we are certain nothing left the process, so a pre-submission risk
rejection can safely free its key. Once a request has been sent (`SUBMITTED`) the
key can **never** be freed; it either settles or becomes `UNKNOWN`. There is no
path that lets a retry reuse a key whose request may have reached the venue.

A key in `UNKNOWN` blocks the whole registry via `has_unknown()`, which the
gateway consults before accepting anything new.

### Trading modes (INVARIANT 11)

`ALLOWED_TRANSITIONS` is the complete table; anything absent is forbidden.

```
   DISABLED ⇄ BACKTEST        HALTED ──▶ DISABLED   (the only way out)
      ⇅          ⇅              ▲
    PAPER ⇄ ─────┘              │  reachable from every state in one step
      ⇅                         │
     LIVE ────────────────────┘
```

`DISABLED` is the default and executes nothing. Only `PAPER` and `LIVE` allow
execution at all. **`LIVE` is reachable only from `PAPER`** — a fresh system
cannot jump straight to live trading, and a halted one cannot snap back to it.

---

## 5. Honest limitations

Everything below is a real gap. None of it is hypothetical, and none of it is
closed by Stage 1.

### Python provides no capability isolation

`ExecutionToken` and the role matrix make an unauthorised execution
**deliberate, visible, and greppable**. They do not make it impossible.

Code running in this process can reach `trading.core.authz._MINT_KEY`, construct
a `Principal(role=Role.EXECUTION_GATEWAY)`, or monkey-patch `authorize`. A
determined author of a strategy module can defeat every control in `authz.py`.

`StrategyRunner` inspects a strategy before running it and refuses one holding a
`BrokerPort`, an `ExecutionToken`, or any object exposing `place_order` — but
that check is **shallow by design**. It walks the strategy's own attributes; it
does not chase nested references, closures, or imports. It is a tripwire for
accidents, not a sandbox against an adversary.

Real isolation requires a process or network boundary. That arrives with the
service split in a later stage. Until then, the honest statement is: *the code in
this repository cannot execute an order by accident, and a reviewer can see any
attempt to do so on purpose.*

### Tamper evidence is not tamper proofing

Each audit record embeds the hash of its predecessor, so editing, deleting, or
reordering any record invalidates every hash after it, and `AuditLog.verify()`
detects that. `test_invariants_end_to_end.py` asserts all three cases.

But an attacker with write access to the store can **recompute the whole chain**.
The chain proves that nobody tampered *casually*; it does not prove that nobody
tampered at all.

Genuine tamper proofing needs an append-only external store — a WORM bucket, a
write-only database role, or shipping records off-host as they are written.
`AuditSink` is the seam where that drops in without touching the safety core.
In Stage 1 the only sink is in memory, so **the audit trail does not survive
process exit.**

### Config authorisation is necessary, never sufficient

Enabling live trading requires two independent signals:

```bash
export TRADING_LIVE=true
export TRADING_LIVE_CONFIRMATION=I_UNDERSTAND_THIS_TRADES_REAL_MONEY
```

A single stray `TRADING_LIVE=true` in a shell profile or a CI variable does
nothing. The exact phrase is `REQUIRED_LIVE_CONFIRMATION` in `config.py`, and a
near-miss is a `ConfigurationError`, not a warning.

Even so, a config with `is_live_authorized` true **does not permit an order**. It
is one necessary input consumed by the mode machine and by gate 5 of the chain.
Reaching `TradingMode.LIVE` is *also* not sufficient: a system that has never
reconciled is refused at gate 7, because it cannot prove its positions match the
venue's. Both conditions are tested separately in
`TestInvariant2NoLiveWithoutAuthorization`.

### Limits of redaction

INVARIANT 9 rests on two independent mechanisms, because either alone is
insufficient. `Secret` **containment** makes the obvious accidents inert:
`str()`, `repr()`, f-strings, and `%`-formatting all yield `***REDACTED***`, and
pickling raises. `Redactor` **scrubbing** removes material that has already
escaped containment — a credential pasted into a config string, an HTTP header
echoed by a server, a secret embedded in a third-party exception message.

Neither is a guarantee. Specifically:

- **`Secret.reveal()` is a disclosure point, and nothing stops a caller passing
  the result to a logger.** The method name exists so that
  `grep -rn "\.reveal()" ` finds every one in review. That is the whole control.
- **Scrubbing only catches what it recognises.** Registered values are scrubbed
  exactly; beyond that there are six patterns — PEM blocks, URL credentials,
  JWTs, `Bearer` headers, `key: value` shapes for a fixed keyword list, and long
  hex runs. A credential in a shape none of those match passes through. A secret
  that was never wrapped in `Secret` and never matches a pattern is not redacted.
- **Values shorter than `MIN_SCRUB_LENGTH` (6) are not added to the scrub list.**
  A 3-character secret would match constantly and turn every log line into
  confetti, destroying the audit trail we depend on. Short secrets get
  containment but not scrubbing.
- **Redaction is one-way and lossy.** Once a record is written, the original is
  gone. That is the point, but it means over-redaction destroys evidence — see
  the next bullet.
- **One heuristic is deliberately narrowed.** The long-hex-run pattern flags any
  32+ character hex string, which is right for text we do not control but wrong
  for a SHA-256 idempotency key — which has exactly that shape and is the one
  field an operator needs in order to reconcile an `UNKNOWN` order. `audit.py`
  therefore calls `Redactor.redact_identifier()` for a closed set of four detail
  keys (`idempotency_key`, `order_id`, `broker_order_id`, `approval_id`), which
  runs every other pattern and skips only the shape heuristic.

  Two omissions there are deliberate: `key` is a generic name used for actual
  credentials elsewhere, and the ambiguity resolves in favour of redacting;
  `token_id` names a *capability*, not an identifier, so it stays redacted.
  `TestIdentifiersSurviveRedaction` in `test_audit.py` covers both halves — that
  identifiers survive, and that the same value under a non-allowlisted key does
  not.
- **Redaction happens at the audit boundary, not at the process boundary.** A
  `print()`, a traceback written to stderr, or a third-party library's own
  logging is not routed through the redactor unless `install_redaction()` has
  been called on that handler.

`Secret.fingerprint()` exists so an operator can confirm "the key I deployed is
the key in use" from logs alone. It is a truncated SHA-256 — a correlation tag,
not a proof of possession.

### Staleness detection depends on a timestamp we did not produce

`StalenessPolicy` compares a quote's `as_of` against our own clock, and `as_of`
comes from outside the process. Three consequences, none of them fixable inside
this codebase:

- **A venue that stamps quotes with its own send time and runs slow** makes fresh
  data look old. That direction is safe — it refuses.
- **A venue that stamps with a clock running fast** makes old data look fresh, so
  the policy treats a timestamp more than `FUTURE_TOLERANCE_SECONDS` ahead of us
  as *stale* rather than as maximally fresh. Without that rule, a fast venue
  clock would keep a frozen feed looking alive indefinitely.
- **An adapter that stamps `as_of` with "now" on receipt** defeats the check
  entirely: a replayed or queued message becomes indistinguishable from a live
  one. `QuoteFeedPort`'s docstring forbids it, and nothing mechanical can enforce
  it, so it is a review obligation on every new feed adapter.

What the mechanism *does* guarantee is that a frozen feed cannot reach the risk
engine as a usable price: `FreshMarkPrices` reports a failing quote as absent, and
absence is already a refusal (gate 8, `RiskLimit.MARK_PRICE_AVAILABLE`). Note that
this refusal is deliberately **not latching** — unlike the kill switch or the
mismatch gate — because a stale tick is an ordinary operational event, and
requiring an operator to clear each one would turn every hiccup into an outage.

### What Stage 1 does not defend against at all

- **A signal that reaches a sizer without a stop.** *(Stage 2.)* `Signal` refuses
  an incoherent stop at construction, so `risk_per_unit` is never zero or
  negative — but a stop is optional, and a stopless signal returns `None` there.
  Nothing yet forces the caller to check: the position sizer that must refuse
  such a signal rather than substitute a default is not built. Until it is,
  `Signal` guarantees only that a stop it *does* carry is usable.
- **Anything requiring a process boundary** — see capability isolation above.
- **Persistence.** No state survives process exit: no orders, no positions, no
  audit trail, no idempotency keys. A restart after an `UNKNOWN` order loses the
  block that `UNKNOWN` was providing. This is the single largest gap, and it is
  why `OrderRepositoryPort` and `PositionRepositoryPort` exist as seams.
- **Concurrency across processes.** Within one process every mutable control
  holds a `threading.Lock` (thirteen of the seventeen core modules; the other
  four — `config`, `errors`, `money`, `sizing` — are immutable value types, the
  exception hierarchy, and a pure calculator). Two processes sharing a venue
  account would each believe they hold the only lock.
- **Clock trust.** `SystemClock` reads the host clock. A host whose monotonic
  clock is broken defeats every cooldown. Tests prove the code does not depend on
  *wall* clock, which is the failure mode that actually occurs.
- **Network failure modes.** There is no network, so there is no timeout tuning,
  no partial-write handling, and no evidence about how a real venue behaves under
  load. The `UNCERTAIN`-and-raise paths are modelled by `SimulatedBroker`; they
  are not validated against a real exchange.
- **The venue lying.** Reconciliation compares our ledger against what the broker
  *reports*. A venue reporting incorrect positions produces a mismatch that
  cannot be resolved by asking it again.
- **Malicious dependencies.** There are none, which is the strongest form of this
  defence available. `test_core_purity.py` asserts it, including for the test
  suite, and asserts that no dependency manifest declares a requirement.

## 6. Operator actions

All of these are audited, and all of them refuse rather than guess.

**Stop everything.** Any role may engage; only `OPERATOR` may release.

```python
kill_switch.engage(principal, reason="why")     # any operational role
kill_switch.release(operator, reason="why")     # OPERATOR only
```

Out of band, with no API call and no working application code, if
`kill_switch_path` is configured — the switch reports ENGAGED while the file
exists, and reports ENGAGED if it cannot tell:

```bash
touch "$TRADING_KILL_SWITCH_PATH"
```

**Resolve an UNKNOWN order.** The only exit from `UNKNOWN` is asking the venue.
Time does not clear it: a day of waiting leaves the block in place.

```python
ack = gateway.resolve_unknown(order, operator=operator)
```

**Clear a latched position mismatch.** Requires a fresh snapshot that is actually
clean; attempting it while still dirty raises.

```python
reconciliation.clear_mismatch(
    operator, reason="why", broker_positions=broker.fetch_positions().positions
)
```

If the venue is right and our ledger is wrong, adopt its view instead of clearing
the flag — the risk engine's reducing-order waiver exists partly so an adopted
over-limit position can still be exited:

```python
reconciliation.adopt_broker_positions(operator, reason="why", broker_positions=...)
```

**Go live.** Only from `PAPER`, only with both config signals, and a clean
reconciliation must already have run.

```python
modes.transition_to(TradingMode.LIVE, actor="operator-1", reason="why")
```

**Verify the audit trail.** Raises `ValueError` naming the first broken sequence
number.

```python
audit.verify()
```

---

## 7. Changing safety code

1. **Find the invariant number first.** `grep -rn "INVARIANT 7" trading/ tests/`
   shows everything that enforces or asserts it. If a change has no invariant
   number, ask whether it belongs in the kernel.
2. **Add the test that fails before the fix.** Every invariant has an end-to-end
   class in `tests/test_invariants_end_to_end.py`; a new safety property belongs
   there as well as in its unit test.
3. **Never widen a gate to make a test pass.** If a gate is inconvenient, the
   test is probably describing an unsafe operation.
4. **Never add a second path to `place_order`.** A bypass is not an optimisation;
   it is the loss of every invariant in §2.
5. **Never add a retry after an uncertain outcome.** See §2.
6. **Run the whole suite.** `python3 -m unittest discover -s tests -t .` — 1 155
   tests in ~4 s. There is no reason to run a subset.

See [ARCHITECTURE.md](ARCHITECTURE.md) for the layering these controls sit in and
for the seams a later stage attaches to.
