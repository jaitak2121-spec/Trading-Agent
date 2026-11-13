"""Offline, paper-only training of a small next-candle direction model."""

from __future__ import annotations

import hashlib
import json
import math
import platform
import statistics
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Mapping

from .historical_candles import validate_candle_dataset

__all__ = [
    "DirectionModelEvaluationReport",
    "DirectionModelTrainingReport",
    "diagnose_direction_model_robustness",
    "direction_model_sha256",
    "evaluate_direction_model",
    "fit_logistic_classifier",
    "paper_model_approval_phrase",
    "predict_logistic_probabilities",
    "predict_direction_probability",
    "split_direction_datasets",
    "train_direction_model",
    "train_validated_direction_model",
    "validate_paper_model_trial",
]

_FEATURES = ("return_1", "return_3", "range_fraction")
_TRAINING_FRACTION = 0.7
_ITERATIONS = 400
_LEARNING_RATE = 0.1
_L2_PENALTY = 0.001
_THRESHOLD = 0.5


def fit_logistic_classifier(
    rows: list[list[float]],
    labels: list[int],
    *,
    feature_names: tuple[str, ...],
    l2_penalty: float,
    iterations: int = _ITERATIONS,
    learning_rate: float = _LEARNING_RATE,
) -> dict[str, object]:
    """Fit deterministic logistic regression with training-only scaling."""
    if not rows or len(rows) != len(labels):
        raise ValueError("logistic training requires aligned non-empty rows and labels")
    if not feature_names or len(set(feature_names)) != len(feature_names):
        raise ValueError("feature names must be non-empty and unique")
    if any(len(row) != len(feature_names) for row in rows):
        raise ValueError("every row must match the feature schema")
    if set(labels) != {0, 1}:
        raise ValueError("logistic training labels must contain both classes")
    if iterations < 1 or learning_rate <= 0 or l2_penalty < 0:
        raise ValueError("logistic configuration is invalid")
    checked = [
        [_finite_float(value, "feature") for value in row]
        for row in rows
    ]
    columns = list(zip(*checked))
    means = [statistics.fmean(column) for column in columns]
    scales = [statistics.pstdev(column) or 1.0 for column in columns]
    standardized = [_standardize(row, means, scales) for row in checked]
    weights = [0.0] * len(feature_names)
    bias = 0.0
    for _ in range(iterations):
        gradients = [0.0] * len(weights)
        bias_gradient = 0.0
        for row, label in zip(standardized, labels):
            error = _sigmoid(bias + sum(w * x for w, x in zip(weights, row))) - label
            bias_gradient += error
            for index, value in enumerate(row):
                gradients[index] += error * value
        count = len(rows)
        bias -= learning_rate * bias_gradient / count
        for index in range(len(weights)):
            weights[index] -= learning_rate * (
                gradients[index] / count + l2_penalty * weights[index]
            )
    return {
        "schema": "offline-logistic-classifier/v2",
        "model_type": "logistic_regression",
        "features": list(feature_names),
        "means": means,
        "scales": scales,
        "weights": weights,
        "bias": bias,
        "threshold": _THRESHOLD,
        "training_samples": len(rows),
        "configuration": {
            "iterations": iterations,
            "learning_rate": learning_rate,
            "l2_penalty": l2_penalty,
            "random_seed": 0,
            "randomness_used": False,
            "preprocessing_fit": "training_rows_only",
        },
    }


def predict_logistic_probabilities(
    model: Mapping[str, object], rows: list[list[float]]
) -> list[float]:
    """Predict with a generalized frozen logistic classifier."""
    if model.get("schema") != "offline-logistic-classifier/v2":
        raise ValueError("unsupported generalized logistic model schema")
    features = model.get("features")
    if not isinstance(features, list) or not features:
        raise ValueError("generalized logistic feature schema is invalid")
    vectors = []
    for name in ("means", "scales", "weights"):
        values = model.get(name)
        if not isinstance(values, list) or len(values) != len(features):
            raise ValueError(f"generalized logistic {name} vector is invalid")
        vectors.append([_finite_float(value, name) for value in values])
    means, scales, weights = vectors
    if any(value <= 0 for value in scales):
        raise ValueError("generalized logistic scales must be positive")
    bias = _finite_float(model.get("bias"), "bias")
    output = []
    for row in rows:
        if len(row) != len(features):
            raise ValueError("prediction row does not match the feature schema")
        checked = [_finite_float(value, "feature") for value in row]
        standardized = _standardize(checked, means, scales)
        output.append(
            _sigmoid(bias + sum(w * x for w, x in zip(weights, standardized)))
        )
    return output


