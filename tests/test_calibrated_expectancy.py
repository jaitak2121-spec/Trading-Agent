import unittest
from trading.app.calibrated_expectancy import line, calibrate, lower_prediction
from trading.app.net_expectancy import DAY


class PayoffCalibrationTests(unittest.TestCase):
    def rows(self):
        return [{'decision_ms':i*DAY,'label_end_ms':(i+1)*DAY,'net_return_target':i*.001-.02} for i in range(75)]

    def test_affine_recovery_and_negative_slope_constraint(self):
        self.assertEqual(line([0.,1.,2.],[1.,3.,5.]),[1.,2.])
        self.assertEqual(line([0.,1.,2.],[5.,3.,1.]),[3.,0.])
        self.assertEqual(line([1.,1.],[2.,4.]),[3.,0.])

    def test_determinism_provenance_and_no_extrapolation(self):
        rows=self.rows();scores=[i*.001 for i in range(75)]
        first=calibrate(rows,scores,80*DAY,100)
        self.assertEqual(first,calibrate(rows,scores,80*DAY,100))
        self.assertAlmostEqual(lower_prediction(first,.05),.03)
        self.assertIsNone(lower_prediction(first,-.01))
        self.assertIsNone(lower_prediction(first,.08))
        self.assertEqual(first['last_label_ms'],75*DAY)

    def test_future_overlapping_and_insufficient_labels_rejected(self):
        rows=self.rows();scores=list(map(float,range(75)))
        with self.assertRaises(ValueError):calibrate(rows,scores,75*DAY,10)
        rows[1]['decision_ms']=DAY-1
        with self.assertRaises(ValueError):calibrate(rows,scores,80*DAY,10)
        with self.assertRaises(ValueError):calibrate(rows[:50],scores[:50],80*DAY,10)

    def test_only_actual_contiguous_blocks_accepted(self):
        rows=self.rows()
        for i,row in enumerate(rows):
            row.update(decision_ms=i*2*DAY,label_end_ms=(i*2+1)*DAY)
        with self.assertRaisesRegex(ValueError,'contiguous'):calibrate(rows,list(map(float,range(75))),160*DAY,10)

    def test_nonfinite_inputs_rejected(self):
        with self.assertRaises(ValueError):line([0.,float('nan')],[1.,2.])
        with self.assertRaises(ValueError):line([0.],[1.])
        with self.assertRaises(ValueError):lower_prediction({},float('inf'))
