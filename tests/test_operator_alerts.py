"""Headless alerts and opt-in desktop delivery use no real notification in tests."""
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from trading.app.runtime import build_demo_paper_application
from trading.adapters.process import notify_local_operator


class OperatorAlertTest(unittest.TestCase):
    def test_monitor_alert_transitions_are_audited_once_without_dashboard(self):
        app=build_demo_paper_application()
        report={'model_sha256':'test-fixture','alerts':[{'code':'feature_drift','feature':'return_1'}]}
        app.service._prediction_monitor=lambda:report
        try:
            app.service.poll_once()
            app.service.poll_once()
            report['alerts']=[]
            app.service.poll_once()
            events=[r for r in app.stack.audit.records() if r.action=='ml.monitor_alert']
            self.assertEqual([r.details['status'] for r in events],['active','recovered'])
            self.assertIsNone(app.service._notification)
            self.assertEqual(app.stack.broker.placement_count,0)
        finally:
            app.stop()

    def test_monitor_failure_blocks_further_orders_and_records_delivery_failure(self):
        app=build_demo_paper_application()
        def failed_monitor():
            raise ValueError('test fixture monitoring failure')
        app.service._prediction_monitor=failed_monitor
        app.service._notification=lambda message:False
        try:
            with self.assertRaises(ValueError):
                app.service.poll_once()
            self.assertTrue(app.stack.kill_switch.is_engaged)
            self.assertEqual(app.stack.broker.placement_count,0)
            self.assertIn('operator.notification_failed',[r.action for r in app.stack.audit.records()])
        finally:
            app.stop()

    def test_desktop_notification_uses_fixed_argv_without_credentials(self):
        message='quoted "message"; not executable code'
        with patch('trading.adapters.process.sys.platform','darwin'):
            with patch.dict('os.environ',{'TRADING_API_SECRET':'synthetic-secret'}):
                with patch('trading.adapters.process.subprocess.run',return_value=SimpleNamespace(returncode=0)) as run:
                    self.assertTrue(notify_local_operator(message))
                    args,options=run.call_args
                    self.assertEqual(args[0][0],'/usr/bin/osascript')
                    self.assertEqual(args[0][-1],message)
                    self.assertNotIn(message,args[0][2])
                    self.assertNotIn('TRADING_API_SECRET',options['env'])
                    self.assertNotIn('shell',options)

    def test_unsupported_platform_does_not_invoke_any_delivery(self):
        with patch('trading.adapters.process.sys.platform','not-macos'):
            with patch('trading.adapters.process.subprocess.run') as run:
                self.assertFalse(notify_local_operator('test'))
                run.assert_not_called()
