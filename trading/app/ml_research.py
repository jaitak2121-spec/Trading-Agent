"""Gap-safe, causal research pipeline for the second direction experiment."""

from __future__ import annotations

import hashlib
import json
import math
import random
import statistics
from collections.abc import Mapping, Sequence
from decimal import Decimal, InvalidOperation

from .historical_candles import validate_candle_dataset
from .dataset_eligibility import assess_dataset,require_qualification_eligibility
from .model_training import fit_logistic_classifier, predict_logistic_probabilities

FEATURE_SCHEMA = "btc-usdt-causal-features/v2"
FEATURE_NAMES = (
    "return_1",
    "return_5",
    "return_15",
    "return_30",
    "return_60",
    "volume_ratio_5",
    "volume_ratio_15",
    "volatility_15",
    "volatility_60",
    "volatility_ratio",
    "ema_gap_5_20",
    "ema_gap_20_60",
    "trend_strength",
    "range_fraction",
    "signed_body_fraction",
    "upper_wick_fraction",
    "lower_wick_fraction",
    "zero_range",
)
HORIZONS = (5, 15, 30)
CONTEXT_CANDLES = 240
MINUTE_MS = 60_000

_FEATURE_DEFINITIONS = {
    "return_k": "close[t] / close[t-k] - 1 for k in 1,5,15,30,60",
    "volume_ratio_k": (
        "sum(volume[t-k+1:t]) divided by the k-minute equivalent of "
        "volume[t-k-59:t-k] for k in 5,15"
    ),
    "volatility_k": "population standard deviation of one-minute returns ending at t",
    "volatility_ratio": "volatility_15 / volatility_60; zero when volatility_60 is zero",
    "ema_gap_a_b": "EMA_a[t] / EMA_b[t] - 1 from a fixed 240-candle context",
    "trend_strength": "ema_gap_20_60 / volatility_60; zero when volatility_60 is zero",
    "range_fraction": "(high[t] - low[t]) / close[t]",
    "signed_body_fraction": "(close[t] - open[t]) / (high[t] - low[t])",
    "upper_wick_fraction": "(high[t] - max(open[t], close[t])) / range[t]",
    "lower_wick_fraction": "(min(open[t], close[t]) - low[t]) / range[t]",
    "zero_range": "1 when high[t] == low[t], otherwise 0",
}

__all__ = [
    "CONTEXT_CANDLES",
    "FEATURE_NAMES",
    "FEATURE_SCHEMA",
    "HORIZONS",
    "baseline_probabilities",
    "binary_classification_metrics",
    "block_bootstrap_confidence_intervals",
    "evaluate_research_model",
    "confirmation_evaluation_phrase",
    "evaluate_frozen_confirmation",
    "fit_final_development_model",
    "fit_research_logistic",
    "generate_causal_dataset",
    "generate_walk_forward_manifest",
    "preregistered_experiment_manifest",
    "run_walk_forward_candidate",
    "seal_confirmation_period",
]


def generate_causal_dataset(
    source: Mapping[str, object], *, horizon_minutes: int
) -> dict[str, object]:
    """Derive one horizon without crossing source gaps or overlapping labels."""
    _validate_source(source)
    if horizon_minutes not in HORIZONS:
        raise ValueError(f"horizon must be one of {HORIZONS}")
    candles = source.get("candles")
    if not isinstance(candles, list):
        raise ValueError("source candles must be a list")
    parsed = [_parse_candle(row) for row in candles]
    if not parsed:
        raise ValueError("source candles are empty")
    segments = _contiguous_segments(parsed)
    samples: list[dict[str, object]] = []
    segment_summaries = []
    for segment_id, segment in enumerate(segments, 1):
        generated = _segment_samples(segment, segment_id, horizon_minutes)
        samples.extend(generated)
        segment_summaries.append(
            {
                "segment_id": segment_id,
                "start_open_time": segment[0]["open_time"],
                "end_open_time": segment[-1]["open_time"],
                "candle_count": len(segment),
                "sample_count": len(generated),
            }
        )
    if not samples:
        raise ValueError("source has no segment long enough for causal samples")
    output: dict[str, object] = {
        "schema": "causal-direction-dataset/v2",
        "feature_schema": FEATURE_SCHEMA,
        "feature_names": list(FEATURE_NAMES),
        "feature_definitions": dict(_FEATURE_DEFINITIONS),
        "label_definition": (
            f"1 when close[t+{horizon_minutes}] > close[t], otherwise 0"
        ),
        "horizon_minutes": horizon_minutes,
        "sampling_stride_minutes": horizon_minutes,
        "context_candles": CONTEXT_CANDLES,
        "symbol": str(source["symbol"]).upper(),
        "exchange": str(source["exchange"]).lower(),
        "source_dataset_sha256": _sha256(source),
        "source_integrity": assess_dataset(source),
        "source_first_open_time": parsed[0]["open_time"],
        "source_last_open_time": parsed[-1]["open_time"],
        "gap_policy": "split; never fill; reset feature state",
        "preprocessing_fitted": False,
        "model_fitted": False,
        "confirmation_evaluated": False,
        "final_test_accessed": False,
        "segments": segment_summaries,
        "samples": samples,
    }
    output["dataset_sha256"] = _sha256(output)
    return output


