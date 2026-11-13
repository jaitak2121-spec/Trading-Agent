"""Composition tests for the continuously runnable local paper application."""

from __future__ import annotations

import base64
import datetime as dt
import hashlib
import json
import io
import threading
import unittest
from unittest.mock import Mock, patch
from types import SimpleNamespace
from contextlib import redirect_stderr
from decimal import Decimal

from trading.app import (
    build_coinswitch_paper_application,
    build_demo_paper_application,
)
from trading.adapters.coinswitch import CoinSwitchSpotStream
from trading.app.demo_stream import DemoMarketStream
from trading.core.clock import ManualClock
from trading.core.money import INR, USD, Money


class DemoMarketStreamTest(unittest.TestCase):
    def test_quote_and_candle_advance_one_coherent_interval(self) -> None:
        clock = ManualClock()
        stream = DemoMarketStream(clock, interval_seconds=60)
        before = clock.now()

        quote = stream.quote_source.poll()[0]
        candle = stream.candle_source.poll()[0]

        self.assertEqual(quote["symbol"], candle["symbol"])
        self.assertEqual(quote["timestamp"], clock.now().isoformat())
        self.assertEqual(candle["open_time"], before.isoformat())
        self.assertEqual((clock.now() - before).total_seconds(), 60)
        self.assertEqual(stream.intervals, 1)

    def test_requires_an_advanceable_clock_and_valid_metadata(self) -> None:
        from trading.core.clock import SystemClock

        with self.assertRaises(TypeError):
            DemoMarketStream(SystemClock())
        with self.assertRaises(ValueError):
            DemoMarketStream(ManualClock(), symbol="")
        with self.assertRaises(ValueError):
            DemoMarketStream(ManualClock(), interval_seconds=0)


