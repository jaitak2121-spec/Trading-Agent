

# AI-Powered Autonomous Trading System

An intelligent, safety-first trading application designed to combine
**market analysis, strategy generation, risk management, broker
execution, portfolio tracking, and controlled automation** in one
system.

> **Project status:** Active development / staged implementation\
> **Current implementation:** Modular Python trading core with a pure
> kernel, execution gateway, broker adapters, order lifecycle
> management, reconciliation, audit logging, and an extensive test
> suite.

------------------------------------------------------------------------

## 1. What Are We Building?

The goal is to build a trading application that can eventually act as a
**controlled autonomous trading assistant**.

The user should be able to:

-   Monitor markets and trading opportunities.
-   Define a trading budget and risk limits.
-   Choose how conservative or aggressive the system should be.
-   Allow the AI/strategy layer to propose trades.
-   Review proposed trades before execution when operating in approval
    mode.
-   Enable automated execution when explicitly allowed.
-   Trade through a broker/exchange adapter, including a future
    **futures trading workflow**.
-   Track orders, fills, positions, portfolio exposure, P&L, and risk.
-   Handle difficult order lifecycle situations such as partial fills,
    cancellations, and uncertain broker responses.
-   Keep an audit trail of important decisions and actions.
-   Learn from historical/performance data to improve strategy
    evaluation over time, while keeping actual trade execution subject
    to hard safety constraints.

The important design principle is:

**The AI can propose decisions, but the safety and execution layers
decide what is actually allowed to happen.**

------------------------------------------------------------------------

# 2. High-Level Vision

The application is intended to evolve into something like:

``` text
                    ┌─────────────────────────┐
                    │       User / UI         │
                    │ Budget • Risk • Mode     │
                    └────────────┬────────────┘
                                 │
                                 ▼
                    ┌─────────────────────────┐
                    │    AI / Strategy Layer  │
                    │ Market analysis          │
                    │ Trade proposals          │
                    └────────────┬────────────┘
                                 │
                                 ▼
                    ┌─────────────────────────┐
                    │     Risk Engine          │
                    │ Position limits          │
                    │ Loss limits              │
                    │ Exposure checks          │
                    │ Futures/leverage checks  │
                    └────────────┬────────────┘
                                 │
                           APPROVED?
                         ┌───────┴───────┐
                         │               │
                        NO              YES
                         │               │
                         ▼               ▼
                       REFUSE      Execution Gateway
                                         │
                                         ▼
                                  Broker / Exchange
                                         │
                                         ▼
                                  Reconciliation
                                         │
                              ┌──────────┴──────────┐
                              ▼                     ▼
                         Order State            Portfolio
                         + Audit Log             + P&L
```

------------------------------------------------------------------------

# 3. Core Product Features

## 3.1 Market Monitoring

The application is intended to provide a central place to monitor
instruments and identify potential opportunities.

Potential inputs include:

-   Price data
-   Volume
-   Technical indicators
-   Market structure
-   Historical data
-   Position information
-   Existing exposure
-   Strategy signals

The strategy layer should **propose an `OrderIntent` rather than
directly executing an order**.

This separation is deliberate.

------------------------------------------------------------------------

## 3.2 AI / Strategy Engine

The AI layer can eventually help with:

-   Market analysis
-   Pattern recognition
-   Strategy selection
-   Trade setup generation
-   Entry/exit reasoning
-   Position sizing suggestions
-   Post-trade analysis
-   Strategy comparison
-   Performance analysis

A critical architectural rule is:

``` text
Strategy → proposes OrderIntent
Strategy → does NOT directly execute broker orders
```

This keeps AI reasoning separate from financial execution.

------------------------------------------------------------------------

# 4. Risk Management

Risk management is one of the most important parts of the application.

The system should allow the user to define things such as:

-   Total trading budget
-   Maximum amount allocated to a trade
-   Maximum risk per trade
-   Maximum daily loss
-   Maximum portfolio exposure
-   Maximum number of open orders
-   Maximum position size
-   Futures leverage limits
-   Stop-loss requirements
-   Maximum drawdown tolerance
-   Trading enable/disable switch

For example:

``` text
Account Budget:       ₹50,000
Risk per trade:        1%
Maximum risk:            ₹500
Maximum open orders:       3
Maximum daily loss:    ₹1,500
Futures leverage:      restricted
```

The exact limits should ultimately be configurable.

### Important principle

A user choosing a higher risk setting should **not bypass fundamental
safety checks**.

The system should have hard limits that cannot be overridden casually.

