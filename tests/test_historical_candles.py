"""Historical CoinSwitch candles are genuine, ordered and training-safe."""

from __future__ import annotations

import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path

from trading.adapters.coinswitch.historical_candles import (
    CandleFetchResult,
    CoinSwitchCandleTransport,
    CoinSwitchCandleTransportError,
)
from trading.app.historical_candles import (
    HistoricalCandleError,
    download_historical_candles,
    recover_historical_candles,
    validate_candle_dataset,
)
from trading.core.secrets import Secret


def candle(start: int, *, close: str = "101") -> dict[str, str]:
    return {
        "o": "100",
        "h": "102",
        "l": "99",
        "c": close,
        "volume": "2.5",
        "symbol": "BTC/USDT",
        "interval": "1",
        "start_time": str(start),
        "close_time": str(start + 60_000),
    }


class HistoricalCandleDownloadTest(unittest.TestCase):
    def test_transport_retries_a_mocked_rate_limit_without_exposing_writes(self) -> None:
        calls = []
        waits = []

        def opener(path, headers, timeout):
            calls.append((path, headers, timeout))
            return (429, b"") if len(calls) == 1 else (200, b'{"data":[]}')

        transport = CoinSwitchCandleTransport(
            api_key=Secret("mock-api-key-not-a-credential"),
            api_secret=Secret("mock-secret-not-used-by-injected-signer"),
            clock_ms=lambda: 1234,
            wait=waits.append,
            opener=opener,
            signer=lambda _secret, _message: "ab" * 64,
        )

        result = transport.fetch_with_details(
            {
                "exchange": "c2c1",
                "symbol": "BTC/USDT",
                "interval": "1",
                "start_time": "0",
                "end_time": "60000",
            }
        )

        self.assertEqual(result.body, b'{"data":[]}')
        self.assertEqual(result.statuses, (429, 200))
        self.assertEqual(result.attempts, 2)
        self.assertEqual(len(calls), 2)
        self.assertEqual(len(waits), 1)
        self.assertIn("/trade/api/v2/candles?", calls[0][0])
        self.assertEqual(len(calls[0][1]["X-AUTH-SIGNATURE"]), 128)
        self.assertFalse(hasattr(transport, "place_order"))

    def test_normalizes_mocked_responses_and_removes_exact_duplicates(self) -> None:
        first = candle(0)
        bodies = {
            "0": {"data": [candle(60_000), first, dict(first)]},
            "120000": {"data": [candle(180_000), candle(120_000)]},
        }

        def fetch(parameters):
            return json.dumps(bodies[parameters["start_time"]]).encode()

        dataset, report = download_historical_candles(
            fetch=fetch,
            exchange="c2c1",
            symbol="BTC/USDT",
            interval_minutes=1,
            start_time_ms=0,
            end_time_ms=240_000,
            chunk_candles=2,
            acquired_at_ms=300_000,
        )

        self.assertTrue(report.passed, report.failures)
        self.assertEqual(report.duplicate_count, 1)
        self.assertTrue(report.reordered)
        self.assertEqual(
            [row["open_time"] for row in dataset["candles"]],
            [0, 60_000, 120_000, 180_000],
        )
        self.assertTrue(dataset["credentials_used"])
        self.assertEqual(dataset["source"]["access"], "authenticated-read-only")
        self.assertEqual(len(dataset["source"]["response_sha256"]), 2)
        self.assertEqual(
            validate_candle_dataset(dataset).candle_sha256,
            dataset["integrity"]["candle_sha256"],
        )

    def test_reports_missing_candles_and_range_boundaries(self) -> None:
        def fetch(_parameters):
            return json.dumps({"data": [candle(0), candle(120_000)]}).encode()

        _, report = download_historical_candles(
            fetch=fetch,
            exchange="c2c1",
            symbol="BTC/USDT",
            interval_minutes=1,
            start_time_ms=0,
            end_time_ms=180_000,
            acquired_at_ms=240_000,
        )

        self.assertFalse(report.passed)
        self.assertEqual(report.gap_count, 1)
        self.assertTrue(any("requires 3" in failure for failure in report.failures))

    def test_rejects_conflicting_duplicates_bad_ohlc_and_incomplete_rows(self) -> None:
        cases = []
        duplicate = candle(0)
        cases.append([duplicate, candle(0, close="100.5")])
        bad_ohlc = candle(0)
        bad_ohlc["l"] = "103"
        cases.append([bad_ohlc])
        incomplete = candle(0)
        incomplete["close_time"] = "120000"
        cases.append([incomplete])

        for rows in cases:
            with self.subTest(rows=rows), self.assertRaises(HistoricalCandleError):
                download_historical_candles(
                    fetch=lambda _parameters, rows=rows: json.dumps(
                        {"data": rows}
                    ).encode(),
                    exchange="c2c1",
                    symbol="BTC/USDT",
                    interval_minutes=1,
                    start_time_ms=0,
                    end_time_ms=60_000,
                    acquired_at_ms=60_000,
                )

    def test_detects_saved_content_changed_after_hashing(self) -> None:
        dataset, report = download_historical_candles(
            fetch=lambda _parameters: json.dumps({"data": [candle(0)]}).encode(),
            exchange="c2c1",
            symbol="BTC/USDT",
            interval_minutes=1,
            start_time_ms=0,
            end_time_ms=60_000,
            acquired_at_ms=60_000,
        )
        self.assertTrue(report.passed)
        dataset["candles"][0]["close"] = "100"

        validation = validate_candle_dataset(dataset)

        self.assertFalse(validation.passed)
        self.assertIn("stored candle hash does not match content", validation.failures)

    def test_validator_rejects_reordered_and_duplicate_stored_rows(self) -> None:
        rows = [candle(0), candle(60_000), candle(120_000)]
        dataset, report = download_historical_candles(
            fetch=lambda _parameters: json.dumps({"data": rows}).encode(),
            exchange="c2c1",
            symbol="BTC/USDT",
            interval_minutes=1,
            start_time_ms=0,
            end_time_ms=180_000,
            acquired_at_ms=180_000,
        )
        self.assertTrue(report.passed)
        dataset.pop("integrity")
        dataset["candles"] = [
            dataset["candles"][1],
            dataset["candles"][0],
            dataset["candles"][1],
            dataset["candles"][2],
        ]

        validation = validate_candle_dataset(dataset)

        self.assertFalse(validation.passed)
        self.assertIn("stored candles are not in chronological order", validation.failures)
        self.assertIn("stored dataset contains duplicate candles", validation.failures)

    def test_cli_validates_a_complete_saved_dataset(self) -> None:
        from trading.app.__main__ import main

        rows = [candle(index * 60_000) for index in range(240)]
        dataset, report = download_historical_candles(
            fetch=lambda _parameters: json.dumps({"data": rows}).encode(),
            exchange="c2c1",
            symbol="BTC/USDT",
            interval_minutes=1,
            start_time_ms=0,
            end_time_ms=240 * 60_000,
            acquired_at_ms=240 * 60_000,
        )
        self.assertTrue(report.passed)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "candles.json"
            path.write_text(json.dumps(dataset), encoding="utf-8")
            with contextlib.redirect_stdout(io.StringIO()):
                code = main(["--validate-candle-data", str(path)])

        self.assertEqual(code, 0)

    def test_cli_refuses_detached_historical_options(self) -> None:
        from trading.app.__main__ import main

        for option in (
            ["--historical-interval-minutes", "5"],
            ["--historical-chunk-candles", "100"],
        ):
            with self.subTest(option=option), contextlib.redirect_stderr(
                io.StringIO()
            ), self.assertRaises(SystemExit):
                main(option)


