import json
import hashlib
import tempfile
import unittest
import subprocess
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch, MagicMock
from trading.app.research_jobs import main, checked_config, _load_bound, run_collection_job
from trading.adapters.process import run_bounded_research_job


class ResearchJobsTest(unittest.TestCase):
    def test_real_bounded_monitor_worker_runs_without_training_or_orders(self):
        from trading.adapters.persistence import SQLiteState
        from trading.app.model_deployment import model_identity
        from tests.test_v2_paper_strategy import fixture
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder)
            state=SQLiteState(root/'audit.db')
            state.close()
            model,_=fixture()
            reference=root/'reference-model.json'
            reference.write_text(json.dumps(model))
            configuration={'enabled':True,'operation':'model_monitor','interval_seconds':60,
                'timeout_seconds':15,'cpu_seconds':5,'memory_mb':1024,
                'state_db':str(root/'audit.db'),'model_sha256':model_identity(model),
                'reference_model':{'path':str(reference),
                    'file_sha256':hashlib.sha256(reference.read_bytes()).hexdigest()},
                'output_directory':str(root/'reports'),
                'policy':{'window':100,'minimum_samples':100,'mean_z_limit':1.0,
                          'tail_fraction_limit':0.1,'max_brier_excess':0.02}}
            path=root/'job.json'
            path.write_text(json.dumps(configuration))
            try:
                resources=run_bounded_research_job(path,timeout_seconds=15)
            except PermissionError as exc:
                self.skipTest(str(exc))
            self.assertEqual(resources['memory_limit_mb'],1024)
            reports=list((root/'reports').glob('monitor-*.json'))
            self.assertEqual(len(reports),1)
            report=json.loads(reports[0].read_text())
            self.assertEqual(report['predictions'],0)
            self.assertFalse(report['model_changed'])
            self.assertFalse(report['promotion_performed'])

    def test_collection_incrementally_preserves_rows_and_deduplicates(self):
        with tempfile.TemporaryDirectory() as folder:
            def observe(stream, **options):
                row = {'open_time':0, 'close':'100', 'source':'test fixture'}
                options['candle_sink'](row)
                options['candle_sink'](dict(row))
                self.assertTrue(list(Path(folder).glob('*-candles.json')))
                return SimpleNamespace(passed=True, as_details=lambda:{'passed':True})
            with patch('trading.adapters.coinswitch.CoinSwitchSpotStream',
                       return_value=SimpleNamespace(symbol='BTC/USDT',exchange='c2c1')):
                with patch('trading.app.coinswitch_observation.run_coinswitch_observation', side_effect=observe):
                    run_collection_job({'symbol':'BTC/USDT', 'exchange':'c2c1',
                                        'observation_seconds':60, 'output_directory':folder})
            dataset = json.loads(next(Path(folder).glob('*-candles.json')).read_text())
            self.assertEqual(len(dataset['candles']), 1)
            self.assertFalse(dataset['credentials_used'])

    def test_disabled_job_never_fits_or_starts_worker(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder)/'job.json'
            path.write_text(json.dumps({'enabled':False}))
            with patch('trading.app.research_jobs.run_bounded_research_job') as worker:
                self.assertEqual(main([str(path), '--once']), 0)
                worker.assert_not_called()

    def test_resource_settings_refuse_unbounded_jobs(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder)/'job.json'
            path.write_text(json.dumps({'enabled':True, 'timeout_seconds':0}))
            with self.assertRaises(ValueError):
                checked_config(path)

    def test_worker_has_fixed_command_and_does_not_inherit_credentials(self):
        process=MagicMock()
        process.poll.side_effect=[None,0,0]
        process.returncode=0
        process.pid=123
        with patch.dict('os.environ', {'TRADING_API_SECRET':'test-secret'}):
            with patch('trading.adapters.process.subprocess.Popen') as launch, patch('trading.adapters.process._resident_bytes',return_value=1024):
                launch.return_value.__enter__.return_value=process
                run_bounded_research_job('job.json', timeout_seconds=10)
                args, kwargs = launch.call_args
                self.assertEqual(args[0][-3:], ['trading.app.research_jobs','job.json','--worker'])
                self.assertNotIn('TRADING_API_SECRET', kwargs['env'])
                self.assertNotIn('shell', kwargs)
                process.kill.assert_not_called()

    def test_timeout_is_visible(self):
        process=MagicMock()
        process.poll.return_value=None
        with patch('trading.adapters.process.subprocess.Popen') as launch, patch('trading.adapters.process.time.monotonic',side_effect=[0,2]):
            launch.return_value.__enter__.return_value=process
            with self.assertRaises(TimeoutError):
                run_bounded_research_job('job.json', timeout_seconds=1)
            process.kill.assert_called_once()

    def test_memory_excess_kills_worker(self):
        process=MagicMock()
        process.poll.return_value=None
        with patch('trading.adapters.process.subprocess.Popen') as launch, patch('trading.adapters.process._resident_bytes',return_value=129*1024*1024):
            launch.return_value.__enter__.return_value=process
            with self.assertRaisesRegex(RuntimeError,'memory budget'):
                run_bounded_research_job('job.json',timeout_seconds=10,memory_mb=128)
            process.kill.assert_called_once()

    def test_shutdown_does_not_launch_new_worker(self):
        with patch('trading.adapters.process.subprocess.Popen') as launch:
            with self.assertRaises(InterruptedError):
                run_bounded_research_job('job.json', timeout_seconds=10,
                                         cancelled=lambda: True)
            launch.assert_not_called()

    def test_shutdown_kills_and_reaps_inflight_worker(self):
        process = MagicMock()
        process.poll.return_value = None
        with patch('trading.adapters.process.subprocess.Popen') as launch:
            launch.return_value.__enter__.return_value = process
            with self.assertRaises(InterruptedError):
                run_bounded_research_job('job.json', timeout_seconds=10,
                                         cancelled=MagicMock(side_effect=[False, True]))
            process.kill.assert_called_once()
            process.wait.assert_called_once()

    def test_scheduler_records_interruption_as_stopped_not_failed(self):
        with tempfile.TemporaryDirectory() as folder:
            configuration = {'enabled': True, 'operation': 'experience_retraining',
                'interval_seconds': 3600, 'timeout_seconds': 600, 'memory_mb': 1024,
                'output_directory': folder}
            with patch('trading.app.research_jobs.checked_config', return_value=configuration), \
                 patch('trading.app.research_jobs.run_bounded_research_job',
                       side_effect=InterruptedError('shutdown')) as worker:
                self.assertEqual(main([str(Path(folder)/'job.json')]), 0)
            self.assertTrue(callable(worker.call_args.kwargs['cancelled']))
            status = json.loads((Path(folder)/'scheduler-status.json').read_text())
            self.assertEqual(status['phase'], 'stopped')
            self.assertFalse(status['deployment_enabled'])

    def test_sealed_name_refused_before_read(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder)/'sealed-confirmation.json'
            path.write_text('test fixture; not market observations')
            with patch.object(Path, 'read_bytes') as read:
                with self.assertRaisesRegex(ValueError, 'protected'):
                    _load_bound({'development':{'path':str(path),'file_sha256':'a'*64}}, 'development')
                read.assert_not_called()