@dataclass(frozen=True, slots=True)
class DirectionModelTrainingReport:
    model: dict[str, object]
    training_samples: int
    validation_samples: int
    validation_accuracy: float
    baseline_accuracy: float

    @property
    def eligible_for_paper_validation(self) -> bool:
        return self.validation_accuracy >= self.baseline_accuracy + 0.02

    def as_details(self) -> dict[str, object]:
        return {
            "training_samples": self.training_samples,
            "validation_samples": self.validation_samples,
            "validation_accuracy": round(self.validation_accuracy, 6),
            "baseline_accuracy": round(self.baseline_accuracy, 6),
            "eligible_for_paper_validation": self.eligible_for_paper_validation,
            "approved": False,
            "paper_only": True,
        }

    def render(self) -> str:
        return "\n".join(
            (
                "offline direction-model training: COMPLETE",
                "==========================================",
                f"samples    : {self.training_samples} train, "
                f"{self.validation_samples} validation",
                f"accuracy   : {self.validation_accuracy:.3f}",
                f"baseline   : {self.baseline_accuracy:.3f}",
                "candidate   : "
                + ("paper validation" if self.eligible_for_paper_validation else "rejected"),
                "approval    : unapproved; paper only",
            )
        )


@dataclass(frozen=True, slots=True)
class DirectionModelEvaluationReport:
    evidence: dict[str, object]
    samples: int
    accuracy: float
    baseline_accuracy: float

    @property
    def eligible_for_paper_trial(self) -> bool:
        return self.accuracy >= self.baseline_accuracy + 0.02

    def render(self) -> str:
        return "\n".join(
            (
                "out-of-sample model evaluation: COMPLETE",
                "========================================",
                f"samples    : {self.samples}",
                f"accuracy   : {self.accuracy:.3f}",
                f"baseline   : {self.baseline_accuracy:.3f}",
                "candidate   : "
                + ("paper trial" if self.eligible_for_paper_trial else "rejected"),
                "approval    : unapproved; paper only",
            )
        )


def train_direction_model(dataset: Mapping[str, object]) -> DirectionModelTrainingReport:
    """Fit logistic regression and evaluate only on the newest 30% of candles."""
    symbol, exchange = _dataset_identity(dataset)
    samples, dataset_sha256, first_open_time, last_open_time = _dataset_samples(dataset)
    split = int(len(samples) * _TRAINING_FRACTION)
    training, validation = samples[:split], samples[split:]
    if len(validation) < 30:
        raise ValueError("dataset leaves fewer than 30 validation samples")
    labels = {label for _, label in training}
    if labels != {0, 1}:
        raise ValueError("training period must contain both upward and downward outcomes")

    generalized = fit_logistic_classifier(
        [features for features, _ in training],
        [label for _, label in training],
        feature_names=_FEATURES,
        l2_penalty=_L2_PENALTY,
    )
    means = generalized["means"]
    scales = generalized["scales"]
    weights = generalized["weights"]
    bias = generalized["bias"]

    training_probabilities = _probabilities(training, means, scales, weights, bias)
    validation_probabilities = _probabilities(validation, means, scales, weights, bias)
    training_metrics = _classification_metrics(
        [label for _, label in training], training_probabilities, _THRESHOLD
    )
    validation_metrics = _classification_metrics(
        [label for _, label in validation], validation_probabilities, _THRESHOLD
    )
    validation_labels = [label for _, label in validation]
    validation_predictions = [int(value >= _THRESHOLD) for value in validation_probabilities]
    accuracy = sum(
        actual == predicted
        for actual, predicted in zip(validation_labels, validation_predictions)
    ) / len(validation_labels)
    baseline = max(
        sum(validation_labels), len(validation_labels) - sum(validation_labels)
    ) / len(validation_labels)
    model: dict[str, object] = {
        "schema": "offline-direction-model/v1",
        "model_type": "logistic_regression",
        "interval_seconds": 60,
        "symbol": symbol,
        "exchange": exchange,
        "features": list(_FEATURES),
        "feature_definitions": {
            "return_1": "close[t] / close[t-1] - 1",
            "return_3": "close[t] / close[t-3] - 1",
            "range_fraction": "(high[t] - low[t]) / close[t]",
        },
        "label_definition": "1 when close[t+1] > close[t], otherwise 0",
        "means": means,
        "scales": scales,
        "weights": weights,
        "bias": bias,
        "threshold": _THRESHOLD,
        "training_configuration": {
            "training_fraction": _TRAINING_FRACTION,
            "iterations": _ITERATIONS,
            "learning_rate": _LEARNING_RATE,
            "l2_penalty": _L2_PENALTY,
            "target_horizon_candles": 1,
            "random_seed": 0,
            "randomness_used": False,
        },
        "training_samples": len(training),
        "validation_samples": len(validation),
        "validation_accuracy": round(accuracy, 6),
        "baseline_accuracy": round(baseline, 6),
        "eligible_for_paper_validation": accuracy >= baseline + 0.02,
        "training_metrics": training_metrics,
        "internal_validation_metrics": validation_metrics,
        "approved": False,
        "paper_only": True,
        "self_promoting": False,
        "training_dataset_sha256": dataset_sha256,
        "training_start_open_time": first_open_time,
        "training_end_open_time": last_open_time,
    }
    return DirectionModelTrainingReport(
        model=model,
        training_samples=len(training),
        validation_samples=len(validation),
        validation_accuracy=accuracy,
        baseline_accuracy=baseline,
    )