class HistoricalCandleRecoveryTest(unittest.TestCase):
    def source(self):
        dataset, _ = download_historical_candles(
            fetch=lambda _parameters: json.dumps(
                {"data": [candle(0), candle(180_000)]}
            ).encode(),
            exchange="c2c1", symbol="BTC/USDT", interval_minutes=1,
            start_time_ms=0, end_time_ms=240_000, acquired_at_ms=300_000,
        )
        return dataset

    def test_recovers_partial_pages_without_duplicates_and_preserves_source(self):
        original = self.source()
        unchanged = json.dumps(original, sort_keys=True)
        replies = [
            [candle(60_000)],
            [candle(60_000), candle(120_000)],
        ]
        checkpoints = []

        def fetch(_parameters):
            rows = replies.pop(0)
            return CandleFetchResult(
                json.dumps({"data": rows}).encode(), (429, 200)
            )

        recovered, report = recover_historical_candles(
            original, fetch=fetch, input_file_sha256="a" * 64,
            chunk_candles=10, checkpoint=lambda value: checkpoints.append(value),
        )

        self.assertTrue(report.passed, report.failures)
        self.assertEqual(json.dumps(original, sort_keys=True), unchanged)
        self.assertEqual([row["open_time"] for row in recovered["candles"]], [0, 60_000, 120_000, 180_000])
        recovery = recovered["source"]["recovery"]
        self.assertEqual(recovery["timestamp_classification"]["counts"]["recovered"], 2)
        self.assertEqual(recovery["timestamp_classification"]["counts"]["unresolved"], 0)
        self.assertEqual(recovery["requests"][1]["duplicates_ignored"], 1)
        self.assertEqual(recovery["requests"][0]["http_statuses"], [429, 200])
        self.assertGreaterEqual(len(checkpoints), 3)

    def test_interrupted_recovery_resumes_only_remaining_confirmation(self):
        checkpoint = []
        calls = 0

        def interrupted(_parameters):
            nonlocal calls
            calls += 1
            if calls == 1:
                return CandleFetchResult(b'{"data":[]}', (200,))
            raise CoinSwitchCandleTransportError("offline", statuses=(500, 500, 500, 500))

        with self.assertRaises(CoinSwitchCandleTransportError):
            recover_historical_candles(
                self.source(), fetch=interrupted, input_file_sha256="a" * 64,
                chunk_candles=10, checkpoint=lambda value: checkpoint.append(value),
            )
        saved = checkpoint[-1]
        resumed_calls = []
        recovered, report = recover_historical_candles(
            saved,
            fetch=lambda parameters: (
                resumed_calls.append(dict(parameters))
                or CandleFetchResult(b'{"data":[]}', (200,))
            ),
            input_file_sha256="b" * 64,
            chunk_candles=10,
        )

        self.assertFalse(report.passed)
        self.assertEqual(len(resumed_calls), 1)
        counts = recovered["source"]["recovery"]["timestamp_classification"]["counts"]
        self.assertEqual(counts, {"recovered": 0, "consistently_unavailable": 2, "unresolved": 0})
        self.assertEqual(len(recovered["candles"]), 2)


if __name__ == "__main__":
    unittest.main()