------------------------------------------------------------------------

# 5. Futures Trading

A future version of the application is intended to support futures
trading through an appropriate broker/exchange adapter.

When enabled by the user, the application could:

1.  Analyze a futures market.
2.  Generate a trade proposal.
3.  Calculate position size according to the user's budget and risk
    rules.
4.  Check leverage and exposure.
5.  Run safety gates.
6.  Ask for user approval in approval mode.
7.  Submit the order in automated mode.
8.  Track the broker acknowledgement.
9.  Reconcile the actual exchange state.
10. Update the local order, position, portfolio, and P&L state.

### Example

``` text
User budget = ₹50,000
Maximum risk/trade = ₹500

AI proposes:
BTC/ETH/etc. futures setup

Risk engine:
✓ Position size within limit
✓ Exposure within limit
✓ Daily loss limit not exceeded
✓ Maximum open orders not exceeded
✓ Kill switch OFF
✓ Required authorization present

→ Order may proceed
```

The system should **never assume that a successful API request means the
trade definitely happened**.

That is why reconciliation exists.

------------------------------------------------------------------------

# 6. Trading Modes

The application can be designed around multiple levels of automation.

### Mode 1 --- Analysis Only

The AI analyzes markets but does not create executable orders.

``` text
Market → AI → Analysis
```

### Mode 2 --- Paper Trading

The system generates and executes simulated trades against a
paper/simulated broker.

Useful for testing strategies without real capital.

### Mode 3 --- Approval Mode

The AI proposes a trade, but the user must approve it.

``` text
AI → Proposal → Risk Checks → USER APPROVAL → Broker
```

### Mode 4 --- Autonomous Mode

If the user has explicitly enabled automated execution, qualifying
trades may be executed automatically after passing all safety gates.

``` text
AI
 ↓
OrderIntent
 ↓
Risk Engine
 ↓
Safety Gates
 ↓
Execution Gateway
 ↓
Broker
```

Autonomous mode should still respect the user's configured budget, risk
limits, kill switch, authorization, and other hard constraints.

------------------------------------------------------------------------

# 7. Order Lifecycle Management

Trading is not simply:

``` text
BUY → DONE
```

A real order can move through multiple states.

The current system defines:

  --------------------------------------------------------------------------------
  State                           Terminal?                Open? Meaning
  -------------------- -------------------- -------------------- -----------------
  `DRAFT`                                No                   No Local, pre-submit
                                                                 order

  `PENDING_NEW`                          No                  Yes Sent, awaiting
                                                                 acknowledgement

  `ACCEPTED`                             No                  Yes Resting at venue

  `PARTIALLY_FILLED`                     No                  Yes Some quantity
                                                                 filled

  `FILLED`                              Yes                   No Fully filled

  `REJECTED`                            Yes                   No Venue rejected
                                                                 order

  `CANCELLED`                           Yes                   No Order cancelled

  `EXPIRED`                             Yes                   No Order expired

  `UNKNOWN`                            No\*                   No State is
                                                                 uncertain and
                                                                 requires
                                                                 reconciliation
  --------------------------------------------------------------------------------

`UNKNOWN` is treated as a blocking state even though it is not
technically terminal.

It can only be resolved through:

``` text
resolve_unknown(..., via_reconciliation=True)
```

This prevents the system from blindly submitting another order when it
does not know what happened to the previous one.

------------------------------------------------------------------------

# 8. Execution Gateway

The application has a central execution boundary:

``` text
ExecutionGateway.submit()
```

This is the main execution chokepoint.

The gateway runs multiple safety gates before reaching the broker.

Conceptually:

``` text
Authorization
      ↓
Kill Switch
      ↓
Order Validation
      ↓
Risk Checks
      ↓
Exposure Checks
      ↓
Token / Idempotency
      ↓
Broker Execution
```

The gateway returns a controlled outcome such as:

``` text
EXECUTED
REFUSED
UNKNOWN
```

There is deliberately **no automatic retry path** for an uncertain
execution.

------------------------------------------------------------------------

# 9. Cancellation and Reconciliation

Cancellation is much more complicated than simply sending:

``` python
broker.cancel_order(order)
```

A cancellation can race with a fill.

For example:

``` text
User requests cancellation
          ↓
Cancel request sent
          ↓
Meanwhile exchange fills the order
          ↓
Cancel acknowledgement arrives
          ↓
What actually happened?
```

Therefore the target cancellation flow is:

