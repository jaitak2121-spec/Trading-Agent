"""Startup gates only; temporary folders, no exchange or real account."""
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from trading.adapters.http.dashboard import wait_for_paper_readiness


class StartupGateTest(unittest.TestCase):
    def test_owned_process_and_genuine_origin_required(self):
        for value in ({'ready': True, 'pid': 999, 'data_origin': {'kind': 'real_market_paper'}},
                      {'ready': True, 'pid': 123, 'data_origin': {'kind': 'synthetic_demo'}},
                      {'ready': False, 'pid': 123, 'data_origin': {'kind': 'real_market_paper'}},
                      []):
            with self.subTest(value=value), patch('os.kill'), \
                 patch('trading.adapters.http.dashboard.time.monotonic', side_effect=[0, 0, 0, 0, 2]), \
                 patch('trading.adapters.http.dashboard.time.sleep'), \
                 patch('urllib.request.build_opener') as opener:
                opener.return_value.open.return_value.__enter__.return_value = io.StringIO(json.dumps(value))
                with self.assertRaises(TimeoutError):
                    wait_for_paper_readiness(123, timeout_seconds=1)

    def test_exited_child_never_probes_http(self):
        with patch('os.kill', side_effect=ProcessLookupError), \
             patch('urllib.request.build_opener') as opener:
            with self.assertRaises(ProcessLookupError):
                wait_for_paper_readiness(123)
            opener.return_value.open.assert_not_called()

    def test_temporary_unavailability_is_retried_until_owned_app_is_ready(self):
        response = MagicMock()
        response.__enter__.return_value = io.StringIO(json.dumps(
            {'ready': True, 'pid': 123, 'data_origin': {'kind': 'real_market_paper'}}))
        with patch('os.kill'), patch('trading.adapters.http.dashboard.time.sleep'), \
             patch('urllib.request.build_opener') as opener:
            opener.return_value.open.side_effect = [OSError('starting'), response]
            wait_for_paper_readiness(123, timeout_seconds=1)
            self.assertEqual(opener.return_value.open.call_count, 2)

    @unittest.skipUnless(shutil.which('zsh'), 'Mac launcher requires zsh')
    def test_existing_database_cannot_start_learning_without_successful_probe(self):
        source = Path(__file__).resolve().parents[1]/'scripts/start-paper.command'
        for ready in (False, True):
            with self.subTest(ready=ready), tempfile.TemporaryDirectory() as folder:
                root = Path(folder)
                (root/'scripts').mkdir()
                launcher = root/'scripts/start-paper.command'
                shutil.copyfile(source, launcher)
                (root/'.venv/bin').mkdir(parents=True)
                (root/'artifacts').mkdir()
                database = root/'artifacts/real-paper-btc-usdt-v032.sqlite3'
                database.write_bytes(b'untouched fixture')
                python = root/'.venv/bin/python'
                python.write_text(f'#!{Path(sys.executable).resolve()}\n'+'''import os,sys,time
from pathlib import Path
args=sys.argv[1:]
if args==['-']:
    sys.stdin.read()
elif args[:2]==['-m','trading.app']:
    time.sleep(.5)
elif args[:2]==['-m','trading.app.research_jobs']:
    Path('learning-started').touch()
elif args[0]=='-c':
    assert 'wait_for_paper_readiness' in args[1]
    os.kill(int(args[2]),0)
    Path('readiness-probed').touch()
    sys.exit(0 if os.environ['FIXTURE_READY']=='yes' else 1)
else:
    sys.exit(99)
''')
                python.chmod(0o755)
                result = subprocess.run([shutil.which('zsh'), str(launcher)], cwd=root,
                    env={**os.environ, 'FIXTURE_READY': 'yes' if ready else 'no'},
                    capture_output=True, text=True, timeout=10)
                self.assertEqual(result.returncode, 0 if ready else 1, result.stderr)
                self.assertTrue((root/'readiness-probed').exists())
                self.assertEqual((root/'learning-started').exists(), ready)
                self.assertEqual(database.read_bytes(), b'untouched fixture')


if __name__ == '__main__':
    unittest.main()