def train_validated_direction_model(
    training_dataset: Mapping[str, object],
    validation_dataset: Mapping[str, object],
    *,
    trained_at: datetime | None = None,
) -> tuple[DirectionModelTrainingReport, DirectionModelEvaluationReport]:
    """Fit segment 1 only, then evaluate the frozen model on segment 3 only."""
    _require_segment_assignment(training_dataset, assignment="training", segment_id=1)
    _require_segment_assignment(validation_dataset, assignment="validation", segment_id=3)
    report = train_direction_model(training_dataset)
    _, validation_sha256, validation_start, validation_end = _dataset_samples(
        validation_dataset
    )
    timestamp = trained_at or datetime.now(timezone.utc)
    if not isinstance(timestamp, datetime) or timestamp.tzinfo is None:
        raise TypeError("trained_at must be a timezone-aware datetime")
    model = dict(report.model)
    model.update(
        {
            "validation_dataset_sha256": validation_sha256,
            "validation_start_open_time": validation_start,
            "validation_end_open_time": validation_end,
            "trained_at": timestamp.astimezone(timezone.utc).isoformat(),
            "library_versions": {
                "python": platform.python_version(),
                "trainer": "stdlib-only logistic regression",
            },
        }
    )
    model["model_content_sha256"] = _sha256(model)
    finalized = DirectionModelTrainingReport(
        model=model,
        training_samples=report.training_samples,
        validation_samples=report.validation_samples,
        validation_accuracy=report.validation_accuracy,
        baseline_accuracy=report.baseline_accuracy,
    )
    return finalized, evaluate_direction_model(model, validation_dataset)


def split_direction_datasets(
    dataset: Mapping[str, object],
) -> tuple[dict[str, object], dict[str, object]]:
    """Split one long capture into chronological, non-overlapping halves."""
    _, source_hash, _, _ = _dataset_samples(dataset)
    candles = dataset["candles"]
    if len(candles) < 240:  # type: ignore[arg-type]
        raise ValueError("at least 240 candles are required for two model datasets")
    midpoint = len(candles) // 2  # type: ignore[arg-type]
    outputs = []
    for name, rows in (
        ("training", candles[:midpoint]),  # type: ignore[index]
        ("evaluation", candles[midpoint:]),  # type: ignore[index]
    ):
        output = dict(dataset)
        output["candles"] = list(rows)
        output["segment"] = name
        output["source_dataset_sha256"] = source_hash
        output.pop("integrity", None)
        integrity = validate_candle_dataset(output)
        if not integrity.passed:
            raise ValueError(
                f"{name} dataset integrity validation failed: "
                + "; ".join(integrity.failures)
            )
        output["integrity"] = integrity.as_details()
        outputs.append(output)
    return outputs[0], outputs[1]