``` text
1. Authorize cancellation
2. Refuse UNKNOWN orders
3. Refuse already-terminal orders
4. Audit cancellation request
5. Send cancel to broker
6. Fetch authoritative venue state
7. Apply any newly discovered fills
8. Update portfolio/P&L
9. If cancellation succeeded and order remains open:
       transition to CANCELLED
10. Audit final result
11. Return broker acknowledgement
```

This is one of the key safety features of the project.

------------------------------------------------------------------------

# 10. Fill Reconciliation

The application uses cumulative venue state and converts it into a local
delta.

For example:

``` text
Previously booked fill:  2 units
Venue now reports:       5 units

New fill delta = 5 - 2 = 3 units
```

Only the additional 3 units should be booked.

This prevents **double-booking**.

The Phase 1 implementation introduced:

``` python
Order.apply_fill_delta()
```

and the gateway's fetched-state handling uses this machinery.

The same approach is intended to be reused during cancellation
reconciliation.

------------------------------------------------------------------------

# 11. Broker Architecture

The system uses broker adapters so that the core trading logic does not
depend directly on one broker.

Current architecture includes:

``` text
trading.adapters
        │
        ├── SimulatedBroker
        │
        └── PaperBroker
```

### SimulatedBroker

Designed to behave more like a hostile/edge-case environment for
testing.

### PaperBroker

Designed for honest paper-trading behavior.

A future production adapter can connect the system to an actual
supported broker/exchange.

This architecture makes it possible to test execution logic without
risking real money.

------------------------------------------------------------------------

# 12. Pure Trading Kernel

The current implementation follows a **stdlib-only modular monolith**
with a mechanically enforced pure kernel.

Conceptually:

``` text
trading.strategy
      ↓
OrderIntent

trading.advisory
      ↓
leaf / cannot reach gateway or broker

trading.core ↔ trading.ports
      ↓
pure kernel

trading.adapters
      ↓
SimulatedBroker / PaperBroker
```

The purpose of this architecture is to make the most important trading
rules deterministic, testable, and independent of external services.

------------------------------------------------------------------------

# 13. Auditability

Important actions should be recorded.

Examples include:

-   Order submission
-   Cancellation request
-   Cancellation completion
-   Refusal
-   Risk failure
-   Authorization failure
-   Reconciliation
-   Safety violations
-   State transitions

For example:

``` text
gateway.cancel_requested
gateway.cancel_completed
```

Audit records can include:

-   Actor
-   Order ID
-   Action
-   Outcome
-   Relevant broker acknowledgement
-   Final order state
-   Timestamp/details

This is important for debugging, accountability, and understanding why
the system made a particular execution decision.

------------------------------------------------------------------------

# 14. Learning From Experience

The long-term vision includes a learning/feedback component.

However, "learning" does **not** mean allowing an AI model to freely
modify its own trading rules and immediately spend real money.

A safer architecture is:

``` text
Historical Data
      ↓
Strategy
      ↓
Backtesting
      ↓
Paper Trading
      ↓
Performance Evaluation
      ↓
Model/Strategy Improvement
      ↓
Controlled Deployment
```

The system can learn from:

-   Historical trades
-   Win/loss statistics
-   Drawdowns
-   Market conditions
-   Entry quality
-   Exit quality
-   Slippage
-   Risk-adjusted returns
-   False signals
-   Strategy performance

But safety limits remain outside the learning system.

### Core rule

**The learning component may improve the strategy; it must not be
allowed to remove the safety rails.**

------------------------------------------------------------------------

# 15. Why the Project Needs Strong AI Coding Assistance

This project is significantly more complicated than a normal CRUD
application.

The difficulty is not just writing Python syntax.

The system contains interacting components such as:

``` text
Orders
  ↕
State Machine
  ↕
Execution Gateway
  ↕
Risk Engine
  ↕
Broker Adapter
  ↕
Reconciliation
  ↕
Portfolio
  ↕
P&L
  ↕
Audit
  ↕
Tests
```

A small change in one area can affect many other areas.

For example:

``` text
Changing cancel()
        ↓
Order state transitions
        ↓
open_orders()
        ↓
max_open_orders
        ↓
risk checks
        ↓
portfolio state
        ↓
audit behavior
        ↓
existing tests
```

This is why a high-context coding agent such as Claude with a large
context/usage allowance can be extremely useful.

It can help:

-   Read the architecture before changing code.
-   Understand relationships between files.
-   Trace execution paths.
-   Find existing implementations that should be reused.
-   Make small, targeted changes.
-   Add regression tests.
-   Run and interpret the test suite.
-   Check for unintended side effects.
-   Maintain project documentation.
-   Work through large staged implementation plans.

