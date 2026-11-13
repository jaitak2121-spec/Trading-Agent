# Public depth and execution-cost diagnostic — 0.2.4

This research tool measures displayed liquidity, without placing orders. It does
not qualify a strategy, estimate profitability, or enable live execution.

## Implementation and protocol

The existing credential-free CoinSwitch stream now optionally retains all book
levels on quote messages. Its default output and production behavior are
unchanged. CoinSwitch documents these as full snapshots, with descending bids
and ascending asks, rather than incremental updates:
[official order-book interface](https://api-trading.coinswitch.co/spot/websockets/order-book).

Before acquisition, recorded the fixed protocol in
`config/public-depth-observation-plan-2026-11-12.json`: BTC/USDT, c2c1, 120 seconds,
100/500/1,000 USDT midpoint notionals, and a 10bp fee scenario matching the
independently verified current account snapshot. No additional private request
or credential access was made.

For each snapshot, base quantity equals requested quote notional divided by
midpoint. Buy and sell sweeps are independent: consume asks or bids in order,
respecting each displayed quantity. These are not linked round-trip trades.
Full-size VWAP and cost estimates are withheld if visible depth is insufficient.
The buy quote spend can exceed the midpoint notional; that notional is a sizing
reference, not a cash budget or risk-authorized position.

Reported directional cost relative to midpoint includes fee on swept quote
notional. The parser rejects market/exchange mismatches, crossed quotes,
nonpositive/nonfinite quantities or prices, malformed or unsorted levels,
duplicate price levels, disagreement with best quotes, and invalid timestamps.
Samples older than five seconds are excluded; snapshots more than five seconds
in the future fail. Exact timestamp/depth duplicates are excluded. Raw capture
is checkpointed every five seconds; output collisions are refused. A hard kill
may lose the final checkpoint fragment; no automatic restart or trading occurs.

## Reproduction

Use a new filename on every run; existing evidence is never overwritten:

```sh
.venv/bin/python -m trading.app.depth_costs artifacts/public-depth-NEW.json --seconds 120
.venv/bin/python -m unittest tests.test_depth_costs tests.test_execution_costs tests.test_coinswitch_spot_stream tests.test_coinswitch_observation tests.test_core_purity -q
```

The command produces the raw capture plus a `.costs.json` report bound to both
the capture's canonical content hash and exact file SHA-256. Each report has its
own canonical SHA-256. No account lookup occurs; the command's fixed 10bp fee is
an explicit scenario, not a continuously refreshed or historical fee claim.

## Actual observation and verification

Captured 120.0023 seconds: 96 snapshots, 93 fresh unique books, three exact
duplicates, zero stale samples. Each initial side contained 20 price levels.
There were **two feed-error polls, one reconnect attempt and one successful
reconnect**. No closed trade candle was received, so the existing combined
quote/candle observer returned **FAIL** and the command exited 2. This status is
preserved; acquisition completion does not imply feed qualification.

The 120.0023 seconds are **monotonic observer time**, not a verified continuous
wall-time sample. Independent timing inspection found 563.7371 seconds between
start and finish, a 557.328-second receive span and a maximum 506.978-second
receive gap. Timestamps are nondecreasing. Wall/monotonic divergence may include
suspension or clock adjustment; the cause was not established. This is another
reason to reject continuous feed qualification. Timing evidence is preserved in
`artifacts/public-depth-capture-2026-11-12.timing.json`, bound to the raw file hash.

All three requested sizes fit within the best displayed level in every observed
book. No full-depth shortfall occurred in this sample. Median directional cost
including the explicit 10bp fee was about **10.00068030bp buy** and
**10.00067894bp sell**, essentially identical at all three sizes. No realized
execution or future market-impact claim follows from that observation.

Independently checked 576 directional sweeps using separate float arithmetic;
maximum difference from decimal calculations was 1.35e-12bp, below the 1e-8bp
verification tolerance. Exact source/report hashes and protocol agreement were
also verified. Original evidence was retained after adding explicit observer
PASS/FAIL fields to future diagnostic reports.

Artifacts:

- `artifacts/public-depth-capture-2026-11-12.json` — raw public capture;
  SHA-256 `b68b662a78673808c10f44a43635479bcb0feb7b162382542a39c1ac58311b51`.
- `artifacts/public-depth-capture-2026-11-12.costs.json` — original diagnostic,
  preserved unchanged, bound to the raw capture.
- `artifacts/public-depth-capture-2026-11-12.status-verified.json` — derived
  diagnostic with explicit failed observer status;
  SHA-256 `4466715f6d1a1f859fa62d92b68839a0c226df876a6e6c74ba5bd5539c0bcd9e`.
- `artifacts/public-depth-capture-2026-11-12.verification.json` — independent
  arithmetic, protocol and hash checks. No additional market acquisition.

Focused tests initially passed 84 checks. Final full suite: **2,290 passed,
zero skips**. Added 11 tests covering arithmetic, insufficient depth, malformed
books, stale/duplicate handling, reproducibility, opt-in compatibility, capture
reuse, output collisions, failed-observation disclosure and release contents.
Compilation and whitespace checks passed. Offline wheel installation, local
dashboard and synthetic crash/restart drills passed. Final installed release:
`dist/depth-ready-0.2.4-final/safety_first_trading_app-0.2.4-py3-none-any.whl`,
SHA-256 `9bc3f7a3ebcf77d7cb1e5d0cdc37e25f0d724735173ae4be9f55f74e823091e6`.
Verification: `artifacts/release-0.2.4-depth-ready-final-verification.json`.

Files changed in this milestone: `spot_stream.py`, `execution_costs.py`, new
`depth_costs.py`, new `test_depth_costs.py`, `test_app_release.py`, `release.py`,
`pyproject.toml`, the fixed protocol JSON and accompanying documentation. The
larger existing working tree was preserved.

## Limits and next milestone

Displayed depth can disappear before submission. Snapshots cannot establish
latency, queue position, hidden liquidity, actual fill quality, fee settlement,
taxes or trading permissions. Receive timestamps are observer polling times.
Event-weighted percentiles over a short window are not time-weighted estimates
or evidence across market regimes. Current depth must not replace unknown
historical costs in failed backtests.

No model was trained, promoted or evaluated; protected observations were not
read. No profitability research criteria, risk controls, strategy thresholds or
production order paths changed. All previously rejected strategies remain
rejected and actual live execution remains disabled.

Next safe milestone: preregister a longer public execution-quality observation
across multiple periods, retaining depth coverage and outage evidence before
choosing any new cost assumptions. Strategy research still needs an independently
justified hypothesis and fresh eligible development data. Exchange sandbox
onboarding, account permissions and actual lifecycle qualification remain
external requirements for real-money work.
