# Historical acquisition recovery report

Scope: BTC/USDT `c2c1`, one-minute candles, development period only. The range ends at `2026-09-03T03:44:00Z`; sealed confirmation data and Segment 7 were not accessed.

## Acquisition completeness

The original artifact remains byte-for-byte unchanged:

- File: `artifacts/backtest-btc-usdt-1m-2026-07-05_2026-09-03-development.json`
- File SHA-256: `20023a0c37bec03343a7ffd3b2eaf18df43a869d92578eb47cf479d3a9b6dceb`
- Candle SHA-256: `261172f176bb2717f3a153eb6789ad749850c998482ff8f7d19be3bc65617866`
- Candles: 56,399 of 86,400

Recovery was written separately:

- File: `artifacts/backtest-btc-usdt-1m-2026-07-05_2026-09-03-development-recovered.json`
- File SHA-256: `2c36a429413621ca33788fd627999658c9afccc98c4f184b994c05d9cbd5a871`
- Requests: 76
- HTTP results: 76 × `200`
- Transport retries: 0
- Returned candles: 0
- Recovered timestamps: 0
- Consistently unavailable timestamps: 30,001
- Unresolved timestamps: 0
- Request-ledger SHA-256: `fd0d34be826024f78b9e18137370c3aa79b2c0fbfc36189e33f311aa49c6036c`
- Integrity: **FAIL**, with 20 gaps and 21 contiguous segments

Before full recovery, representative one-minute, 73-minute and one-hour windows were each requested twice. Every request returned HTTP 200, an empty data array and response SHA-256 `8fe32e407a1038ee38753b70e5374b3a46d6ae9d5f16cd5b73c53abaca8f5ed0`.

The evidence rules out client-side pagination and rate limiting for these recovery attempts: every missing range was subdivided into at most 1,440-minute windows, requested twice, returned HTTP 200, and returned no candles. It does not prove that no trades occurred. The timestamps are classified only as consistently unavailable from the CoinSwitch `c2c1` historical candle endpoint. Venue-history retention or upstream availability remains the likely external limitation.

No candle was interpolated, forward-filled or fabricated.

## Backtesting assumptions

Each contiguous segment starts with fresh 10,000 USDT equity and independent indicator warm-up. The existing Moving Average Crossover remains unchanged. Execution is at the next candle open with 10 bps fees, 2 bps spread, 2 bps slippage, 0.5% risk per entry and the existing risk ceilings.

| Segment | Candles | Trades | Total return | Status |
|---:|---:|---:|---:|---|
| 1 | 6,076 | 116 | -2.8079% | evaluated |
| 2 | 1,469 | 27 | -0.7504% | evaluated |
| 3 | 3,221 | 64 | -1.6240% | evaluated |
| 4 | 5,011 | 93 | -2.6041% | evaluated |
| 5 | 6,465 | 128 | -3.2024% | evaluated |
| 6 | 6 | — | — | insufficient warm-up |
| 7 | 6,771 | 86 | -2.1650% | evaluated |
| 8 | 164 | 3 | -0.0728% | evaluated |
| 9 | 1,560 | 29 | -0.7352% | evaluated |
| 10 | 2,908 | 50 | -1.2653% | evaluated |
| 11 | 1,233 | 5 | -0.1307% | evaluated |
| 12 | 54 | 0 | 0.0000% | evaluated |
| 13 | 3 | — | — | insufficient warm-up |
| 14 | 3,201 | 53 | -1.1526% | evaluated |
| 15 | 233 | 3 | +0.0189% | evaluated |
| 16 | 1,372 | 27 | -0.5317% | evaluated |
| 17 | 1,116 | 21 | -0.3975% | evaluated |
| 18 | 379 | 6 | -0.1997% | evaluated |
| 19 | 19 | — | — | insufficient warm-up |
| 20 | 14,990 | 296 | -7.4854% | evaluated |
| 21 | 148 | 3 | -0.0705% | evaluated |

Exact boundaries, segment hashes, metrics, trades and equity curves are stored in `artifacts/backtest-ma-crossover-recovered-segments.json` (SHA-256 `aa04d789ef9e6387b282ec0deebed13bf4d9dfde5e96e8d9f13eb909bbc6636f`).

The separate aggregate gap-safe result is unchanged: 1,010 trades, -25.1697% total return and 25.1697% maximum drawdown. Its artifact is `artifacts/backtest-ma-crossover-recovered-aggregate.json` (SHA-256 `341311a43dd04192efe494f3d841ee9e6b9a58843d06b448dcd504f484bca973`).

## Interpretation

Acquisition completeness and strategy performance are independent conclusions:

- Acquisition remains incomplete because 30,001 timestamps are unavailable.
- Segment backtests avoid crossing gaps, but the segments have unequal lengths and market regimes.
- The predominantly negative results do not establish profitability or stable out-of-sample performance.
- No parameter optimization, model promotion, paper orders or live orders were performed.

## Verification

- Focused historical/backtest tests: 23 passed
- Full suite: 2,152 passed, 4 skipped
- Tests cover retry evidence, checkpoint/resume, partial recovery, exact duplicate suppression, conflicting duplicate refusal, gap handling, independent warm-up and deterministic segment reports.
