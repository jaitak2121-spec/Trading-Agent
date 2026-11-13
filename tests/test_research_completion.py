import unittest
import datetime as dt
from types import SimpleNamespace
from decimal import Decimal
from trading.app.backtest import HistoricalBacktester, BacktestConfig
from trading.app.model_monitor import prediction_monitor
from trading.strategy.research import HistoricalRuleStrategy
from tests.test_backtest import bars, risk


class ResearchCompletionTest(unittest.TestCase):
    def test_paper_outcomes_resolve_once_and_do_not_fill_missing_target_bars(self):
        from trading.app.runtime import build_demo_paper_application
        from trading.core.audit import AuditCategory
        for has_target in (False, True):
            app = build_demo_paper_application()
            try:
                candles = bars([100, 101, 102], start=app.stack.clock.now())
                prediction = {'prediction_id':'fixture', 'bar_open_time':candles[0].open_time.isoformat(),
                              'symbol':candles[0].symbol, 'horizon_seconds':60,
                              'reference_close':'100', 'up_probability':'0.8',
                              'threshold':'0.5', 'model_sha256':'fixture-model'}
                app.stack.audit.record(AuditCategory.SIGNAL, 'ml.prediction', details=prediction)
                app.service._store.append(candles[1] if has_target else candles[2])
                app.stack.clock.set_wall_clock(candles[2].close_time)
                app.service._resolve_predictions(app.stack.clock.now())
                app.service._resolve_predictions(app.stack.clock.now())
                outcomes = [r for r in app.stack.audit.records() if r.action == 'ml.outcome']
                self.assertEqual(len(outcomes), int(has_target))
                self.assertEqual(app.stack.broker.placement_count, 0)
            finally:
                app.stop()

    def test_empty_validation_window_is_not_reported_as_zero_return_evidence(self):
        candles = bars(range(100, 145))
        with self.assertRaisesRegex(ValueError, 'no observations'):
            HistoricalBacktester(HistoricalRuleStrategy('momentum')).run(
                candles, trade_start_ms=9999999999999)
    def test_fixed_rules_are_deterministic_and_reports_include_cost_benchmarks(self):
        candles = bars([100+i for i in range(45)] + [145-i for i in range(20)])
        for name in ('momentum', 'mean-reversion', 'volatility-filtered-momentum'):
            first = HistoricalBacktester(HistoricalRuleStrategy(name), risk=risk()).run(candles).report
            second = HistoricalBacktester(HistoricalRuleStrategy(name), risk=risk()).run(candles).report
            self.assertEqual(first, second)
            metrics = first['metrics']
            self.assertIn('sortino_ratio', metrics)
            self.assertIn('buy_hold_benchmark_return_after_costs', metrics)
            self.assertEqual(metrics['cash_benchmark_return'], '0')

    def test_buy_hold_benchmark_decreases_with_fees(self):
        candles = bars(range(100, 145))
        values = []
        for fee in ('0', '10'):
            report = HistoricalBacktester(HistoricalRuleStrategy('momentum'), risk=risk(),
                       config=BacktestConfig(fee_bps=Decimal(fee))).run(candles)
            values.append(Decimal(report.metrics['buy_hold_benchmark_return_after_costs']))
        self.assertLess(values[1], values[0])

    def test_monitor_uses_resolved_outcomes_only_and_never_promotes(self):
        records = [SimpleNamespace(action='ml.prediction', details={'model_sha256':'a'}),
                   SimpleNamespace(action='ml.outcome', details={'model_sha256':'a',
                       'prediction_id':'one', 'label':1, 'up_probability':'0.8'})]
        report = prediction_monitor(records, 'a')
        self.assertEqual(report['resolved_outcomes'], 1)
        self.assertAlmostEqual(report['metrics']['brier_score'], 0.04)
        self.assertFalse(report['promotion_performed'])
        self.assertIsNone(prediction_monitor(records, 'another')['metrics'])
