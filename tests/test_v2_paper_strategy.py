"""Static V2 fixtures only: no genuine model fitting or protected artifact reads."""
import copy
import datetime as dt
import json
import tempfile
import unittest
from pathlib import Path

from trading.app.ml_research import (FEATURE_NAMES, FEATURE_SCHEMA, _sha256,
                                     features_from_closed_candles, generate_causal_dataset)
from trading.app.model_deployment import model_identity, paper_approval_phrase, validate_paper_deployment
from trading.app.model_strategy import PaperDirectionModelStrategy
from trading.app.model_registry import ModelRegistry, require_registry_approval
from trading.app.runtime import build_demo_paper_application
from trading.adapters.persistence import SQLiteState, SQLiteAuditSink
from trading.core.audit import AuditLog, AuditCategory
from trading.core.authz import Principal, Role
from trading.core.clock import UTC
from trading.core.marketdata import Candle, Quote, MarketSnapshot
from trading.core.money import Money, Price, Quantity, USDT
from trading.strategy.context import MarketContext
from tests.test_ml_research import source
from tests.test_backtest import bars


def fixture(horizon=30):
    model = {'schema':'offline-logistic-classifier/v2', 'research_schema':FEATURE_SCHEMA,
             'artifact_role':'final_frozen_development_candidate',
             'symbol':'BTC/USDT', 'exchange':'c2c1', 'horizon_minutes':horizon,
             'features':list(FEATURE_NAMES), 'feature_schema_sha256':_sha256(list(FEATURE_NAMES)),
             'means':[0.0]*18, 'scales':[1.0]*18, 'weights':[0.0]*18, 'bias':2.0,
             'threshold':0.5, 'paper_inference_start_ms':0,
             'configuration':{'preprocessing_fit':'training_rows_only'},
             'confirmation_rows_used_for_fitting':0, 'validation_rows_used_for_fitting':0,
             'development_artifact_sha256':'a'*64}
    model.update({name:False for name in ('threshold_optimized','features_tuned_after_selection',
        'hyperparameters_tuned_after_selection','confirmation_loaded','confirmation_evaluated',
        'final_test_accessed','model_promoted','live_orders','paper_orders','live_trading',
        'approved','production_model')})
    from trading.app.dataset_eligibility import assess_dataset
    model['source_integrity']=assess_dataset(source(360))
    model['source_dataset_sha256']=model['source_integrity']['dataset_sha256']
    model['model_sha256'] = _sha256(model)
    criteria = {key:True for key in ('required_six_folds','mean_walk_forward_roc_auc_at_least_0_52',
        'at_least_five_folds_above_random','confirmation_roc_auc_ci_lower_above_0_5',
        'confirmation_average_precision_above_prevalence','confirmation_brier_beats_constant_prevalence',
        'no_prediction_collapse')}
    evidence = {'schema':'sealed-confirmation-evaluation/v2', 'model_sha256':model['model_sha256'],
        'development_artifact_sha256':'a'*64, 'confirmation_artifact_sha256':'b'*64,
        'confirmation_sample_sha256':'c'*64,'prediction_sha256':'d'*64,'authorization_sha256':'e'*64,
        'samples':1000,
        'accepted':True, 'confirmation_evaluated_once':True, 'combined_acceptance_criteria':criteria,
        'metrics':{'roc_auc':0.6, 'average_precision':0.6, 'prevalence_baseline':0.5, 'brier_score':0.2,
                   'prediction_distribution':{'positive_rate':0.5}},
        'confidence_intervals':{'roc_auc_95_interval':[0.55,0.65]},
        'baselines':{'constant_prevalence':{'brier_score':0.25}}}
    evidence.update({key:False for key in ('model_retrained','preprocessing_refit','threshold_optimized',
        'features_selected_on_confirmation','final_test_accessed','paper_orders','live_orders')})
    evidence['evidence_sha256'] = _sha256(evidence)
    return model, evidence


def candle_history(rows):
    return [Candle(symbol='BTC/USDT', open_time=dt.datetime.fromtimestamp(row['open_time']/1000, UTC),
                   interval_seconds=60, source='synthetic test fixture',
                   volume=Quantity(row['volume'],'BTC'),
                   **{name:Price(row[name], USDT, max_scale=18) for name in ('open','high','low','close')})
            for row in rows]