def evaluate_direction_model(
    model: Mapping[str, object], dataset: Mapping[str, object]
) -> DirectionModelEvaluationReport:
    """Evaluate a frozen candidate on a distinct chronological dataset."""
    means, scales, weights, bias, threshold = _model_parameters(model)
    if _dataset_identity(dataset) != (model["symbol"], model["exchange"]):
        raise ValueError("evaluation market must match the model training market")
    samples, dataset_sha256, first_open_time, last_open_time = _dataset_samples(dataset)
    if model.get("training_dataset_sha256") == dataset_sha256:
        raise ValueError("evaluation dataset must differ from the training dataset")
    training_start, training_end = _training_range(model)
    if first_open_time <= training_end and last_open_time >= training_start:
        raise ValueError("evaluation dataset must not overlap the training dataset")
    if first_open_time <= training_end:
        raise ValueError("evaluation dataset must begin after the training dataset")
    probabilities = _probabilities(samples, means, scales, weights, bias)
    labels = [label for _, label in samples]
    metrics = _classification_metrics(labels, probabilities, threshold)
    predictions = [int(value >= threshold) for value in probabilities]
    accuracy = sum(
        actual == predicted for actual, predicted in zip(labels, predictions)
    ) / len(labels)
    baseline = max(sum(labels), len(labels) - sum(labels)) / len(labels)
    eligible = accuracy >= baseline + 0.02
    evidence: dict[str, object] = {
        "schema": "offline-direction-evaluation/v1",
        "model_sha256": _sha256(model),
        "evaluation_dataset_sha256": dataset_sha256,
        "samples": len(samples),
        "accuracy": round(accuracy, 6),
        "baseline_accuracy": round(baseline, 6),
        "metrics": metrics,
        "eligible_for_paper_trial": eligible,
        "approved": False,
        "paper_only": True,
        "live_trading": False,
        "self_promoting": False,
    }
    return DirectionModelEvaluationReport(
        evidence=evidence,
        samples=len(samples),
        accuracy=accuracy,
        baseline_accuracy=baseline,
    )


def diagnose_direction_model_robustness(
    model: Mapping[str, object],
    training_dataset: Mapping[str, object],
    validation_dataset: Mapping[str, object],
    robustness_5_dataset: Mapping[str, object],
    robustness_6_dataset: Mapping[str, object],
) -> dict[str, object]:
    """Evaluate one frozen model on the approved diagnostic segments only."""
    _require_segment_assignment(training_dataset, assignment="training", segment_id=1)
    _require_segment_assignment(validation_dataset, assignment="validation", segment_id=3)
    _require_segment_assignment(
        robustness_5_dataset, assignment="robustness", segment_id=5
    )
    _require_segment_assignment(
        robustness_6_dataset, assignment="robustness", segment_id=6
    )
    means, scales, weights, bias, threshold = _model_parameters(model)
    training_samples, training_hash, _, _ = _dataset_samples(training_dataset)
    if training_hash != model.get("training_dataset_sha256"):
        raise ValueError("training reference does not match the frozen model")
    fitted_count = model.get("training_samples")
    if (
        isinstance(fitted_count, bool)
        or not isinstance(fitted_count, int)
        or fitted_count < 1
        or fitted_count > len(training_samples)
    ):
        raise ValueError("model training sample count is invalid")

    evaluations: dict[str, object] = {}
    for name, dataset in (
        ("segment_3", validation_dataset),
        ("segment_5", robustness_5_dataset),
        ("segment_6", robustness_6_dataset),
    ):
        report = evaluate_direction_model(model, dataset)
        samples, dataset_hash, first_open_time, last_open_time = _dataset_samples(dataset)
        probabilities = _probabilities(samples, means, scales, weights, bias)
        labels = [label for _, label in samples]
        metrics = dict(report.evidence["metrics"])  # type: ignore[arg-type]
        predicted_positives = metrics["predicted_class_distribution"]["up"]  # type: ignore[index]
        metrics["positive_prediction_rate"] = round(
            predicted_positives / len(samples), 6
        )
        metrics["label_up_rate"] = round(sum(labels) / len(labels), 6)
        evaluations[name] = {
            "dataset_sha256": dataset_hash,
            "first_open_time": first_open_time,
            "last_open_time": last_open_time,
            "metrics": metrics,
            "feature_distribution": _feature_distribution(samples, means, scales),
            "calibration": _calibration_diagnostics(labels, probabilities),
        }

    segment_metrics = {
        name: value["metrics"]  # type: ignore[index]
        for name, value in evaluations.items()
    }
    aucs = {
        name: metrics["roc_auc"]  # type: ignore[index]
        for name, metrics in segment_metrics.items()
    }
    comparable_aucs = [value for value in aucs.values() if isinstance(value, float)]
    return {
        "schema": "offline-direction-robustness/v1",
        "model_sha256": _sha256(model),
        "threshold": threshold,
        "threshold_optimized": False,
        "model_retrained": False,
        "paper_orders": False,
        "live_orders": False,
        "final_test_accessed": False,
        "training_reference": {
            "dataset_sha256": training_hash,
            "fitted_samples": fitted_count,
            "feature_distribution": _feature_distribution(
                training_samples[:fitted_count], means, scales
            ),
        },
        "evaluations": evaluations,
        "comparison": {
            "roc_auc_by_segment": aucs,
            "mean_roc_auc": round(statistics.fmean(comparable_aucs), 6),
            "all_roc_auc_above_random": bool(comparable_aucs)
            and len(comparable_aucs) == len(aucs)
            and all(value > 0.5 for value in comparable_aucs),
            "accuracy_by_segment": {
                name: metrics["accuracy"]  # type: ignore[index]
                for name, metrics in segment_metrics.items()
            },
            "majority_baseline_by_segment": {
                name: metrics["majority_baseline_accuracy"]  # type: ignore[index]
                for name, metrics in segment_metrics.items()
            },
            "positive_prediction_rate_by_segment": {
                name: metrics["positive_prediction_rate"]  # type: ignore[index]
                for name, metrics in segment_metrics.items()
            },
            "label_up_rate_by_segment": {
                name: metrics["label_up_rate"]  # type: ignore[index]
                for name, metrics in segment_metrics.items()
            },
        },
    }


