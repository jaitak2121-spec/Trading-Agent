"""Version-aware frozen paper-deployment checks; never opens observation data."""
import math
from collections.abc import Mapping

from .model_training import direction_model_sha256, validate_paper_model_trial
from .ml_research import FEATURE_NAMES, FEATURE_SCHEMA, HORIZONS, _sha256, _is_sha256
from .dataset_eligibility import require_qualification_eligibility


def model_identity(model):
    if not isinstance(model, Mapping):
        raise ValueError('model must be an artifact object')
    if model.get('schema') != 'offline-logistic-classifier/v2':
        return direction_model_sha256(model)
    if (model.get('research_schema') != FEATURE_SCHEMA
            or model.get('artifact_role') != 'final_frozen_development_candidate'
            or model.get('model_sha256') != _sha256({k:v for k,v in model.items() if k != 'model_sha256'})):
        raise ValueError('invalid frozen V2 model identity')
    names = model.get('features')
    if (names not in [list(FEATURE_NAMES), ['return_1','return_5','range_fraction']]
            or model.get('feature_schema_sha256') != _sha256(names)
            or model.get('horizon_minutes') not in HORIZONS
            or model.get('threshold') != 0.5):
        raise ValueError('unsupported frozen V2 feature schema or horizon')
    for name in ('means','scales','weights'):
        values = model.get(name)
        if not isinstance(values, list) or len(values) != len(names) or any(
            isinstance(v, bool) or not isinstance(v, (int,float)) or not math.isfinite(v) for v in values
        ):
            raise ValueError('invalid frozen V2 parameter vector')
    if any(v <= 0 for v in model['scales']):
        raise ValueError('invalid frozen V2 scaling')
    bias = model.get('bias')
    if isinstance(bias, bool) or not isinstance(bias, (int,float)) or not math.isfinite(bias):
        raise ValueError('invalid frozen V2 bias')
    return model['model_sha256']


def paper_approval_phrase(model):
    return 'APPROVE PAPER MODEL ' + model_identity(model)


def validate_paper_deployment(model, evidence, confirmation):
    if model.get('schema') != 'offline-logistic-classifier/v2':
        return validate_paper_model_trial(model, evidence, confirmation)
    identity = model_identity(model)
    require_qualification_eligibility(model.get('source_integrity'))
    if model['source_integrity']['dataset_sha256']!=model.get('source_dataset_sha256'):
        raise ValueError('model dataset-integrity provenance mismatch')
    if confirmation != paper_approval_phrase(model):
        raise ValueError('paper model confirmation does not match the frozen identity')
    if model.get('symbol') != 'BTC/USDT' or model.get('exchange') not in {'c2c1','c2c2'}:
        raise ValueError('V2 paper deployment requires explicit frozen market provenance')
    if (type(model.get('paper_inference_start_ms')) is not int
            or model['paper_inference_start_ms'] < 0
            or model.get('approved') is not False or model.get('production_model') is not False
            or model.get('live_trading') is not False or model.get('paper_orders') is not False):
        raise ValueError('V2 deployment boundary or safety flags are invalid')
    flags = ('threshold_optimized','features_tuned_after_selection','hyperparameters_tuned_after_selection',
             'confirmation_loaded','confirmation_evaluated','final_test_accessed','model_promoted','live_orders')
    if (any(model.get(name) is not False for name in flags)
            or model.get('confirmation_rows_used_for_fitting') != 0
            or model.get('validation_rows_used_for_fitting') != 0
            or model.get('configuration', {}).get('preprocessing_fit') != 'training_rows_only'):
        raise ValueError('V2 fitting or selection safeguards are invalid')
    if not isinstance(evidence, Mapping) or evidence.get('schema') != 'sealed-confirmation-evaluation/v2':
        raise ValueError('V2 requires previously completed confirmation evidence, not observations')
    if (evidence.get('evidence_sha256') != _sha256({k:v for k,v in evidence.items() if k != 'evidence_sha256'})
            or evidence.get('model_sha256') != identity
            or evidence.get('development_artifact_sha256') != model.get('development_artifact_sha256')
            or evidence.get('accepted') is not True
            or evidence.get('confirmation_evaluated_once') is not True
            or type(evidence.get('samples')) is not int or evidence['samples'] <= 0
            or any(evidence.get(name) is not False for name in
                   ('model_retrained','preprocessing_refit','threshold_optimized','features_selected_on_confirmation','final_test_accessed','paper_orders','live_orders'))):
        raise ValueError('V2 confirmation was rejected, altered or mismatched')
    if any(not _is_sha256(evidence.get(name)) for name in
           ('development_artifact_sha256','confirmation_artifact_sha256','confirmation_sample_sha256',
            'prediction_sha256','authorization_sha256')):
        raise ValueError('V2 confirmation provenance hashes are invalid')
    required = {'required_six_folds', 'mean_walk_forward_roc_auc_at_least_0_52',
                'at_least_five_folds_above_random', 'confirmation_roc_auc_ci_lower_above_0_5',
                'confirmation_average_precision_above_prevalence',
                'confirmation_brier_beats_constant_prevalence', 'no_prediction_collapse'}
    criteria = evidence.get('combined_acceptance_criteria', {})
    if set(criteria) != required or any(value is not True for value in criteria.values()):
        raise ValueError('V2 preregistered acceptance criteria did not all pass')
    try:
        metrics = evidence['metrics']
        interval=evidence['confidence_intervals']['roc_auc_95_interval']
        values=[*interval, metrics['roc_auc'], metrics['average_precision'],metrics['prevalence_baseline'],
                metrics['brier_score'],evidence['baselines']['constant_prevalence']['brier_score'],
                metrics['prediction_distribution']['positive_rate']]
        if len(interval) != 2 or any(isinstance(value,bool) or not isinstance(value,(int,float))
            or not math.isfinite(value) or not 0 <= value <= 1 for value in values) or interval[0] > interval[1]:
            raise ValueError('V2 confirmation metrics are invalid')
        checks = (interval[0] > 0.5, metrics['roc_auc'] > 0.5,
                  metrics['average_precision'] > metrics['prevalence_baseline'],
                  metrics['brier_score'] < evidence['baselines']['constant_prevalence']['brier_score'],
                  0.1 <= metrics['prediction_distribution']['positive_rate'] <= 0.9)
    except (KeyError, TypeError, IndexError) as exc:
        raise ValueError('V2 confirmation metrics are incomplete') from exc
    if not all(checks):
        raise ValueError('V2 confirmation metrics contradict acceptance')
