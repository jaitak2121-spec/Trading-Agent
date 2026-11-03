"""Release artifact and persisted restart-drill verification."""

from __future__ import annotations

import json
import tempfile
import unittest
import zipfile
from pathlib import Path

from trading.app import run_restart_drill, verify_release_wheel


class RestartDrillTest(unittest.TestCase):
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


if __name__ == "__main__":
    unittest.main()
