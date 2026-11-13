"""Synthetic fixtures test research mechanics, never profitability evidence."""
import copy
import datetime as dt
from dataclasses import replace
from decimal import Decimal
import json
import unittest

from trading.app.net_expectancy import (features,samples,trade_target,fit_model,predict,
    FEATURES,digest,split_samples,confidence,prepare,ExpectancyStrategy,stop_fraction)
from trading.app.backtest import HistoricalBacktester
from trading.core.marketdata import Candle
from trading.core.money import Price,Quantity,USDT
from trading.strategy.signals import Signal,SignalStrategy,SignalDirection


def bars(n=350):
    start=dt.datetime(2024,1,1,tzinfo=dt.timezone.utc);result=[]
    for i in range(n):
        p=Decimal('100')+Decimal(i)/10
        result.append(Candle(symbol='BTC/USDT',open_time=start+dt.timedelta(minutes=5*i),
            interval_seconds=300,open=Price(p,USDT),high=Price(p+Decimal('.5'),USDT),
            low=Price(p-Decimal('.5'),USDT),close=Price(p+Decimal('.1'),USDT),
            volume=Quantity('10','BTC'),source='synthetic-unit-test'))
    return result


class NetExpectancyTests(unittest.TestCase):
    def test_causal_features_exact_returns_and_future_independence(self):
        series=bars();row=features(series[:289])
        self.assertEqual(len(row),18)
        self.assertAlmostEqual(row[0],float(series[288].close.amount/series[287].close.amount-1))
        before=samples(series,12)[0]
        changed=series[:304]+[replace(bar,**{name:Price(getattr(bar,name).amount*2,USDT)
            for name in ('open','high','low','close')}) for bar in series[304:]]
        after=samples(changed,12)[0]
        self.assertEqual(before['features'],after['features'])
        self.assertEqual(before['decision_ms'],after['decision_ms'])
        self.assertGreater(before['label_end_ms'],before['decision_ms'])
        self.assertNotEqual(before['net_return_target'],after['net_return_target'])

    def test_features_and_targets_reject_gaps_and_incomplete_windows(self):
        series=bars()
        with self.assertRaises(ValueError):features(series[:100])
        with self.assertRaises(ValueError):features(series[:200]+series[201:290])
        with self.assertRaises(ValueError):trade_target(series[:10],0,12,Decimal('90'))
        with self.assertRaises(ValueError):trade_target(series[:4]+series[5:20],0,12,Decimal('90'))

    def test_labels_match_existing_simulator_fills_fees_and_stops(self):
        for stop in (Decimal('110'),Decimal('128.5')):
            series=bars(310);index=288;horizon=12
            class Fixed(SignalStrategy):
                name='synthetic-target-parity'
                def generate(self,context):
                    now=context.as_of
                    if now==series[index].close_time:direction=SignalDirection.LONG
                    elif now==series[index+horizon].close_time and not context.is_flat('BTC/USDT'):direction=SignalDirection.EXIT
                    else:return []
                    return [Signal(strategy_name=self.name,signal_id=str(now),symbol='BTC/USDT',
                        direction=direction,reference_price=context.price('BTC/USDT'),as_of=now,
                        rationale='synthetic target parity',**({'stop_loss':Price(stop,USDT)} if direction is SignalDirection.LONG else {}))]
            result=HistoricalBacktester(Fixed()).run(series)
            trades=result.report['trades'];self.assertEqual(len(trades),1)
            self.assertAlmostEqual(float(trades[0]['net_return']),trade_target(series,index,horizon,stop),places=12)

    def test_full_horizon_purge_and_outer_separation(self):
        rows=[{'decision_ms':t,'label_end_ms':t+10} for t in (0,10,20,30,40)]
        fold={'training_start_ms':0,'training_end_ms':30,'validation_start_ms':30,'validation_end_ms':60}
        train,validation=split_samples(rows,fold)
        self.assertEqual([r['decision_ms'] for r in train],[0,10])
        self.assertEqual([r['decision_ms'] for r in validation],[30,40])
        with self.assertRaises(ValueError):split_samples(rows,{**fold,'training_end_ms':31})

    def test_models_persist_deterministic_training_only_scaling_and_integrity(self):
        rows=[{'features':[float(i%11)/10]+[0.]*(len(FEATURES)-1),
               'net_return_target':.02 if i%11>5 else -.01,'label_end_ms':i} for i in range(120)]
        for kind in ('ridge','tree','logistic_payoff'):
            model=fit_model(rows,kind);loaded=json.loads(json.dumps(model))
            self.assertEqual(model,fit_model(rows,kind))
            vector=rows[0]['features'];self.assertEqual(predict(model,vector),predict(loaded,vector))
            before=copy.deepcopy(model);predict(model,[1000.]*len(FEATURES));self.assertEqual(model,before)
            with self.assertRaises(ValueError):predict(model,vector[:-1])
            loaded['target_mean']=100
            with self.assertRaises(ValueError):predict(loaded,vector)

    def test_training_provenance_changes_with_target_and_feature_changes(self):
        rows=[{'features':[i/100.]*len(FEATURES),'net_return_target':i/1000.,'label_end_ms':i} for i in range(100)]
        first=fit_model(rows,'ridge');rows[0]['net_return_target']=.5
        self.assertNotEqual(first['training_rows_sha256'],fit_model(rows,'ridge')['training_rows_sha256'])
        with self.assertRaises(ValueError):fit_model(rows[:50],'ridge')

    def test_block_interval_no_trade_baseline_and_reproducibility(self):
        zero=confidence([[0.]*15]*3,9,replicates=100)
        self.assertEqual(zero['lower'],0);self.assertEqual(zero['upper'],0)
        self.assertEqual(zero,confidence([[0.]*15]*3,9,replicates=100))
        self.assertIsNone(confidence([[1.]*15],9))

    def test_protected_paths_refused_before_read(self):
        for path in ('artifacts/segment-7.json','artifacts/confirmation.json',
                     'artifacts/btc-usdt-1m-2025-11-02_2026-11-02-development-source.json'):
            with self.assertRaises(ValueError):prepare([path])

    def test_hourly_features_models_and_holding_period_preserve_native_units(self):
        series=[replace(bar,open_time=bar.open_time.replace(hour=0,minute=0)+dt.timedelta(hours=i),
                        interval_seconds=3600) for i,bar in enumerate(bars(330))]
        # Use one fixed base date, never reinterpret historical data.
        start=dt.datetime(2024,1,1,tzinfo=dt.timezone.utc)
        series=[replace(bar,open_time=start+dt.timedelta(hours=i)) for i,bar in enumerate(series)]
        with self.assertRaises(ValueError):features(series[:289])
        vector=features(series[:289],3600);self.assertEqual(len(vector),18)
        rows=[{'features':[i/100.]*18,'net_return_target':i/10000.,'label_end_ms':i} for i in range(100)]
        model=fit_model(rows,'ridge',3600)
        with self.assertRaises(ValueError):predict(model,vector)
        self.assertIsInstance(predict(model,vector,3600),float)
        when=int(series[299].close_time.timestamp()*1000)
        strategy=ExpectancyStrategy(model,6,{when:.1})
        self.assertIn('-h6-',strategy.name)
        result=HistoricalBacktester(strategy).run(series)
        trade=result.report['trades'][0]
        self.assertEqual(dt.datetime.fromisoformat(trade['exit_time'])-dt.datetime.fromisoformat(trade['entry_time']),dt.timedelta(hours=6))
        stop=Price.rounded(series[299].close.amount*(1-Decimal(str(stop_fraction(features(series[11:300],3600),6)))),USDT)
        self.assertAlmostEqual(float(trade['net_return']),trade_target(series,299,6,stop.amount),places=12)

    def test_hourly_decision_schedule_does_not_change_five_minute_simulation(self):
        series=bars(340)
        rows=[{'features':[i/100.]*18,'net_return_target':i/10000.,'label_end_ms':i} for i in range(100)]
        model=fit_model(rows,'ridge')
        predictions={int(series[i].close_time.timestamp()*1000):.1 for i in (299,311)}
        fast=ExpectancyStrategy(model,12,predictions)
        original=ExpectancyStrategy(model,12,predictions)
        original.decision_due=lambda candle:True
        a=HistoricalBacktester(fast).run(series);b=HistoricalBacktester(original).run(series)
        self.assertEqual(a.report['trades'],b.report['trades'])
        self.assertEqual(a.metrics,b.metrics)

    def test_hourly_protocol_refuses_unrelated_and_protected_sources(self):
        from trading.app.hourly_expectancy import prepare as hourly_prepare
        for path in ('artifacts/ml-research-v2/development-30m.json','artifacts/native-5m-complete-runs/run-1.json'):
            with self.assertRaises(ValueError):hourly_prepare([path])

    def test_auxiliary_features_are_causal_aligned_and_gap_safe(self):
        from trading.app.cross_market_expectancy import auxiliary_features
        start=dt.datetime(2024,1,1,tzinfo=dt.timezone.utc)
        native=[replace(b,open_time=start+dt.timedelta(hours=i),interval_seconds=3600) for i,b in enumerate(bars(310))]
        eth=[replace(b,symbol='ETH/USDT',volume=Quantity('10','ETH')) for b in native]
        extra=auxiliary_features(native[:289],eth[:289])
        self.assertEqual(len(extra),7)
        self.assertAlmostEqual(extra[2],0);self.assertAlmostEqual(extra[3],1)
        self.assertAlmostEqual(extra[4],1);self.assertEqual(extra[6],1)
        with self.assertRaises(ValueError):auxiliary_features(native[:289],eth[1:290])
        with self.assertRaises(ValueError):auxiliary_features(native[:289],eth[:200]+eth[201:291])
        with self.assertRaises(ValueError):auxiliary_features(native[:289],eth[:100])
        with self.assertRaises(ValueError):auxiliary_features(native[:289],native[:289])

    def test_auxiliary_column_schema_persistence_and_nonlinear_learning(self):
        names=FEATURES+('causal_auxiliary_test',)
        rows=[{'features':[0.]*len(FEATURES)+[float(i%2)],
               'net_return_target':.02 if i%2 else -.01,'label_end_ms':i} for i in range(120)]
        for kind in ('ridge','tree','logistic_payoff'):
            model=fit_model(rows,kind,3600,names)
            low=predict(model,rows[0]['features'],3600,names)
            high=predict(json.loads(json.dumps(model)),rows[1]['features'],3600,names)
            self.assertGreater(high,low)
            with self.assertRaises(ValueError):predict(model,rows[0]['features'],3600)

    def test_regime_learning_is_deterministic_and_training_only(self):
        rows=[]
        for i in range(120):
            vector=[0.]*18
            for j in (3,6,9,10,16,17):vector[j]=float(i%3)
            rows.append({'features':vector,'net_return_target':[-.02,0.,.03][i%3],'label_end_ms':i})
        model=fit_model(rows,'regime',3600)
        self.assertEqual(model,fit_model(rows,'regime',3600))
        self.assertEqual(sum(model['regime_training_counts']),120)
        self.assertGreater(predict(model,rows[2]['features'],3600),predict(model,rows[0]['features'],3600))
        self.assertEqual(predict(model,rows[2]['features'],3600),predict(json.loads(json.dumps(model)),rows[2]['features'],3600))

    def test_adaptive_selection_cash_fallback_purge_and_hierarchical_interval(self):
        from trading.app.adaptive_expectancy import select_inner,purged_rows,interval
        choices=[{'eligible':False,'score':.5,'kind':'ridge','horizon_bars':24},
                 {'eligible':True,'score':-.1,'kind':'tree','horizon_bars':72}]
        self.assertIsNone(select_inner(choices))
        choices.append({'eligible':True,'score':.1,'kind':'regime','horizon_bars':168})
        self.assertEqual(select_inner(choices)['kind'],'regime')
        rows=[{'decision_ms':i,'label_end_ms':i+5} for i in range(10)]
        self.assertEqual([r['decision_ms'] for r in purged_rows(rows,2,10)],[2,3,4])
        self.assertEqual(interval([[0.]*30]*6,100)['lower'],0)
        self.assertEqual(interval([[.01]*30]*6,100)['lower'],.01)

    def test_daily_decisions_are_causal_and_explicit(self):
        start=dt.datetime(2024,1,1,tzinfo=dt.timezone.utc)
        series=[replace(b,open_time=start+dt.timedelta(hours=i),interval_seconds=3600) for i,b in enumerate(bars(400))]
        rows=samples(series,24,3600,24)
        self.assertTrue(rows)
        self.assertTrue(all(row['decision_ms']%(24*3600000)==0 for row in rows))
        self.assertTrue(all(row['label_end_ms']-row['decision_ms']==24*3600000 for row in rows))

    def funding_fixture(self):
        from trading.app.funding_expectancy import EIGHT_HOURS,LAG
        return {'schema':'public-funding-development/v1','symbol':'BTCUSDT',
            'provider':'Binance USD-M perpetual public API','credentials_used':False,
            'orders_executed':False,'spot_candle_replacement':False,'availability_lag_ms':LAG,
            'requested_start_ms':0,'requested_end_ms':30*EIGHT_HOURS,
            'events':[{'symbol':'BTCUSDT','fundingTime':i*EIGHT_HOURS,'fundingRate':str(i/10000)} for i in range(30)]}

    def test_funding_features_lag_settlement_and_ignore_future_records(self):
        from trading.app.funding_expectancy import funding_features,EIGHT_HOURS,LAG
        events=self.funding_fixture()['events']
        before=funding_features(events,21*EIGHT_HOURS)
        self.assertEqual(before[0],float(events[20]['fundingRate']))
        after=funding_features(events,21*EIGHT_HOURS+LAG)
        self.assertEqual(after[0],float(events[21]['fundingRate']))
        events[-1]['fundingRate']='.5'
        self.assertEqual(before,funding_features(events,21*EIGHT_HOURS))
        self.assertIsNone(funding_features(events,1000))

    def test_funding_integrity_rejects_duplicates_gaps_and_wrong_contract(self):
        from trading.app.funding_expectancy import validate_funding
        original=self.funding_fixture();self.assertEqual(validate_funding(original)['events'],30)
        for mode in ('gap','duplicate','wrong','future','nonfinite'):
            data=copy.deepcopy(original)
            if mode=='gap':data['events'].pop(10)
            if mode=='duplicate':data['events'].insert(10,data['events'][10])
            if mode=='wrong':data['events'][5]['symbol']='ETHUSDT'
            if mode=='future':data['requested_end_ms']=1800000000000
            if mode=='nonfinite':data['events'][5]['fundingRate']='NaN'
            with self.subTest(mode=mode),self.assertRaises(ValueError):validate_funding(data)

    def test_funding_does_not_fill_stale_or_missing_observations(self):
        from trading.app.funding_expectancy import funding_features,EIGHT_HOURS,LAG
        events=self.funding_fixture()['events']
        self.assertIsNone(funding_features(events,events[-1]['fundingTime']+EIGHT_HOURS+LAG+60001))
        events.pop(10)
        self.assertIsNone(funding_features(events,22*EIGHT_HOURS))

    def test_boosted_net_return_learning_and_persistence(self):
        rows=[{'features':[float(i%2)]+[0.]*17,'net_return_target':.02 if i%2 else -.01,
               'label_end_ms':i} for i in range(120)]
        model=fit_model(rows,'boosted',3600)
        self.assertEqual(model,fit_model(rows,'boosted',3600))
        low=predict(model,rows[0]['features'],3600)
        high=predict(json.loads(json.dumps(model)),rows[1]['features'],3600)
        self.assertGreater(high,low)
        self.assertEqual(model['boosting_rounds'],12)

    def test_inner_entry_and_sizing_controls_keep_existing_risk_caps(self):
        rows=[{'features':[i/100.]*18,'net_return_target':i/1000.,'label_end_ms':i} for i in range(100)]
        model=fit_model(rows,'ridge')
        series=bars(340);when=int(series[299].close_time.timestamp()*1000)
        buffered=HistoricalBacktester(ExpectancyStrategy(model,12,{when:.0001})).run(series)
        self.assertEqual(buffered.metrics['trade_count'],0)
        opened=[]
        for full in (False,True):
            configured=copy.deepcopy(model);configured.pop('model_sha256')
            configured.update(research_entry_floor=0.,research_full_risk=full)
            configured['model_sha256']=digest(configured)
            result=HistoricalBacktester(ExpectancyStrategy(configured,12,{when:.0001})).run(series)
            self.assertEqual(result.metrics['trade_count'],1)
            trade=result.report['trades'][0]
            self.assertLessEqual(Decimal(trade['quantity'])*Decimal(trade['entry_price']),Decimal('1000'))

    def test_independent_ledger_audit_and_protected_registry_paths(self):
        from trading.app.expectancy_registry import verify_ledger,checked_path
        from trading.app.net_expectancy import daily_returns,COSTS
        rows=[{'features':[i/100.]*18,'net_return_target':i/1000.,'label_end_ms':i} for i in range(100)]
        model=fit_model(rows,'ridge');series=bars(340);when=int(series[299].close_time.timestamp()*1000)
        report=dict(HistoricalBacktester(ExpectancyStrategy(model,12,{when:.1})).run(series).report)
        daily=daily_returns(report.pop('equity_curve'))
        entry={'fold':{'validation_start_ms':int(series[0].open_time.timestamp()*1000),
                       'validation_end_ms':int(series[-1].close_time.timestamp()*1000)},
               'scenarios':{'base':{'report':report,'daily_returns':daily}}}
        self.assertEqual(verify_ledger(entry,{'costs':COSTS})['trade_scenario_records'],1)
        report['trades'][0]['exit_fee']='100'
        with self.assertRaises(ValueError):verify_ledger(entry,{'costs':COSTS})
        for path in ('artifacts/confirmation.json','artifacts/segment-7.json'):
            with self.assertRaises(ValueError):checked_path(path)

    def flow_fixture(self,n=200):
        from trading.app.flow_expectancy import HOUR
        return {'schema':'public-flow-development/v1','provider':'Binance public spot API','symbol':'BTCUSDT',
            'interval_ms':HOUR,'publication_lag_ms':HOUR,'credentials_used':False,'orders_executed':False,
            'spot_candle_replacement':False,'requested_start_ms':0,'requested_end_ms':n*HOUR,
            'rows':[[i*HOUR,'100','101','99','100','10',(i+1)*HOUR-1,'1000',100,'6','600','0'] for i in range(n)]}

    def test_flow_publication_lag_and_future_independence(self):
        from trading.app.flow_expectancy import flow_features,HOUR,validate_flow
        data=self.flow_fixture();validate_flow(data)
        before=flow_features(data['rows'],170*HOUR)
        self.assertAlmostEqual(before[0],.2)
        data['rows'][169][9]='9'
        self.assertEqual(before,flow_features(data['rows'],170*HOUR))
        self.assertNotEqual(before,flow_features(data['rows'],171*HOUR))
        self.assertIsNone(flow_features(data['rows'],168*HOUR))

    def test_flow_integrity_derives_explicit_complete_parts_without_filling(self):
        from trading.app.flow_expectancy import validate_flow,derive_runs,flow_features,HOUR
        data=self.flow_fixture(400);data['rows'].pop(200)
        with self.assertRaises(ValueError):validate_flow(data)
        before=copy.deepcopy(data);parts=derive_runs(data,'a'*64)
        self.assertEqual(data,before);self.assertEqual(len(parts),2)
        self.assertEqual(sum(len(p['rows']) for p in parts),399)
        self.assertTrue(all(validate_flow(p)['passed'] for p in parts))
        self.assertFalse(parts[0]['derivation']['parent_integrity']['passed'])
        self.assertIsNone(flow_features(data['rows'],300*HOUR))

    def test_flow_rejects_invalid_aggressor_volume_and_native_time_units(self):
        from trading.app.flow_expectancy import validate_flow
        for column,value in [(9,'11'),(10,'1001'),(6,3600000),(8,-1),(4,'NaN')]:
            data=self.flow_fixture();data['rows'][0][column]=value
            with self.subTest(column=column),self.assertRaises(ValueError):validate_flow(data)

    def test_partial_native_hours_quarantined_without_altering_original_or_weakening_integrity(self):
        from trading.app.flow_expectancy import quarantine_partial_hours,validate_flow,HOUR
        original=self.flow_fixture(400);original['rows'][200][6]=original['rows'][200][0]+HOUR//2-1
        before=copy.deepcopy(original)
        with self.assertRaises(ValueError):validate_flow(original)
        clean,quarantine=quarantine_partial_hours(original,'b'*64)
        self.assertEqual(original,before);self.assertEqual(quarantine,[original['rows'][200]])
        self.assertEqual(len(clean['rows']),399);self.assertFalse(clean['integrity']['passed'])
        with self.assertRaises(ValueError):validate_flow(clean)
        clean['rows'][0][9]='999'
        with self.assertRaises(ValueError):quarantine_partial_hours(clean,'b'*64)

    def test_dashboard_exposes_hash_verified_research_without_approval(self):
        import tempfile
        from pathlib import Path
        from trading.app.strategy_research import load_dashboard_summary
        report={'schema':'net-expectancy-experiment-registry/v1','actual_orders_executed':False,
            'protected_observations_accessed':False,'model_promoted':False,'total_saved_model_artifacts':1,
            'total_outer_fold_evaluations':1,'experiments':[{'id':'synthetic-test','comparisons':{
                'model':{'development_qualified':False,'summary':{'base':{'mean_return':-.1}},'checks':{'net':False}}}}]}
        report['registry_sha256']=digest(report)
        with tempfile.TemporaryDirectory() as folder:
            path=Path(folder)/'report.json';path.write_text(json.dumps(report))
            summary=load_dashboard_summary(path)
            self.assertIsNone(summary['approved_strategy']);self.assertIn('synthetic-test',summary['experiments'])
            report['model_promoted']=True;report.pop('registry_sha256');report['registry_sha256']=digest(report)
            path.write_text(json.dumps(report))
            with self.assertRaises(ValueError):load_dashboard_summary(path)

    def test_eth_market_reuses_existing_usdt_risk_and_fill_accounting(self):
        from trading.app.backtest import BacktestConfig
        series=[replace(bar,symbol='ETH/USDT',volume=Quantity('10','ETH')) for bar in bars(340)]
        rows=[{'features':[i/100.]*18,'net_return_target':i/1000.,'label_end_ms':i} for i in range(100)]
        model=fit_model(rows,'ridge');model.pop('model_sha256');model['symbol']='ETH/USDT';model['model_sha256']=digest(model)
        when=int(series[299].close_time.timestamp()*1000)
        strategy=ExpectancyStrategy(model,12,{when:.1})
        result=HistoricalBacktester(strategy,config=BacktestConfig(symbol='ETH/USDT',asset='ETH')).run(series)
        self.assertEqual(result.metrics['trade_count'],1);self.assertEqual(strategy.symbol,'ETH/USDT')
        trade=result.report['trades'][0]
        self.assertLessEqual(Decimal(trade['quantity'])*Decimal(trade['entry_price']),Decimal('1000'))