class PaperApplicationRuntimeTest(unittest.TestCase):
    def test_abbreviated_account_and_control_options_fail_before_composition(self):
        from trading.app.__main__ import main
        for args in (['--state-d', '/tmp/unopened-account.sqlite3'],
                     ['--learning-r', '/tmp/unopened-learning'],
                     ['--coinswitch-sy', 'BTC/USDT'], ['--resume-p'],
                     ['--por', '9999'], ['--hos', 'localhost']):
            with self.subTest(args=args), redirect_stderr(io.StringIO()), \
                 patch('trading.app.__main__.build_paper_stack') as stack, \
                 patch('trading.app.__main__.build_demo_paper_application') as demo, \
                 patch('trading.app.__main__.build_coinswitch_paper_application') as real:
                with self.assertRaises(SystemExit) as caught:
                    main(args)
                self.assertEqual(caught.exception.code, 2)
                stack.assert_not_called()
                demo.assert_not_called()
                real.assert_not_called()

    def test_cli_refuses_invalid_limit_protection_before_network_start(self) -> None:
        from trading.app.__main__ import main

        error = io.StringIO()
        with redirect_stderr(error), self.assertRaises(SystemExit) as caught:
            main([
                "--coinswitch-symbol",
                "BTC/USDT",
                "--limit-protection-bps",
                "1001",
            ])
        self.assertEqual(caught.exception.code, 2)
        self.assertIn("invalid CoinSwitch paper configuration", error.getvalue())

    def test_builds_coinswitch_data_with_paper_only_execution(self) -> None:
        stream = CoinSwitchSpotStream(
            symbol="BTC/INR", connection_factory=lambda *_args: None
        )
        app = build_coinswitch_paper_application(
            stream=stream, starting_cash=Money("100000.00", INR), port=0
        )
        try:
            self.assertIs(app.stream, stream)
            self.assertEqual(app.stack.config.environment, "paper")
            self.assertFalse(app.stack.config.live_trading)
            self.assertEqual(app.stack.portfolio.cash.currency, INR)
            self.assertEqual(app.stack.broker.__class__.__name__, "PaperBroker")
        finally:
            app.stop()

    def test_coinswitch_builder_refuses_cash_currency_mismatch(self) -> None:
        with self.assertRaises(ValueError):
            build_coinswitch_paper_application(
                starting_cash=Money("100000.00", USD), port=0
            )

    def test_coinswitch_builder_requires_complete_market_specification(self) -> None:
        with self.assertRaisesRegex(ValueError, "must be supplied together"):
            build_coinswitch_paper_application(
                quantity_step="0.0001", port=0
            )

    def test_coinswitch_builder_uses_captured_trade_info_constraints(self) -> None:
        response = json.dumps(
            {
                "data": {
                    "coinswitchx": {
                        "BTC/INR": {
                            "quote": {"min": "150", "max": "2500000"},
                            "precision": {"base": 6, "quote": 2, "limit": 0},
                        }
                    }
                }
            }
        ).encode()
        body = json.dumps(
            {
                "schema": "coinswitch-trade-info-capture/v1",
                "source": "/trade/api/v2/tradeInfo",
                "captured_at": dt.datetime.now(dt.timezone.utc).isoformat(),
                "exchange": "coinswitchx",
                "symbol": "BTC/INR",
                "response_body_base64": base64.b64encode(response).decode(),
                "response_sha256": hashlib.sha256(response).hexdigest(),
            }
        ).encode()
        stream = CoinSwitchSpotStream(
            symbol="BTC/INR", connection_factory=lambda *_args: None
        )

        app = build_coinswitch_paper_application(
            stream=stream,
            starting_cash=Money("100000.00", INR),
            market_info_body=body,
            port=0,
        )
        try:
            policy = app.service._order_policy  # type: ignore[attr-defined]
            self.assertEqual(policy.market.quantity_step, Decimal("0.000001"))
            self.assertEqual(policy.market.min_notional, Money("150", INR))
        finally:
            app.stop()

    def test_captured_and_manual_market_constraints_are_mutually_exclusive(self) -> None:
        with self.assertRaisesRegex(ValueError, "mutually exclusive"):
            build_coinswitch_paper_application(
                market_info_body=b"{}",
                quantity_step="0.0001",
                min_order_notional="1",
                max_order_notional="10",
                port=0,
            )

    def test_application_starts_and_stops_a_lifecycle_aware_stream(self) -> None:
        class LifecycleStream:
            starts = 0
            stops = 0

            def start(self):
                self.starts += 1

            def stop(self):
                self.stops += 1

        app = build_demo_paper_application(port=0, poll_interval_seconds=0.01)
        stream = LifecycleStream()
        class NoopServer:
            def start(self):
                return None

            def stop(self):
                return None

        app.stream = stream
        app.server = NoopServer()
        app.start()
        app.stop()
        self.assertEqual((stream.starts, stream.stops), (1, 1))

    def test_complete_synthetic_path_enters_and_exits_through_the_gateway(self) -> None:
        app = build_demo_paper_application(port=0)
        try:
            outcomes = []
            for _ in range(19):
                outcomes.extend(app.service.poll_once().outcomes)

            self.assertEqual([outcome.outcome for outcome in outcomes], ["executed", "executed"])
            self.assertEqual([outcome.side for outcome in outcomes], ["buy", "sell"])
            self.assertEqual(app.stack.portfolio.open_positions(), [])
            self.assertGreater(app.stack.portfolio.realized().amount, 0)
            json.dumps(app.dashboard.snapshot())
        finally:
            app.stop()

    def test_supervisor_drives_the_composed_service(self) -> None:
        app = build_demo_paper_application(
            port=0, poll_interval_seconds=0.001
        )
        try:
            app.supervisor.start()
            for _ in range(200):
                if app.supervisor.status().passes >= 3:
                    break
                threading.Event().wait(0.002)
            app.supervisor.stop()

            status = app.supervisor.status()
            self.assertGreaterEqual(status.passes, 3)
            self.assertIsNone(status.last_error)
            self.assertGreaterEqual(app.stream.intervals, 3)
        finally:
            app.stop()

    def test_stop_is_idempotent_and_a_closed_app_cannot_restart(self) -> None:
        app = build_demo_paper_application(port=0)
        app.stop()
        app.stop()
        with self.assertRaises(RuntimeError):
            app.start()

    def test_shutdown_releases_resources_after_transition_or_feed_failure(self):
        from trading.app.runtime import PaperApplication
        for failure in ('transition', 'stream'):
            with self.subTest(failure=failure):
                stack = SimpleNamespace(bot=Mock(state='paused'), operator_id='operator',
                                        persistence=Mock())
                stream, supervisor, server = Mock(), Mock(), Mock()
                target = stack.bot.transition if failure == 'transition' else stream.stop
                target.side_effect = RuntimeError(failure)
                app = PaperApplication(stack, stream, Mock(), supervisor, Mock(), server)
                with self.assertRaisesRegex(RuntimeError, failure):
                    app.stop()
                supervisor.stop.assert_called_once()
                stream.stop.assert_called_once()
                stack.persistence.close.assert_called_once()
                self.assertTrue(app._closed)
                app.stop()
                stack.persistence.close.assert_called_once()

    def test_shutdown_timeout_keeps_database_owned_until_worker_stops(self):
        from trading.app.runtime import PaperApplication
        stack = SimpleNamespace(bot=Mock(state='emergency_stopped'), operator_id='operator',
                                persistence=Mock())
        stream, supervisor, server = Mock(), Mock(), Mock()
        supervisor.stop.side_effect = [RuntimeError('worker still running'), None]
        app = PaperApplication(stack, stream, Mock(), supervisor, Mock(), server)
        with self.assertRaisesRegex(RuntimeError, 'worker still running'):
            app.stop()
        stack.persistence.close.assert_not_called()
        stream.stop.assert_not_called()
        self.assertFalse(app._closed)
        app.stop()
        stack.bot.transition.assert_not_called()
        stack.persistence.close.assert_called_once()

    def test_cli_startup_failure_still_closes_application(self):
        from trading.app.__main__ import _serve_application
        app = Mock()
        app.start.side_effect = OSError('dashboard port occupied')
        with self.assertRaisesRegex(OSError, 'dashboard port occupied'):
            _serve_application(app, description='isolated fixture')
        app.stop.assert_called_once()


if __name__ == "__main__":
    unittest.main()
