"""End-to-end safety verification for the repeatable application soak mode."""

from __future__ import annotations

import contextlib
import datetime as dt
import hashlib
import io
import json
import tempfile
import unittest
from pathlib import Path

from trading.app import build_demo_paper_application, run_soak, run_timed_soak
from trading.adapters.soak_evidence import write_soak_evidence
from trading.core.clock import UTC


class SoakRunnerTest(unittest.TestCase):
    def test_one_complete_price_path_passes_with_entry_exit_and_verified_audit(self) -> None:
        app = build_demo_paper_application(port=0)
        try:
            report = run_soak(app, cycles=19)

            self.assertTrue(report.passed, report.failures)
            self.assertEqual(report.cycles_completed, 19)
            self.assertEqual(report.healthy_cycles, 19)
            self.assertEqual(report.executed, 2)
            self.assertEqual(report.unknown, 0)
            self.assertEqual(report.open_positions, 0)
            self.assertGreater(report.audit_records, 0)
            self.assertIn("paper application soak: PASS", report.render())
            json.dumps(report.as_details())
        finally:
            app.stop()

    def test_operational_block_is_a_failed_soak_not_a_silent_success(self) -> None:
        app = build_demo_paper_application(port=0)
        try:
            app.stack.kill_switch.engage(
                app.stack.operator_id, reason="test the soak failure surface"
            )
            report = run_soak(app, cycles=1)

            self.assertFalse(report.passed)
            self.assertIn("kill_switch_engaged", " ".join(report.failures))
            self.assertIn("FAIL", report.render())
        finally:
            app.stop()

    def test_argument_validation(self) -> None:
        app = build_demo_paper_application(port=0)
        try:
            with self.assertRaises(TypeError):
                run_soak(app, cycles=1.5)  # type: ignore[arg-type]
            with self.assertRaises(ValueError):
                run_soak(app, cycles=0)
            with self.assertRaises(TypeError):
                run_soak("bad", cycles=1)  # type: ignore[arg-type]
        finally:
            app.stop()

    def test_cli_exposes_a_machine_repeatable_soak_command(self) -> None:
        from trading.app.__main__ import main

        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            code = main(["--soak-cycles", "19"])

        self.assertEqual(code, 0)
        self.assertIn("paper application soak: PASS", output.getvalue())
        self.assertIn("cycles          : 19/19", output.getvalue())

    def test_timed_soak_paces_cycles_across_requested_duration(self) -> None:
        app = build_demo_paper_application(port=0)
        waits: list[float] = []
        clock = [0.0]

        def wait(seconds: float) -> None:
            waits.append(seconds)
            clock[0] += seconds

        try:
            report = run_timed_soak(
                app,
                duration_seconds=10,
                interval_seconds=3,
                wait=wait,
                monotonic=lambda: clock[0],
            )
        finally:
            app.stop()

        self.assertTrue(report.passed, report.failures)
        self.assertEqual(report.cycles_completed, 4)
        self.assertEqual(len(waits), 4)
        self.assertAlmostEqual(sum(waits), 10.0)
        self.assertEqual(report.duration_requested_seconds, 10.0)
        self.assertEqual(report.duration_elapsed_seconds, 10.0)

    def test_evidence_is_atomic_redacted_and_checksums_closed_database(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            database = root / "state.sqlite3"
            database.write_bytes(b"closed durable state")
            destination = root / "evidence.json"
            started = dt.datetime(2026, 11, 3, tzinfo=UTC)

            written = write_soak_evidence(
                destination,
                report={"passed": True, "failures": []},
                started_at=started,
                finished_at=started + dt.timedelta(seconds=60),
                state_db=database,
            )
            evidence = json.loads(written.read_text(encoding="utf-8"))

            self.assertEqual(evidence["schema"], "paper-soak-evidence/v1")
            self.assertFalse(evidence["live_trading"])
            self.assertEqual(
                evidence["state_database"]["sha256"],
                hashlib.sha256(database.read_bytes()).hexdigest(),
            )
            self.assertEqual(list(root.glob(".*.tmp")), [])

    def test_cli_writes_verifiable_soak_evidence(self) -> None:
        from trading.app.__main__ import main

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            database = root / "state.sqlite3"
            evidence = root / "soak.json"
            with contextlib.redirect_stdout(io.StringIO()):
                code = main(
                    [
                        "--soak-cycles",
                        "19",
                        "--state-db",
                        str(database),
                        "--soak-report",
                        str(evidence),
                    ]
                )
            payload = json.loads(evidence.read_text(encoding="utf-8"))

        self.assertEqual(code, 0)
        self.assertTrue(payload["report"]["passed"])
        self.assertEqual(len(payload["state_database"]["sha256"]), 64)


if __name__ == "__main__":
    unittest.main()
