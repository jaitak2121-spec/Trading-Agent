# Trading-Agent — 0.3.3rc3

A local, safety-first cryptocurrency **paper-trading application and research toolkit**.
It combines genuine CoinSwitch market-data collection, an automated strategy,
risk-checked simulated execution, persistent accounting and a localhost dashboard.

**Engineering candidate; operational acceptance pending. Real-money execution is
disabled. No ML model has an established predictive edge, no strategy has qualified
for profitability, and paper/backtest profits do not establish live profitability.**

## Start here

- [macOS installation and isolated dashboard check](docs/MACOS_INSTALLATION.md)
- [Final release checklist and summary](docs/FINAL_SOFTWARE_RELEASE.md)
- [Existing-account recovery checklist](docs/PAPER_OPERATOR_RECOVERY_CHECKLIST.md)
- [CoinSwitch precision clarification request](docs/COINSWITCH_RULE_CLARIFICATION_REQUEST.txt)
- [Engineering audit and evidence history](docs/FINAL_COMPLETION_AUDIT.md)

The genuine BTC/USDT c2c1 account is blocked pending authoritative interpretation
of `precision.limit=2`, effective quantity/tick rules and minimum BTC quantity.
The retained unsupported capture is correctly rejected. **Do not run the genuine
launcher, release Emergency Stop or resume that account during installation.**
Use the isolated PAUSED dashboard check in the Mac guide; it cannot submit orders
while paused and is explicitly synthetic, not genuine-market performance evidence.

## Architecture

```text
CoinSwitch public quotes → normalization/freshness → closed candles → strategy signals
                                                      ↓
Dashboard ← SQLite/accounting ← paper fills ← gateway/risk ← sizing/order rules
                                 ↑
                    lifecycle/reconciliation/idempotency

Genuine stored candles → chronological ML research/evaluation → approval registry
                                   (independent qualification required)
```

| Location | Responsibility |
|---|---|
| `trading/core` | Domain values, permissions, risk, order lifecycle and execution controls |
| `trading/strategy` | Signal strategies, indicators and risk-based sizing; strategies cannot place orders |
| `trading/adapters/coinswitch` | Public market feed, scoped historical/metadata GET readers, offline execution contracts |
| `trading/adapters/paper` | Simulated broker, costs, inventory and idempotent fills |
| `trading/adapters/persistence` | SQLite account/audit/candle storage and verified backups |
| `trading/adapters/http` | Local dashboard, controls, snapshots, readiness and health |
| `trading/app` | Composition, service/supervision, research, training, approval and monitoring |
| `tests`, `scripts`, `config`, `docs` | Tests, operator tools, example configuration and evidence |

## Strategy and paper execution

The default strategy is long-only Moving Average Crossover: fast SMA 10, slow SMA
30, ATR 14, stop distance 2 ATR and reward multiple 2. It uses closed candles,
enters on an upward cross and exits on the reverse cross. These are implemented
rules, not a proven trading edge. Runtime exits use sequential capped orders;
pending/partial orders suppress duplicates and untradeable residuals remain visible.

Genuine-paper defaults cross observed bid/ask and assume **10 bps fee plus 2 bps
adverse slippage per fill**. These are simulation assumptions, not verified account
fees or guaranteed exchange fills. Paper simulation does not model all queues,
latency, liquidity, tax or exchange failure behavior.

The USDT example profile caps order/position/gross notional at 1,000 USDT, daily
loss at 100 USDT, one open order/position, drawdown at 10%, and entry risk fraction
at 0.001. Exact configuration is in `config/paper-usdt.example.toml`. Exits still
pass risk, inventory, precision, minimum-notional, metadata and state checks.

## Safety, state and persistence

- PAPER is the default. No live exchange order transport is enabled; mocked
  lifecycle contracts do not establish CoinSwitch trading permissions/readiness.
- Running, paused, stopped and emergency-stopped states are audited. No execution
  is allowed while inactive. Restart does not automatically resume an account;
  Emergency Stop remains latched until explicitly authorized operator recovery.
- SQLite persists orders/fills, balances, positions/basis, P&L, equity, risk state,
  bot transitions, signals/predictions and audit records. WAL, writer ownership,
  hash-chain validation and idempotency protect restart consistency.
- Cash, realized/unrealized P&L, fees and equity are separate. Stale marks produce
  unavailable valuations rather than invented prices. Never reset a database to
  clear a safety incident or change `--cash` expecting existing balances to reset.
- Verified SQLite backups include committed WAL data and refuse overwrite.
- Genuine closed candles retain provenance, hashes and duplicate/conflict checks.
  Gaps are not filled or passed through feature/label windows silently.

## Dashboard and learning

The localhost dashboard exposes portfolio/trades, bot/risk state, genuine-versus-
synthetic provenance, feed timestamps, venue metadata, experience and ML status.
`/api/snapshot` returns observations; `/readyz` verifies startup/collection readiness;
`/healthz` is stricter trading health and can correctly return 503 while paused.
The launcher starts learning only after its own process and genuine origin pass
readiness. Controls require localhost/token checks; they do not bypass the latch.

Bounded scheduled learning uses validated genuine contiguous experience,
chronological development validation and training-only preprocessing. Minimum data
and new-data cooldowns prevent pointless refits. Weak models are rejected; other
candidates remain held for independent qualification. Monitoring supports drift,
performance alerts, pause and controlled rollback. Availability of these modules
is not evidence that a qualified model is active or that learning is improving P&L.

## ML research and backtesting

Existing logistic-regression experiments, causal feature schemas, horizon-specific
labels, walk-forward folds, baselines, bootstrap diagnostics and registries retain
both accepted evidence and failures. Initial and expanded experiments did **not**
establish a qualified predictive/net trading edge. Rejected models and sealed
confirmation periods must not be reused for tuning or automatic deployment.

The historical MA backtester uses causal decisions and later execution, fees,
spread/slippage, equity/drawdown, trades and risk limits. It reports returns,
Sharpe, win rate, profit factor and benchmarks. Explicit gap/segment handling does
not make incomplete datasets complete. Only eligible development data may be used
for research; no qualification from failed integrity or repeatedly inspected tests.

See [research findings](docs/ML_PROFITABILITY_RESEARCH_REPORT.md),
[continuation](docs/ML_RESEARCH_CONTINUATION_REPORT.md),
[lifecycle integration](docs/ML_LIFECYCLE_COMPLETION_0_3_1.md) and
[backtest limitations](docs/BACKTEST_RESEARCH_REPORT.md).
Historical downloads need separately configured credentials/permissions; public
feed and isolated dashboard inspection do not. Never place secrets in Git or reports.

## Development and verification (source checkout)

Python 3.11+; no mandatory third-party runtime dependencies. The optional
`historical-data` extra uses cryptography. Do not install extras for dashboard checks.

```sh
python3 -m venv .venv
.venv/bin/python -m pip install --no-index --no-deps /path/to/release.whl
.venv/bin/python -m unittest discover -s tests -t . -q
.venv/bin/python -m trading.app --help
.venv/bin/python -m trading.app.backtest_cli --help
```

Tests use isolated fixtures; their synthetic P&L is not market evidence. Release
artifacts, source/build hashes and installed-dashboard evidence accompany the
release bundle. Protected historical datasets/models, credentials and the existing
paper account are deliberately excluded. Unit-test success is engineering evidence;
operational acceptance requires the separate recovery and observation checklist.