def context(history, at=None):
    at = at or history[-1].close_time
    quote = Quote(symbol='BTC/USDT', bid=history[-1].close, ask=history[-1].close,
                  as_of=at, source='synthetic test fixture')
    return MarketContext(as_of=at, equity=Money('10000',USDT),
                         snapshot=MarketSnapshot(as_of=at, quotes={'BTC/USDT':quote}),
                         history={'BTC/USDT':history})


class V2PaperStrategyTest(unittest.TestCase):
    def test_model_with_failed_missing_or_unbound_dataset_integrity_cannot_deploy(self):
        for change in ('failed','missing','unbound'):
            model,evidence=fixture()
            if change=='missing':model.pop('source_integrity')
            elif change=='unbound':model['source_dataset_sha256']='f'*64
            else:
                gate=model['source_integrity']
                gate['eligible_for_strategy_qualification']=False
                gate['failures']=['missing candles']
                gate['evidence_sha256']=_sha256({k:v for k,v in gate.items() if k!='evidence_sha256'})
            model['model_sha256']=_sha256({k:v for k,v in model.items() if k!='model_sha256'})
            evidence['model_sha256']=model['model_sha256']
            evidence['evidence_sha256']=_sha256({k:v for k,v in evidence.items() if k!='evidence_sha256'})
            with self.assertRaises(ValueError):self.strategy(model,evidence)

    def test_context_resets_all_indicator_history_at_gaps_and_rejects_duplicates(self):
        history=candle_history(source(360)['candles'][:241])
        selected=history[:100]+history[101:]
        current=context(selected)
        self.assertEqual(current.candles('BTC/USDT'),tuple(history[101:]))
        self.assertFalse(current.has_history('BTC/USDT',at_least=240))
        with self.assertRaisesRegex(ValueError,'duplicate'):
            context(history+[history[-1]])

    def test_application_builder_checks_model_market_before_opening_stream(self):
        from trading.app.runtime import build_coinswitch_paper_application
        from trading.adapters.coinswitch import CoinSwitchSpotStream
        stream=CoinSwitchSpotStream(symbol='BTC/USDT',exchange='c2c2',connection_factory=lambda *_:None)
        with self.assertRaisesRegex(ValueError,'provenance'):
            build_coinswitch_paper_application(stream=stream,strategy=self.strategy())

    def test_v2_signal_still_cannot_bypass_gateway_risk(self):
        from trading.app.composition import build_paper_stack
        from trading.core.config import TradingConfig
        from trading.core.clock import ManualClock
        from trading.core.orders import OrderIntent, OrderSide
        history=candle_history(source(360)['candles'][:241])
        current=context(history)
        signal=self.strategy().generate(current)[0]
        config=TradingConfig.from_mapping({'base_currency':'USDT','max_order_notional':'1'}).risk
        stack=build_paper_stack(starting_cash=Money('1000',USDT),risk=config,clock=ManualClock(current.as_of))
        stack.feed.publish('BTC/USDT',signal.reference_price,signal.reference_price)
        result=stack.gateway.submit(OrderIntent(signal.strategy_name,signal.signal_id,signal.symbol,
            OrderSide.BUY,Quantity('0.1','BTC')),proposer=stack.strategy_id,
            mark_prices={'BTC/USDT':signal.reference_price})
        self.assertNotEqual(result.outcome,'executed')
        self.assertEqual(stack.broker.placement_count,0)

    def strategy(self, model=None, evidence=None):
        if model is None:
            model, evidence = fixture()
        return PaperDirectionModelStrategy(model=model, evaluation=evidence,
            confirmation=paper_approval_phrase(model), symbol='BTC/USDT', exchange='c2c1')

    def test_online_features_equal_offline_features_for_all_horizons(self):
        captured = source(360)
        history = candle_history(captured['candles'][:241])
        for horizon in (5,15,30):
            dataset = generate_causal_dataset(captured, horizon_minutes=horizon)
            expected = next(row['features'] for row in dataset['samples'] if row['feature_open_time']==240*60000)
            self.assertEqual(features_from_closed_candles(history,symbol='BTC/USDT'), expected)
            model,evidence = fixture(horizon)
            strategy = self.strategy(model,evidence)
            self.assertTrue(strategy.generate(context(history)))
            self.assertEqual(strategy.last_prediction['horizon_seconds'],horizon*60)
            self.assertEqual(strategy.last_prediction['features'],list(expected.values()))

    def test_warmup_gaps_and_off_stride_do_not_predict(self):
        history = candle_history(source(360)['candles'])
        for selected in (history[:239], history[:100]+history[101:241], history[:242]):
            strategy = self.strategy()
            self.assertEqual(strategy.generate(context(selected)),[])
            self.assertIsNone(strategy.last_prediction)

    def test_future_bars_do_not_change_current_prediction(self):
        history = candle_history(source(360)['candles'])
        strategy = self.strategy()
        strategy.generate(context(history[:241]))
        expected = copy.deepcopy(strategy.last_prediction)
        strategy.generate(context(history, at=history[240].close_time))
        self.assertEqual(strategy.last_prediction,expected)

    def test_rejected_or_contradictory_confirmation_cannot_deploy(self):
        for change in ('accepted','brier'):
            model,evidence = fixture()
            if change == 'accepted':
                evidence['accepted'] = False
            else:
                evidence['metrics']['brier_score']=0.3
            evidence.pop('evidence_sha256')
            evidence['evidence_sha256']=_sha256(evidence)
            with self.assertRaises(ValueError):
                self.strategy(model,evidence)

    def test_market_provenance_missing_or_exchange_mismatched_refuses(self):
        model,evidence=fixture()
        with self.assertRaisesRegex(ValueError,'exchange'):
            PaperDirectionModelStrategy(model=model,evaluation=evidence,
                confirmation=paper_approval_phrase(model),symbol='BTC/USDT',exchange='c2c2')
        model.pop('symbol')
        model.pop('model_sha256')
        model['model_sha256']=_sha256(model)
        with self.assertRaisesRegex(ValueError,'provenance'):
            self.strategy(model,evidence)

    def test_deployment_boundary_and_in_memory_parameter_isolation(self):
        model,evidence=fixture()
        strategy=self.strategy(model,evidence)
        history=candle_history(source(360)['candles'][:241])
        strategy.generate(context(history))
        expected=copy.deepcopy(strategy.last_prediction)
        model['weights'][0]=10000
        strategy.generate(context(history))
        self.assertEqual(strategy.last_prediction,expected)
        model,evidence=fixture()
        model['paper_inference_start_ms']=10**15
        model.pop('model_sha256');model['model_sha256']=_sha256(model)
        evidence['model_sha256']=model['model_sha256']
        evidence.pop('evidence_sha256');evidence['evidence_sha256']=_sha256(evidence)
        strategy=self.strategy(model,evidence)
        self.assertEqual(strategy.generate(context(history)),[])

    def test_registry_handles_v2_without_modifying_frozen_files(self):
        model,evidence=fixture()
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            model_path,evidence_path=root/'model.json',root/'evaluation.json'
            model_path.write_text(json.dumps(model));evidence_path.write_text(json.dumps(evidence))
            before=model_path.read_bytes()
            state=SQLiteState(root/'registry.db')
            try:
                sink=SQLiteAuditSink(state)
                registry=ModelRegistry(state,AuditLog(sink,initial_records=sink.records))
                operator=Principal('operator',Role.OPERATOR)
                identity=registry.register(model_path,evidence_path,operator,reason='synthetic test fixture')
                registry.transition(identity,'approved_for_paper',operator,reason='test',
                                    confirmation=paper_approval_phrase(model))
                require_registry_approval(root/'registry.db',model,model_path,evidence_path)
                self.assertEqual(model_path.read_bytes(),before)
            finally:
                state.close()

    def test_multiminute_outcome_requires_every_intermediate_candle(self):
        for missing in (False,True):
            app=build_demo_paper_application()
            try:
                history=bars([100,101,102,103,104,105])
                app.stack.audit.record(AuditCategory.SIGNAL,'ml.prediction',details={
                    'prediction_id':'synthetic-test','symbol':'BTC/USDT','horizon_seconds':300,
                    'reference_close':'100','up_probability':'0.8','threshold':'0.5',
                    'model_sha256':'test-fixture','bar_open_time':history[0].open_time.isoformat()})
                for index,bar in enumerate(history[1:],1):
                    if not (missing and index==2):
                        app.service._store.append(bar)
                app.stack.clock.set_wall_clock(history[-1].close_time)
                app.service._resolve_predictions(app.stack.clock.now())
                self.assertEqual(len([r for r in app.stack.audit.records() if r.action=='ml.outcome']),int(not missing))
                self.assertEqual(app.stack.broker.placement_count,0)
            finally:
                app.stop()
