# Historical development extension — 2026-11-12

## Latest targeted recovery — 2026-11-12

Reused the existing GET-only recovery CLI against the four missing May 7 minutes,
using the original hash and a separate `2025-05-07.recovered.json` checkpoint.
The exact missing ranges were requested twice each: six HTTP 200 responses,
one attempt per request, zero returned candles and zero recovered timestamps.
All four are classified as consistently omitted by these requests, with zero
unresolved timestamps under the existing repeated-observation rule. This
classification does **not** establish zero trading volume, permanent absence,
or the root cause. The official [candle reference](https://api-trading.coinswitch.co/spot/reference/candles)
does not supply a guarantee that every minute is returned or establish the
semantics of these particular omissions. No interpolation or inferred OHLCV was used.

The recovered artifact still fails: **1,436/1,440 candles, four missing minutes,
three gaps**. Independent timestamp-set, duplicate/order, close-boundary and
finite OHLCV checks confirm the failure. Original file bytes remain unchanged.
The 180-day acquisition was not resumed and no combined dataset was published.

Evidence under the same acquisition directory:

- `2025-05-07.recovered.json`, SHA-256
  `4d27f8cf8f702f36be7f9f5d4735cc670e282958ba36d74c7f82aa783c177d9e`.
- `targeted-recovery-verification.json`, SHA-256
  `1d56557632ee14c1f72b11a31e0f3751e8ec573f109ffe68a70e3b4ce5b57613`.
- Original candle hash still
  `132f92dda3ac2dc5dcbab9dfbf47fecba2136408e24ba83f3bdfbb28ccb61fe8`.

Requests covered 02:35–02:36, 12:13–12:14 and 18:02–18:04 UTC, twice each.
Every response body hash was
`8fe32e407a1038ee38753b70e5374b3a46d6ae9d5f16cd5b73c53abaca8f5ed0`.
The preserved request ledger contains timestamps, exact ranges, counts and hashes;
no credentials or signed headers were persisted.

No new candidate was trained or evaluated. Prior failed research remains rejected.
The current all-or-nothing completeness protocol cannot proceed. Alternatives
require a new explicit data protocol: investigate surrounding-window/venue
behavior without merging unrelated markets, preregister gap-separated development
runs with fresh warm-ups and no cross-gap labels, or collect new prospective data.
None can be represented as a fully recovered continuous source. Final-test and
confirmation observations remain protected. Focused tests: **122 passed**;
full suite: **2,238 passed, no skips**.

## Initial acquisition record — preserved below

**Status: stopped at the first incomplete day. No usable 180-day dataset has
been claimed or published. No training, strategy evaluation or orders occurred.**

The six cost-aware hypotheses remain rejected. This milestone investigated
whether a larger independent development source could be acquired, rather than
retuning those hypotheses. Older data broadens regimes but is not a prospective
confirmation of a strategy chosen after examining more recent markets.

## Acquisition and verified boundaries

Used the existing authenticated GET-only CoinSwitch adapter, candle normalizer,
integrity validator and atomic JSON writer. The two credentials were resolved
from the previously supplied macOS Keychain services for the current account,
captured without displaying them, and passed only to the downloader child process.
Credentials were removed from its environment after transport construction. No
credential value was stored in artifacts, source, command arguments or reports.

An availability probe requested BTC/USDT, `c2c1`, one-minute candles for
**2025-11-01 00:00 to 2025-11-02 00:00 UTC, end exclusive**. It returned all
1,440 expected candles. Acquisition and stored-data validation passed.

The proposed extension is **2025-05-06 00:00 to 2025-11-02 00:00 UTC**, exactly
180 days / 259,200 expected minutes. It ends before the existing 365-day research
source begins; it does not overlap its confirmation or the October–November 2026
protected ML Segment 7. Only metadata/preregistration was consulted for separation;
protected observations and rejected model files were not opened.

Added `trading/app/research_acquisition.py`, a fixed-range daily checkpoint driver,
not a new downloader. It reuses existing implementations, validates each persisted
day before continuing, verifies recorded hashes on resume, preserves failed days,
waits one second after requests, and retains the transport's bounded retry/backoff.
It refuses to overwrite a completed combined dataset. A failed cached day remains
blocked on restart; it is not silently replaced or skipped. Intended for one
operator invocation at a time; concurrent writers are not supported.

## Actual outcome

| Requested day UTC | Actual / expected candles | Missing | Duplicates | Integrity |
|---|---:|---:|---:|---|
| 2025-05-06 | 1,440 / 1,440 | 0 | 0 | PASS |
| 2025-05-07 | 1,436 / 1,440 | 4 | 0 | FAIL |

The second day has three gaps containing four missing timestamps:

- 2025-05-07 02:35 UTC
- 2025-05-07 12:13 UTC
- 2025-05-07 18:02 UTC
- 2025-05-07 18:03 UTC

Both requests returned **HTTP 200 on the first attempt**, with no retries. This
does not prove legitimate zero-trade omissions or exclude an endpoint/pagination
limitation. All four missing timestamps are **unresolved**, not classified as
consistently unavailable. No further acquisition or targeted recovery was attempted
after validation failed.

An independent check, separate from the application validator, verified exact
timestamp membership, ordering, duplicates, minute alignment, interval and close
boundaries, finite positive OHLC, OHLC consistency and nonnegative volume. It
confirmed the same four missing minutes. No values were filled or invented.

## Preserved evidence

Under `artifacts/development-extension-2025-05-06_2025-11-02/`:

- `2025-05-06.json`: SHA-256
  `0e53005256271b170f476b8ecbaa9d7d9a0ea95b364abafb6c17b5c380ef23b7`.
- `2025-05-07.json`: SHA-256
  `43e38d1248b964d37f74846496aa6fd88c5827cc0272ddc0d892ba9e253a9281`.
- `acquisition.json`: exact requested ranges, request times, HTTP statuses,
  attempts, response hashes and the validated daily checkpoint hash.
- `independent-integrity.json`: explicit missing timestamps and independent
  checks; SHA-256
  `24d6184da48ce055ab2f18b5b54ba89224b2fbbdc4f1aee6348863973cb21ef3`.

Probe evidence: `artifacts/historical-extension-availability-probe.json` and
`artifacts/btc-usdt-1m-2025-11-01_2025-11-02-availability-probe.json`.
Probe dataset file SHA-256:
`7b76e82413b28b05b878708e43d66d396fcac6cca0ea8c331c4c9bbfc36e2a73`.

No combined `development-candles.json` was created. Original datasets, frozen
research plans, previous results and protected artifacts were preserved.

## Verification and next step

Added seven tests covering complete days, market/range/interval mismatches, gaps,
duplicates, invalid OHLC, protected-range separation, missing credentials and safe
restart refusal for invalid or hash-mismatched checkpoints. Focused historical,
acquisition, cost-aware and backtest tests: **40 passed**. Compilation and
whitespace checks passed. Final full suite: **2,226 passed, no skips**, using
package-aware discovery with localhost/worker access. The initial full-suite run caught the repository's import
policy because tests imported a script package; the helper was moved into
`trading.app` without relaxing the policy.

Next action: perform a separately reviewed targeted GET recovery of the four
timestamps using the existing recovery utility and a **separate** recovered daily
artifact. If recovered and independently validated, resume remaining acquisition;
otherwise investigate the endpoint and preregister a segment-based approach or
use prospective data collection. Do not skip gaps to declare the 180-day dataset
complete, and do not train against these partial results.
