"""Runtime fees, durable controls and accounting recovery."""
import json
import tempfile
import unittest

from trading.app.runtime import build_demo_paper_application
from trading.app.composition import build_paper_stack
from trading.adapters.persistence.durable import PersistenceError
from trading.core.errors import SafetyViolation
from trading.core.money import Money, Price, Quantity, USD
from trading.core.orders import OrderSide, OrderIntent


class RuntimeStateTest(unittest.TestCase):
    def test_spot_cash_guard_includes_fees_and_exact_boundary(self):
        for starting, expected in [('10.00', False), ('10.01', True)]:
            stack=build_paper_stack(starting_cash=Money(starting,USD),fee_bps=10)
            stack.feed.publish('BTCUSD',Price('100',USD),Price('100',USD))
            order=OrderIntent('test','cash-boundary','BTCUSD',OrderSide.BUY,Quantity('0.1','BTC'))
            result=stack.gateway.submit(order,proposer=stack.strategy_id,
                                        mark_prices={'BTCUSD':Price('100',USD)})
            self.assertEqual(stack.broker.placement_count,1)
            held=stack.portfolio.position('BTCUSD',asset='BTC').quantity.amount
            self.assertEqual(held > 0, expected)
            self.assertGreaterEqual(stack.portfolio.cash.amount,0)

    def test_spot_sell_cannot_create_an_unfunded_short(self):
        stack=build_paper_stack()
        stack.feed.publish('BTCUSD',Price('100',USD),Price('100',USD))
        order=OrderIntent('test','no-short','BTCUSD',OrderSide.SELL,Quantity('0.1','BTC'))
        stack.gateway.submit(order,proposer=stack.strategy_id,mark_prices={'BTCUSD':Price('100',USD)})
        self.assertEqual(stack.broker.placement_count,1)
        self.assertEqual(stack.portfolio.open_positions(),[])

    def test_spot_partial_fill_checks_only_executed_quantity(self):
        stack=build_paper_stack(starting_cash=Money('10.01',USD),fee_bps=10)
        stack.feed.publish('BTCUSD',Price('100',USD),Price('100',USD))
        stack.broker.set_depth('BTCUSD',Quantity('0.1','BTC'))
        order=OrderIntent('test','partial-cash','BTCUSD',OrderSide.BUY,Quantity('1','BTC'))
        stack.gateway.submit(order,proposer=stack.strategy_id,mark_prices={'BTCUSD':Price('100',USD)})
        self.assertEqual(stack.portfolio.cash,Money('0',USD))
        self.assertEqual(stack.portfolio.position('BTCUSD',asset='BTC').quantity,Quantity('0.1','BTC'))

    def test_database_ownership_prevents_concurrent_writers_and_releases(self):
        from trading.adapters.persistence import SQLiteState
        with tempfile.TemporaryDirectory() as directory:
            path=directory+'/paper.db'
            first=SQLiteState(path)
            try:
                with self.assertRaisesRegex(PersistenceError,'writer'):
                    SQLiteState(path)
            finally:
                first.close()
            second=SQLiteState(path)
            second.close()

    def test_fees_reach_daily_risk_and_fill_audit(self):
        app = build_demo_paper_application(fee_bps=10)
        try:
            for _ in range(19):
                app.service.poll_once()
            self.assertGreater(app.stack.portfolio.fees.amount, 0)
            records = [r for r in app.stack.audit.records() if r.action == 'portfolio.fill']
            self.assertTrue(records)
            self.assertEqual(sum((Money(r.details['realized_pnl'], USD).amount for r in records)),
                             app.stack.portfolio.realized().amount)
        finally:
            app.stop()

    def test_invalid_fee_is_refused_before_database_creation(self):
        for rate in [True, -1, 10000, 1.5]:
            with self.assertRaises(ValueError):
                build_paper_stack(fee_bps=rate)

    def test_open_inventory_recovers_and_does_not_trade_until_resumed(self):
        with tempfile.TemporaryDirectory() as directory:
            path = directory + '/paper.db'
            app = build_demo_paper_application(sqlite_path=path, fee_bps=10)
            for _ in range(19):
                app.service.poll_once()
                if app.stack.portfolio.open_positions():
                    break
            positions = app.stack.portfolio.snapshot()
            cash = app.stack.portfolio.cash
            orders = len(app.stack.orders.all_orders())
            app.stop()
            restored = build_demo_paper_application(sqlite_path=path, fee_bps=10)
            try:
                self.assertTrue(restored.stack.startup.ok)
                self.assertEqual(restored.stack.portfolio.snapshot(), positions)
                self.assertEqual(restored.stack.portfolio.cash, cash)
                self.assertEqual(restored.stack.broker.fetch_positions().positions, positions)
                self.assertEqual(len(restored.stack.orders.all_orders()), orders)
                with self.assertRaises(SafetyViolation):
                    restored.service.poll_once()
            finally:
                restored.stop()

    def test_fee_round_trip_and_rebuild(self):
        with tempfile.TemporaryDirectory() as directory:
            path = directory + '/paper.db'
            stack = build_paper_stack(sqlite_path=path, fee_bps=10,
                                      starting_cash=Money('1000', USD))
            for side, price in [(OrderSide.BUY, '100'), (OrderSide.SELL, '110')]:
                stack.portfolio.apply_fill('BTCUSD', side, Quantity('1', 'BTC'), Price(price, USD))
            self.assertEqual(stack.portfolio.cash, Money('1009.79', USD))
            self.assertEqual(stack.portfolio.realized(), Money('9.79', USD))
            self.assertEqual(stack.portfolio.fees, Money('0.21', USD))
            self.assertEqual(stack.persistence.connection.execute('SELECT count(*) FROM paper_fills').fetchone()[0], 2)
            stack.persistence.close()
            restored = build_paper_stack(sqlite_path=path, fee_bps=10)
            self.assertEqual(restored.portfolio.cash, Money('1009.79', USD))
            self.assertEqual(restored.portfolio.starting_cash, Money('1000', USD))
            self.assertEqual(restored.portfolio.fees, Money('0.21', USD))
            self.assertEqual(restored.bot.state, 'paused')
            restored.persistence.close()

    def test_pause_blocks_service_and_direct_gateway_and_requires_resume(self):
        with tempfile.TemporaryDirectory() as directory:
            path = directory + '/paper.db'
            app = build_demo_paper_application(sqlite_path=path)
            app.pause(reason='operator pause')
            with self.assertRaises(SafetyViolation):
                app.service.poll_once()
            intent = OrderIntent('test', 'one', 'BTCUSD', OrderSide.BUY, Quantity('1', 'BTC'))
            result = app.stack.gateway.submit(intent, proposer=app.stack.strategy_id)
            self.assertEqual(app.stack.broker.placement_count, 0)
            app.resume(reason='operator resume')
            app.service.poll_once()
            self.assertEqual(app.stack.persistence.connection.execute('SELECT count(*) FROM equity_snapshots').fetchone()[0], 1)
            app.stop()
            restored = build_demo_paper_application(sqlite_path=path)
            self.assertEqual(restored.stack.bot.state, 'stopped')
            with self.assertRaises(SafetyViolation):
                restored.service.poll_once()
            restored.stop()

    def test_emergency_latch_survives_restart(self):
        with tempfile.TemporaryDirectory() as directory:
            path = directory + '/paper.db'
            app = build_demo_paper_application(sqlite_path=path)
            app.emergency_stop(reason='operator emergency')
            app.stop()
            restored = build_demo_paper_application(sqlite_path=path)
            self.assertTrue(restored.stack.kill_switch.is_engaged)
            self.assertEqual(restored.stack.bot.state, 'emergency_stopped')
            with self.assertRaises(SafetyViolation):
                restored.resume(reason='invalid shortcut')
            restored.stop()

    def test_cash_checkpoint_corruption_fails_closed(self):
        with tempfile.TemporaryDirectory() as directory:
            path = directory + '/paper.db'
            stack = build_paper_stack(sqlite_path=path)
            stack.persistence.connection.execute("UPDATE portfolio SET cash='1.00'")
            stack.persistence.close()
            with self.assertRaises(PersistenceError):
                build_paper_stack(sqlite_path=path)

    def test_stale_marks_do_not_invent_equity(self):
        app = build_demo_paper_application()
        app.stack.portfolio.apply_fill('BTCUSD', OrderSide.BUY, Quantity('1', 'BTC'), Price('100', USD))
        self.assertIsNone(app.dashboard.snapshot()['portfolio']['equity'])
        app.stop()
