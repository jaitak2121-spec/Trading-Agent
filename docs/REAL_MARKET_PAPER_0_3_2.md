# Genuine-market paper integration — 0.3.2

2026-11-13. The genuine BTC/USDT paper application is running at
**http://127.0.0.1:8766/**. No real-money execution is enabled. No profitable or
approved ML model is claimed. Existing strategy, risk, broker and learning code
was reused; no research study was repeated.

## Changes

- `scripts/start-paper.command` now launches CoinSwitch BTC/USDT c2c1, genuine
  public bid/ask and trade-built candles, existing MA crossover, a 10,000-USDT
  virtual account, 10bp fees per fill and 2bp adverse slippage. Spread comes from
  observed bid/ask, not invented historical quotes. Fills remain simulations;
  queue, latency, taxes and current account-specific fees are not guaranteed.
- Dedicated account: `artifacts/real-paper-btc-usdt-v032.sqlite3`. Startup checks
  persisted market/domain/completeness provenance **before opening a writer**.
  Synthetic, legacy unverified and differently bound accounts cannot be mixed.
  Old `paper-state.sqlite3` and its demo P&L remain untouched and isolated.
- Dashboard labels REAL-MARKET PAPER, SYNTHETIC DEMO or unverified results;
  exposes provenance, real quote timestamps/prices, execution assumptions,
  persisted collection count, portfolio/orders/risk and learning status.
- Fixed two previously untested scheduler integration defects: invoking the
  shutdown property as a method, and experience workers falling through to
  monitor dispatch. Actual bounded worker tests now cover training and waiting.
- Launcher starts the existing bounded experience scheduler, hourly, alongside
  the application and stops its owned children on shutdown. It generates
  `artifacts/real-paper-learning/job-v032.local.json` once and preserves it.
  Scheduler status/readiness are persisted and shown in the dashboard.
- Learning requires a genuinely bound account, hashed rows and the latest
  contiguous run. It waits for 720 candles, fits/evaluates chronologically,
  retains artifacts, rejects weak models and holds all candidates pending
  independent cost-aware qualification and explicit paper approval. Existing
  approval/revocation checks, monitoring safety pauses and explicit rollback
  remain in force. No automatic model deployment.
- Startup/reconnect now discards the first trade-built bucket: subscription may
  start mid-minute, so that bucket cannot represent full OHLCV coverage. Gaps
  remain gaps; no candles are filled or interpolated. The initial four-candle
  verification account predates this guard and was preserved separately, marked
  ineligible, never used for training. The new account records the completeness
  policy in its provenance, preventing accidental legacy adoption.

## Verified scope

**115 focused tests passed. Full suite: 2,355 passed, zero skips.** Five new tests
cover native Socket.IO/trade aggregation → existing MA → sizing/risk → paper
fills/fees → persisted portfolio → dashboard/restart, read-only domain mismatch
refusal, actual bounded experience-worker dispatch, insufficient-data waiting,
and startup/reconnect partial-minute exclusion. Native feed integration prices
are explicitly controlled test fixtures, not genuine profit evidence.

Actual public-feed check of the new running account independently verified:

- Advancing genuine quote timestamps, connected BTC/USDT c2c1 feed.
- Two complete minute candles at the evidence snapshot, valid OHLCV, zero gaps
  or duplicates; persisted and hash-validated. Collection continues afterward.
- RUNNING / READY paper service, initial equity 10,000 USDT, zero orders at that
  snapshot; MA warm-up/crossover must occur naturally. No genuine-fill claim.
- Hourly scheduler waiting normally; ML awaiting experience, no model deployed.
- No API credentials or authenticated exchange/order calls.

Evidence: `artifacts/real-market-integration-2026-11-13/` contains operational
snapshot, validated candle snapshot, initial ineligible-check evidence, tests and
release metadata. Candle snapshot file SHA-256:
`b78f9e72a40a69ba7c4451b937fa741d02345d521226e8a7cbdff9d15355c62d`.
Compilation, CLI and whitespace checks passed. v0.3.2 wheel has 131 Python
modules byte-matched to source and passes isolated import/CLI verification:
`dist/real-market-paper-0.3.2/safety_first_trading_app-0.3.2-py3-none-any.whl`.
SHA-256: `ea8ed1363c81b7c6bfc055740f012cd4767a1cf1b9b3d0a87eca4ef6e2db585b`.

Files changed: launcher; runtime; persistence SQLite provenance checks; native
stream completeness guard; dashboard; experience learning; scheduler; release
version/verifier; native-stream, experience and new real-market integration tests;
README, operating guide and completion reports. No dependencies added, protected
datasets opened, rejected models changed or live execution activated.

## Launch and operate

The app is already running on port 8766; do not start a second writer. For the
next launch from this checkout:

```sh
cd "/Users/jaitak/Documents/Trading app"
./scripts/start-paper.command
```

After a clean stop/restart, inspect recovery and explicitly resume through the
local dashboard, or launch with `./scripts/start-paper.command --resume-paper`.
No state reset is needed. Ctrl-C in the launcher terminal gracefully stops its
application and scheduler. Pause stops new trading while genuine collection
continues. The old port-8765 demo is separate; it is not the genuine account.

Direct application CLI (learning scheduler must then be run separately):

```sh
.venv/bin/python -m trading.app --coinswitch-symbol BTC/USDT \
  --coinswitch-exchange c2c1 --config config/paper-usdt.example.toml --cash 10000 \
  --fee-bps 10 --slippage-bps 2 --port 8766 --paper-controls \
  --state-db artifacts/real-paper-btc-usdt-v032.sqlite3 \
  --learning-reports artifacts/real-paper-learning
.venv/bin/python -m trading.app.research_jobs \
  artifacts/real-paper-learning/job-v032.local.json
```

Remaining: enough contiguous forward experience (at least 12 hours for 720 bars,
longer if gaps), independent economic qualification of a candidate, and extended
genuine-market paper validation. Classification screening is not profitability.
No model is approved; the MA paper baseline is not claimed profitable. CoinSwitch
sandbox/permissions/live qualification remain unverified and real orders disabled.
