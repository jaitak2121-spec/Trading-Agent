"""Synthetic controls for diagnostics, never claimed as market performance."""
import unittest
from trading.app.signal_information import moments,correlation,information,nonoverlapping_rows,before_fee_return


class SignalInformationTests(unittest.TestCase):
    def test_within_period_centering_cannot_confuse_regime_levels_with_ranking(self):
        groups=[moments([1.,1.,1.],[10.,11.,12.]),moments([100.,100.,100.],[100.,101.,102.])]
        self.assertIsNone(correlation(groups))
        self.assertEqual(correlation([moments([1.,2.,3.],[2.,4.,6.])]),1.)

    def test_planted_information_is_detected_and_reproducible(self):
        groups=[{'scores':list(map(float,range(12))),'outcomes':[float(i)*.01 for i in range(12)]} for _ in range(6)]
        first=information(groups,replicates=200)
        self.assertAlmostEqual(first['within_fold_ic'],1.)
        self.assertLess(first['two_sided_circular_shift_p'],.05)
        self.assertEqual(first,information(groups,replicates=200))
        self.assertEqual(first['nonoverlapping_samples'],72)

    def test_constant_forecasts_or_outcomes_have_no_identifiable_rank_skill(self):
        self.assertIsNone(information([{'scores':[1.,1.],'outcomes':[2.,3.]}],100)['within_fold_ic'])
        self.assertIsNone(information([{'scores':[1.,2.],'outcomes':[2.,2.]}],100)['within_fold_ic'])

    def test_nonoverlap_boundary_is_exact_and_duplicate_rejected(self):
        hour=3600000;rows=[{'decision_ms':i*hour,'label_end_ms':(i+3)*hour} for i in range(10)]
        self.assertEqual([r['decision_ms']//hour for r in nonoverlapping_rows(rows,3)],[0,3,6,9])
        with self.assertRaises(ValueError):nonoverlapping_rows([rows[0],rows[0]],3)
        with self.assertRaises(ValueError):nonoverlapping_rows([{'decision_ms':0,'label_end_ms':4*hour}],3)

    def test_no_fill_never_receives_an_invented_fee_credit(self):
        self.assertEqual(before_fee_return(0.,False),0.)
        net=1.02*.999/1.001-1
        self.assertAlmostEqual(before_fee_return(net,True),.02)
        with self.assertRaises(ValueError):before_fee_return(float('nan'),True)
