"""Offline, paper-only training of a small next-candle direction model."""

from __future__ import annotations

import hashlib
import json
import math
import statistics
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Mapping

__all__ = [
    "DirectionModelEvaluationReport",
    "DirectionModelTrainingReport",
    "direction_model_sha256",
    "evaluate_direction_model",
    "paper_model_approval_phrase",
    "predict_direction_probability",
    "split_direction_datasets",
    "train_direction_model",
    "validate_paper_model_trial",
]

_FEATURES = ("return_1", "return_3", "range_fraction")


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
    split = int(len(samples) * 0.7)
    training, validation = samples[:split], samples[split:]
    if len(validation) < 30:
        raise ValueError("dataset leaves fewer than 30 validation samples")
    labels = {label for _, label in training}
    if labels != {0, 1}:
        raise ValueError("training period must contain both upward and downward outcomes")

    columns = list(zip(*(features for features, _ in training)))
    means = [statistics.fmean(column) for column in columns]
    scales = [statistics.pstdev(column) or 1.0 for column in columns]
    train_x = [_standardize(row, means, scales) for row, _ in training]
    train_y = [label for _, label in training]
    weights = [0.0] * len(_FEATURES)
    bias = 0.0
    for _ in range(400):
        gradients = [0.0] * len(weights)
        bias_gradient = 0.0
        for row, label in zip(train_x, train_y):
            error = _sigmoid(bias + sum(w * x for w, x in zip(weights, row))) - label
            bias_gradient += error
            for index, value in enumerate(row):
                gradients[index] += error * value
        count = len(train_x)
        bias -= 0.1 * bias_gradient / count
        for index in range(len(weights)):
            weights[index] -= 0.1 * (gradients[index] / count + 0.001 * weights[index])

    predictions = []
    validation_labels = []
    for row, label in validation:
        values = _standardize(row, means, scales)
        probability = _sigmoid(bias + sum(w * x for w, x in zip(weights, values)))
        predictions.append(int(probability >= 0.5))
        validation_labels.append(label)
    accuracy = sum(a == b for a, b in zip(predictions, validation_labels)) / len(validation)
    majority = max(sum(validation_labels), len(validation_labels) - sum(validation_labels))
    baseline = majority / len(validation_labels)
    model: dict[str, object] = {
        "schema": "offline-direction-model/v1",
        "model_type": "logistic_regression",
        "symbol": symbol,
        "exchange": exchange,
        "features": list(_FEATURES),
        "means": means,
        "scales": scales,
        "weights": weights,
        "bias": bias,
        "threshold": 0.5,
        "training_samples": len(training),
        "validation_samples": len(validation),
        "validation_accuracy": round(accuracy, 6),
        "baseline_accuracy": round(baseline, 6),
        "eligible_for_paper_validation": accuracy >= baseline + 0.02,
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
    predictions = []
    labels = []
    for row, label in samples:
        values = _standardize(row, means, scales)
        probability = _sigmoid(bias + sum(w * x for w, x in zip(weights, values)))
        predictions.append(int(probability >= threshold))
        labels.append(label)
    accuracy = sum(a == b for a, b in zip(predictions, labels)) / len(labels)
    majority = max(sum(labels), len(labels) - sum(labels))
    baseline = majority / len(labels)
    eligible = accuracy >= baseline + 0.02
    evidence: dict[str, object] = {
        "schema": "offline-direction-evaluation/v1",
        "model_sha256": _sha256(model),
        "evaluation_dataset_sha256": dataset_sha256,
        "samples": len(samples),
        "accuracy": round(accuracy, 6),
        "baseline_accuracy": round(baseline, 6),
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
    model_hash = direction_model_sha256(model)
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
    _dataset_identity(dataset)
    if dataset.get("schema") != "coinswitch-public-candles/v1":
        raise ValueError("unsupported candle dataset schema")
    if not (
        dataset.get("public_data_only") is True
        and dataset.get("credentials_used") is False
        and dataset.get("live_trading") is False
        and dataset.get("training_only") is True
    ):
        raise ValueError("dataset safety markers are missing or contradictory")
    candles = dataset.get("candles")
    if not isinstance(candles, list) or len(candles) < 120:
        raise ValueError("at least 120 chronological candles are required")
    parsed = [_candle_values(candle) for candle in candles]
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
    except (KeyError, InvalidOperation) as exc:
        raise ValueError("candle has invalid OHLC values") from exc
    open_time = candle.get("open_time")
    interval = candle.get("interval_seconds")
    if isinstance(open_time, bool) or not isinstance(open_time, int):
        raise ValueError("candle open_time must be integer milliseconds")
    if isinstance(interval, bool) or not isinstance(interval, int) or interval <= 0:
        raise ValueError("candle interval_seconds must be a positive integer")
    open_, high, low, close = values
    if any(not value.is_finite() or value <= 0 for value in values):
        raise ValueError("candle OHLC values must be finite and positive")
    if low > min(open_, close) or high < max(open_, close) or low > high:
        raise ValueError("candle OHLC values are inconsistent")
    return open_time, interval, open_, high, low, close


def _standardize(row: list[float], means: list[float], scales: list[float]) -> list[float]:
    return [(value - mean) / scale for value, mean, scale in zip(row, means, scales)]


def _sigmoid(value: float) -> float:
    value = max(-60.0, min(60.0, value))
    return 1.0 / (1.0 + math.exp(-value))
