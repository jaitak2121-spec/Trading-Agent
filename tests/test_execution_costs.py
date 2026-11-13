"""Fixed-fill cost attribution is not a profitability counterfactual."""
import copy
import datetime as dt
from decimal import Decimal
import json
from pathlib import Path
import tempfile
import unittest

from trading.app.execution_costs import (trade_cost_diagnostic,analyze_research_costs,
    summarize_spreads,canonical_hash,main)


def trade(exit_price='99',net='-3'):
    return {'quantity':'1','entry_price':'100','exit_price':exit_price,'entry_fee':'1',
        'exit_fee':'1','slippage_cost':'.2','net_pnl':net}


class ExecutionCostTests(unittest.TestCase):
    def test_dashboard_supports_native_and_cost_reports_without_approval(self):
        from trading.app.strategy_research import load_dashboard_summary
        native={'schema':'native-five-minute-development-results/v1','production_changed':False,
            'confirmation_accessed':False,'final_test_accessed':False,'plan_sha256':'a'*64,
            'results':{'fixture':{'development_qualified':False,'scenarios':{
                'base':{'folds':[],'mean_return':'-.01','trade_count':10}}}}}
        native['result_sha256']=canonical_hash(native)
        costs={'schema':'frozen-fill-cost-diagnostics/v1','strategy_changed':False,'model_promoted':False,
            'diagnostics':{'fixture':{'base':{'pooled_frozen_fills':{'net_pnl':'-1'}}}},'limitations':['fixed fills only']}
        costs['report_sha256']=canonical_hash(costs)
        with tempfile.TemporaryDirectory() as folder:
            for report in (native,costs):
                path=Path(folder)/'report.json';path.write_text(json.dumps(report))
                summary=load_dashboard_summary(path)
                self.assertIsNone(summary['approved_strategy'])
                self.assertIn('development',summary['scope'])
                report['altered']=True;path.write_text(json.dumps(report))
                with self.assertRaises(ValueError):load_dashboard_summary(path)

    def test_negative_pre_fee_cannot_be_rescued_by_zero_fees_on_fixed_fills(self):
        d=trade_cost_diagnostic([trade()])
        self.assertEqual(d['same_fill_pnl_before_fees'],'-1')
        self.assertTrue(d['loses_at_zero_fees_on_recorded_fills'])
        self.assertIsNone(d['maximum_flat_fee_bps_at_break_even'])
        self.assertIn('not a zero-cost counterfactual',d['interpretation'])

    def test_break_even_and_payoffs_have_exact_units(self):
        d=trade_cost_diagnostic([trade('101','-1')])
        self.assertEqual(Decimal(d['maximum_flat_fee_bps_at_break_even']),Decimal(1)/Decimal(201)*10000)
        zero=trade_cost_diagnostic([trade('100','-2')])
        self.assertEqual(Decimal(zero['maximum_flat_fee_bps_at_break_even']),0)
        self.assertFalse(zero['loses_at_zero_fees_on_recorded_fills'])
        self.assertIsNone(trade_cost_diagnostic([])['observed_win_rate'])

    def test_corrupt_accounting_nonfinite_and_float_inputs_rejected(self):
        for key,value in [('net_pnl','100'),('quantity','0'),('entry_fee','NaN'),('entry_price',100.0)]:
            item=trade();item[key]=value
            with self.assertRaises((ValueError,TypeError)):trade_cost_diagnostic([item])

    def test_public_spread_exact_duplicates_and_stale_samples(self):
        quote={'symbol':'BTC/USDT','timestamp':10000,'received_at_ms':10001,'bid':'99','ask':'101'}
        rows=[quote,dict(quote),{**quote,'timestamp':0}]
        result=summarize_spreads(rows,symbol='BTC/USDT')
        self.assertEqual(result['fresh_unique_samples'],1)
        self.assertEqual(result['duplicates_excluded'],1)
        self.assertEqual(result['stale_samples_excluded'],1)
        self.assertEqual(Decimal(result['spread_bps']['median']),200)
        self.assertFalse(result['slippage_or_market_impact_estimated'])
        self.assertFalse(result['live_cost_qualification'])

    def test_mixed_market_crossed_future_or_invalid_timestamp_rejected(self):
        base={'symbol':'BTC/USDT','timestamp':10000,'received_at_ms':10001,'bid':'99','ask':'101'}
        for key,value in [('symbol','ETH/USDT'),('bid','102'),('timestamp',20000),('timestamp',True)]:
            with self.assertRaises(ValueError):summarize_spreads([{**base,key:value}],symbol='BTC/USDT')

    def test_report_hash_fees_and_trade_sums_are_verified(self):
        r={'schema':'native-five-minute-development-results/v1','results':{'mock':{'scenarios':{
            'base':{'folds':[{'fold':{'fold_id':'fixture'},'result':{'trades':[trade()],
                'metrics':{'trade_count':1,'realized_pnl':'-3','fee_total':'2'}}}]}}}}}
        r['result_sha256']=canonical_hash(r)
        before=copy.deepcopy(r)
        result=analyze_research_costs(r)
        self.assertEqual(before,r)
        self.assertFalse(result['strategy_changed'])
        r['results']['mock']['scenarios']['base']['folds'][0]['result']['metrics']['fee_total']='3'
        with self.assertRaises(ValueError):analyze_research_costs(r)
        r['result_sha256']=canonical_hash({k:v for k,v in r.items() if k!='result_sha256'})
        with self.assertRaises(ValueError):analyze_research_costs(r)
