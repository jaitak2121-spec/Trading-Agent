"""Synthetic book fixtures only; never evidence of actual liquidity."""
import copy
from decimal import Decimal
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from trading.adapters.coinswitch.spot_stream import CoinSwitchSpotStream
from trading.app.depth_costs import snapshot_costs, analyze_depth, assess_capture_quality, main
from trading.app.execution_costs import canonical_hash, capture_public_spreads


def book():
    return {'symbol':'BTC/USDT','exchange':'c2c1','timestamp':10000,'received_at_ms':10001,
            'bid':'99','ask':'101','bids':[['99','1'],['98','2']],
            'asks':[['101','1'],['102','2']]}


def evidence(rows):
    return {'schema':'public-depth-capture/v1','completed':True,'symbol':'BTC/USDT',
            'exchange':'c2c1','quotes':rows,'credentials_used':False,'orders_executed':False,
            'observation':{'passed':True,'failures':[]}}


class DepthCostTests(unittest.TestCase):
    def test_explicit_observation_boundaries_exclude_cleanup_without_loosening_limits(self):
        capture=self.continuous_capture()
        capture['finished_at']='1970-01-01T00:01:05+00:00'
        capture['observation'].update(observation_started_at_ns=0,
                                     observation_finished_at_ns=60000000000)
        quality=assess_capture_quality(capture)
        self.assertTrue(quality['eligible'],quality['failures'])
        self.assertEqual(quality['timing']['wall_span_seconds'],60)
        self.assertEqual(quality['timing']['capture_wall_span_seconds'],65)
        self.assertEqual(quality['policy']['maximum_clock_divergence_seconds'],2)
        self.assertEqual(quality['policy']['maximum_receive_gap_ms'],5000)
        capture['observation']['observation_finished_at_ns']=64000000000
        self.assertFalse(assess_capture_quality(capture)['eligible'])

    def test_invalid_or_partial_explicit_boundaries_cannot_fall_back_to_legacy(self):
        for start,finish in [(None,60000000000),(0,None),(-1,60000000000),
                             (1000000000,0),(0,61000000000),(False,60000000000)]:
            capture=self.continuous_capture()
            capture['observation'].update(observation_started_at_ns=start,
                                         observation_finished_at_ns=finish)
            with self.subTest(start=start,finish=finish):
                self.assertFalse(assess_capture_quality(capture)['eligible'])

    def test_receive_after_observation_but_before_cleanup_is_invalid(self):
        capture=self.continuous_capture();capture['finished_at']='1970-01-01T00:01:05+00:00'
        capture['observation'].update(observation_started_at_ns=0,
                                     observation_finished_at_ns=60000000000)
        capture['quotes'][-1]['received_at_ms']=62000
        self.assertIn('receive timestamps outside capture boundaries',assess_capture_quality(capture)['failures'])

    def continuous_capture(self):
        rows=[{**book(),'timestamp':second*1000,'received_at_ms':second*1000}
              for second in range(1,61)]
        capture=evidence(rows)
        capture.update(started_at='1970-01-01T00:00:00+00:00',
                       finished_at='1970-01-01T00:01:00+00:00',duration_requested_seconds=60)
        capture['observation'].update(symbol='BTC/USDT',exchange='c2c1',
            duration_elapsed_seconds=60.,duration_requested_seconds=60.,
            feed_error_polls=0,reconnect_attempts=0,quote_messages=len(rows))
        return capture

    def test_continuous_capture_eligibility_never_implies_live_qualification(self):
        report=analyze_depth(self.continuous_capture())
        self.assertTrue(report['research_observation_quality']['eligible'])
        self.assertFalse(report['live_cost_qualification'])
        self.assertEqual(report['research_observation_quality']['timing']['maximum_receive_gap_ms'],1000)

    def test_wall_clock_divergence_is_not_hidden_by_monotonic_duration(self):
        capture=self.continuous_capture();capture['finished_at']='1970-01-01T00:10:00+00:00'
        quality=assess_capture_quality(capture)
        self.assertFalse(quality['eligible'])
        self.assertIn('wall and monotonic clocks diverged',quality['failures'])
        self.assertIn('receive gap exceeds fixed research limit',quality['failures'])

    def test_receive_gaps_startup_tail_and_reversed_times_fail(self):
        for mode in ('startup','tail','middle','reverse','outside','source-reverse'):
            capture=self.continuous_capture()
            if mode=='startup':capture['quotes']=capture['quotes'][6:]
            if mode=='tail':capture['quotes']=capture['quotes'][:-6]
            if mode=='middle':capture['quotes']=capture['quotes'][:10]+capture['quotes'][16:]
            if mode=='reverse':capture['quotes']=list(reversed(capture['quotes']))
            if mode=='outside':capture['quotes'][-1]['received_at_ms']=61000
            if mode=='source-reverse':capture['quotes'][-1]['timestamp']=1000
            capture['observation']['quote_messages']=len(capture['quotes'])
            with self.subTest(mode=mode):self.assertFalse(assess_capture_quality(capture)['eligible'])

    def test_invalid_or_missing_timing_feed_status_and_identity_fail_closed(self):
        for key,value in [('feed_error_polls',1),('reconnect_attempts',1),('quote_messages',0),
                          ('duration_elapsed_seconds',float('nan')),('symbol','ETH/USDT')]:
            capture=self.continuous_capture();capture['observation'][key]=value
            with self.subTest(key=key):self.assertFalse(assess_capture_quality(capture)['eligible'])
        for key,value in [('started_at','1970-01-01T00:00:00'),('finished_at','bad'),
                          ('duration_requested_seconds',True)]:
            capture=self.continuous_capture();capture[key]=value
            with self.subTest(key=key):self.assertFalse(assess_capture_quality(capture)['eligible'])
        self.assertFalse(assess_capture_quality(evidence([book()]))['eligible'])

    def test_offline_assessment_hash_and_protected_symlink_guards(self):
        import hashlib
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);source=root/'public.json';output=root/'report.json'
            source.write_text(json.dumps(self.continuous_capture()))
            digest=hashlib.sha256(source.read_bytes()).hexdigest()
            with patch('trading.app.depth_costs.capture_public_spreads') as capture:
                self.assertEqual(main([str(output),'--input',str(source),'--sha256',digest]),0)
                capture.assert_not_called()
            result=json.loads(output.read_text())
            self.assertEqual(result['source_file_sha256'],digest)
            self.assertTrue(result['research_observation_quality']['eligible'])
            with self.assertRaises(SystemExit):main([str(root/'bad.json'),'--input',str(source),'--sha256','0'*64])
            self.assertFalse((root/'bad.json').exists())
            secret=root/'segment-7.json';secret.write_text('must not be read')
            alias=root/'alias.json';alias.symlink_to(secret)
            with patch.object(Path,'read_bytes') as read:
                with self.assertRaises(SystemExit):main([str(root/'bad.json'),'--input',str(alias),'--sha256',digest])
                read.assert_not_called()

    def sweep(self, row=None, size='200', fee='10'):
        return snapshot_costs(row or book(), symbol='BTC/USDT', exchange='c2c1',
                              notionals=(size,), fee_bps=fee)

    def test_exact_multi_level_vwap_and_directional_fee_units(self):
        buy, sell = self.sweep()
        self.assertEqual(Decimal(buy['vwap']), Decimal('101.5'))
        self.assertEqual(Decimal(sell['vwap']), Decimal('98.5'))
        self.assertEqual(Decimal(buy['cost_from_mid_including_fee_bps']), Decimal('160.15'))
        self.assertEqual(Decimal(sell['cost_from_mid_including_fee_bps']), Decimal('159.85'))

    def test_partial_visible_liquidity_never_assumes_full_fill(self):
        for row in self.sweep(size='400'):
            self.assertFalse(row['sufficient_visible_depth'])
            self.assertEqual(Decimal(row['visible_base_filled']),3)
            self.assertIsNone(row['vwap'])
            self.assertIsNone(row['cost_from_mid_including_fee_bps'])
        self.assertTrue(self.sweep(size='300')[0]['sufficient_visible_depth'])

    def test_untrusted_book_invariants_fail_closed(self):
        for key, value in [('exchange','c2c2'),('symbol','ETH/USDT'),('bid','100'),
            ('timestamp',True),('timestamp',20001),('asks',[]),
            ('asks',[['101','0']]),('asks',[['101','NaN']]),('asks',[[101.0,'1']]),
            ('asks',[['101','1'],['101','2']]),('asks',[['101','1'],['100','1']]),
            ('bids',[['99','1'],['100','1']]),('asks',[['101','1','extra']])]:
            with self.subTest(key=key,value=value), self.assertRaises((ValueError,TypeError)):
                self.sweep({**book(),key:value})
        for size, fee in [('0','10'),('100','-1'),('100','10000')]:
            with self.assertRaises(ValueError):self.sweep(size=size,fee=fee)

    def test_reproducible_dedup_stale_and_hash_no_mutation(self):
        original=evidence([book(),book(),{**book(),'timestamp':0}])
        before=copy.deepcopy(original)
        report=analyze_depth(original,notionals=('100','400'))
        self.assertEqual(original,before)
        self.assertEqual(report,analyze_depth(original,notionals=('100','400')))
        self.assertEqual(report['fresh_unique_snapshots'],1)
        self.assertEqual(report['duplicates_excluded'],1)
        self.assertEqual(report['stale_excluded'],1)
        self.assertEqual(report['sweeps']['buy-400']['insufficient_depth'],1)
        self.assertFalse(report['live_cost_qualification'])
        self.assertEqual(report['report_sha256'],canonical_hash({k:v for k,v in report.items() if k!='report_sha256'}))

    def test_changed_deeper_levels_are_not_top_quote_duplicates(self):
        second=book();second['asks'][1][1]='3'
        self.assertEqual(analyze_depth(evidence([book(),second]))['fresh_unique_snapshots'],2)

    def test_incomplete_or_private_capture_rejected(self):
        for key,value in [('completed',False),('credentials_used',True),('orders_executed',True),('schema','other')]:
            with self.assertRaises(ValueError):analyze_depth({**evidence([book()]),key:value})

    def test_failed_observation_is_explicit_in_diagnostic(self):
        capture=evidence([book()])
        capture['observation']={'passed':False,'failures':['no closed candle']}
        report=analyze_depth(capture)
        self.assertFalse(report['capture_observation_passed'])
        self.assertEqual(report['capture_observation_failures'],['no closed candle'])
        self.assertFalse(report['live_cost_qualification'])
        capture.pop('observation')
        with self.assertRaises(ValueError):analyze_depth(capture)

    def test_stream_depth_is_opt_in_and_preserves_default(self):
        payload={'s':'BTC,USDT','timestamp':10000,'bids':book()['bids'],'asks':book()['asks']}
        normal=CoinSwitchSpotStream(symbol='BTC/USDT',exchange='c2c1')._decode_book(payload)
        self.assertNotIn('bids',normal)
        depth=CoinSwitchSpotStream(symbol='BTC/USDT',exchange='c2c1',include_depth=True)._decode_book(payload)
        self.assertEqual(depth['bids'],payload['bids']);self.assertEqual(depth['exchange'],'c2c1')
        with self.assertRaises(ValueError):CoinSwitchSpotStream(symbol='BTC/USDT',exchange='c2c1',include_depth='yes')

    def test_capture_reuses_observer_and_preserves_depth(self):
        class Report:
            def as_details(self):return {'passed':True}
        def observe(stream,**kwargs):
            self.assertTrue(stream._include_depth)
            kwargs['quote_sink'](book())
            return Report()
        with tempfile.TemporaryDirectory() as folder:
            path=Path(folder)/'capture.json'
            with patch('trading.app.coinswitch_observation.run_coinswitch_observation',side_effect=observe):
                result=capture_public_spreads(path,seconds=60,include_depth=True)
            self.assertEqual(result['schema'],'public-depth-capture/v1')
            self.assertEqual(json.loads(path.read_text())['quotes'][0]['asks'],book()['asks'])
            with self.assertRaises(FileExistsError):capture_public_spreads(path,include_depth=True)

    def test_cli_refuses_report_collision_before_network(self):
        with tempfile.TemporaryDirectory() as folder:
            path=Path(folder)/'capture.json'
            path.with_suffix('.costs.json').write_text('{}')
            with patch('trading.app.depth_costs.capture_public_spreads') as capture:
                with self.assertRaises(SystemExit):main([str(path)])
                capture.assert_not_called()