The goal is **not** to let the AI blindly rewrite the entire project.

The goal is to use a strong coding agent as an engineering partner while
keeping the architecture and safety requirements under human control.

------------------------------------------------------------------------

# 16. Why Large Context Matters

The project contains a large amount of interconnected information:

``` text
gateway.py
orders.py
risk.py
ports
adapters
portfolio
P&L
tests
architecture documentation
safety documentation
handoff documentation
```

For a change such as cancellation reconciliation, the agent needs to
understand:

-   Current order states
-   Valid transitions
-   Broker cancellation behavior
-   Broker fetch behavior
-   Fill delta logic
-   Portfolio bookkeeping
-   Risk/open-order calculations
-   Existing tests
-   Safety invariants

A small-context coding assistant may focus on the immediate function and
accidentally break an invariant elsewhere.

A large-context agent can reason over more of the repository
simultaneously.

------------------------------------------------------------------------

# 17. Current Phase 1 Status

The repository currently reports:

``` text
Full suite: 2,024 tests passing in the current working tree
```

Phase 1 introduced lifecycle primitives including:

### `Order.apply_fill_delta()`

Applies venue cumulative fills as local increments only.

### `_apply_fetched_state()`

Interprets fetched broker state acknowledgements and books portfolio
deltas through the existing fill machinery.

### `resolve_unknown()`

Uses fetched state and reconciliation to resolve uncertain orders while
preventing double-booking.

The public gateway surface is intentionally constrained to:

``` text
submit
cancel
resolve_unknown
```

------------------------------------------------------------------------

# 18. Current Phase 2 Goal

The immediate Phase 2 objective is to make:

``` python
gateway.cancel()
```

correctly handle the complete cancellation lifecycle.

The planned behavior includes:

-   Authorization
-   UNKNOWN-order protection
-   Terminal-order protection
-   Cancellation audit
-   Broker cancellation
-   Authoritative state fetch
-   Fill reconciliation
-   Portfolio updates
-   Correct `CANCELLED` transition
-   Final audit
-   Regression tests

------------------------------------------------------------------------

# 19. Critical Cancellation Scenarios

The test suite should cover scenarios such as:

### Normal cancellation

``` text
ACCEPTED → CANCELLED
```

### Partial fill then cancellation

``` text
ACCEPTED
   ↓
PARTIALLY_FILLED
   ↓
CANCELLED
```

The already-filled quantity must remain booked.

### Fill wins the cancellation race

``` text
Cancel requested
      ↓
Venue reports FILLED
      ↓
Order becomes FILLED
      ↓
Portfolio books fill
```

The system must not incorrectly mark the order as cancelled.

### Cancel rejected because the order was already filled

``` text
cancel_order → REJECTED
fetch_order_state → FILLED

Result:
FILLED
NOT CANCELLED
```

### Missing broker record after successful cancellation

Some brokers remove the order record after cancellation.

Therefore:

``` text
cancel → ACCEPTED
fetch → REJECTED / no record

Result:
CANCELLED
```

The `REJECTED` fetch must not be interpreted as a genuinely rejected
order in this specific cancellation context.

------------------------------------------------------------------------

# 20. Safety Model

The project is designed around defense in depth.

Important safety risks include:

  -----------------------------------------------------------------------
  Risk                                Mitigation
  ----------------------------------- -----------------------------------
  Fill/cancel race                    Fetch authoritative state after
                                      cancellation

  Double-booking fills                Cumulative-to-delta reconciliation

  Cancel on UNKNOWN                   Refuse and reconcile first

  Cancel on terminal order            Refuse

  False rejection after cancellation  Cancellation-specific fetch
                                      interpretation

  Open-order count stuck              Correct `CANCELLED` transition

  Unauthorized cancellation           Authorization gate

  Audit ordering problems             Record request before broker call
                                      and outcome after settlement

  Dangerous retries                   No automatic retry path for
                                      uncertain execution

  Strategy directly executing         Strategy only proposes
                                      `OrderIntent`

  AI bypassing risk                   Risk/execution layers remain
                                      independent of strategy
  -----------------------------------------------------------------------

------------------------------------------------------------------------

# 21. Testing Philosophy

The project uses tests as a safety mechanism, not merely as a way to
check syntax.

The repository currently contains extensive tests covering areas such
as:

``` text
test_gateway.py
test_paper_broker.py
test_adapters.py
test_orders.py
test_lifecycle.py
test_resolve_unknown.py
test_dedupe_reconciliation.py
test_risk.py
```

