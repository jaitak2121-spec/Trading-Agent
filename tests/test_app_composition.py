"""Tests for the paper-trading composition root.

``build_paper_stack`` is the production-shaped equivalent of the test harness's
``build_rig``. These tests pin the two things that matter about it: that it comes
up in a safe posture (paper mode, live trading off, a paper venue, no
credentials), and that the stack it returns actually executes an order end to end
through the gateway.
"""

from __future__ import annotations

import unittest
import tempfile
from decimal import Decimal
from pathlib import Path

from trading.adapters.paper.broker import PaperBroker
from trading.app import build_paper_stack
from trading.app.composition import DEFAULT_STARTING_CASH
from trading.core.config import RiskConfig
from trading.core.modes import TradingMode
from trading.core.money import USD, Money, Price, Quantity
from trading.core.orders import OrderIntent, OrderSide


class BuildPaperStackTest(unittest.TestCase):
    def test_comes_up_in_paper_mode_not_live(self) -> None:
        stack = build_paper_stack()
        self.assertFalse(stack.config.live_trading)
        self.assertFalse(stack.config.is_live_authorized, "live must never be authorized here")
        self.assertEqual(stack.modes.mode, TradingMode.PAPER)
        self.assertEqual(stack.config.environment, "paper")
        self.assertTrue(stack.startup.ok, stack.startup.recovery_findings)
        self.assertEqual(stack.reconciliation_coordinator.sweeps, 1)

    def test_the_venue_is_the_in_process_paper_broker(self) -> None:
        stack = build_paper_stack()
        self.assertIsInstance(stack.broker, PaperBroker)

    def test_carries_no_credentials(self) -> None:
        # INVARIANT 9: no secret ever reaches the system through this path.
        stack = build_paper_stack()
        self.assertIsNone(stack.config.api_key)
        self.assertIsNone(stack.config.api_secret)

    def test_default_starting_cash(self) -> None:
        stack = build_paper_stack()
        self.assertEqual(stack.portfolio.cash, DEFAULT_STARTING_CASH)
        self.assertEqual(stack.portfolio.cash, Money("1000000.00", USD))

    def test_custom_starting_cash_and_risk(self) -> None:
        risk = RiskConfig(
            max_order_notional=Money("5000.00", USD),
            max_position_notional=Money("10000.00", USD),
            max_gross_exposure=Money("20000.00", USD),
            max_daily_loss=Money("1000.00", USD),
            risk_fraction_per_trade=Decimal("0.01"),
        )
        stack = build_paper_stack(starting_cash=Money("50000.00", USD), risk=risk)
        self.assertEqual(stack.portfolio.cash, Money("50000.00", USD))
        self.assertEqual(stack.config.risk.max_order_notional, Money("5000.00", USD))

    def test_a_market_order_executes_end_to_end(self) -> None:
        """A quote in the feed, a market order through the gateway, a paper fill."""
        stack = build_paper_stack()
        stack.feed.publish("BTCUSD", Price("49990", USD), Price("50010", USD))
        intent = OrderIntent(
            strategy_id="strategy-1",
            signal_id="sig-1",
            symbol="BTCUSD",
            side=OrderSide.BUY,
            quantity=Quantity("0.001", "BTC"),  # ~$50 notional, inside the tiny defaults
        )
        result = stack.gateway.submit(
            intent, proposer=stack.strategy_id, mark_prices={"BTCUSD": Price("50000", USD)}
        )
        self.assertEqual(result.outcome, "executed", result.reason)
        # The fill reached the portfolio through the gateway, not around it.
        self.assertEqual(
            stack.portfolio.position("BTCUSD", asset="BTC").quantity,
            Quantity("0.001", "BTC"),
        )

    def test_monitor_reports_ready_on_a_clean_paper_stack(self) -> None:
        stack = build_paper_stack()
        status = stack.monitor.status()
        self.assertTrue(status.ready, status.blocked_reasons)
        self.assertEqual(status.blocked_reasons, ())

    def test_sqlite_stack_restores_execution_and_risk_state(self) -> None:
        path = Path(tempfile.mkdtemp()) / "paper.sqlite3"
        stack = build_paper_stack(sqlite_path=path)
        stack.feed.publish("BTCUSD", Price("49990", USD), Price("50010", USD))
        intent = OrderIntent(
            strategy_id="strategy-1",
            signal_id="durable-signal",
            symbol="BTCUSD",
            side=OrderSide.BUY,
            quantity=Quantity("0.001", "BTC"),
        )
        result = stack.gateway.submit(
            intent,
            proposer=stack.strategy_id,
            mark_prices={"BTCUSD": Price("50000", USD)},
        )
        cash = stack.portfolio.cash
        self.assertEqual(result.outcome, "executed")
        self.assertEqual(stack.risk.submissions_in_window(), 1)
        stack.persistence.close()

        restored = build_paper_stack(sqlite_path=path)
        self.assertEqual(restored.orders.get(result.order.order_id).state.value, "filled")
        self.assertEqual(restored.portfolio.cash, cash)
        self.assertEqual(
            restored.portfolio.position("BTCUSD", asset="BTC").quantity,
            Quantity("0.001", "BTC"),
        )
        self.assertEqual(restored.risk.submissions_in_window(), 1)
        self.assertTrue(restored.startup.ok)
        self.assertFalse(restored.reconciliation.has_mismatch)
        self.assertEqual(restored.bot.state, 'paused')
        restored.audit.verify()
        restored.persistence.close()

    def test_flat_sqlite_restart_reconciles_clean_and_is_ready(self) -> None:
        path = Path(tempfile.mkdtemp()) / "paper.sqlite3"
        stack = build_paper_stack(sqlite_path=path)
        stack.feed.publish("BTCUSD", Price("49990", USD), Price("50010", USD))
        for signal_id, side in (("entry", OrderSide.BUY), ("exit", OrderSide.SELL)):
            result = stack.gateway.submit(
                OrderIntent(
                    strategy_id="strategy-1",
                    signal_id=signal_id,
                    symbol="BTCUSD",
                    side=side,
                    quantity=Quantity("0.001", "BTC"),
                ),
                proposer=stack.strategy_id,
                mark_prices={"BTCUSD": Price("50000", USD)},
            )
            self.assertEqual(result.outcome, "executed")
        cash = stack.portfolio.cash
        stack.persistence.close()

        restored = build_paper_stack(sqlite_path=path)
        self.assertTrue(restored.startup.ok, restored.startup.recovery_findings)
        self.assertTrue(restored.monitor.ready(), restored.monitor.blocked_reasons())
        self.assertEqual(restored.portfolio.cash, cash)
        restored.persistence.close()


class CliTest(unittest.TestCase):
    def test_main_runs_the_demo_and_prints_a_report(self) -> None:
        import contextlib
        import io

        from trading.app.__main__ import main

        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            code = main([])  # argparse defaults: $100000, 0 slippage
        output = buffer.getvalue()

        self.assertEqual(code, 0)
        self.assertIn("paper trading session report", output)
        self.assertIn("executed", output)
        self.assertIn("live trading off", output)

    def test_main_accepts_cash_and_slippage_arguments(self) -> None:
        import contextlib
        import io

        from trading.app.__main__ import main

        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            code = main(["--cash", "25000.00", "--slippage-bps", "5"])
        self.assertEqual(code, 0)
        self.assertIn("starting cash   : 25000.00 USD", buffer.getvalue())


if __name__ == "__main__":
    unittest.main()