def generate_walk_forward_manifest(
    dataset: Mapping[str, object],
    *,
    range_start_ms: int,
    range_end_ms: int,
    training_days: int = 120,
    validation_days: int = 30,
    step_days: int = 30,
    confirmation_days: int = 60,
) -> dict[str, object]:
    """Create deterministic rolling folds while leaving confirmation sealed."""
    _validate_development_dataset(dataset)
    for name, value in (
        ("range_start_ms", range_start_ms),
        ("range_end_ms", range_end_ms),
        ("training_days", training_days),
        ("validation_days", validation_days),
        ("step_days", step_days),
        ("confirmation_days", confirmation_days),
    ):
        if isinstance(value, bool) or not isinstance(value, int):
            raise TypeError(f"{name} must be an int")
    if range_start_ms < 0 or range_end_ms <= range_start_ms:
        raise ValueError("walk-forward range is invalid")
    if min(training_days, validation_days, step_days, confirmation_days) <= 0:
        raise ValueError("walk-forward day counts must be positive")
    day_ms = 86_400_000
    confirmation_start = range_end_ms - confirmation_days * day_ms
    if confirmation_start <= range_start_ms:
        raise ValueError("confirmation period consumes the full range")
    samples = dataset["samples"]
    folds = []
    fold_start = range_start_ms
    fold_id = 1
    while True:
        training_end = fold_start + training_days * day_ms
        validation_end = training_end + validation_days * day_ms
        if validation_end > confirmation_start:
            break
        training_rows = _samples_in_range(samples, fold_start, training_end)
        validation_rows = _samples_in_range(samples, training_end, validation_end)
        if training_rows and validation_rows:
            folds.append(
                {
                    "fold_id": fold_id,
                    "training_start": fold_start,
                    "training_end_exclusive": training_end,
                    "validation_start": training_end,
                    "validation_end_exclusive": validation_end,
                    "training_samples": len(training_rows),
                    "validation_samples": len(validation_rows),
                    "training_sample_sha256": _sha256(training_rows),
                    "validation_sample_sha256": _sha256(validation_rows),
                    "preprocessing_policy": "fit on this fold's training rows only",
                    "threshold_policy": "fixed at 0.5; outer validation never tunes it",
                }
            )
            fold_id += 1
        fold_start += step_days * day_ms
    if not folds:
        raise ValueError("range does not contain one complete walk-forward fold")
    sealed_confirmation = dataset["sealed_confirmation"]
    if not isinstance(sealed_confirmation, Mapping) or not (
        sealed_confirmation.get("start") == confirmation_start
        and sealed_confirmation.get("end_exclusive") == range_end_ms
        and sealed_confirmation.get("sealed") is True
        and sealed_confirmation.get("evaluated") is False
        and isinstance(sealed_confirmation.get("sample_count"), int)
        and sealed_confirmation.get("sample_count", 0) > 0
        and _is_sha256(sealed_confirmation.get("sample_sha256"))
    ):
        raise ValueError("sealed confirmation descriptor does not match the range")
    manifest: dict[str, object] = {
        "schema": "direction-walk-forward/v2",
        "development_artifact_sha256": dataset["artifact_sha256"],
        "feature_schema": FEATURE_SCHEMA,
        "horizon_minutes": dataset["horizon_minutes"],
        "folds": folds,
        "confirmation": {
            "start": confirmation_start,
            "end_exclusive": range_end_ms,
            "sample_count": sealed_confirmation["sample_count"],
            "sample_sha256": sealed_confirmation["sample_sha256"],
            "sealed": True,
            "evaluated": False,
        },
        "final_test": {"sealed": True, "accessed": False, "included": False},
    }
    manifest["manifest_sha256"] = _sha256(manifest)
    return manifest


def seal_confirmation_period(
    dataset: Mapping[str, object],
    *,
    confirmation_start_ms: int,
    range_end_ms: int,
) -> tuple[dict[str, object], dict[str, object]]:
    """Physically separate development rows from the locked confirmation rows."""
    _validate_causal_dataset(dataset)
    if (
        isinstance(confirmation_start_ms, bool)
        or not isinstance(confirmation_start_ms, int)
        or isinstance(range_end_ms, bool)
        or not isinstance(range_end_ms, int)
        or confirmation_start_ms <= 0
        or range_end_ms <= confirmation_start_ms
    ):
        raise ValueError("confirmation range is invalid")
    samples = dataset["samples"]
    development_rows = [
        sample
        for sample in samples
        if isinstance(sample, Mapping)
        and isinstance(sample.get("label_close_time"), int)
        and sample["label_close_time"] <= confirmation_start_ms
    ]
    confirmation_rows = _samples_in_range(
        samples, confirmation_start_ms, range_end_ms
    )
    if not development_rows or not confirmation_rows:
        raise ValueError("development or confirmation partition is empty")
    sample_hash = _sha256(confirmation_rows)
    common = {
        "feature_schema": FEATURE_SCHEMA,
        "symbol": dataset['symbol'],
        "exchange": dataset['exchange'],
        "feature_names": list(FEATURE_NAMES),
        "horizon_minutes": dataset["horizon_minutes"],
        "source_dataset_sha256": dataset["source_dataset_sha256"],
        "causal_dataset_sha256": dataset["dataset_sha256"],
        "source_integrity": dataset.get('source_integrity'),
        "final_test_accessed": False,
    }
    confirmation: dict[str, object] = {
        "schema": "sealed-direction-confirmation/v2",
        **common,
        "start": confirmation_start_ms,
        "end_exclusive": range_end_ms,
        "sample_count": len(confirmation_rows),
        "sample_sha256": sample_hash,
        "sealed": True,
        "evaluated": False,
        "samples": confirmation_rows,
    }
    confirmation["artifact_sha256"] = _sha256(confirmation)
    development: dict[str, object] = {
        "schema": "direction-development-dataset/v2",
        **common,
        "preprocessing_fitted": False,
        "model_fitted": False,
        "samples": development_rows,
        "sealed_confirmation": {
            "start": confirmation_start_ms,
            "end_exclusive": range_end_ms,
            "sample_count": len(confirmation_rows),
            "sample_sha256": sample_hash,
            "artifact_sha256": confirmation["artifact_sha256"],
            "sealed": True,
            "evaluated": False,
        },
    }
    development["artifact_sha256"] = _sha256(development)
    return development, confirmation


def preregistered_experiment_manifest(
    *, source_dataset_sha256: str, walk_forward_manifest_sha256: Sequence[str]
) -> dict[str, object]:
    """Freeze the small approved candidate matrix before any fitting."""
    if not _is_sha256(source_dataset_sha256):
        raise ValueError("source dataset hash is invalid")
    hashes = list(walk_forward_manifest_sha256)
    if len(hashes) != len(HORIZONS) or any(not _is_sha256(value) for value in hashes):
        raise ValueError("one valid walk-forward manifest hash is required per horizon")
    candidates = []
    for horizon in HORIZONS:
        candidates.append(
            {
                "horizon_minutes": horizon,
                "feature_set": "price_baseline_3",
                "features": ["return_1", "return_5", "range_fraction"],
                "l2_penalty": 0.001,
            }
        )
        for penalty in (0.0001, 0.001, 0.01):
            candidates.append(
                {
                    "horizon_minutes": horizon,
                    "feature_set": "expanded_18",
                    "features": list(FEATURE_NAMES),
                    "l2_penalty": penalty,
                }
            )
    manifest: dict[str, object] = {
        "schema": "direction-experiment-preregistration/v2",
        "source_dataset_sha256": source_dataset_sha256,
        "walk_forward_manifest_sha256": hashes,
        "candidates": candidates,
        "threshold": 0.5,
        "threshold_optimized_on_outer_validation": False,
        "selection_metrics": [
            "roc_auc",
            "average_precision",
            "balanced_accuracy",
            "brier_score",
            "prediction_distribution",
        ],
        "baselines": ["majority", "constant_prevalence", "momentum", "contrarian"],
        "acceptance": {
            "minimum_mean_walk_forward_roc_auc": 0.52,
            "minimum_folds_above_random": 5,
            "required_folds": 6,
            "roc_auc_confidence_lower_bound_above": 0.5,
            "average_precision_must_exceed_prevalence": True,
            "brier_must_beat_constant_prevalence": True,
            "prediction_collapse_forbidden": True,
        },
        "confirmation": {"sealed": True, "evaluated": False},
        "final_test": {"sealed": True, "accessed": False},
        "model_fitted": False,
    }
    manifest["manifest_sha256"] = _sha256(manifest)
    return manifest


