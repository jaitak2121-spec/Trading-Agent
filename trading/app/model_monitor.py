"""Descriptive paper prediction monitoring; never fits or promotes models."""
import argparse
import json
import math
from .model_training import _classification_metrics
from ..adapters.persistence.maintenance import load_audit_records


def validate_monitor_policy(policy):
    required={'window','minimum_samples','mean_z_limit','tail_fraction_limit','max_brier_excess'}
    if not isinstance(policy, dict) or not required<=set(policy) or set(policy)-required-{'on_alert'}:
        raise ValueError('monitor policy must explicitly declare its window, sample minimum and alert limits')
    if policy.get('on_alert','warn') not in {'warn','pause'}:
        raise ValueError('monitor on_alert must be warn or pause')
    if (type(policy['window']) is not int or not 1 <= policy['window'] <= 100000
            or type(policy['minimum_samples']) is not int
            or not 1 <= policy['minimum_samples'] <= policy['window']):
        raise ValueError('invalid monitor sample window')
    for name in ('mean_z_limit','tail_fraction_limit','max_brier_excess'):
        value=policy[name]
        if isinstance(value,bool) or not isinstance(value,(int,float)) or not math.isfinite(value) or value < 0:
            raise ValueError('invalid monitor threshold: '+name)
    if policy['mean_z_limit'] == 0 or policy['tail_fraction_limit'] > 1:
        raise ValueError('invalid drift threshold')
    return dict(policy)


def prediction_monitor(records, identity, reference=None, policy=None):
    if policy is not None:
        policy=validate_monitor_policy(policy)
    window=100 if policy is None else policy['window']
    predictions = [r.details for r in records if r.action == 'ml.prediction'
                   and r.details.get('model_sha256') == identity]
    outcomes = {r.details['prediction_id']:r.details for r in records
                if r.action == 'ml.outcome' and r.details.get('model_sha256') == identity}
    recent=list(outcomes.values())[-window:]
    labels = [int(o['label']) for o in recent]
    probabilities = [float(o['up_probability']) for o in recent]
    thresholds = {float(o.get('threshold', 0.5)) for o in outcomes.values()}
    if len(thresholds) > 1:
        raise ValueError('prediction outcomes contain inconsistent thresholds for one model')
    metrics = None if not labels else _classification_metrics(labels, probabilities, next(iter(thresholds)))
    if metrics is not None:
        prevalence = sum(labels)/len(labels)
        metrics['brier_score'] = sum((p-y)**2 for p,y in zip(probabilities, labels))/len(labels)
        metrics['constant_prevalence_brier'] = prevalence*(1-prevalence)
        from .ml_research import binary_classification_metrics
        expanded=binary_classification_metrics(labels,probabilities,threshold=next(iter(thresholds)))
        metrics['average_precision']=expanded['average_precision']
        metrics['balanced_accuracy']=expanded['balanced_accuracy']
    drift = None
    if reference is not None:
        from .model_deployment import model_identity
        if model_identity(reference) != identity:
            raise ValueError('monitor reference does not match model identity')
        features = [p['features'] for p in predictions[-window:] if 'features' in p]
        if features:
            drift = {'samples':len(features), 'window':window, 'features':{}}
            for index, name in enumerate(reference['features']):
                standardized = [(row[index]-reference['means'][index])/reference['scales'][index] for row in features]
                drift['features'][name] = {'mean_training_z':sum(standardized)/len(standardized),
                                          'fraction_outside_3_training_sd':sum(abs(z)>3 for z in standardized)/len(standardized)}
            drift['interpretation'] = 'descriptive training-reference drift; operational alerts never tune or promote'
    alerts=[]
    if policy is not None:
        if drift is not None and drift['samples'] >= policy['minimum_samples']:
            for name,values in drift['features'].items():
                if (abs(values['mean_training_z']) > policy['mean_z_limit']
                        or values['fraction_outside_3_training_sd'] > policy['tail_fraction_limit']):
                    alerts.append({'code':'feature_drift','feature':name,'severity':'warning',
                                   'message':'training-reference drift exceeds the configured monitoring policy'})
        if metrics is not None and len(labels) >= policy['minimum_samples']:
            if metrics['brier_score']-metrics['constant_prevalence_brier'] > policy['max_brier_excess']:
                alerts.append({'code':'calibration_deterioration','severity':'warning',
                               'message':'Brier score exceeds the prevalence baseline by the configured margin'})
    return {'schema':'paper-prediction-monitor/v1', 'model_sha256':identity,
            'predictions':len(predictions), 'resolved_outcomes':len(outcomes),
            'metrics':metrics, 'model_changed':False, 'promotion_performed':False,
            'feature_drift':drift,
            'window':window, 'alert_policy':policy, 'alerts':alerts,
            'limitations':['descriptive forward paper evidence; no profitability conclusion',
                          'unresolved labels across gaps are excluded, not filled',
                          'monitoring thresholds are operational settings, not calibrated evidence of an edge']}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--state-db', required=True)
    parser.add_argument('--model-sha256', required=True)
    parser.add_argument('--reference-model', help='optional explicit frozen model for training-reference drift')
    parser.add_argument('--policy', help='explicit JSON monitoring limits; never fitted from evaluation data')
    args = parser.parse_args(argv)
    reference = None
    from pathlib import Path
    if args.reference_model:
        reference = json.loads(Path(args.reference_model).read_text())
    policy=None if args.policy is None else json.loads(Path(args.policy).read_text())
    print(json.dumps(prediction_monitor(load_audit_records(args.state_db), args.model_sha256, reference, policy),
                     indent=2, sort_keys=True))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