def direction_model_sha256(model: Mapping[str, object]) -> str:
    """Return the canonical identity used to bind evaluation and approval."""
    _model_parameters(model)
    return _sha256(model)


def paper_model_approval_phrase(model: Mapping[str, object]) -> str:
    """Exact operator confirmation for a paper trial of this model."""
    return f"APPROVE PAPER MODEL {direction_model_sha256(model)}"


def validate_paper_model_trial(
    model: Mapping[str, object],
    evidence: Mapping[str, object],
    confirmation: str,
) -> None:
    """Refuse paper inference unless frozen out-of-sample evidence is valid."""
    if model.get('deployment_hold'):
        raise ValueError('model deployment is held pending independent cost-aware qualification')
    model_hash = direction_model_sha256(model)
    if type(model.get('interval_seconds')) is not int or model['interval_seconds']!=60:
        raise ValueError('paper deployment requires explicit one-minute training cadence')
    if not isinstance(evidence, Mapping):
        raise TypeError("model evaluation evidence must be a mapping")
    if evidence.get("schema") != "offline-direction-evaluation/v1":
        raise ValueError("unsupported model evaluation schema")
    if not (
        model.get("eligible_for_paper_validation") is True
        and evidence.get("model_sha256") == model_hash
        and _is_sha256(evidence.get("evaluation_dataset_sha256"))
        and isinstance(evidence.get("samples"), int)
        and not isinstance(evidence.get("samples"), bool)
        and evidence.get("samples", 0) > 0
        and evidence.get("eligible_for_paper_trial") is True
        and evidence.get("approved") is False
        and evidence.get("paper_only") is True
        and evidence.get("live_trading") is False
        and evidence.get("self_promoting") is False
    ):
        raise ValueError("model or evaluation is not eligible for a paper trial")
    if confirmation != paper_model_approval_phrase(model):
        raise ValueError(
            "paper model confirmation must exactly match "
            f"{paper_model_approval_phrase(model)!r}"
        )


def predict_direction_probability(
    model: Mapping[str, object], features: list[float]
) -> float:
    """Predict next-candle upward probability with a frozen candidate."""
    means, scales, weights, bias, _ = _model_parameters(model)
    if len(features) != len(_FEATURES):
        raise ValueError(f"model requires {len(_FEATURES)} features")
    row = [_finite_float(value, "feature") for value in features]
    values = _standardize(row, means, scales)
    return _sigmoid(bias + sum(w * x for w, x in zip(weights, values)))