def fit_research_logistic(
    training_samples: Sequence[Mapping[str, object]],
    *,
    feature_names: Sequence[str],
    l2_penalty: float,
) -> dict[str, object]:
    """Fit one preregistered candidate; callers must supply training rows only."""
    names = tuple(feature_names)
    rows, labels = _matrix(training_samples, names)
    model = fit_logistic_classifier(
        rows,
        labels,
        feature_names=names,
        l2_penalty=l2_penalty,
    )
    model.update(
        {
            "research_schema": FEATURE_SCHEMA,
            "training_sample_sha256": _sha256(list(training_samples)),
            "threshold_optimized": False,
            "confirmation_evaluated": False,
            "final_test_accessed": False,
            "approved": False,
            "paper_only": False,
            "live_trading": False,
        }
    )
    model["model_sha256"] = _sha256(model)
    return model


def fit_final_development_model(
    development: Mapping[str, object],
    frozen_selection: Mapping[str, object],
    selected_result: Mapping[str, object],
    preregistration: Mapping[str, object],
) -> dict[str, object]:
    """Fit exactly one selected configuration on every development row."""
    _validate_development_dataset(development)
    require_qualification_eligibility(development.get('source_integrity'))
    if not _qualification_data_valid(development):
        raise ValueError('development dataset-integrity provenance mismatch')
    for value, hash_field, name in (
        (frozen_selection, "freeze_sha256", "selection"),
        (selected_result, "result_sha256", "selected result"),
        (preregistration, "manifest_sha256", "preregistration"),
    ):
        if not isinstance(value, Mapping) or value.get(hash_field) != _sha256(
            {key: item for key, item in value.items() if key != hash_field}
        ):
            raise ValueError(f"{name} hash is invalid")
    candidate = frozen_selection.get("candidate_configuration")
    if not (
        frozen_selection.get("schema") == "frozen-development-candidate/v2"
        and frozen_selection.get("candidate_id")
        == selected_result.get("candidate_id")
        and frozen_selection.get("development_result_sha256")
        == selected_result.get("result_sha256")
        and frozen_selection.get("development_artifact_sha256")
        == development.get("artifact_sha256")
        and frozen_selection.get("preregistration_sha256")
        == preregistration.get("manifest_sha256")
        and frozen_selection.get("development_accepted") is True
        and frozen_selection.get("new_model_fitted_after_selection") is False
        and selected_result.get("accepted") is True
        and selected_result.get("candidate_configuration") == candidate
        and isinstance(candidate, Mapping)
        and candidate in preregistration.get("candidates", [])
        and candidate.get("horizon_minutes") == development.get("horizon_minutes")
        and frozen_selection.get("confirmation_loaded") is False
        and frozen_selection.get("confirmation_evaluated") is False
        and frozen_selection.get("final_test_accessed") is False
    ):
        raise ValueError("selected development candidate provenance is invalid")
    feature_names = candidate.get("features")
    l2_penalty = candidate.get("l2_penalty")
    if not isinstance(feature_names, list) or not isinstance(l2_penalty, (int, float)):
        raise ValueError("selected candidate configuration is invalid")
    samples = development["samples"]
    model = fit_research_logistic(
        samples,
        feature_names=feature_names,
        l2_penalty=float(l2_penalty),
    )
    model.pop("model_sha256", None)
    model.update(
        {
            "artifact_role": "final_frozen_development_candidate",
            "source_integrity": development['source_integrity'],
            "source_dataset_sha256": development['source_dataset_sha256'],
            "symbol": development.get('symbol'),
            "exchange": development.get('exchange'),
            "paper_inference_start_ms": development['sealed_confirmation']['end_exclusive'],
            "candidate_id": frozen_selection["candidate_id"],
            "horizon_minutes": candidate["horizon_minutes"],
            "feature_schema_sha256": _sha256(feature_names),
            "development_artifact_sha256": development["artifact_sha256"],
            "development_sample_sha256": _sha256(samples),
            "development_samples_used": len(samples),
            "selection_freeze_sha256": frozen_selection["freeze_sha256"],
            "selected_result_sha256": selected_result["result_sha256"],
            "preregistration_sha256": preregistration["manifest_sha256"],
            "validation_rows_used_for_fitting": 0,
            "confirmation_rows_used_for_fitting": 0,
            "threshold_optimized": False,
            "features_tuned_after_selection": False,
            "hyperparameters_tuned_after_selection": False,
            "confirmation_loaded": False,
            "confirmation_evaluated": False,
            "final_test_accessed": False,
            "approved": False,
            "production_model": False,
            "model_promoted": False,
            "paper_orders": False,
            "live_orders": False,
        }
    )
    model["model_sha256"] = _sha256(model)
    return model


def confirmation_evaluation_phrase(
    model: Mapping[str, object], confirmation_descriptor: Mapping[str, object]
) -> str:
    """Bind future one-time confirmation authorization to model and artifact."""
    if not (
        isinstance(model, Mapping)
        and model.get("artifact_role") == "final_frozen_development_candidate"
        and model.get("model_sha256")
        == _sha256({key: value for key, value in model.items() if key != "model_sha256"})
        and model.get("confirmation_loaded") is False
        and model.get("confirmation_evaluated") is False
        and model.get("final_test_accessed") is False
        and isinstance(confirmation_descriptor, Mapping)
        and confirmation_descriptor.get("sealed") is True
        and confirmation_descriptor.get("evaluated") is False
        and _is_sha256(confirmation_descriptor.get("artifact_sha256"))
    ):
        raise ValueError("model or sealed confirmation descriptor is invalid")
    return (
        "AUTHORIZE V2 CONFIRMATION "
        f"{model['model_sha256']} {confirmation_descriptor['artifact_sha256']}"
    )


