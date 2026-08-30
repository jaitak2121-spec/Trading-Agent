
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
Full suite: 1477 tests passing
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

**Current focus:** Reliable order lifecycle and cancellation
reconciliation.

**Current reported test status:** `1477 tests passing`.

**Next major milestone:** Complete Phase 2 cancellation lifecycle,
regression tests, and verification before expanding into higher-level
AI, UI, and production broker functionality.
