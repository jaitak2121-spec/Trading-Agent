"""Finite qualification of the public CoinSwitch observation path."""

from __future__ import annotations

import contextlib
import datetime as dt
import io
import json
import tempfile
import unittest
from unittest.mock import patch
from pathlib import Path

from trading.adapters.marketdata import FeedDisconnected
from trading.adapters.observation_evidence import (
    write_observation_dataset,
    write_observation_evidence,
)
from trading.app.coinswitch_observation import run_coinswitch_observation
from trading.core.clock import UTC


class _Source:
    def __init__(self, results):
        self._results = iter(results)

    def poll(self):
        result = next(self._results, [])
        if isinstance(result, Exception):
            raise result
        return result


class _Stream:
    symbol = "BTC/INR"
    exchange = "coinswitchx"
    reconnect_attempts = 1
    successful_reconnects = 1

    def __init__(self, quotes, candles):
        self.quote_source = _Source(quotes)
        self.candle_source = _Source(candles)
        self.started = False
        self.stopped = False

    def start(self):
        self.started = True

    def stop(self):
        self.stopped = True


class _FailingStream(_Stream):
    def start(self):
        raise FeedDisconnected("cannot connect")


class CoinSwitchObservationTest(unittest.TestCase):
    def test_wall_observation_boundary_precedes_transport_cleanup(self):
        stream=_Stream([[{'bid':'1'}]],[[{'close':'1'}]])
        clock=[1.0]
        def stop():
            clock[0]+=5
            stream.stopped=True
        stream.stop=stop
        with patch('trading.app.coinswitch_observation.time.time_ns',
                   side_effect=lambda:int(clock[0]*1000000000)):
            report=run_coinswitch_observation(stream,duration_seconds=1,
                monotonic=lambda:clock[0],wait=lambda seconds:clock.__setitem__(0,clock[0]+seconds))
        self.assertEqual(report.duration_elapsed_seconds,1)
        self.assertEqual(report.observation_started_at_ns,1000000000)
        self.assertEqual(report.observation_finished_at_ns,2000000000)
        self.assertEqual(clock[0],7)
        self.assertTrue(stream.stopped)
        self.assertEqual(report.as_details()['observation_finished_at_ns'],2000000000)

    def test_quote_sink_records_messages_and_sink_failure_stops_stream(self):
        stream=_Stream([[{'bid':'1'}]],[[{'close':'1'}]])
        clock=[0.0];captured=[]
        run_coinswitch_observation(stream,duration_seconds=1,monotonic=lambda:clock[0],
            wait=lambda seconds:clock.__setitem__(0,clock[0]+seconds),quote_sink=captured.append)
        self.assertEqual(captured,[{'bid':'1'}])
        self.assertTrue(stream.stopped)
        stream=_Stream([[{'bid':'1'}]],[[]])
        def fail(message):raise ValueError('fixture sink failure')
        with self.assertRaises(ValueError):run_coinswitch_observation(stream,duration_seconds=1,quote_sink=fail)
        self.assertTrue(stream.stopped)

    def test_counts_both_channels_and_allows_a_recovered_interruption(self) -> None:
        stream = _Stream(
            [[{"bid": "1"}], FeedDisconnected("temporary"), [{"bid": "2"}]],
            [[{"close": "1"}], FeedDisconnected("temporary"), []],
        )
        clock = [0.0]

        def wait(seconds: float) -> None:
            clock[0] += seconds

        recorded = []
        report = run_coinswitch_observation(
            stream,
            duration_seconds=3,
            poll_interval_seconds=1,
            wait=wait,
            monotonic=lambda: clock[0],
            candle_sink=recorded.append,
        )

        self.assertTrue(report.passed, report.failures)
        self.assertTrue(stream.started)
        self.assertTrue(stream.stopped)
        self.assertEqual(report.quote_messages, 2)
        self.assertEqual(report.candle_messages, 1)
        self.assertEqual(report.feed_error_polls, 1)
        self.assertEqual(report.successful_reconnects, 1)
        self.assertEqual(recorded, [{"close": "1"}])

    def test_fails_without_a_closed_candle_or_when_feed_ends_unavailable(self) -> None:
        stream = _Stream(
            [[{"bid": "1"}], FeedDisconnected("offline")],
            [[], FeedDisconnected("offline")],
        )
        clock = [0.0]
        report = run_coinswitch_observation(
            stream,
            duration_seconds=2,
            poll_interval_seconds=1,
            wait=lambda seconds: clock.__setitem__(0, clock[0] + seconds),
            monotonic=lambda: clock[0],
        )

        self.assertFalse(report.passed)
        self.assertIn("no closed one-minute trade candles were observed", report.failures)
        self.assertIn(
            "observation ended while the public feed was unavailable",
            report.failures,
        )

    def test_writes_atomic_credential_free_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            destination = Path(directory) / "observation.json"
            started = dt.datetime(2026, 11, 3, tzinfo=UTC)
            write_observation_evidence(
                destination,
                report={"passed": True, "quote_messages": 10},
                started_at=started,
                finished_at=started + dt.timedelta(hours=2),
            )
            evidence = json.loads(destination.read_text(encoding="utf-8"))

            self.assertEqual(
                evidence["schema"], "coinswitch-public-observation/v1"
            )
            self.assertTrue(evidence["public_data_only"])
            self.assertFalse(evidence["credentials_used"])
            self.assertFalse(evidence["live_trading"])
            self.assertEqual(list(Path(directory).glob(".*.tmp")), [])

    def test_startup_failure_is_a_redacted_report_not_an_exception(self) -> None:
        stream = _FailingStream([], [])
        report = run_coinswitch_observation(stream, duration_seconds=60)

        self.assertFalse(report.passed)
        self.assertEqual(report.poll_attempts, 0)
        self.assertIn("public stream startup failed", report.failures[0])

    def test_writes_public_training_dataset_atomically(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            destination = Path(directory) / "candles.json"
            started = dt.datetime(2026, 11, 10, tzinfo=UTC)
            write_observation_dataset(
                destination,
                symbol="BTC/USDT",
                exchange="c2c1",
                candles=[{"open_time": 1, "close": "100"}],
                started_at=started,
                finished_at=started + dt.timedelta(minutes=1),
            )
            dataset = json.loads(destination.read_text(encoding="utf-8"))

            self.assertEqual(dataset["schema"], "coinswitch-public-candles/v1")
            self.assertTrue(dataset["training_only"])
            self.assertFalse(dataset["credentials_used"])
            self.assertEqual(dataset["candles"][0]["close"], "100")

    def test_cli_requires_a_symbol_and_report_mode(self) -> None:
        from trading.app.__main__ import main

        for argv, message in (
            (["--coinswitch-observe-seconds", "60"], "requires --coinswitch-symbol"),
            (["--observation-report", "out.json"], "requires --coinswitch-observe-seconds"),
            (["--observation-data", "out.json"], "requires --coinswitch-observe-seconds"),
        ):
            error = io.StringIO()
            with contextlib.redirect_stderr(error), self.assertRaises(SystemExit):
                main(argv)
            self.assertIn(message, error.getvalue())


if __name__ == "__main__":
    unittest.main()