def _dataset_samples(
    dataset: Mapping[str, object],
) -> tuple[list[tuple[list[float], int]], str, int, int]:
    if not isinstance(dataset, Mapping):
        raise TypeError("dataset must be a mapping")
    if dataset.get("integrity") is not None:
        integrity = validate_candle_dataset(dataset)
        if not integrity.passed:
            raise ValueError(
                "dataset integrity validation failed: " + "; ".join(integrity.failures)
            )
    _dataset_identity(dataset)
    if dataset.get("schema") != "coinswitch-public-candles/v1":
        raise ValueError("unsupported candle dataset schema")
    if not (
        dataset.get("public_data_only") is True
        and isinstance(dataset.get("credentials_used"), bool)
        and dataset.get("live_trading") is False
        and dataset.get("training_only") is True
    ):
        raise ValueError("dataset safety markers are missing or contradictory")
    candles = dataset.get("candles")
    if not isinstance(candles, list) or len(candles) < 120:
        raise ValueError("at least 120 chronological candles are required")
    parsed = [_candle_values(candle) for candle in candles]
    if any(row[1]!=60 for row in parsed):
        raise ValueError('the existing direction model requires one-minute candles')
    for previous, current in zip(parsed, parsed[1:]):
        expected = previous[0] + previous[1] * 1000
        if current[0] != expected or current[1] != previous[1]:
            raise ValueError("candles must be chronological, contiguous, and one interval")
    samples: list[tuple[list[float], int]] = []
    for index in range(3, len(parsed) - 1):
        close = parsed[index][5]
        features = [
            float(close / parsed[index - 1][5] - 1),
            float(close / parsed[index - 3][5] - 1),
            float((parsed[index][3] - parsed[index][4]) / close),
        ]
        samples.append((features, int(parsed[index + 1][5] > close)))
    return samples, _sha256(dataset), parsed[0][0], parsed[-1][0]


def _model_parameters(
    model: Mapping[str, object],
) -> tuple[list[float], list[float], list[float], float, float]:
    if not isinstance(model, Mapping):
        raise TypeError("model must be a mapping")
    if model.get("schema") != "offline-direction-model/v1":
        raise ValueError("unsupported model schema")
    for name in ("symbol", "exchange"):
        value = model.get(name)
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"model {name} must be a non-empty string")
    if not (
        model.get("approved") is False
        and model.get("paper_only") is True
        and model.get("self_promoting") is False
        and model.get("features") == list(_FEATURES)
    ):
        raise ValueError("model safety markers or features are invalid")
    training_sha256 = model.get("training_dataset_sha256")
    if not _is_sha256(training_sha256):
        raise ValueError("model training dataset hash is invalid")
    _training_range(model)
    vectors = []
    for name in ("means", "scales", "weights"):
        value = model.get(name)
        if not isinstance(value, list) or len(value) != len(_FEATURES):
            raise ValueError(f"model {name} vector is invalid")
        vector = [_finite_float(item, name) for item in value]
        vectors.append(vector)
    if any(value <= 0 for value in vectors[1]):
        raise ValueError("model scales must be positive")
    bias = _finite_float(model.get("bias"), "bias")
    threshold = _finite_float(model.get("threshold"), "threshold")
    if not 0 < threshold < 1:
        raise ValueError("model threshold must be between zero and one")
    return vectors[0], vectors[1], vectors[2], bias, threshold


def _require_segment_assignment(
    dataset: Mapping[str, object], *, assignment: str, segment_id: int
) -> None:
    metadata = dataset.get("ml_segment")
    if not isinstance(metadata, Mapping):
        raise ValueError("validated ML segment provenance is required")
    if not (
        metadata.get("schema") == "contiguous-ml-segment/v1"
        and metadata.get("assignment") == assignment
        and metadata.get("segment_id") == segment_id
        and metadata.get("preprocessing_fitted") is False
        and _is_sha256(metadata.get("source_file_sha256"))
        and _is_sha256(metadata.get("source_dataset_sha256"))
    ):
        raise ValueError(
            f"dataset must be validated {assignment} segment {segment_id}"
        )


