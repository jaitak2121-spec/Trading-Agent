"""Explicit paper-only recovery after an interrupted open-position run."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from trading.app import (
    PAPER_POSITION_RESET_CONFIRMATION,
    build_demo_paper_application,
    recover_interrupted_paper_position,
    run_soak,
)


class InterruptedPaperPositionRecoveryTest(unittest.TestCase):
    def create_interrupted_state(self, path: Path) -> None:
        app = build_demo_paper_application(sqlite_path=path, port=0)
        try:
            for _ in range(19):
                app.service.poll_once()
                if app.stack.portfolio.open_positions():
                    break
            self.assertTrue(app.stack.portfolio.open_positions())
            self.assertEqual(app.stack.orders.open_orders(), [])
            # Exercise legacy databases which predate durable venue inventory.
            app.stack.persistence.connection.execute(
                "DELETE FROM metadata WHERE key='paper_venue'"
            )
        finally:
            app.stop()

    def test_operator_adopts_ephemeral_paper_venue_then_cleanly_rebuilds(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "interrupted.sqlite3"
            self.create_interrupted_state(path)

            blocked = build_demo_paper_application(sqlite_path=path, port=0)
            try:
                self.assertFalse(blocked.stack.startup.ok)
                self.assertTrue(blocked.stack.reconciliation.has_mismatch)
                self.assertFalse(blocked.stack.monitor.status().ready)
            finally:
                blocked.stop()

            report = recover_interrupted_paper_position(
                sqlite_path=path,
                confirmation=PAPER_POSITION_RESET_CONFIRMATION,
                reason="discard interrupted local paper position after venue reset",
            )
            self.assertTrue(report.passed)
            self.assertTrue(report.before)
            self.assertEqual(report.adopted, {})

            recovered = build_demo_paper_application(sqlite_path=path, port=0)
            try:
                self.assertTrue(recovered.stack.startup.ok)
                self.assertTrue(recovered.stack.monitor.status().ready)
                self.assertEqual(recovered.stack.portfolio.open_positions(), [])
                recovered.resume(reason='operator verified paper recovery')
                soak = run_soak(recovered, cycles=19)
                self.assertTrue(soak.passed, soak.failures)
                actions = [record.action for record in recovered.stack.audit.records()]
                self.assertIn("broker_positions_adopted", actions)
                self.assertIn("position_mismatch_cleared", actions)
            finally:
                recovered.stop()

    def test_wrong_confirmation_refuses_without_touching_state(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "interrupted.sqlite3"
            self.create_interrupted_state(path)

            with self.assertRaisesRegex(ValueError, "exact.*confirmation"):
                recover_interrupted_paper_position(
                    sqlite_path=path,
                    confirmation="yes",
                    reason="should not be accepted",
                )

            blocked = build_demo_paper_application(sqlite_path=path, port=0)
            try:
                self.assertTrue(blocked.stack.reconciliation.has_mismatch)
                self.assertTrue(blocked.stack.portfolio.open_positions())
            finally:
                blocked.stop()


if __name__ == "__main__":
    unittest.main()
