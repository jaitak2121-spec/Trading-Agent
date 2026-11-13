import tempfile
import unittest
from pathlib import Path
from trading.app.composition import build_paper_stack
from trading.adapters.persistence import SQLiteState, PersistenceError
from trading.app.maintenance import copy_database, inspect_database


class MaintenanceTest(unittest.TestCase):
    def test_version_one_migrates_without_losing_accounting(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder)/'legacy.db'
            stack = build_paper_stack(sqlite_path=path)
            cash = stack.portfolio.cash
            stack.persistence.connection.execute("UPDATE metadata SET value='1' WHERE key='schema_version'")
            stack.persistence.close()
            restored = build_paper_stack(sqlite_path=path)
            try:
                self.assertEqual(restored.portfolio.cash, cash)
                self.assertEqual(inspect_database(path)['schema_version'], '2')
                self.assertIsNotNone(restored.persistence.connection.execute(
                    "SELECT value FROM metadata WHERE key='migration_1_to_2'").fetchone())
            finally:
                restored.persistence.close()

    def test_unknown_schema_is_refused(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder)/'future.db'
            state = SQLiteState(path)
            state.connection.execute("UPDATE metadata SET value='99' WHERE key='schema_version'")
            state.close()
            with self.assertRaises(PersistenceError):
                SQLiteState(path)

    def test_committed_wal_backup_and_restore_and_no_overwrite(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            stack = build_paper_stack(sqlite_path=root/'source.db')
            try:
                report = copy_database(root/'source.db', root/'backup.db')
                self.assertEqual(report['audit_records'], stack.audit.count)
                self.assertEqual(len(report['sha256']), 64)
                with self.assertRaises(FileExistsError):
                    copy_database(root/'source.db', root/'backup.db')
                copy_database(root/'backup.db', root/'restored.db')
                self.assertEqual(inspect_database(root/'restored.db')['integrity'], 'passed')
                restored = build_paper_stack(sqlite_path=root/'restored.db')
                self.assertEqual(restored.bot.state, 'paused')
                restored.persistence.close()
            finally:
                stack.persistence.close()

    def test_invalid_database_never_publishes_backup(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            source = root/'source.db'
            source.write_bytes(b'invalid database')
            with self.assertRaises(Exception):
                copy_database(source, root/'backup.db')
            self.assertFalse((root/'backup.db').exists())
