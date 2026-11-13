"""Completeness checks cannot be bypassed by a stored PASS flag."""
import copy
import json
import unittest
from trading.app.dataset_eligibility import assess_dataset,require_qualification_eligibility
from trading.app.historical_candles import download_historical_candles
from tests.test_historical_candles import candle


class DatasetEligibilityTests(unittest.TestCase):
    def setUp(self):
        self.dataset,_=download_historical_candles(fetch=lambda p:json.dumps({'data':[candle(0),candle(60000),candle(120000)]}).encode(),
            exchange='c2c1',symbol='BTC/USDT',interval_minutes=1,start_time_ms=0,end_time_ms=180000,acquired_at_ms=200000)

    def test_complete_dataset_can_supply_integrity_evidence(self):
        evidence=assess_dataset(self.dataset)
        self.assertTrue(evidence['eligible_for_strategy_qualification'])
        require_qualification_eligibility(evidence)

    def test_boundaries_missing_duplicates_and_forged_pass_refused(self):
        for operation in ('gap','tail','duplicates','range','hash','source','stored'):
            d=copy.deepcopy(self.dataset)
            if operation=='gap':del d['candles'][1]
            elif operation=='tail':d['candles'].pop()
            elif operation=='duplicates':d['candles'].append(d['candles'][-1])
            elif operation=='range':d['source']['requested_end_time']+=60000
            elif operation=='hash':d['integrity']['candle_sha256']='0'*64
            elif operation=='source':d['candles'][0]['source']='binance'
            else:d.pop('integrity')
            evidence=assess_dataset(d)
            self.assertFalse(evidence['eligible_for_strategy_qualification'],operation)
            with self.assertRaises(ValueError):require_qualification_eligibility(evidence)

    def test_unknown_provenance_and_bad_structure_fail_closed(self):
        for d in ({}, {'schema':'wrong'}, {**self.dataset,'source':{}}):
            self.assertFalse(assess_dataset(d)['eligible_for_strategy_qualification'])
        with self.assertRaises(ValueError):require_qualification_eligibility({})

    def test_hashes_deterministic_and_no_mutation(self):
        before=copy.deepcopy(self.dataset)
        self.assertEqual(assess_dataset(before),assess_dataset(self.dataset))
        self.assertEqual(before,self.dataset)
