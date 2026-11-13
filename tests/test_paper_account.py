"""Account setup cannot reset existing balances or unexpectedly start a bot."""
import contextlib
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from trading.app.paper_account import create_paper_account,main
from trading.app.composition import build_paper_stack
from trading.app.runtime import build_demo_paper_application
from trading.adapters.persistence.sqlite import load_audit_records
from trading.core.money import Money,USD
from trading.core.errors import SafetyViolation,PrecisionError


class PaperAccountTests(unittest.TestCase):
    def test_create_restart_paused_and_balance_survives_different_cli_cash(self):
        with tempfile.TemporaryDirectory() as folder:
            path=Path(folder)/'account.sqlite3'
            result=create_paper_account(path,cash='1234.56')
            self.assertEqual(result['state'],'paused')
            self.assertEqual(result['integrity'],'passed')
            app=build_demo_paper_application(sqlite_path=path,starting_cash=Money('99999',USD))
            try:
                self.assertEqual(app.stack.portfolio.cash,Money('1234.56',USD))
                self.assertEqual(app.stack.portfolio.starting_cash,Money('1234.56',USD))
                self.assertEqual(app.stack.bot.state,'paused')
                self.assertEqual(app.stack.orders.all_orders(),[])
                with self.assertRaises(SafetyViolation):app.service.poll_once()
                records=load_audit_records(path)
                self.assertTrue(any(r.action=='paper.account_created' for r in records))
                app.stack.bot.transition('running',app.stack.operator_id,reason='test explicit resume')
                self.assertEqual(app.stack.bot.state,'running')
            finally:app.stop()

    def test_existing_file_never_changed(self):
        with tempfile.TemporaryDirectory() as folder:
            path=Path(folder)/'existing.sqlite3'
            path.write_bytes(b'preserve this history')
            with self.assertRaises(FileExistsError):create_paper_account(path,cash='100')
            self.assertEqual(path.read_bytes(),b'preserve this history')

    def test_symlink_and_sidecar_refused(self):
        with tempfile.TemporaryDirectory() as folder:
            path=Path(folder)/'account.sqlite3'
            path.symlink_to(Path(folder)/'missing')
            with self.assertRaises(FileExistsError):create_paper_account(path,cash='100')
            path.unlink()
            Path(str(path)+'-wal').write_bytes(b'preserved')
            with self.assertRaises(FileExistsError):create_paper_account(path,cash='100')
            self.assertFalse(path.exists())

    def test_invalid_amounts_do_not_create_files(self):
        with tempfile.TemporaryDirectory() as folder:
            for cash in ('0','-1','NaN','Infinity','invalid','1.001',True):
                with self.subTest(cash=cash):
                    with self.assertRaises((ValueError,TypeError,PrecisionError)):
                        create_paper_account(Path(folder)/'account.sqlite3',cash=cash)
                    self.assertEqual(list(Path(folder).iterdir()),[])

    def test_publish_race_preserves_competing_account(self):
        with tempfile.TemporaryDirectory() as folder:
            path=Path(folder)/'account.sqlite3'
            real_link=os.link
            def race(source,target):
                Path(target).write_bytes(b'another account')
                return real_link(source,target)
            with patch('trading.app.paper_account.os.link',side_effect=race):
                with self.assertRaises(FileExistsError):create_paper_account(path,cash='100')
            self.assertEqual(path.read_bytes(),b'another account')
            self.assertEqual(list(Path(folder).iterdir()),[path])

    def test_failed_verification_does_not_publish(self):
        with tempfile.TemporaryDirectory() as folder:
            path=Path(folder)/'account.sqlite3'
            with patch('trading.app.paper_account.inspect_database',side_effect=ValueError('corrupt')):
                with self.assertRaises(ValueError):create_paper_account(path,cash='100')
            self.assertEqual(list(Path(folder).iterdir()),[])

    def test_quote_currencies_and_private_file_permissions(self):
        with tempfile.TemporaryDirectory() as folder:
            for currency in ('USD','INR','USDT'):
                path=Path(folder)/(currency+'.sqlite3')
                result=create_paper_account(path,cash='100',currency=currency)
                self.assertEqual(result['currency'],currency)
                self.assertEqual(path.stat().st_mode&0o777,0o600)
                self.assertEqual(load_audit_records(path)[-1].details['currency'],currency)
            with self.assertRaises(ValueError):create_paper_account(Path(folder)/'bad',cash='1',currency='BTC')

    def test_cli_reports_virtual_paused_account(self):
        with tempfile.TemporaryDirectory() as folder,contextlib.redirect_stdout(io.StringIO()) as output:
            self.assertEqual(main(['create','--state-db',folder+'/test.db','--cash','5000']),0)
            result=json.loads(output.getvalue())
            self.assertEqual(result['starting_cash'],'5000')
            self.assertFalse(result['live_trading'])
            self.assertEqual(result['state'],'paused')

    def test_cli_precision_error_has_no_traceback_or_database(self):
        with tempfile.TemporaryDirectory() as folder,contextlib.redirect_stderr(io.StringIO()) as output:
            path=Path(folder)/'invalid.sqlite3'
            with self.assertRaises(SystemExit) as error:
                main(['create','--state-db',str(path),'--cash','1.001'])
            self.assertEqual(error.exception.code,2)
            self.assertNotIn('Traceback',output.getvalue())
            self.assertFalse(path.exists())

    def test_existing_valid_account_and_audit_preserved(self):
        with tempfile.TemporaryDirectory() as folder:
            path=Path(folder)/'existing.sqlite3'
            create_paper_account(path,cash='500')
            before=path.read_bytes()
            audit_before=load_audit_records(path)
            with self.assertRaises(FileExistsError):create_paper_account(path,cash='90000')
            self.assertEqual(before,path.read_bytes())
            self.assertEqual(audit_before,load_audit_records(path))


if __name__=='__main__':unittest.main()
