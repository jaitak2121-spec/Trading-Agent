"""Release artifact and persisted restart-drill verification."""

from __future__ import annotations

import json
import importlib.util
import os
import subprocess
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

from trading.app import run_restart_drill, verify_release_wheel


class RestartDrillTest(unittest.TestCase):
    def test_package_and_distribution_share_release_candidate_identity(self):
        import tomllib
        import trading
        configuration = tomllib.loads((Path(__file__).resolve().parents[1]/'pyproject.toml').read_text())
        self.assertEqual(trading.__version__, configuration['project']['version'])

    def test_state_cash_audit_and_readiness_survive_rebuild(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            report = run_restart_drill(
                sqlite_path=Path(directory) / "state.sqlite3",
                cycles_per_run=19,
            )

        self.assertTrue(report.passed, report.failures)
        self.assertEqual(report.recovered_cash, report.first.final_cash)
        self.assertGreaterEqual(
            report.recovered_audit_records, report.first.audit_records
        )
        self.assertTrue(report.second.passed)
        json.dumps(report.as_details())


class ReleaseArtifactTest(unittest.TestCase):
    def test_release_drill_selects_current_version_not_newest_unrelated_wheel(self):
        script = Path(__file__).resolve().parents[1]/'scripts/release_drill.py'
        spec = importlib.util.spec_from_file_location('release_selection_fixture', script)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root/'pyproject.toml').write_text('[project]\nversion="0.3.3rc2"\n')
            dist = root/'dist'
            dist.mkdir()
            expected = dist/'safety_first_trading_app-0.3.3rc2-py3-none-any.whl'
            expected.touch()
            unrelated = dist/'safety_first_trading_app-9.9.9-py3-none-any.whl'
            unrelated.touch()
            os.utime(unrelated, (2000000000, 2000000000))
            with patch.object(module, 'ROOT', root), patch.object(module, 'run'), \
                 patch.object(sys, 'argv', ['release_drill.py', '--dist', str(dist)]), \
                 patch.object(module, 'verify_release_wheel',
                              side_effect=RuntimeError('selection inspected')) as verify:
                with self.assertRaisesRegex(RuntimeError, 'selection inspected'):
                    module.main()
                verify.assert_called_once_with(expected.resolve())

    def test_lifecycle_release_requires_learning_and_persistent_candles(self):
        with tempfile.TemporaryDirectory() as folder:
            wheel=Path(folder)/'lifecycle.whl'
            with zipfile.ZipFile(wheel,'w') as archive:
                archive.writestr('x.dist-info/METADATA','Name: safety-first-trading-app\nVersion: 0.3.1\n')
                archive.writestr('x.dist-info/entry_points.txt','trading-paper = trading.app.__main__:main\n')
                archive.writestr('trading/app/__main__.py','')
            failures=verify_release_wheel(wheel).failures
            self.assertTrue(any('experience_learning.py' in f for f in failures))
            self.assertTrue(any('persistence/candles.py' in f for f in failures))

    def test_expectancy_release_cannot_omit_research_executor(self):
        with tempfile.TemporaryDirectory() as folder:
            wheel=Path(folder)/'research.whl'
            with zipfile.ZipFile(wheel,'w') as archive:
                archive.writestr('x.dist-info/METADATA','Name: safety-first-trading-app\nVersion: 0.3.0\n')
                archive.writestr('x.dist-info/entry_points.txt',
                    'trading-paper = trading.app.__main__:main\ntrading-paper-account = trading.app.paper_account:main\n')
                for name in ('__main__','paper_account','cost_research','research_acquisition',
                             'dataset_eligibility','complete_runs','native_research','execution_costs','depth_costs'):
                    archive.writestr(f'trading/app/{name}.py','')
                for name in ('execution_contract','trading_fees'):
                    archive.writestr(f'trading/adapters/coinswitch/{name}.py','')
            self.assertTrue(any('expectancy research module' in f for f in verify_release_wheel(wheel).failures))
            with zipfile.ZipFile(wheel,'a') as archive:
                for name in ('net_expectancy','hourly_expectancy','cross_market_expectancy','adaptive_expectancy',
                             'funding_expectancy','decision_expectancy','flow_expectancy','eth_expectancy','expectancy_registry'):
                    archive.writestr(f'trading/app/{name}.py','')
            self.assertTrue(verify_release_wheel(wheel).passed)

    def test_depth_release_requires_depth_module(self):
        with tempfile.TemporaryDirectory() as folder:
            wheel=Path(folder)/'depth.whl'
            with zipfile.ZipFile(wheel,'w') as archive:
                archive.writestr('x.dist-info/METADATA','Name: safety-first-trading-app\nVersion: 0.2.4\n')
                archive.writestr('x.dist-info/entry_points.txt',
                    'trading-paper = trading.app.__main__:main\ntrading-paper-account = trading.app.paper_account:main\n')
                for name in ('__main__','paper_account','cost_research','research_acquisition',
                             'dataset_eligibility','complete_runs','native_research','execution_costs'):
                    archive.writestr(f'trading/app/{name}.py','')
                for name in ('execution_contract','trading_fees'):
                    archive.writestr(f'trading/adapters/coinswitch/{name}.py','')
            self.assertIn('wheel is missing public depth diagnostics',verify_release_wheel(wheel).failures)
            with zipfile.ZipFile(wheel,'a') as archive:
                archive.writestr('trading/app/depth_costs.py','')
            self.assertTrue(verify_release_wheel(wheel).passed)

    def test_integrity_release_cannot_omit_its_offline_contract(self):
        with tempfile.TemporaryDirectory() as folder:
            wheel=Path(folder)/'incomplete.whl'
            with zipfile.ZipFile(wheel,'w') as archive:
                archive.writestr('x.dist-info/METADATA','Name: safety-first-trading-app\nVersion: 0.2.2\n')
                archive.writestr('x.dist-info/entry_points.txt',
                    'trading-paper = trading.app.__main__:main\ntrading-paper-account = trading.app.paper_account:main\n')
                for name in ('__main__','paper_account','cost_research','research_acquisition'):
                    archive.writestr(f'trading/app/{name}.py','')
            result=verify_release_wheel(wheel)
            self.assertFalse(result.passed)
            self.assertTrue(any('execution_contract' in f for f in result.failures))

    @unittest.skipIf(os.name=='nt','POSIX fallback launcher regression')
    def test_fallback_launcher_handles_python_and_package_paths_with_spaces(self):
        script=Path(__file__).resolve().parents[1]/'scripts/release_drill.py'
        spec=importlib.util.spec_from_file_location('release_drill_fixture',script)
        module=importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        with tempfile.TemporaryDirectory(prefix='release space ') as directory:
            root=Path(directory)
            python=root/'python with spaces'
            python.symlink_to(sys.executable)
            wheel=root/'fixture.whl'
            with zipfile.ZipFile(wheel,'w') as archive:
                archive.writestr('trading/__init__.py','')
                archive.writestr('trading/app/__init__.py','')
                for name in ('__main__','paper_account'):
                    archive.writestr(f'trading/app/{name}.py',
                        f'def main():\n print({name!r})\n return 0\n')
            with patch.object(module.venv.EnvBuilder,'create',side_effect=subprocess.CalledProcessError(1,'ensurepip')),\
                    patch.object(module.sys,'executable',str(python)):
                _,console=module.install_isolated(wheel,root/'isolated env')
            for name,expected in (('trading-paper','__main__'),('trading-paper-account','paper_account')):
                result=subprocess.run([str(console.with_name(name)),'--help'],cwd=root,
                                      capture_output=True,text=True,check=True)
                self.assertEqual(result.stdout.strip(),expected)

    def test_valid_dependency_free_wheel_contract_passes(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            wheel = Path(directory) / "release.whl"
            with zipfile.ZipFile(wheel, "w") as archive:
                archive.writestr("trading/app/__main__.py", "")
                archive.writestr(
                    "safety_first_trading_app-0.1.0.dist-info/METADATA",
                    "Metadata-Version: 2.1\n"
                    "Name: safety-first-trading-app\n"
                    "Version: 0.1.0\n",
                )
                archive.writestr(
                    "safety_first_trading_app-0.1.0.dist-info/entry_points.txt",
                    "[console_scripts]\n"
                    "trading-paper = trading.app.__main__:main\n",
                )

            report = verify_release_wheel(wheel)

        self.assertTrue(report.passed, report.failures)
        self.assertEqual(report.package_name, "safety-first-trading-app")
        self.assertEqual(report.version, "0.1.0")
        self.assertEqual(len(report.sha256), 64)

    def test_corrupt_or_dependency_declaring_artifact_fails(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            corrupt = Path(directory) / "corrupt.whl"
            corrupt.write_bytes(b"not a wheel")
            self.assertFalse(verify_release_wheel(corrupt).passed)

            wheel = Path(directory) / "dependency.whl"
            with zipfile.ZipFile(wheel, "w") as archive:
                archive.writestr("trading/app/__main__.py", "")
                archive.writestr(
                    "x.dist-info/METADATA",
                    "Name: safety-first-trading-app\n"
                    "Version: 0.1.0\nRequires-Dist: requests\n",
                )
                archive.writestr(
                    "x.dist-info/entry_points.txt",
                    "trading-paper = trading.app.__main__:main\n",
                )
            self.assertFalse(verify_release_wheel(wheel).passed)

    def test_current_release_requires_account_entrypoint_and_recent_modules(self):
        with tempfile.TemporaryDirectory() as directory:
            wheel=Path(directory)/'release.whl'
            for omitted in ('account-entrypoint','research_acquisition',None):
                with self.subTest(omitted=omitted):
                    with zipfile.ZipFile(wheel,'w') as archive:
                        archive.writestr('x.dist-info/METADATA',
                            'Name: safety-first-trading-app\nVersion: 0.2.1\n')
                        entries='[console_scripts]\ntrading-paper = trading.app.__main__:main\n'
                        if omitted!='account-entrypoint':
                            entries+='trading-paper-account = trading.app.paper_account:main\n'
                        archive.writestr('x.dist-info/entry_points.txt',entries)
                        for module in ('__main__','paper_account','cost_research','research_acquisition'):
                            if module!=omitted:archive.writestr(f'trading/app/{module}.py','')
                    report=verify_release_wheel(wheel)
                    self.assertEqual(report.passed,omitted is None,report.failures)


if __name__ == "__main__":
    unittest.main()
