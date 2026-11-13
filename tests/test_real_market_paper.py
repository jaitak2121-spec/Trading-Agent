"""Native feed contract fixtures verify wiring, never genuine profit evidence."""
import datetime as dt
import json
from pathlib import Path
import queue
import tempfile
import threading
import unittest
from unittest.mock import patch

from trading.adapters.coinswitch import CoinSwitchSpotStream
from trading.adapters.marketdata import FeedDisconnected
from trading.adapters.persistence import SQLiteState
from trading.adapters.persistence.sqlite import bind_paper_origin,check_paper_origin
from trading.app.runtime import build_coinswitch_paper_application,build_demo_paper_application
from trading.app.research_jobs import main as jobs_main
from trading.core.clock import ManualClock,UTC
from trading.core.money import Money,USDT,Price,Quantity
from trading.core.orders import OrderIntent,OrderSide,OrderType
from tests import test_experience_learning as fixtures
from tests.test_coinswitch_market import capture,trade_info


class NativeConnection:
    def __init__(self):
        self.messages=queue.Queue();self.drained=threading.Event()
        self.messages.put('0{"sid":"fixture"}');self.messages.put('40/c2c1,{"sid":"fixture"}')
    def recv_text(self):
        value=self.messages.get(timeout=5)
        if value is None:raise FeedDisconnected('closed fixture')
        return value
    def send_text(self,value):
        if value=='3':self.drained.set()
    def close(self):self.messages.put(None)
    def event(self,name,body):self.messages.put('42/c2c1,'+json.dumps([name,body]))


