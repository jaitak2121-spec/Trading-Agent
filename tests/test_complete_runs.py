"""A complete derivative never changes or launders an incomplete parent."""
import copy
import json
import unittest
from trading.app.complete_runs import derive_complete_runs
from trading.app.dataset_eligibility import assess_dataset
from trading.app.historical_candles import download_historical_candles
from tests.test_historical_candles import candle


class CompleteRunTests(unittest.TestCase):
    def test_native_plan_json_roundtrip_is_stable(self):
        from trading.app.native_research import specification
        plan=specification([])
        self.assertEqual(plan,json.loads(json.dumps(plan)))

    def setUp(self):
        self.parent,_=download_historical_candles(fetch=lambda p:json.dumps({'data':[candle(i*60000) for i in (0,1,3,4,5)]}).encode(),
            exchange='c2c1',symbol='BTC/USDT',interval_minutes=1,start_time_ms=0,end_time_ms=360000,acquired_at_ms=400000)

    def test_complete_ranges_independently_pass_parent_keeps_failure(self):
        before=copy.deepcopy(self.parent)
        derived=derive_complete_runs(self.parent,'a'*64,minimum_candles=2)
        self.assertEqual([len(d['candles']) for d in derived],[2,3])
        self.assertFalse(assess_dataset(self.parent)['eligible_for_strategy_qualification'])
        self.assertTrue(all(assess_dataset(d)['eligible_for_strategy_qualification'] for d in derived))
        self.assertTrue(all(not d['development_derivation']['parent_integrity_passed'] for d in derived))
        self.assertEqual(before,self.parent)
        self.assertEqual(derived,derive_complete_runs(self.parent,'a'*64,minimum_candles=2))

    def test_coverage_only_filter_and_no_gap_rows_created(self):
        derived=derive_complete_runs(self.parent,'a'*64,minimum_candles=3)
        self.assertEqual(len(derived),1)
        self.assertEqual(derived[0]['candles'],self.parent['candles'][2:])
        self.assertEqual(derived[0]['source']['requested_start_time'],180000)

    def test_corruption_duplicate_wrong_provenance_or_hash_rejected(self):
        for change in ('duplicate','price','source'):
            data=copy.deepcopy(self.parent)
            if change=='duplicate':data['candles'].append(data['candles'][-1])
            elif change=='price':data['candles'][0]['high']='1'
            else:data['source']['provider']='Other venue'
            with self.assertRaises(ValueError):derive_complete_runs(data,'a'*64,minimum_candles=1)
        with self.assertRaises(ValueError):derive_complete_runs(self.parent,'not-hash',minimum_candles=1)
