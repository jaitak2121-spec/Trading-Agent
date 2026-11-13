"""Synthetic fixtures test mechanics, never serve as market performance evidence."""
import dataclasses
import unittest
from decimal import Decimal

from tests.test_backtest import bars, Scripted, risk
from trading.app.backtest import BacktestConfig, HistoricalBacktester
from trading.app.cost_research import bootstrap, contiguous, periods, preregister
from trading.strategy.cost_aware import aggregate_complete_minutes, SlowerResearchStrategy
from trading.strategy.signals import SignalDirection
from trading.strategy.context import MarketContext
from trading.core.marketdata import MarketSnapshot
from trading.core.money import Money, USDT


class CostResearchTests(unittest.TestCase):
    def test_aggregation_real_values_and_complete_buckets(self):
        source=bars(range(100,111))
        coarse=aggregate_complete_minutes(source,5)
        self.assertEqual(len(coarse),2)
        self.assertEqual(coarse[0].open.amount,Decimal(100))
        self.assertEqual(coarse[0].close.amount,Decimal(104))
        self.assertEqual(coarse[0].high.amount,Decimal(106))
        self.assertEqual(coarse[0].low.amount,Decimal(98))
        self.assertEqual(coarse[0].volume.amount,Decimal(5))
        self.assertEqual(coarse[0].close_time,source[4].close_time)

    def test_missing_minute_is_not_filled(self):
        source=bars([100]*15)
        del source[7]
        coarse=aggregate_complete_minutes(source,5)
        self.assertEqual([c.open_time.minute for c in coarse],[0,10])
        self.assertEqual([len(r) for r in contiguous(source)],[7,7])

    def test_unaligned_edges_discarded(self):
        source=bars([100]*16)[1:]
        self.assertEqual([b.open_time.minute for b in aggregate_complete_minutes(source,5)],[5,10])

    def test_invalid_aggregation_rejected(self):
        source=bars([100]*5)
        for invalid in (source+[source[-1]],list(reversed(source)),
                        [dataclasses.replace(source[0],interval_seconds=120)]):
            with self.assertRaises(ValueError):
                aggregate_complete_minutes(invalid,5)
        for frame in (True,0,1,60):
            with self.assertRaises(ValueError):
                SlowerResearchStrategy('breakout',frame)

    def test_prefix_aggregation_cannot_see_future(self):
        source=bars(range(100,120))
        before=aggregate_complete_minutes(source[:10],5)
        changed=source[:10]+bars([900]*10,start=source[10].open_time)
        self.assertEqual(before,aggregate_complete_minutes(changed,5)[:2])

    def test_decision_cadence_and_name(self):
        strategy=SlowerResearchStrategy('breakout',5)
        source=bars([100]*10)
        self.assertEqual([i for i,c in enumerate(source) if strategy.decision_due(c)],[4,9])
        self.assertEqual(strategy.inner.name,strategy.name)

    def test_stop_processed_between_coarse_decisions(self):
        class Cadenced(Scripted):
            def decision_due(self,candle):
                return candle.open_time.minute%5==0
        strategy=Cadenced({0:SignalDirection.LONG})
        source=bars([100,100,80,100,100,100])
        result=HistoricalBacktester(strategy,config=BacktestConfig(fee_bps=0,
            slippage_bps=0,spread_bps=0),risk=risk()).run(source)
        trade=result.report['trades'][0]
        self.assertEqual(trade['entry_time'],source[1].open_time.isoformat())
        self.assertEqual(trade['exit_reason'],'stop_gap')
        self.assertIn('00:02:',trade['exit_time'])

    def test_strategy_deterministic_no_incomplete_bars(self):
        source=bars(range(100,400))
        engine=HistoricalBacktester(SlowerResearchStrategy('breakout',5))
        context=MarketContext(as_of=source[259].close_time,equity=Money('10000',USDT),
            snapshot=MarketSnapshot(source[259].close_time,{'BTC/USDT':engine._quote(source[259])}),
            history={'BTC/USDT':source})
        strategy=SlowerResearchStrategy('breakout',5)
        a=strategy.generate(context)
        truncated=dataclasses.replace(context,history={'BTC/USDT':source[:260]})
        self.assertEqual(a,strategy.generate(truncated))
        self.assertTrue(a)
        self.assertEqual(a[0].direction,SignalDirection.LONG)
        self.assertEqual(a,strategy.generate(context))

    def test_bootstrap_reproducible_and_sparse_rejected(self):
        self.assertIsNone(bootstrap([[1]*29]))
        a=bootstrap([[1]*20,[2]*20],100)
        self.assertEqual(a,bootstrap([[1]*20,[2]*20],100))
        self.assertGreaterEqual(a['lower'],1)
        self.assertLessEqual(a['upper'],2)
        self.assertEqual(a['method'],'moving_trade_blocks_within_period')

    def test_coverage_selection_uses_counts_only(self):
        self.assertEqual(periods(bars([100]*4319)),[])
        selected=periods(bars([100]*4320))
        self.assertEqual(len(selected),1)
        self.assertEqual(selected[0]['trade_start'],'2026-01-02T02:00:00+00:00')


if __name__=='__main__':
    unittest.main()