def _probabilities(
    samples: list[tuple[list[float], int]],
    means: list[float],
    scales: list[float],
    weights: list[float],
    bias: float,
) -> list[float]:
    return [
        _sigmoid(
            bias
            + sum(
                weight * value
                for weight, value in zip(
                    weights, _standardize(row, means, scales)
                )
            )
        )
        for row, _ in samples
    ]


def _classification_metrics(
    labels: list[int], probabilities: list[float], threshold: float
) -> dict[str, object]:
    if not labels or len(labels) != len(probabilities):
        raise ValueError("classification metrics require aligned non-empty values")
    predictions = [int(value >= threshold) for value in probabilities]
    true_positive = sum(a == b == 1 for a, b in zip(labels, predictions))
    true_negative = sum(a == b == 0 for a, b in zip(labels, predictions))
    false_positive = sum(a == 0 and b == 1 for a, b in zip(labels, predictions))
    false_negative = sum(a == 1 and b == 0 for a, b in zip(labels, predictions))
    positives = sum(labels)
    negatives = len(labels) - positives
    predicted_positives = sum(predictions)
    precision = true_positive / predicted_positives if predicted_positives else 0.0
    recall = true_positive / positives if positives else 0.0
    f1 = 2 * precision * recall / (precision + recall) if precision + recall else 0.0
    accuracy = (true_positive + true_negative) / len(labels)
    majority_class = 1 if positives > negatives else 0
    majority_accuracy = max(positives, negatives) / len(labels)
    ordered = sorted(probabilities)
    warnings = []
    if min(positives, negatives) / len(labels) < 0.4:
        warnings.append("class distribution is imbalanced beyond 60/40")
    if predicted_positives in {0, len(labels)}:
        warnings.append("model predicts only one class at the configured threshold")
    elif min(predicted_positives, len(labels) - predicted_positives) / len(labels) < 0.1:
        warnings.append("predicted class distribution is imbalanced beyond 90/10")
    if ordered[-1] - ordered[0] < 0.1:
        warnings.append("predicted probabilities occupy a narrow range")
    return {
        "samples": len(labels),
        "class_distribution": {"down_or_flat": negatives, "up": positives},
        "predicted_class_distribution": {
            "down_or_flat": len(labels) - predicted_positives,
            "up": predicted_positives,
        },
        "accuracy": round(accuracy, 6),
        "precision": round(precision, 6),
        "recall": round(recall, 6),
        "f1": round(f1, 6),
        "roc_auc": None if positives == 0 or negatives == 0 else round(
            _roc_auc(labels, probabilities), 6
        ),
        "confusion_matrix": {
            "true_negative": true_negative,
            "false_positive": false_positive,
            "false_negative": false_negative,
            "true_positive": true_positive,
        },
        "probability_distribution": {
            "minimum": round(ordered[0], 6),
            "p05": round(_quantile(ordered, 0.05), 6),
            "p25": round(_quantile(ordered, 0.25), 6),
            "median": round(_quantile(ordered, 0.5), 6),
            "p75": round(_quantile(ordered, 0.75), 6),
            "p95": round(_quantile(ordered, 0.95), 6),
            "maximum": round(ordered[-1], 6),
            "mean": round(statistics.fmean(ordered), 6),
        },
        "majority_class": majority_class,
        "majority_baseline_accuracy": round(majority_accuracy, 6),
        "warnings": warnings,
    }


def _feature_distribution(
    samples: list[tuple[list[float], int]],
    means: list[float],
    scales: list[float],
) -> dict[str, object]:
    result: dict[str, object] = {}
    for index, name in enumerate(_FEATURES):
        values = [features[index] for features, _ in samples]
        ordered = sorted(values)
        standardized = [(value - means[index]) / scales[index] for value in values]
        result[name] = {
            "count": len(values),
            "mean": round(statistics.fmean(values), 12),
            "standard_deviation": round(statistics.pstdev(values), 12),
            "p05": round(_quantile(ordered, 0.05), 12),
            "median": round(_quantile(ordered, 0.5), 12),
            "p95": round(_quantile(ordered, 0.95), 12),
            "standardized_mean": round(statistics.fmean(standardized), 6),
            "standardized_standard_deviation": round(
                statistics.pstdev(standardized), 6
            ),
        }
    return result