def evaluate_frozen_confirmation(
    model: Mapping[str, object],
    development: Mapping[str, object],
    confirmation: Mapping[str, object],
    selected_result: Mapping[str, object],
    preregistration: Mapping[str, object],
    authorization: str,
    *,
    bootstrap_iterations: int = 500,
) -> dict[str, object]:
    """Consume the sealed confirmation once after exact bound authorization."""
    _validate_development_dataset(development)
    descriptor = development["sealed_confirmation"]
    if authorization != confirmation_evaluation_phrase(model, descriptor):
        raise ValueError("confirmation authorization phrase does not exactly match")
    for value, hash_field, name in (
        (selected_result, "result_sha256", "selected result"),
        (preregistration, "manifest_sha256", "preregistration"),
        (confirmation, "artifact_sha256", "confirmation"),
    ):
        if not isinstance(value, Mapping) or value.get(hash_field) != _sha256(
            {key: item for key, item in value.items() if key != hash_field}
        ):
            raise ValueError(f"{name} hash is invalid")
    samples = confirmation.get("samples")
    if not (
        model.get("artifact_role") == "final_frozen_development_candidate"
        and model.get("model_sha256")
        == _sha256({key: value for key, value in model.items() if key != "model_sha256"})
        and model.get("development_artifact_sha256")
        == development.get("artifact_sha256")
        and model.get("selected_result_sha256")
        == selected_result.get("result_sha256")
        and model.get("preregistration_sha256")
        == preregistration.get("manifest_sha256")
        and selected_result.get("accepted") is True
        and confirmation.get("schema") == "sealed-direction-confirmation/v2"
        and confirmation.get("artifact_sha256") == descriptor.get("artifact_sha256")
        and confirmation.get("sample_sha256") == descriptor.get("sample_sha256")
        and confirmation.get("sample_count") == descriptor.get("sample_count")
        and confirmation.get("start") == descriptor.get("start")
        and confirmation.get("end_exclusive") == descriptor.get("end_exclusive")
        and confirmation.get("sealed") is True
        and confirmation.get("evaluated") is False
        and confirmation.get("final_test_accessed") is False
        and isinstance(samples, list)
        and len(samples) == descriptor.get("sample_count")
        and _sha256(samples) == descriptor.get("sample_sha256")
        and model.get("confirmation_loaded") is False
        and model.get("confirmation_evaluated") is False
        and model.get("final_test_accessed") is False
    ):
        raise ValueError("frozen model or confirmation provenance is invalid")
    if any(
        not isinstance(sample, Mapping)
        or sample.get("feature_open_time", 0) < descriptor["start"]
        or sample.get("label_close_time", descriptor["end_exclusive"] + 1)
        > descriptor["end_exclusive"]
        for sample in samples
    ):
        raise ValueError("confirmation sample crosses its sealed boundary")
    features = model.get("features")
    if not isinstance(features, list):
        raise ValueError("frozen model feature schema is invalid")
    rows, labels = _matrix(samples, tuple(features))
    probabilities = predict_logistic_probabilities(model, rows)
    metrics = binary_classification_metrics(labels, probabilities)
    baseline_values = baseline_probabilities(
        [int(sample["label"]) for sample in development["samples"]],
        samples,
        horizon_minutes=int(model["horizon_minutes"]),
    )
    baselines = {
        name: binary_classification_metrics(labels, values)
        for name, values in baseline_values.items()
    }
    block_size = max(2, 1440 // int(model["horizon_minutes"]))
    confidence = block_bootstrap_confidence_intervals(
        labels,
        probabilities,
        block_size=min(block_size, len(labels)),
        iterations=bootstrap_iterations,
        seed=0,
    )
    baseline_confidence = {
        name: block_bootstrap_confidence_intervals(
            labels,
            values,
            block_size=min(block_size, len(labels)),
            iterations=bootstrap_iterations,
            seed=0,
        )
        for name, values in baseline_values.items()
    }
    development_criteria = selected_result.get("acceptance_criteria")
    if not isinstance(development_criteria, Mapping):
        raise ValueError("selected result acceptance criteria are missing")
    combined_criteria = {
        "required_six_folds": development_criteria.get("required_six_folds") is True,
        "mean_walk_forward_roc_auc_at_least_0_52": (
            development_criteria.get("mean_walk_forward_roc_auc_at_least_0_52") is True
        ),
        "at_least_five_folds_above_random": (
            development_criteria.get("at_least_five_folds_above_random") is True
        ),
        "confirmation_roc_auc_ci_lower_above_0_5": (
            confidence["roc_auc_95_interval"][0] > 0.5
        ),
        "confirmation_average_precision_above_prevalence": (
            metrics["average_precision"] > metrics["prevalence_baseline"]
        ),
        "confirmation_brier_beats_constant_prevalence": (
            metrics["brier_score"] < baselines["constant_prevalence"]["brier_score"]
        ),
        "no_prediction_collapse": (
            development_criteria.get("no_prediction_collapse") is True
            and 0.1 <= metrics["prediction_distribution"]["positive_rate"] <= 0.9
        ),
    }
    evidence: dict[str, object] = {
        "schema": "sealed-confirmation-evaluation/v2",
        "model_sha256": model["model_sha256"],
        "development_artifact_sha256": development["artifact_sha256"],
        "confirmation_artifact_sha256": confirmation["artifact_sha256"],
        "confirmation_sample_sha256": confirmation["sample_sha256"],
        "samples": len(samples),
        "metrics": metrics,
        "confidence_intervals": confidence,
        "baselines": baselines,
        "baseline_confidence_intervals": baseline_confidence,
        "bootstrap": {
            "method": "moving_block",
            "block_size_samples": block_size,
            "iterations": bootstrap_iterations,
            "seed": 0,
        },
        "development_acceptance_criteria": dict(development_criteria),
        "combined_acceptance_criteria": combined_criteria,
        "accepted": all(combined_criteria.values()),
        "prediction_sha256": _sha256(probabilities),
        "authorization_sha256": hashlib.sha256(authorization.encode()).hexdigest(),
        "model_retrained": False,
        "preprocessing_refit": False,
        "threshold_optimized": False,
        "features_selected_on_confirmation": False,
        "confirmation_evaluated_once": True,
        "final_test_accessed": False,
        "paper_orders": False,
        "live_orders": False,
    }
    evidence["evidence_sha256"] = _sha256(evidence)
    return evidence


def run_walk_forward_candidate(
    development: Mapping[str, object],
    walk_forward: Mapping[str, object],
    candidate: Mapping[str, object],
    *,
    preregistration_sha256: str,
    bootstrap_iterations: int = 500,
) -> dict[str, object]:
    """Fit and score one preregistered candidate on development folds only."""
    _validate_development_dataset(development)
    if not _is_sha256(preregistration_sha256):
        raise ValueError("preregistration hash is invalid")
    if not isinstance(walk_forward, Mapping) or not (
        walk_forward.get("schema") == "direction-walk-forward/v2"
        and walk_forward.get("development_artifact_sha256")
        == development.get("artifact_sha256")
        and walk_forward.get("manifest_sha256")
        == _sha256(
            {
                key: value
                for key, value in walk_forward.items()
                if key != "manifest_sha256"
            }
        )
        and walk_forward.get("final_test") == {
            "sealed": True,
            "accessed": False,
            "included": False,
        }
    ):
        raise ValueError("walk-forward manifest is invalid or mismatched")
    horizon = candidate.get("horizon_minutes")
    feature_set = candidate.get("feature_set")
    feature_names = candidate.get("features")
    l2_penalty = candidate.get("l2_penalty")
    if not (
        horizon == development.get("horizon_minutes")
        and horizon in HORIZONS
        and isinstance(feature_set, str)
        and isinstance(feature_names, list)
        and feature_names
        and all(name in FEATURE_NAMES for name in feature_names)
        and isinstance(l2_penalty, (int, float))
        and not isinstance(l2_penalty, bool)
        and l2_penalty >= 0
    ):
        raise ValueError("candidate configuration is invalid or mismatched")
    candidate_id = (
        f"h{horizon:02d}-{feature_set}-l2-"
        f"{format(float(l2_penalty), '.4g')}"
    )
    samples = development["samples"]
    folds = walk_forward.get("folds")
    if not isinstance(folds, list) or not folds:
        raise ValueError("walk-forward folds are missing")
    feature_hash = _sha256(feature_names)
    fold_results = []
    pooled_labels: list[int] = []
    pooled_probabilities: list[float] = []
    pooled_baselines = {
        name: []
        for name in ("majority", "constant_prevalence", "momentum", "contrarian")
    }
    block_size = max(2, 1440 // horizon)
    for fold in folds:
        if not isinstance(fold, Mapping):
            raise ValueError("walk-forward fold is invalid")
        training = _samples_in_range(
            samples, fold["training_start"], fold["training_end_exclusive"]
        )
        validation = _samples_in_range(
            samples, fold["validation_start"], fold["validation_end_exclusive"]
        )
        if not (
            len(training) == fold.get("training_samples")
            and len(validation) == fold.get("validation_samples")
            and _sha256(training) == fold.get("training_sample_sha256")
            and _sha256(validation) == fold.get("validation_sample_sha256")
        ):
            raise ValueError("walk-forward fold rows do not match their hashes")
        model = fit_research_logistic(
            training,
            feature_names=feature_names,
            l2_penalty=float(l2_penalty),
        )
        model.pop("model_sha256", None)
        model.update(
            {
                "candidate_id": candidate_id,
                "horizon_minutes": horizon,
                "fold_id": fold["fold_id"],
                "feature_schema_sha256": feature_hash,
                "development_artifact_sha256": development["artifact_sha256"],
                "walk_forward_manifest_sha256": walk_forward["manifest_sha256"],
                "preregistration_sha256": preregistration_sha256,
                "validation_rows_used_for_fitting": 0,
            }
        )
        model["model_sha256"] = _sha256(model)
        validation_rows, labels = _matrix(validation, tuple(feature_names))
        probabilities = predict_logistic_probabilities(model, validation_rows)
        metrics = binary_classification_metrics(labels, probabilities)
        baseline_values = baseline_probabilities(
            [int(sample["label"]) for sample in training],
            validation,
            horizon_minutes=horizon,
        )
        baseline_metrics = {
            name: binary_classification_metrics(labels, values)
            for name, values in baseline_values.items()
        }
        pooled_labels.extend(labels)
        pooled_probabilities.extend(probabilities)
        for name, values in baseline_values.items():
            pooled_baselines[name].extend(values)
        fold_results.append(
            {
                "fold_id": fold["fold_id"],
                "training_start": fold["training_start"],
                "training_end_exclusive": fold["training_end_exclusive"],
                "validation_start": fold["validation_start"],
                "validation_end_exclusive": fold["validation_end_exclusive"],
                "training_samples": len(training),
                "validation_samples": len(validation),
                "training_sample_sha256": fold["training_sample_sha256"],
                "validation_sample_sha256": fold["validation_sample_sha256"],
                "model": model,
                "prediction_sha256": _sha256(probabilities),
                "metrics": metrics,
                "baselines": baseline_metrics,
            }
        )
    pooled_metrics = binary_classification_metrics(
        pooled_labels, pooled_probabilities
    )
    pooled_bootstrap = block_bootstrap_confidence_intervals(
        pooled_labels,
        pooled_probabilities,
        block_size=min(block_size, len(pooled_labels)),
        iterations=bootstrap_iterations,
        seed=0,
    )
    pooled_baseline_metrics = {
        name: binary_classification_metrics(pooled_labels, values)
        for name, values in pooled_baselines.items()
    }
    pooled_baseline_bootstrap = {
        name: block_bootstrap_confidence_intervals(
            pooled_labels,
            values,
            block_size=min(block_size, len(pooled_labels)),
            iterations=bootstrap_iterations,
            seed=0,
        )
        for name, values in pooled_baselines.items()
    }
    fold_aucs = [fold["metrics"]["roc_auc"] for fold in fold_results]
    mean_auc = statistics.fmean(
        value for value in fold_aucs if isinstance(value, float)
    )
    folds_above_random = sum(
        isinstance(value, float) and value > 0.5 for value in fold_aucs
    )
    positive_rates = [
        fold["metrics"]["prediction_distribution"]["positive_rate"]
        for fold in fold_results
    ] + [pooled_metrics["prediction_distribution"]["positive_rate"]]
    criteria = {
        "required_six_folds": len(fold_results) == 6,
        "mean_walk_forward_roc_auc_at_least_0_52": mean_auc >= 0.52,
        "at_least_five_folds_above_random": folds_above_random >= 5,
        "pooled_roc_auc_ci_lower_above_0_5": (
            pooled_bootstrap["roc_auc_95_interval"][0] > 0.5
        ),
        "pooled_average_precision_above_prevalence": (
            pooled_metrics["average_precision"]
            > pooled_metrics["prevalence_baseline"]
        ),
        "pooled_brier_beats_constant_prevalence": (
            pooled_metrics["brier_score"]
            < pooled_baseline_metrics["constant_prevalence"]["brier_score"]
        ),
        "no_prediction_collapse": all(0.1 <= rate <= 0.9 for rate in positive_rates),
    }
    result: dict[str, object] = {
        "schema": "development-walk-forward-candidate/v2",
        "candidate_id": candidate_id,
        "candidate_configuration": dict(candidate),
        "feature_schema_sha256": feature_hash,
        "development_artifact_sha256": development["artifact_sha256"],
        "walk_forward_manifest_sha256": walk_forward["manifest_sha256"],
        "preregistration_sha256": preregistration_sha256,
        "threshold": 0.5,
        "threshold_optimized": False,
        "bootstrap": {
            "method": "moving_block",
            "block_size_samples": block_size,
            "iterations": bootstrap_iterations,
            "seed": 0,
        },
        "prediction_collapse_definition": (
            "positive prediction rate outside inclusive [0.1, 0.9] in any fold or pooled"
        ),
        "folds": fold_results,
        "fold_roc_auc": fold_aucs,
        "mean_fold_roc_auc": round(mean_auc, 6),
        "folds_above_random": folds_above_random,
        "pooled": {
            "metrics": pooled_metrics,
            "confidence_intervals": pooled_bootstrap,
            "baselines": pooled_baseline_metrics,
            "baseline_confidence_intervals": pooled_baseline_bootstrap,
            "prediction_sha256": _sha256(pooled_probabilities),
        },
        "acceptance_criteria": criteria,
        "source_integrity": development.get('source_integrity'),
        "accepted": all(criteria.values()) and _qualification_data_valid(development),
        "model_promoted": False,
        "confirmation_loaded": False,
        "confirmation_evaluated": False,
        "final_test_accessed": False,
        "paper_orders": False,
        "live_orders": False,
    }
    result["result_sha256"] = _sha256(result)
    return result


def _qualification_data_valid(development):
    try:
        evidence=development.get('source_integrity')
        require_qualification_eligibility(evidence)
        return evidence['dataset_sha256']==development.get('source_dataset_sha256')
    except ValueError:
        return False


def evaluate_research_model(
    model: Mapping[str, object], samples: Sequence[Mapping[str, object]]
) -> dict[str, object]:
    """Evaluate a frozen research model at its fixed 0.5 threshold."""
    features = model.get("features")
    if not isinstance(features, list) or not all(isinstance(item, str) for item in features):
        raise ValueError("research model feature schema is invalid")
    rows, labels = _matrix(samples, tuple(features))
    probabilities = predict_logistic_probabilities(model, rows)
    return binary_classification_metrics(labels, probabilities)


def baseline_probabilities(
    training_labels: Sequence[int],
    samples: Sequence[Mapping[str, object]],
    *,
    horizon_minutes: int,
) -> dict[str, list[float]]:
    """Return fixed, non-learned comparison probabilities."""
    if horizon_minutes not in HORIZONS or not training_labels:
        raise ValueError("baseline inputs are invalid")
    prevalence = sum(training_labels) / len(training_labels)
    majority = float(prevalence >= 0.5)
    return_name = f"return_{horizon_minutes}"
    momentum = []
    for sample in samples:
        features = sample.get("features")
        if not isinstance(features, Mapping) or return_name not in features:
            raise ValueError(f"samples are missing {return_name}")
        momentum.append(float(float(features[return_name]) > 0))
    return {
        "majority": [majority] * len(samples),
        "constant_prevalence": [prevalence] * len(samples),
        "momentum": momentum,
        "contrarian": [1.0 - value for value in momentum],
    }


def binary_classification_metrics(
    labels: Sequence[int], probabilities: Sequence[float], *, threshold: float = 0.5
) -> dict[str, object]:
    """Classification, ranking and calibration metrics without threshold tuning."""
    checked_labels = list(labels)
    checked_probabilities = [float(value) for value in probabilities]
    if (
        not checked_labels
        or len(checked_labels) != len(checked_probabilities)
        or any(label not in {0, 1} for label in checked_labels)
        or any(not math.isfinite(value) or not 0 <= value <= 1 for value in checked_probabilities)
        or not 0 < threshold < 1
    ):
        raise ValueError("classification metrics inputs are invalid")
    predictions = [int(value >= threshold) for value in checked_probabilities]
    tp = sum(a == b == 1 for a, b in zip(checked_labels, predictions))
    tn = sum(a == b == 0 for a, b in zip(checked_labels, predictions))
    fp = sum(a == 0 and b == 1 for a, b in zip(checked_labels, predictions))
    fn = sum(a == 1 and b == 0 for a, b in zip(checked_labels, predictions))
    positives = tp + fn
    negatives = tn + fp
    precision = tp / (tp + fp) if tp + fp else 0.0
    recall = tp / positives if positives else 0.0
    specificity = tn / negatives if negatives else 0.0
    f1 = 2 * precision * recall / (precision + recall) if precision + recall else 0.0
    ordered = sorted(checked_probabilities)
    return {
        "samples": len(checked_labels),
        "class_distribution": {"down_or_flat": negatives, "up": positives},
        "prediction_distribution": {
            "down_or_flat": len(predictions) - sum(predictions),
            "up": sum(predictions),
            "positive_rate": round(sum(predictions) / len(predictions), 6),
            "minimum_probability": round(ordered[0], 6),
            "median_probability": round(_quantile(ordered, 0.5), 6),
            "maximum_probability": round(ordered[-1], 6),
            "mean_probability": round(statistics.fmean(ordered), 6),
        },
        "accuracy": round((tp + tn) / len(checked_labels), 6),
        "balanced_accuracy": round((recall + specificity) / 2, 6),
        "precision": round(precision, 6),
        "recall": round(recall, 6),
        "f1": round(f1, 6),
        "roc_auc": None if not positives or not negatives else round(
            _roc_auc(checked_labels, checked_probabilities), 6
        ),
        "average_precision": None if not positives else round(
            _average_precision(checked_labels, checked_probabilities), 6
        ),
        "prevalence_baseline": round(positives / len(checked_labels), 6),
        "brier_score": round(
            statistics.fmean(
                (probability - label) ** 2
                for probability, label in zip(checked_probabilities, checked_labels)
            ),
            6,
        ),
        "confusion_matrix": {"tn": tn, "fp": fp, "fn": fn, "tp": tp},
        "threshold": threshold,
        "threshold_optimized": False,
    }


def block_bootstrap_confidence_intervals(
    labels: Sequence[int],
    probabilities: Sequence[float],
    *,
    block_size: int,
    iterations: int = 500,
    seed: int = 0,
) -> dict[str, object]:
    """Deterministic moving-block intervals for ROC-AUC and Brier score."""
    labels = list(labels)
    probabilities = list(probabilities)
    binary_classification_metrics(labels, probabilities)
    if block_size < 1 or block_size > len(labels) or iterations < 20:
        raise ValueError("block bootstrap configuration is invalid")
    rng = random.Random(seed)
    aucs = []
    briers = []
    for _ in range(iterations):
        indexes = []
        while len(indexes) < len(labels):
            start = rng.randrange(len(labels))
            indexes.extend((start + offset) % len(labels) for offset in range(block_size))
        indexes = indexes[: len(labels)]
        sampled_labels = [labels[index] for index in indexes]
        sampled_probabilities = [probabilities[index] for index in indexes]
        if len(set(sampled_labels)) == 2:
            aucs.append(_roc_auc(sampled_labels, sampled_probabilities))
        briers.append(
            statistics.fmean(
                (sampled_probabilities[index] - sampled_labels[index]) ** 2
                for index in range(len(sampled_labels))
            )
        )
    if not aucs:
        raise ValueError("bootstrap samples never contained both classes")
    aucs.sort()
    briers.sort()
    return {
        "schema": "moving-block-bootstrap/v1",
        "block_size": block_size,
        "iterations": iterations,
        "seed": seed,
        "roc_auc_95_interval": [
            round(_quantile(aucs, 0.025), 6),
            round(_quantile(aucs, 0.975), 6),
        ],
        "brier_score_95_interval": [
            round(_quantile(briers, 0.025), 6),
            round(_quantile(briers, 0.975), 6),
        ],
    }


def _segment_samples(
    segment: list[dict[str, object]], segment_id: int, horizon: int
) -> list[dict[str, object]]:
    output = []
    first_index = CONTEXT_CANDLES - 1
    for index in range(first_index, len(segment) - horizon):
        open_time = int(segment[index]["open_time"])
        if (open_time // MINUTE_MS) % horizon:
            continue
        context = segment[index - first_index : index + 1]
        features = _features(context)
        if features is None:
            continue
        following = segment[index + horizon]
        close = Decimal(str(segment[index]["close"]))
        future_close = Decimal(str(following["close"]))
        output.append(
            {
                "segment_id": segment_id,
                "feature_window_start_open_time": context[0]["open_time"],
                "feature_open_time": open_time,
                "feature_close_time": segment[index]["close_time"],
                "label_open_time": segment[index + 1]["open_time"],
                "label_close_time": following["close_time"],
                "horizon_minutes": horizon,
                "features": features,
                "future_return": float(future_close / close - 1),
                "label": int(future_close > close),
            }
        )
    return output


def _features(context: list[dict[str, object]]) -> dict[str, float] | None:
    closes = [float(row["close"]) for row in context]
    volumes = [float(row["volume"]) for row in context]
    current = context[-1]
    output = {
        f"return_{period}": closes[-1] / closes[-period - 1] - 1
        for period in (1, 5, 15, 30, 60)
    }
    for period in (5, 15):
        recent = sum(volumes[-period:])
        prior = sum(volumes[-period - 60 : -period])
        if prior == 0:
            return None
        output[f"volume_ratio_{period}"] = recent / (prior * period / 60)
    returns = [later / earlier - 1 for earlier, later in zip(closes, closes[1:])]
    volatility_15 = statistics.pstdev(returns[-15:])
    volatility_60 = statistics.pstdev(returns[-60:])
    output.update(
        {
            "volatility_15": volatility_15,
            "volatility_60": volatility_60,
            "volatility_ratio": volatility_15 / volatility_60 if volatility_60 else 0.0,
        }
    )
    ema_5 = _ema(closes, 5)
    ema_20 = _ema(closes, 20)
    ema_60 = _ema(closes, 60)
    ema_gap_5_20 = ema_5 / ema_20 - 1
    ema_gap_20_60 = ema_20 / ema_60 - 1
    output.update(
        {
            "ema_gap_5_20": ema_gap_5_20,
            "ema_gap_20_60": ema_gap_20_60,
            "trend_strength": ema_gap_20_60 / volatility_60 if volatility_60 else 0.0,
        }
    )
    open_ = float(current["open"])
    high = float(current["high"])
    low = float(current["low"])
    close = float(current["close"])
    candle_range = high - low
    output["range_fraction"] = candle_range / close
    if candle_range:
        output.update(
            {
                "signed_body_fraction": (close - open_) / candle_range,
                "upper_wick_fraction": (high - max(open_, close)) / candle_range,
                "lower_wick_fraction": (min(open_, close) - low) / candle_range,
                "zero_range": 0.0,
            }
        )
    else:
        output.update(
            {
                "signed_body_fraction": 0.0,
                "upper_wick_fraction": 0.0,
                "lower_wick_fraction": 0.0,
                "zero_range": 1.0,
            }
        )
    if set(output) != set(FEATURE_NAMES) or any(not math.isfinite(value) for value in output.values()):
        raise ValueError("causal feature generation produced an invalid row")
    return {name: output[name] for name in FEATURE_NAMES}


def features_from_closed_candles(candles, *, symbol):
    """Use exactly the offline 240-bar feature context, with no gap filling."""
    if len(candles) < CONTEXT_CANDLES:
        return None
    context = candles[-CONTEXT_CANDLES:]
    for index, candle in enumerate(context):
        if (candle.symbol != symbol or candle.interval_seconds != 60
                or candle.open_time.second or candle.open_time.microsecond):
            return None
        if index and context[index-1].close_time != candle.open_time:
            return None
    rows = [{name:str(getattr(candle, name).amount) for name in
             ('open','high','low','close','volume')} for candle in context]
    return _features(rows)


def _ema(values: list[float], period: int) -> float:
    value = statistics.fmean(values[:period])
    multiplier = 2 / (period + 1)
    for current in values[period:]:
        value = (current - value) * multiplier + value
    return value


def _parse_candle(value: object) -> dict[str, object]:
    if not isinstance(value, Mapping):
        raise ValueError("every source candle must be an object")
    open_time = value.get("open_time")
    close_time = value.get("close_time")
    interval = value.get("interval_seconds")
    if (
        isinstance(open_time, bool)
        or not isinstance(open_time, int)
        or interval != 60
        or close_time != open_time + MINUTE_MS
    ):
        raise ValueError("research candles must be closed, aligned one-minute rows")
    try:
        open_, high, low, close, volume = (
            Decimal(str(value[name]))
            for name in ("open", "high", "low", "close", "volume")
        )
    except (KeyError, InvalidOperation) as exc:
        raise ValueError("research candle contains invalid OHLCV") from exc
    if (
        any(not item.is_finite() or item <= 0 for item in (open_, high, low, close))
        or not volume.is_finite()
        or volume < 0
        or low > min(open_, close)
        or high < max(open_, close)
    ):
        raise ValueError("research candle contains inconsistent OHLCV")
    return {
        "open_time": open_time,
        "close_time": close_time,
        "open": str(open_),
        "high": str(high),
        "low": str(low),
        "close": str(close),
        "volume": str(volume),
    }


def _contiguous_segments(rows: list[dict[str, object]]) -> list[list[dict[str, object]]]:
    segments: list[list[dict[str, object]]] = []
    for row in rows:
        if segments and int(row["open_time"]) <= int(segments[-1][-1]["open_time"]):
            raise ValueError("source candles must be strictly chronological and unique")
        if not segments or int(row["open_time"]) != int(segments[-1][-1]["open_time"]) + MINUTE_MS:
            segments.append([])
        segments[-1].append(row)
    return segments


def _validate_source(source: Mapping[str, object]) -> None:
    if not isinstance(source, Mapping) or source.get("schema") != "coinswitch-public-candles/v1":
        raise ValueError("unsupported research source dataset")
    if not (
        source.get("public_data_only") is True
        and isinstance(source.get("credentials_used"), bool)
        and source.get("live_trading") is False
        and source.get("training_only") is True
        and isinstance(source.get("symbol"), str)
        and isinstance(source.get("exchange"), str)
    ):
        raise ValueError("research source safety markers are invalid")
    validation = validate_candle_dataset(source)
    allowed = {
        "stored integrity report did not pass",
    }
    unexpected = [
        failure
        for failure in validation.failures
        if failure not in allowed
        and not (
            failure.startswith("dataset contains ")
            and failure.endswith(" candle gap(s)")
        )
    ]
    if unexpected:
        raise ValueError(
            "research source integrity failed: " + "; ".join(unexpected)
        )


def _validate_causal_dataset(dataset: Mapping[str, object]) -> None:
    if not isinstance(dataset, Mapping) or dataset.get("schema") != "causal-direction-dataset/v2":
        raise ValueError("unsupported causal dataset")
    if not (
        dataset.get("feature_schema") == FEATURE_SCHEMA
        and dataset.get("feature_names") == list(FEATURE_NAMES)
        and dataset.get("preprocessing_fitted") is False
        and dataset.get("model_fitted") is False
        and dataset.get("confirmation_evaluated") is False
        and dataset.get("final_test_accessed") is False
        and _is_sha256(dataset.get("dataset_sha256"))
        and dataset.get("dataset_sha256")
        == _sha256({key: value for key, value in dataset.items() if key != "dataset_sha256"})
        and isinstance(dataset.get("samples"), list)
    ):
        raise ValueError("causal dataset provenance or safety markers are invalid")


def _validate_development_dataset(dataset: Mapping[str, object]) -> None:
    if not isinstance(dataset, Mapping) or dataset.get("schema") != "direction-development-dataset/v2":
        raise ValueError("walk-forward generation requires a development artifact")
    if not (
        dataset.get("feature_schema") == FEATURE_SCHEMA
        and dataset.get("feature_names") == list(FEATURE_NAMES)
        and dataset.get("preprocessing_fitted") is False
        and dataset.get("model_fitted") is False
        and dataset.get("final_test_accessed") is False
        and _is_sha256(dataset.get("artifact_sha256"))
        and dataset.get("artifact_sha256")
        == _sha256({key: value for key, value in dataset.items() if key != "artifact_sha256"})
        and isinstance(dataset.get("samples"), list)
        and isinstance(dataset.get("sealed_confirmation"), Mapping)
    ):
        raise ValueError("development artifact provenance or safety markers are invalid")


def _samples_in_range(samples: object, start: int, end: int) -> list[object]:
    if not isinstance(samples, list):
        raise ValueError("causal samples are missing")
    return [
        sample
        for sample in samples
        if isinstance(sample, Mapping)
        and isinstance(sample.get("feature_open_time"), int)
        and isinstance(sample.get("label_close_time"), int)
        and sample["feature_open_time"] >= start
        and sample["label_close_time"] <= end
    ]


def _matrix(
    samples: Sequence[Mapping[str, object]], feature_names: tuple[str, ...]
) -> tuple[list[list[float]], list[int]]:
    rows = []
    labels = []
    for sample in samples:
        features = sample.get("features")
        label = sample.get("label")
        if not isinstance(features, Mapping) or label not in {0, 1}:
            raise ValueError("research sample is invalid")
        try:
            row = [float(features[name]) for name in feature_names]
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError("research sample does not match the feature schema") from exc
        if any(not math.isfinite(value) for value in row):
            raise ValueError("research sample contains a non-finite feature")
        rows.append(row)
        labels.append(int(label))
    if not rows:
        raise ValueError("research sample selection is empty")
    return rows, labels


def _roc_auc(labels: Sequence[int], probabilities: Sequence[float]) -> float:
    ranked = sorted(zip(probabilities, labels), key=lambda item: item[0])
    positive_rank_sum = 0.0
    index = 0
    while index < len(ranked):
        end = index + 1
        while end < len(ranked) and ranked[end][0] == ranked[index][0]:
            end += 1
        positive_rank_sum += (((index + 1) + end) / 2) * sum(
            label for _, label in ranked[index:end]
        )
        index = end
    positives = sum(labels)
    negatives = len(labels) - positives
    return (positive_rank_sum - positives * (positives + 1) / 2) / (positives * negatives)


def _average_precision(labels: Sequence[int], probabilities: Sequence[float]) -> float:
    ranked = sorted(zip(probabilities, labels), key=lambda item: item[0], reverse=True)
    positives = sum(labels)
    true_positives = 0
    examined = 0
    previous_recall = 0.0
    area = 0.0
    index = 0
    while index < len(ranked):
        end = index + 1
        while end < len(ranked) and ranked[end][0] == ranked[index][0]:
            end += 1
        true_positives += sum(label for _, label in ranked[index:end])
        examined = end
        recall = true_positives / positives
        area += (recall - previous_recall) * (true_positives / examined)
        previous_recall = recall
        index = end
    return area


def _quantile(ordered: Sequence[float], fraction: float) -> float:
    position = (len(ordered) - 1) * fraction
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return ordered[lower]
    weight = position - lower
    return ordered[lower] * (1 - weight) + ordered[upper] * weight


def _sha256(value: object) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def _is_sha256(value: object) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(character in "0123456789abcdef" for character in value)
    )