class RealMarketPaperTests(unittest.TestCase):
    def test_recovery_only_composition_cannot_fill_against_a_real_market_database(self):
        from trading.app.composition import build_paper_stack
        from trading.core.config import RiskConfig
        with tempfile.TemporaryDirectory() as folder:
            path=Path(folder)/'real.db';state=SQLiteState(path)
            bind_paper_origin(state,{'kind':'real_market_paper','provider':'CoinSwitch public feed','symbol':'BTC/USDT','exchange':'c2c1'})
            state.close()
            risk=RiskConfig(base_currency=USDT,max_order_notional=Money('1000',USDT),max_position_notional=Money('1000',USDT),
                max_gross_exposure=Money('1000',USDT),max_daily_loss=Money('100',USDT))
            stack=build_paper_stack(sqlite_path=path,starting_cash=Money('10000',USDT),risk=risk)
            try:
                stack.feed.publish('BTC/USDT',Price('100',USDT),Price('100',USDT))
                intent=OrderIntent('fixture','recovery-only','BTC/USDT',OrderSide.BUY,Quantity('.1','BTC'),
                    order_type=OrderType.LIMIT,limit_price=Price('100',USDT))
                result=stack.gateway.submit(intent,proposer=stack.strategy_id,mark_prices={'BTC/USDT':Price('100',USDT)})
                self.assertEqual(result.outcome,'refused')
                self.assertEqual(stack.portfolio.cash,Money('10000',USDT));self.assertFalse(stack.portfolio.open_positions())
            finally:stack.persistence.close()

    def test_direct_gateway_rejects_invalid_precision_and_minimum_notional(self):
        clock=ManualClock(dt.datetime(2024,1,1,tzinfo=UTC))
        metadata=capture(trade_info(exchange='c2c1',symbol='BTC/USDT',minimum='5',maximum='30000',base=5),
            exchange='c2c1',symbol='BTC/USDT',captured_at=clock.now().isoformat())
        with patch('trading.app.runtime.SystemClock',return_value=clock):
            app=build_coinswitch_paper_application(symbol='BTC/USDT',starting_cash=Money('10000',USDT),market_info_body=metadata,port=0)
        try:
            app.stack.feed.publish('BTC/USDT',Price('100',USDT),Price('100',USDT))
            for i,(quantity,price) in enumerate((('.050001','100'),('.1','100.001'),('.04999','100'))):
                intent=OrderIntent('fixture',str(i),'BTC/USDT',OrderSide.BUY,Quantity(quantity,'BTC'),
                    order_type=OrderType.LIMIT,limit_price=Price(price,USDT))
                result=app.stack.gateway.submit(intent,proposer=app.stack.strategy_id,mark_prices={'BTC/USDT':Price('100',USDT)})
                self.assertEqual(result.outcome,'refused')
            self.assertEqual(app.stack.portfolio.cash,Money('10000',USDT))
            self.assertFalse(app.stack.portfolio.open_positions())
        finally:app.stop()

    def test_expiry_blocks_fill_of_existing_limit_even_with_fresh_crossing_quote(self):
        clock=ManualClock(dt.datetime(2024,1,1,tzinfo=UTC))
        metadata=capture(trade_info(exchange='c2c1',symbol='BTC/USDT',minimum='5',maximum='30000',base=5),
            exchange='c2c1',symbol='BTC/USDT',captured_at=clock.now().isoformat())
        with patch('trading.app.runtime.SystemClock',return_value=clock):
            app=build_coinswitch_paper_application(symbol='BTC/USDT',starting_cash=Money('10000',USDT),market_info_body=metadata,port=0)
        try:
            app.stack.feed.publish('BTC/USDT',Price('100',USDT),Price('100',USDT))
            intent=OrderIntent('fixture','resting','BTC/USDT',OrderSide.BUY,Quantity('.1','BTC'),order_type=OrderType.LIMIT,limit_price=Price('90',USDT))
            result=app.stack.gateway.submit(intent,proposer=app.stack.strategy_id,mark_prices={'BTC/USDT':Price('100',USDT)})
            self.assertTrue(result.is_executed);self.assertTrue(result.order.filled_quantity.is_zero)
            clock.advance(86401)
            app.stack.feed.publish('BTC/USDT',Price('89',USDT),Price('89',USDT))
            app.stack.gateway.sync_order(result.order,operator=app.stack.operator_id)
            self.assertTrue(result.order.filled_quantity.is_zero)
            self.assertEqual(app.stack.portfolio.cash,Money('10000',USDT))
            self.assertFalse(app.stack.portfolio.open_positions())
        finally:app.stop()

    def test_missing_metadata_blocks_real_market_readiness_and_execution(self):
        stream=CoinSwitchSpotStream(symbol='BTC/USDT',exchange='c2c1',connection_factory=lambda *_:None)
        app=build_coinswitch_paper_application(stream=stream,starting_cash=Money('10000',USDT),port=0)
        try:
            snapshot=app.dashboard.snapshot()
            self.assertTrue(snapshot['market_metadata']['blocked'])
            self.assertIn('market_metadata_missing',snapshot['operations']['blocked_reasons'])
            cycle=app.service.poll_once()
            self.assertIn('metadata is missing or unverified',cycle.skipped_reason)
            self.assertEqual(app.stack.broker.placement_count,0)
        finally:app.stop()
    def test_native_trades_to_ma_risk_fills_persistence_dashboard_and_restart(self):
        clock=ManualClock(dt.datetime(2024,1,1,tzinfo=UTC));connection=NativeConnection()
        stream=CoinSwitchSpotStream(symbol='BTC/USDT',exchange='c2c1',connection_factory=lambda *_:connection)
        prices=['100','99.5','99','98.5','98','97.5','97','98','100','103','106','109','112','110','107','103','99','95','92']*3
        with tempfile.TemporaryDirectory() as folder,patch('trading.app.runtime.SystemClock',return_value=clock):
            database=Path(folder)/'real.db'
            metadata=capture(trade_info(exchange='c2c1',symbol='BTC/USDT',minimum='0',maximum='100000',base=8),
                exchange='c2c1',symbol='BTC/USDT',captured_at=clock.now().isoformat())
            app=build_coinswitch_paper_application(stream=stream,starting_cash=Money('10000',USDT),sqlite_path=database,
                fee_bps=10,slippage_bps=2,port=0,market_info_body=metadata)
            try:
                stream.start()
                for i,price in enumerate(prices):
                    clock.advance(60)
                    at=int(clock.now().timestamp()*1000)
                    from decimal import Decimal
                    connection.drained.clear()
                    connection.event('FETCH_ORDER_BOOK_CS_PRO',{'s':'BTC,USDT','timestamp':at,
                        'bids':[[str(Decimal(price)-Decimal('.01')),'10']],
                        'asks':[[str(Decimal(price)+Decimal('.01')),'10']]})
                    connection.event('FETCH_TRADES_CS_PRO',{'E':at,'p':price,'q':'1','s':'BTC,USDT','t':i+1,'e':'c2c1'})
                    connection.messages.put('2');self.assertTrue(connection.drained.wait(2))
                    app.service.poll_once()
                status=app.dashboard.snapshot()
                self.assertEqual(status['data_origin']['kind'],'real_market_paper')
                self.assertEqual(status['portfolio']['result_type'],'real_market_paper')
                self.assertEqual(status['experience']['candles'],len(prices)-2)
                self.assertTrue(status['orders']);self.assertGreater(app.stack.portfolio.fees.amount,0)
                self.assertTrue(any(r.action=='strategy.signalled' for r in app.stack.audit.records()))
                self.assertTrue(any(r.action=='portfolio.fill' for r in app.stack.audit.records()))
                self.assertTrue(all(o.symbol=='BTC/USDT' for o in app.stack.orders.all_orders()))
                self.assertFalse(status['application']['live_trading'])
                cash=app.stack.portfolio.cash;orders=len(app.stack.orders.all_orders())
            finally:app.stop()
            restored=build_coinswitch_paper_application(symbol='BTC/USDT',exchange='c2c1',starting_cash=Money('10000',USDT),
                sqlite_path=database,fee_bps=10,slippage_bps=2,port=0,market_info_body=metadata)
            try:
                self.assertTrue(restored.stack.startup.ok)
                self.assertEqual(restored.stack.portfolio.cash,cash)
                self.assertEqual(len(restored.stack.orders.all_orders()),orders)
                self.assertNotEqual(restored.stack.bot.state,'running')
            finally:restored.stop()

    def test_origin_mismatch_refuses_before_state_changes(self):
        with tempfile.TemporaryDirectory() as folder:
            db=Path(folder)/'demo.db';app=build_demo_paper_application(sqlite_path=db,port=0)
            app.stop();before=db.read_bytes()
            expected={'kind':'real_market_paper','candle_policy':'complete-connected-minutes/v1','provider':'CoinSwitch public feed','symbol':'BTC/USDT','exchange':'c2c1'}
            with self.assertRaisesRegex(ValueError,'origin mismatch'):check_paper_origin(db,expected)
            self.assertEqual(db.read_bytes(),before)

    def test_actual_bounded_experience_worker_dispatches_and_rejects_or_holds(self):
        fixture=fixtures.ExperienceLearningTests()
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);state=SQLiteState(root/'real.db');store=fixture.store(state)
            for i in range(800):store.append(fixture.bar(i))
            state.close()
            configuration={'enabled':True,'operation':'experience_retraining','interval_seconds':3600,
                'timeout_seconds':20,'cpu_seconds':15,'memory_mb':1024,'state_db':str(root/'real.db'),
                'registry_db':str(root/'models.db'),'symbol':'BTC/USDT','exchange':'c2c1','output_directory':str(root/'learning')}
            config=root/'job.json';config.write_text(json.dumps(configuration))
            self.assertEqual(jobs_main([str(config),'--once']),0)
            reports=list((root/'learning').glob('experience-*-complete.json'));self.assertEqual(len(reports),1)
            report=json.loads(reports[0].read_text());self.assertFalse(report['deployed'])
            self.assertIn(report['status'],{'rejected','awaiting_independent_qualification'})
            self.assertEqual(json.loads((root/'learning'/'scheduler-status.json').read_text())['phase'],'stopped')

    def test_worker_waits_cleanly_when_genuine_experience_is_insufficient(self):
        fixture=fixtures.ExperienceLearningTests()
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);state=SQLiteState(root/'real.db');store=fixture.store(state);store.append(fixture.bar(0));state.close()
            config={'enabled':True,'operation':'experience_retraining','interval_seconds':3600,'timeout_seconds':15,
                'cpu_seconds':10,'memory_mb':1024,'state_db':str(root/'real.db'),'symbol':'BTC/USDT','exchange':'c2c1',
                'output_directory':str(root/'learning')}
            path=root/'job.json';path.write_text(json.dumps(config));self.assertEqual(jobs_main([str(path),'--once']),0)
            self.assertEqual(json.loads((root/'learning'/'learning-readiness.json').read_text())['status'],'awaiting_experience')
            self.assertFalse(list((root/'learning').glob('*-model.json')))