The project should maintain the invariant:

``` text
Existing behavior must remain correct
        +
New behavior must have regression tests
```

Before considering a change complete:

``` bash
python3 -m unittest discover -s tests -t .
```

The full suite should continue to pass.

------------------------------------------------------------------------

# 22. Development Roadmap

## Stage 1 --- Core Trading Kernel

-   Order model
-   Order state machine
-   Broker ports
-   Portfolio
-   P&L
-   Risk primitives
-   Execution gateway
-   Audit system

## Stage 2 --- Reliable Order Lifecycle

-   Fill reconciliation
-   Cancellation lifecycle
-   UNKNOWN resolution
-   Idempotency
-   Race-condition handling
-   Extensive regression testing

## Stage 3 --- Paper Trading

-   Simulated market environment
-   Strategy execution
-   Portfolio dashboard
-   P&L tracking
-   Performance reports

## Stage 4 --- Market Intelligence

-   Market data integration
-   Indicators
-   Strategy engine
-   AI-assisted analysis
-   Trade proposal generation

## Stage 5 --- User Application

Potential UI features:

-   Dashboard
-   Market watchlist
-   Open positions
-   Orders
-   Trade history
-   P&L
-   Risk settings
-   AI analysis
-   Strategy settings
-   Approval queue
-   Audit history
-   Kill switch

## Stage 6 --- Broker Integration

-   Broker authentication
-   Production order adapter
-   Real-time order updates
-   Position synchronization
-   Reconciliation
-   Rate-limit handling
-   Production safety controls

## Stage 7 --- Controlled Autonomous Trading

-   User-configurable automation
-   Strict risk limits
-   Futures support
-   Automated execution
-   Continuous reconciliation
-   Alerts
-   Emergency kill switch

## Stage 8 --- Learning / Optimization

-   Backtesting
-   Strategy evaluation
-   Performance attribution
-   Market-regime analysis
-   Controlled strategy optimization
-   Paper validation before deployment

------------------------------------------------------------------------

# 23. Example End-to-End Future Flow

A future autonomous trade could look like:

``` text
Market data
    ↓
AI / Strategy Engine
    ↓
Trade Proposal
    ↓
Position sizing
    ↓
Risk engine
    ↓
Budget check
    ↓
Exposure check
    ↓
Daily loss check
    ↓
Futures/leverage check
    ↓
Authorization
    ↓
Kill switch
    ↓
Execution Gateway
    ↓
Broker
    ↓
Acknowledgement
    ↓
Fetch authoritative state
    ↓
Reconciliation
    ↓
Order state update
    ↓
Portfolio update
    ↓
P&L update
    ↓
Audit log
```

The same architecture should work whether the trade is:

``` text
Paper
   or
Real
```

with the broker adapter being the major difference.

------------------------------------------------------------------------

# 24. What This Project Is NOT

This project is not intended to be:

-   An AI that can freely spend money without limits.
-   A system that blindly trusts broker acknowledgements.
-   A strategy that directly calls the broker.
-   A system that retries uncertain orders automatically.
-   A self-modifying trading bot without validation.
-   A guarantee of profit.
-   A replacement for responsible financial decision-making.

The system is intended to make automated trading **more controlled,
observable, testable, and risk-aware**.

------------------------------------------------------------------------

# 25. Project Principles

The most important engineering principles are:

### 1. Safety before automation

Automation is useful only when the safety system is stronger than the
strategy.

### 2. Strategy and execution are separate

``` text
Strategy → OrderIntent
Execution Gateway → Actual order
```

### 3. Broker state is authoritative

When local state and venue state disagree, reconcile before making
assumptions.

### 4. Never double-book fills

Always convert cumulative venue quantities into local increments.

### 5. UNKNOWN is dangerous

Never blindly retry or submit another order when the previous execution
is uncertain.

### 6. Tests are part of the design

Every important lifecycle behavior should have a regression test.

### 7. AI does not own the safety rails

AI can analyze, propose, and optimize within defined boundaries.

### 8. Human control remains available

The user should always have the ability to disable automated execution
and activate the kill switch.

------------------------------------------------------------------------

# 26. Technology Direction

The current core is intentionally lightweight:

-   Python
-   Standard library
-   Modular architecture
-   Unit testing
-   Broker interfaces/adapters

Future layers may add:

-   Web/mobile UI
-   Market data APIs
-   Broker APIs
-   Database/storage
-   AI/LLM services
-   Backtesting infrastructure
-   Monitoring/alerting
-   Authentication and authorization

