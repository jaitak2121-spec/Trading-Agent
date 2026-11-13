"""Monitoring alerts use fixed operator limits and never change a model."""
import copy
import unittest
from types import SimpleNamespace

from trading.app.model_deployment import model_identity
from trading.app.model_monitor import prediction_monitor, validate_monitor_policy
from tests.test_v2_paper_strategy import fixture


POLICY={'window':100,'minimum_samples':100,'mean_z_limit':1.0,
        'tail_fraction_limit':0.1,'max_brier_excess':0.02}


class ModelMonitorTest(unittest.TestCase):
    def test_fixed_alerts_and_minimum_samples_do_not_modify_model(self):
        model,_=fixture()
        before=copy.deepcopy(model)
        identity=model_identity(model)
        records=[]
        for index in range(100):
            records.extend([
                SimpleNamespace(action='ml.prediction',details={'model_sha256':identity,'features':[10.0]*18}),
                SimpleNamespace(action='ml.outcome',details={'model_sha256':identity,'prediction_id':str(index),
                    'label':index%2,'up_probability':'0.9','threshold':'0.5'})])
        incomplete=prediction_monitor(records[:-2],identity,model,POLICY)
        self.assertEqual(incomplete['alerts'],[])
        report=prediction_monitor(records,identity,model,POLICY)
        self.assertEqual({alert['code'] for alert in report['alerts']},
                         {'feature_drift','calibration_deterioration'})
        self.assertFalse(report['model_changed'])
        self.assertFalse(report['promotion_performed'])
        self.assertEqual(model,before)
        self.assertIn('balanced_accuracy',report['metrics'])

    def test_bad_monitor_policies_refuse(self):
        for changes in ({'mean_z_limit':float('nan')},{'window':0},
                        {'tail_fraction_limit':2},{'minimum_samples':True}):
            with self.assertRaises(ValueError):
                validate_monitor_policy({**POLICY,**changes})

    def test_no_implicit_alert_thresholds(self):
        model,_=fixture()
        report=prediction_monitor([],model_identity(model),model)
        self.assertIsNone(report['alert_policy'])
        self.assertEqual(report['alerts'],[])