def _calibration_diagnostics(
    labels: list[int], probabilities: list[float]
) -> dict[str, object]:
    bins = []
    for index in range(10):
        members = [
            (probability, label)
            for probability, label in zip(probabilities, labels)
            if min(int(probability * 10), 9) == index
        ]
        if not members:
            continue
        mean_probability = statistics.fmean(value for value, _ in members)
        observed_rate = statistics.fmean(label for _, label in members)
        bins.append(
            {
                "lower_bound": index / 10,
                "upper_bound": (index + 1) / 10,
                "count": len(members),
                "mean_probability": round(mean_probability, 6),
                "observed_up_rate": round(observed_rate, 6),
                "calibration_gap": round(mean_probability - observed_rate, 6),
            }
        )
    return {
        "brier_score": round(
            statistics.fmean(
                (probability - label) ** 2
                for probability, label in zip(probabilities, labels)
            ),
            6,
        ),
        "fixed_probability_bins": bins,
    }


def _roc_auc(labels: list[int], probabilities: list[float]) -> float:
    ranked = sorted(zip(probabilities, labels), key=lambda item: item[0])
    positive_rank_sum = 0.0
    index = 0
    while index < len(ranked):
        end = index + 1
        while end < len(ranked) and ranked[end][0] == ranked[index][0]:
            end += 1
        average_rank = ((index + 1) + end) / 2
        positive_rank_sum += average_rank * sum(label for _, label in ranked[index:end])
        index = end
    positives = sum(labels)
    negatives = len(labels) - positives
    return (positive_rank_sum - positives * (positives + 1) / 2) / (
        positives * negatives
    )


def _quantile(ordered: list[float], fraction: float) -> float:
    position = (len(ordered) - 1) * fraction
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return ordered[lower]
    weight = position - lower
    return ordered[lower] * (1 - weight) + ordered[upper] * weight


def _dataset_identity(dataset: Mapping[str, object]) -> tuple[str, str]:
    values = []
    for name in ("symbol", "exchange"):
        value = dataset.get(name)
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"dataset {name} must be a non-empty string")
        values.append(value.strip().upper() if name == "symbol" else value.strip().lower())
    return values[0], values[1]


def _training_range(model: Mapping[str, object]) -> tuple[int, int]:
    start = model.get("training_start_open_time")
    end = model.get("training_end_open_time")
    if (
        isinstance(start, bool)
        or not isinstance(start, int)
        or isinstance(end, bool)
        or not isinstance(end, int)
        or start >= end
    ):
        raise ValueError("model training time range is invalid")
    return start, end


def _finite_float(value: object, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"model {name} must be numeric")
    converted = float(value)
    if not math.isfinite(converted):
        raise ValueError(f"model {name} must be finite")
    return converted


def _is_sha256(value: object) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(character in "0123456789abcdef" for character in value)
    )


def _sha256(value: Mapping[str, object]) -> str:
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()


def _candle_values(
    candle: object,
) -> tuple[int, int, Decimal, Decimal, Decimal, Decimal]:
    if not isinstance(candle, Mapping):
        raise TypeError("every candle must be an object")
    try:
        values = tuple(Decimal(str(candle[name])) for name in ("open", "high", "low", "close"))
        volume = Decimal(str(candle["volume"]))
    except (KeyError, InvalidOperation) as exc:
        raise ValueError("candle has invalid OHLCV values") from exc
    open_time = candle.get("open_time")
    interval = candle.get("interval_seconds")
    if isinstance(open_time, bool) or not isinstance(open_time, int):
        raise ValueError("candle open_time must be integer milliseconds")
    if isinstance(interval, bool) or not isinstance(interval, int) or interval <= 0:
        raise ValueError("candle interval_seconds must be a positive integer")
    open_, high, low, close = values
    if any(not value.is_finite() or value <= 0 for value in values):
        raise ValueError("candle OHLC values must be finite and positive")
    if not volume.is_finite() or volume < 0:
        raise ValueError("candle volume must be finite and non-negative")
    if low > min(open_, close) or high < max(open_, close) or low > high:
        raise ValueError("candle OHLC values are inconsistent")
    return open_time, interval, open_, high, low, close


def _standardize(row: list[float], means: list[float], scales: list[float]) -> list[float]:
    return [(value - mean) / scale for value, mean, scale in zip(row, means, scales)]


def _sigmoid(value: float) -> float:
    value = max(-60.0, min(60.0, value))
    return 1.0 / (1.0 + math.exp(-value))