These should be added without compromising the core safety boundaries.

------------------------------------------------------------------------

# 27. Project Philosophy

The ambition is not simply:

> "Build a bot that predicts prices."

The ambition is:

> **Build a complete, auditable trading system where intelligent
> strategies can operate inside strict engineering and risk
> boundaries.**

That distinction matters.

A profitable-looking strategy is not enough.

A production trading system also needs to answer:

-   What order did we send?
-   Was it accepted?
-   Did it fill?
-   How much filled?
-   Did cancellation race with a fill?
-   What does the exchange say happened?
-   Did we book the fill exactly once?
-   How much capital is exposed?
-   Is the next trade allowed?
-   Why was the trade refused?
-   Can we reconstruct what happened afterward?

This project is designed around answering those questions reliably.

------------------------------------------------------------------------

## Disclaimer

This is a software engineering project and does not guarantee trading
profits or eliminate financial risk. Real-money trading, especially
leveraged futures trading, can result in rapid and substantial losses.
Production deployment should use appropriate broker controls,
conservative limits, extensive paper testing, monitoring, and human
oversight.

------------------------------------------------------------------------

## Development Status

**Current focus:** CoinSwitch sandbox access and real-data paper-model validation.

**Current reported test status:** `2,106 tests passing` (`4` loopback socket
tests are skipped when the environment forbids local binds).

**Current integrated milestone:** `RealtimePaperService.poll_once()` now performs
one deterministic operational cycle: ingest and validate quotes and closed bars,
synchronize prior order lifecycle state, fail closed on startup/data/lifecycle
findings, build a freshness-filtered strategy context, size signals, and submit
only through the existing execution gateway. It does not contain a network
client or a second execution route.

The local paper product is runnable with a supervised synthetic stream and a
loopback-only, read-only operator dashboard:

```bash
python3 -m trading.app --dashboard --state-db ./paper-state.sqlite3
```

Open `http://127.0.0.1:8765/`. The dashboard exposes status, readiness, cash,
positions, orders, audit history, and the last service cycle. Synthetic data is
clearly identified; live trading remains off.

For a reproducible local install, use Python 3.11+ and the dependency-free
package definition:

```bash
python3.11 -m pip install .
trading-paper --config config/paper.example.toml \
  --dashboard --state-db ./paper-state.sqlite3
```

The example TOML contains reviewed paper-risk limits only. The CLI rejects live
mode and API credentials in application profiles. `SIGINT` and `SIGTERM` request
the same ordered shutdown of dashboard, supervisor, stream, and persistence.
For a paced unattended soak—for example, six hours—run:

```bash
trading-paper --config config/paper.example.toml \
  --soak-seconds 21600 --poll-interval 1 \
  --state-db ./paper-soak.sqlite3
```

Before treating a wheel as releasable, run the automated installed-artifact
drill:

```bash
python3.11 scripts/release_drill.py --python python3.11
```

It builds and SHA-256 verifies the wheel, confirms there are no runtime
dependencies, installs it into an isolated target, checks the console command,
starts and health-checks the dashboard, waits for a flat checkpoint, sends
`SIGTERM`, restarts on the same SQLite state, and performs a second deterministic
recovery soak. Stopping during an open paper position is deliberately not called
a clean restart: the rebuilt in-memory paper venue cannot claim the old venue
position, so reconciliation correctly blocks it.

The November 10, 2026 final installed-artifact drill passed after the dashboard
usability review. The dependency-free wheel is
`dist/safety_first_trading_app-0.1.0-py3-none-any.whl`; its SHA-256 is
`951d301dcc95c33fc9bf0407ce5e7589b6297d03de301142e484aa4b22d3f094`.
The dashboard now labels every table column, bounds long histories in scrollable
panels, and includes public-feed connectivity and metadata expiry in its health
badge.

For that paper-only interruption case, the operator may explicitly adopt the
rebuilt in-memory paper venue (normally an empty position snapshot):

```bash
trading-paper \
  --recover-paper-position ./paper-state.sqlite3 \
  --confirm-paper-position-reset I_ACCEPT_PAPER_VENUE_POSITION_RESET \
  --paper-recovery-reason "discard interrupted local paper position after venue reset"
```

This deliberately overwrites the persisted **paper position**, records the old
and adopted values in the audit chain, clears the mismatch only after a fresh
comparison, closes the database, and verifies a clean rebuild. It refuses wrong
confirmation, empty reason, open/UNKNOWN orders, non-paper brokers, and states
without a mismatch. It is not available for CoinSwitch or live execution.

