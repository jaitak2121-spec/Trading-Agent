"""Synthetic fixtures only: runtime durability, learning and safe nondeployment."""
import datetime as dt
from dataclasses import replace
import json
from pathlib import Path
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from trading.adapters.persistence import SQLiteState
from trading.adapters.persistence.candles import SQLiteCandleStore,candle_row
from trading.adapters.marketdata import CandleIngestor,CandleNormalizer
from trading.app.experience_learning import snapshot_experience,run_experience_job,learning_status
from trading.app import build_demo_paper_application
from trading.app.model_training import validate_paper_model_trial,paper_model_approval_phrase
from trading.core.clock import ManualClock,UTC
from trading.core.marketdata import Candle
from trading.core.money import Price,Quantity,USDT
from tests.test_paper_service import ScriptedSource

BASE=dt.datetime(2024,1,1,tzinfo=UTC)


class ExperienceLearningTests(unittest.TestCase):
    def store(self,state,cache_size=4096):
        from trading.adapters.persistence.sqlite import bind_paper_origin
        bind_paper_origin(state,{'kind':'real_market_paper','candle_policy':'complete-connected-minutes/v1','provider':'CoinSwitch public feed','symbol':'BTC/USDT','exchange':'c2c1'})
        return SQLiteCandleStore(state,symbol='BTC/USDT',currency=USDT,asset='BTC',source='coinswitch-c2c1',
            clock=ManualClock(BASE+dt.timedelta(days=30)),cache_size=cache_size)

    def bar(self,index):
        close=str(100+(index%7))
        return Candle(symbol='BTC/USDT',open_time=BASE+dt.timedelta(minutes=index),interval_seconds=60,
            open=Price('100',USDT),high=Price('110',USDT),low=Price('90',USDT),close=Price(close,USDT),
            volume=Quantity('1','BTC'),source='coinswitch-c2c1')

    def test_restart_dedup_conflict_and_bounded_cache(self):
        with tempfile.TemporaryDirectory() as folder:
            path=Path(folder)/'paper.db';state=SQLiteState(path)
            store=self.store(state,cache_size=4)
            for i in range(10):store.append(self.bar(i))
            self.assertEqual(len(store),4);self.assertEqual(store.status()['candles'],10)
            store.append(self.bar(9))
            with self.assertRaisesRegex(ValueError,'conflicting'):
                store.append(replace(self.bar(9),close=Price('101',USDT)))
            state.close();state=SQLiteState(path)
            try:
                recovered=self.store(state,cache_size=4)
                self.assertEqual(recovered.last('BTC/USDT'),self.bar(9))
                ingestor=CandleIngestor(source=ScriptedSource([[candle_row(self.bar(9))],[candle_row(self.bar(11))]]),
                    normalizer=CandleNormalizer(clock=recovered.clock,currency=USDT,volume_asset='BTC',source='coinswitch-c2c1'),
                    store=recovered,clock=recovered.clock)
                self.assertEqual(ingestor.poll_once().duplicates,1)
                self.assertEqual(ingestor.poll_once().gaps[0].missing,1)
                self.assertEqual(recovered.status()['candles'],11)
            finally:state.close()

    def test_incomplete_and_corrupted_recovery_refuse(self):
        with tempfile.TemporaryDirectory() as folder:
            state=SQLiteState(Path(folder)/'paper.db')
            try:
                store=self.store(state)
                with self.assertRaises(ValueError):store.append(replace(self.bar(0),open_time=BASE+dt.timedelta(days=31)))
                store.append(self.bar(0))
                with state.transaction():state.connection.execute("UPDATE market_candles SET payload='{}'")
                with self.assertRaisesRegex(ValueError,'hash'):self.store(state)
            finally:state.close()

    def test_latest_contiguous_run_never_bridges_gaps(self):
        with tempfile.TemporaryDirectory() as folder:
            state=SQLiteState(Path(folder)/'paper.db');store=self.store(state)
            try:
                for i in list(range(10))+list(range(20,740)):store.append(self.bar(i))
                data=snapshot_experience(state.path,symbol='BTC/USDT',exchange='c2c1')
                self.assertEqual(len(data['candles']),720)
                self.assertEqual(data['experience']['discarded_pre_gap_rows'],10)
                self.assertTrue(data['integrity']['passed'])
                with self.assertRaises(ValueError):snapshot_experience(state.path,symbol='BTC/USDT',exchange='c2c2')
            finally:state.close()

    def test_real_fit_future_evaluation_registry_rejection_or_hold_and_dedup(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);state=SQLiteState(root/'paper.db');store=self.store(state)
            try:
                for i in range(800):store.append(self.bar(i))
                config={'state_db':str(state.path),'registry_db':str(root/'models.db'),'symbol':'BTC/USDT','exchange':'c2c1',
                        'output_directory':str(root/'learning')}
                output=run_experience_job(config);record=json.loads(output.read_text())
                self.assertEqual(learning_status(root/'learning')['status'],record['status'])
                self.assertFalse(record['deployed']);self.assertIn(record['status'],{'rejected','awaiting_independent_qualification'})
                attempt=Path(record['artifacts'][0]['path']).parent
                model=json.loads((attempt/'model.json').read_text());evaluation=json.loads((attempt/'evaluation.json').read_text())
                future=json.loads((attempt/'evaluation-data.json').read_text())
                self.assertGreater(future['candles'][0]['open_time'],model['training_end_open_time'])
                with self.assertRaisesRegex(ValueError,'held'):
                    validate_paper_model_trial(model,evaluation,paper_model_approval_phrase(model))
                with patch('trading.app.experience_learning.train_direction_model') as fit:
                    self.assertEqual(run_experience_job(config),output);fit.assert_not_called()
                    store.append(self.bar(800))
                    self.assertEqual(run_experience_job(config),output);fit.assert_not_called()
                (attempt/'model.json').write_text('{}')
                with self.assertRaisesRegex(ValueError,'changed'):run_experience_job(config)
            finally:state.close()

    def test_paused_collection_never_calls_strategy_or_gateway(self):
        app=build_demo_paper_application(port=0)
        try:
            app.pause(reason='test collection only')
            with patch.object(app.service._runner,'generate') as generate,patch.object(app.stack.gateway,'submit') as submit:
                cycle=app.service.collect_only()
                self.assertTrue(cycle.candles.accepted)
                generate.assert_not_called();submit.assert_not_called()
                self.assertEqual(app.stack.bot.state,'paused')
        finally:app.stop()

    def test_dashboard_learning_and_durable_collection_are_visible(self):
        from trading.app.runtime import build_coinswitch_paper_application
        from trading.adapters.coinswitch import CoinSwitchSpotStream
        from trading.core.money import Money
        with tempfile.TemporaryDirectory() as folder:
            stream=CoinSwitchSpotStream(symbol='BTC/USDT',exchange='c2c1',connection_factory=lambda *_:None)
            app=build_coinswitch_paper_application(stream=stream,starting_cash=Money('10000',USDT),sqlite_path=Path(folder)/'paper.db',
                learning_reports=Path(folder)/'learning',port=0)
            try:
                status=app.dashboard.snapshot()
                self.assertTrue(status['experience']['persistent'])
                self.assertEqual(status['ml']['learning']['status'],'awaiting_experience')
                self.assertFalse(status['application']['live_trading'])
            finally:app.stop()

    def test_approved_fixture_inference_risk_fill_accounting_and_restart(self):
        from trading.app.runtime import build_coinswitch_paper_application
        from trading.app.model_training import train_direction_model,direction_model_sha256
        from trading.app.model_strategy import PaperDirectionModelStrategy
        from trading.app.model_registry import ModelRegistry,require_registry_approval
        from trading.adapters.persistence import SQLiteAuditSink
        from trading.core.audit import AuditLog
        from trading.core.authz import Principal,Role
        from trading.core.money import Money
        from tests.test_model_training import dataset
        # Explicit synthetic approval fixture; never an approval of market research.
        model=dict(train_direction_model(dataset()).model);model.update(weights=[0.,0.,0.],bias=10.)
        evidence={'schema':'offline-direction-evaluation/v1','model_sha256':direction_model_sha256(model),
            'evaluation_dataset_sha256':'a'*64,'samples':100,'eligible_for_paper_trial':True,
            'approved':False,'paper_only':True,'live_trading':False,'self_promoting':False}
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);mp=root/'model.json';ep=root/'evaluation.json'
            mp.write_text(json.dumps(model));ep.write_text(json.dumps(evidence))
            registry_state=SQLiteState(root/'models.db');sink=SQLiteAuditSink(registry_state)
            registry=ModelRegistry(registry_state,AuditLog(sink,initial_records=sink.records));operator=Principal('test',Role.OPERATOR)
            identity=registry.register(mp,ep,operator,reason='synthetic integration fixture')
            registry.transition(identity,'approved_for_paper',operator,reason='synthetic integration fixture',confirmation=paper_model_approval_phrase(model))
            registry_state.close()
            def strategy():
                return PaperDirectionModelStrategy(model=model,evaluation=evidence,confirmation=paper_model_approval_phrase(model),symbol='BTC/USDT',
                    approval_check=lambda:require_registry_approval(root/'models.db',model,mp,ep))
            now=dt.datetime.now(UTC);minute=now.replace(second=0,microsecond=0)
            from tests.test_coinswitch_market import capture,trade_info
            metadata=capture(trade_info(exchange='c2c1',symbol='BTC/USDT',minimum='0',maximum='100000',base=8),
                exchange='c2c1',symbol='BTC/USDT',captured_at=now.isoformat())
            last=replace(self.bar(0),open_time=minute-dt.timedelta(minutes=1))
            stream=SimpleNamespace(symbol='BTC/USDT',quote_asset='USDT',base_asset='BTC',exchange='c2c1',
                quote_source=ScriptedSource([[{'symbol':'BTC/USDT','bid':'99.99','ask':'100.01','timestamp':now.isoformat()}]]),
                candle_source=ScriptedSource([[candle_row(last)]]))
            app=build_coinswitch_paper_application(stream=stream,starting_cash=Money('10000',USDT),
                sqlite_path=root/'paper.db',strategy=strategy(),fee_bps=10,slippage_bps=2,port=0,market_info_body=metadata)
            try:
                for i in range(16,1,-1):app.service._store.append(replace(self.bar(0),open_time=minute-dt.timedelta(minutes=i)))
                cycle=app.service.poll_once();self.assertTrue(cycle.signals)
                app.service.poll_once()
                self.assertTrue(app.stack.portfolio.open_positions())
                self.assertGreater(app.stack.portfolio.fees.amount,0)
                self.assertTrue(any(r.action=='ml.prediction' for r in app.stack.audit.records()))
                held=app.stack.portfolio.open_positions()[0].quantity
            finally:app.stop()
            recovered=build_coinswitch_paper_application(stream=stream,starting_cash=Money('10000',USDT),
                sqlite_path=root/'paper.db',strategy=strategy(),fee_bps=10,slippage_bps=2,port=0,market_info_body=metadata)
            try:
                self.assertTrue(recovered.stack.startup.ok)
                self.assertNotEqual(recovered.stack.bot.state,'running')
                self.assertEqual(recovered.stack.portfolio.open_positions()[0].quantity,held)
                self.assertEqual(recovered.service._store.status()['candles'],16)
            finally:recovered.stop()

    def test_monitor_safety_halt_precedes_signal_generation(self):
        app=build_demo_paper_application(port=0)
        app.service._prediction_monitor=lambda:{'model_sha256':'a'*64,'alert_policy':{'on_alert':'pause'},
            'alerts':[{'code':'feature_drift','feature':'test'}]}
        try:
            with patch.object(app.service._runner,'generate') as generate,patch.object(app.stack.gateway,'submit') as submit:
                cycle=app.service.poll_once()
                self.assertEqual(cycle.skipped_reason,'ML monitoring safety halt')
                self.assertEqual(app.stack.bot.state,'paused')
                app.resume(reason='test review; alert remains active')
                app.service.poll_once()
                self.assertEqual(app.stack.bot.state,'paused')
                generate.assert_not_called();submit.assert_not_called()
        finally:app.stop()
