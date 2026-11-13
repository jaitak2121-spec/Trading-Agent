"""Acquisition integrity checks use explicitly mocked, non-market fixtures."""
import copy
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from trading.app.research_acquisition import START,END,check_day,main
from tests.test_historical_candles import candle
from trading.app.historical_candles import download_historical_candles
from trading.app import research_acquisition as acquisition


class ResearchAcquisitionTests(unittest.TestCase):
    def setUp(self):
        self.dataset,_=download_historical_candles(
            fetch=lambda p:json.dumps({'data':[candle(i*60000) for i in range(1440)]}).encode(),
            exchange='c2c1',symbol='BTC/USDT',interval_minutes=1,
            start_time_ms=0,end_time_ms=86400000,acquired_at_ms=86400001)

    def test_exact_day_passes(self):
        self.assertTrue(check_day(self.dataset,0,86400000).passed)

    def test_wrong_boundaries_market_or_interval_rejected(self):
        for key,value in [('symbol','ETH/USDT'),('exchange','other')]:
            dataset=copy.deepcopy(self.dataset)
            dataset[key]=value
            with self.assertRaises(ValueError):check_day(dataset,0,86400000)
        with self.assertRaises(ValueError):check_day(self.dataset,60000,86460000)
        dataset=copy.deepcopy(self.dataset)
        dataset['source']['interval_minutes']=5
        with self.assertRaises(ValueError):check_day(dataset,0,86400000)

    def test_gap_duplicate_and_corruption_rejected(self):
        for change in ('gap','duplicate','price'):
            dataset=copy.deepcopy(self.dataset)
            if change=='gap':del dataset['candles'][50]
            elif change=='duplicate':dataset['candles'].insert(50,dataset['candles'][50])
            else:dataset['candles'][50]['close']='1000'
            with self.assertRaises(ValueError):check_day(dataset,0,86400000)

    def test_fixed_range_precedes_existing_research(self):
        self.assertEqual((END-START).days,180)
        self.assertEqual(END.isoformat(),'2025-11-02T00:00:00+00:00')

    def test_missing_credentials_prevent_transport(self):
        with patch.dict('os.environ',{},clear=True),patch('trading.app.research_acquisition.CoinSwitchCandleTransport') as transport:
            with self.assertRaises(ValueError):main()
            transport.assert_not_called()

    def test_resume_refuses_corrupt_checkpoint_before_network(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder)
            day=START.date().isoformat()+'.json'
            (root/day).write_text('{}')
            (root/'acquisition.json').write_text(json.dumps({'start':START.isoformat(),
                'end_exclusive':END.isoformat(),'requests':[],
                'days':{day:{'file_sha256':'0'*64}}}))
            with patch.object(acquisition,'FOLDER',root),patch.dict('os.environ',
                    {'COINSWITCH_API_KEY':'mock-key','COINSWITCH_API_SECRET':'mock-secret'}),\
                    patch.object(acquisition,'CoinSwitchCandleTransport') as transport:
                with self.assertRaisesRegex(ValueError,'checkpoint hash mismatch'):main()
                transport.return_value.fetch_with_details.assert_not_called()

    def test_resume_invalid_existing_day_is_not_overwritten_or_refetched(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder)
            day=root/(START.date().isoformat()+'.json')
            original=json.dumps(self.dataset)
            day.write_text(original)
            with patch.object(acquisition,'FOLDER',root),patch.dict('os.environ',
                    {'COINSWITCH_API_KEY':'mock-key','COINSWITCH_API_SECRET':'mock-secret'}),\
                    patch.object(acquisition,'CoinSwitchCandleTransport') as transport:
                with self.assertRaisesRegex(ValueError,'boundaries mismatch'):main()
                transport.return_value.fetch_with_details.assert_not_called()
            self.assertEqual(day.read_text(),original)