The same paper stack can consume CoinSwitch's unauthenticated Spot WebSocket
stream. Prices and candles are real venue data, but every order still goes only
to the in-process `PaperBroker`:

```bash
python3 -m trading.app --coinswitch-symbol BTC/USDT --state-db ./coinswitch-paper.sqlite3
```

CoinSwitch paper orders are venue-compatible LIMIT intents rather than MARKET
intents (the documented Spot API supports LIMIT only). By default a buy is
capped 10 basis points above the current ask and rounded upward to two price
decimals; a sell is floored 10 basis points below the bid and rounded downward.
Use `--limit-protection-bps` and `--limit-price-decimals` to match the selected
market's published precision. These are hard execution bounds, not estimates.
The optional venue specification also floors the base quantity to its published
step and refuses orders outside the published quote-notional range. All three
constraint flags must be supplied together, using exact metadata for the chosen
market rather than guessed values:

```bash
python3 -m trading.app --coinswitch-symbol BTC/USDT \
  --quantity-step <base-step> \
  --min-order-notional <quote-min> \
  --max-order-notional <quote-max>
```

Prefer a versioned offline capture of CoinSwitch's documented
`/trade/api/v2/tradeInfo` response when one is available:

```bash
python3 -m trading.app --coinswitch-symbol BTC/USDT \
  --coinswitch-market-info-file ./btc-usdt-trade-info.json
```

The capture envelope is `coinswitch-trade-info-capture/v1`. It records the exact
exchange, symbol, UTC `captured_at`, source path, base64-encoded response bytes,
and their SHA-256 checksum. The default maximum age is 24 hours; use
`--market-info-max-age-hours` within 1–720 hours when an explicit operating
policy requires a different window. Stale, future-dated, mismatched, corrupt,
or unsupported captures stop startup. The expiry also travels with the venue
specification, so an already-running app refuses new orders after it expires.

The offline translator derives base step and price decimals and validates
min/max quote notional. It rejects malformed precision, contradictory bounds,
and the currently ambiguous non-zero `precision.limit` adjustment. The capture
replaces the three manual constraint flags and must not contain credentials.
The checksum detects accidental payload corruption; it is not a CoinSwitch
digital signature and does not authenticate a maliciously rewritten file.

This mode contains no CoinSwitch credentials or order transport. Exchange
selection is constrained to the venue's documented market split:
`coinswitchx` for INR and `c2c1`/`c2c2` for USDT. The November 2, 2026 live
smoke check received BTC/INR book snapshots and BTC/USDT trade prints. Because
the venue's candlestick event acknowledged but did not deliver, the adapter
builds exact one-minute OHLCV bars from public trades; a live closed BTC/USDT
bar was verified end to end. No binary floating-point value enters financial
calculation.

The stream reconnects with bounded exponential backoff. While disconnected its
sources retain and surface the transport error, so ingestion remains fail-closed;
after a verified re-subscription the error clears and delivery resumes. Partial
trade bars are discarded across a disconnect rather than joined over an unknown
gap. Reconnect attempts, successful recoveries, and the redacted error type are
available in the dashboard snapshot. A finite, credential-free qualification
can now write atomic JSON evidence and exits non-zero unless it observes both
order-book quotes and a completed one-minute candle, completes the requested
duration, and finishes with the feed recovered:

```bash
trading-paper --coinswitch-symbol BTC/USDT --coinswitch-exchange c2c1 \
  --coinswitch-observe-seconds 14400 --poll-interval 1 \
  --observation-report ./artifacts/public-market-observation.json \
  --observation-data ./artifacts/public-candles.json
```

The November 10–11, 2026 four-hour live qualification passed with 26,260 quotes,
240 closed candles, seven transient feed-error polls, and four successful
reconnections from four attempts. Its `coinswitch-public-observation/v1`
evidence explicitly records public-data-only operation, no credentials, and
live trading off; the evidence SHA-256 is
`448bfe9c19d7e15f70dc5761877707fd8288fd0c395e1a648eba075fe293eaba`.
The optional `--observation-data` file uses the
`coinswitch-public-candles/v1` schema and preserves exact completed public
candles for offline research. It is marked `training_only`, contains no
credentials, and is never read directly by the running strategy. Training,
independent evaluation, and an exact model-bound paper approval remain separate
steps, so the app cannot retrain or promote itself.

Split a capture of at least 240 contiguous candles into two non-overlapping
datasets:

```bash
trading-paper --split-model-data ./artifacts/public-candles.json \
  --training-data-output ./artifacts/training-candles.json \
  --evaluation-data-output ./artifacts/evaluation-candles.json
```

Train an offline candidate from the first half:

```bash
trading-paper --train-model ./artifacts/training-candles.json \
  --model-output ./artifacts/candidate-direction-model.json
```

The dependency-free trainer fits a small logistic direction model on the oldest
70% of samples and evaluates it on the newest 30%. It rejects shuffled, gapped,
single-direction, unsafe, or undersized datasets and compares validation accuracy
with the majority-class baseline. Every output remains `approved=false`,
`paper_only=true`, and `self_promoting=false`; the execution service does not
load it automatically.

Evaluate the frozen candidate on a different contiguous dataset:

```bash
trading-paper --evaluate-model ./artifacts/candidate-direction-model.json \
  --evaluation-data ./artifacts/evaluation-candles.json \
  --evaluation-output ./artifacts/model-evaluation.json
```

The evaluator hashes both inputs and refuses either the training dataset or any
candle range that overlaps it from being presented as independent evidence. Its
market must also match the model's recorded symbol and exchange. Its result
remains unapproved and paper-only, even when it beats the holdout baseline.

After both stages beat their baselines, the exact frozen model can run in the
CoinSwitch paper application. Replace `<model_sha256>` with the value recorded in
`model-evaluation.json`:

```bash
trading-paper --coinswitch-symbol BTC/USDT --coinswitch-exchange c2c1 \
  --paper-model ./artifacts/candidate-direction-model.json \
  --paper-model-evaluation ./artifacts/model-evaluation.json \
  --confirm-paper-model "APPROVE PAPER MODEL <model_sha256>"
```

Startup refuses missing, failed, mismatched, or modified evidence. The strategy
is long-only, attaches ATR-based stops and targets, records the model hash and
probability on every signal, displays the active model hash prefix on the
dashboard, and still routes solely to the in-process paper
broker. It cannot promote itself, change risk limits, or reach live execution.

Run a fast, repeatable end-to-end safety soak before changing a transport or
configuration:

```bash
python3 -m trading.app --soak-cycles 190
```

It exits non-zero if a cycle is unhealthy, reconciliation blocks, UNKNOWN
execution remains, readiness is false, or the audit chain fails.

CoinSwitch's official documentation currently publishes only the production
Spot host and real-account API-key flow; it does not publish a sandbox/testnet
host. Accordingly, no authenticated HTTP transport was added. The offline
`CoinSwitchSpotResponseTranslator` now maps the documented order envelopes and
all lifecycle states into the injected sandbox boundary, preserves financial
JSON tokens without binary floats, and refuses contradictory data. The broker
observation contract now preserves a cumulative partial fill together with a
terminal cancelled/expired state. The gateway books only the fill delta first,
then makes the terminal transition atomically, so neither exposure nor venue
truth is lost.

This boundary was re-verified against CoinSwitch's official API Surfaces,
Quickstart, and Changelog on November 10, 2026. They still list production URLs
and real-account keys only; sandbox availability therefore requires written
confirmation from `api@coinswitch.co` before authenticated transport work starts.
A credential-free request template is available at
[`docs/COINSWITCH_SANDBOX_REQUEST.md`](docs/COINSWITCH_SANDBOX_REQUEST.md).

The dashboard also retains a bounded, in-memory, transition-only alert feed for
readiness blocking/restoration, market-data loss/recovery, metadata
warning/expiry/refresh, and supervisor failure/recovery. Repeated polling does
not duplicate alerts, messages are redacted, and the surface remains read-only.

The November 3, 2026 durable qualification produced two checksum-backed evidence
artifacts under `artifacts/`:

- `qualification-soak-2026-11-03.json`: 62.401 seconds, 247/247 healthy paced
  cycles, 50 executions, zero UNKNOWN outcomes, zero open positions, 798 audit
  records, and a matching SQLite SHA-256.
- `endurance-soak-2026-11-03.json`: 10,013/10,013 healthy cycles (527 complete
  strategy paths, about seven days of synthetic one-minute market time), 2,106
  executions, zero UNKNOWN outcomes, zero open positions, 32,666 audit records,
  and a matching SQLite SHA-256.

Timed qualification rounds to complete synthetic strategy paths and refuses to
pass with any open position. Evidence is written atomically only after the
database closes and includes its final filename, byte size, and hash.

**Next major milestone:** Collect a separate 240+ candle training capture and
obtain written CoinSwitch confirmation of a non-production test environment.
The local paper product and four-hour public-data qualification are complete;
production execution remains deliberately absent.
