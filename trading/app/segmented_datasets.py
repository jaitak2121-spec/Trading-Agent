"""Gap-safe, deterministic ML datasets derived from genuine candle segments."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from decimal import Decimal, InvalidOperation

from .historical_candles import validate_candle_dataset

__all__ = [
    "finalize_segment_manifest",
    "prepare_segmented_datasets",
    "validate_segment_dataset",
]

_ASSIGNMENTS = {
    1: "training",
    3: "validation",
    5: "robustness",
    6: "robustness",
    7: "test",
}
_DISCARDED = {2: "too_short", 4: "too_short"}
_FEATURES = (
    {
        "name": "return_1",
        "definition": "close[t] / close[t-1] - 1",
        "uses_candles": [0, -1],
    },
    {
        "name": "return_3",
        "definition": "close[t] / close[t-3] - 1",
        "uses_candles": [0, -3],
    },
    {
        "name": "range_fraction",
        "definition": "(high[t] - low[t]) / close[t]",
        "uses_candles": [0],
    },
)


def prepare_segmented_datasets(
    dataset: Mapping[str, object], *, source_file_sha256: str
) -> tuple[dict[str, dict[str, object]], dict[str, object]]:
    """Split the approved seven-segment capture without crossing any gap."""
    _validate_source(dataset, source_file_sha256)
    source_dataset_sha256 = _sha256(dataset)
    segments = _segments(dataset["candles"])  # type: ignore[arg-type]
    if len(segments) != 7:
        raise ValueError(f"approved derivation requires 7 segments, found {len(segments)}")

    outputs: dict[str, dict[str, object]] = {}
    manifest_segments = []
    for segment_id, rows in enumerate(segments, 1):
        assignment = _ASSIGNMENTS.get(segment_id)
        if assignment is None:
            manifest_segments.append(
                _segment_summary(segment_id, rows, _DISCARDED[segment_id], discarded=True)
            )
            continue
        key = assignment if assignment != "robustness" else f"robustness_{segment_id}"
        output = _build_segment(
            dataset,
            rows,
            segment_id=segment_id,
            assignment=assignment,
            source_file_sha256=source_file_sha256,
            source_dataset_sha256=source_dataset_sha256,
        )
        validate_segment_dataset(output)
        outputs[key] = output
        manifest_segments.append(
            _segment_summary(segment_id, rows, assignment, discarded=False)
        )

    manifest: dict[str, object] = {
        "schema": "coinswitch-ml-segment-manifest/v1",
        "source_file_sha256": source_file_sha256,
        "source_dataset_sha256": source_dataset_sha256,
        "source_candle_sha256": dataset.get("integrity", {}).get("candle_sha256"),  # type: ignore[union-attr]
        "symbol": dataset["symbol"],
        "exchange": dataset["exchange"],
        "interval_seconds": 60,
        "feature_definitions": list(_FEATURES),
        "label_definition": "1 when close[t+1] > close[t], otherwise 0",
        "label_horizon_candles": 1,
        "warmup_rows_per_segment": 3,
        "terminal_rows_per_segment": 1,
        "preprocessing": {
            "fitted": False,
            "policy": "fit on training samples only during a later training milestone",
        },
        "segments": manifest_segments,
        "artifacts": {},
        "validation_passed": True,
        "training_performed": False,
    }
    return outputs, finalize_segment_manifest(manifest)


def finalize_segment_manifest(manifest: Mapping[str, object]) -> dict[str, object]:
    """Return a copy whose hash covers every field except the hash itself."""
    output = dict(manifest)
    output.pop("derivation_sha256", None)
    output["derivation_sha256"] = _sha256(output)
    return output


def validate_segment_dataset(dataset: Mapping[str, object]) -> None:
    """Refuse any derived artifact whose candles, samples, or metadata drift."""
    validation = validate_candle_dataset(dataset, minimum_candles=5)
    if not validation.passed:
        raise ValueError("segment candle validation failed: " + "; ".join(validation.failures))
    metadata = dataset.get("ml_segment")
    samples = dataset.get("samples")
    candles = dataset.get("candles")
    if not isinstance(metadata, Mapping) or not isinstance(samples, list):
        raise ValueError("segment ML metadata or samples are missing")
    if not isinstance(candles, list):
        raise ValueError("segment candles must be a list")
    expected = _samples(candles)
    if samples != expected:
        raise ValueError("segment samples do not match candle-local feature derivation")
    required = {
        "segment_id",
        "assignment",
        "source_file_sha256",
        "source_dataset_sha256",
    }
    if not required.issubset(metadata):
        raise ValueError("segment provenance metadata is incomplete")
    if metadata.get("start_open_time") != candles[0]["open_time"]:
        raise ValueError("segment start boundary does not match candles")
    if metadata.get("end_close_time") != candles[-1]["close_time"]:
        raise ValueError("segment end boundary does not match candles")
    if metadata.get("candle_count") != len(candles):
        raise ValueError("segment candle count does not match content")
    if metadata.get("sample_count") != len(samples):
        raise ValueError("segment sample count does not match content")
    if metadata.get("sample_sha256") != _sha256(samples):
        raise ValueError("segment sample hash does not match content")
    if metadata.get("feature_definitions") != list(_FEATURES):
        raise ValueError("segment feature definitions changed")
    if metadata.get("label_horizon_candles") != 1:
        raise ValueError("segment label horizon changed")
    expected_derivation = _derivation_sha256(metadata, candles, samples)
    if metadata.get("derivation_sha256") != expected_derivation:
        raise ValueError("segment derivation hash does not match content")


def _build_segment(
    source: Mapping[str, object],
    rows: list[dict[str, object]],
    *,
    segment_id: int,
    assignment: str,
    source_file_sha256: str,
    source_dataset_sha256: str,
) -> dict[str, object]:
    output = dict(source)
    output["candles"] = [dict(row) for row in rows]
    output.pop("integrity", None)
    integrity = validate_candle_dataset(output, minimum_candles=5)
    if not integrity.passed:
        raise ValueError("derived segment is invalid: " + "; ".join(integrity.failures))
    output["integrity"] = integrity.as_details()
    samples = _samples(output["candles"])  # type: ignore[arg-type]
    metadata: dict[str, object] = {
        "schema": "contiguous-ml-segment/v1",
        "segment_id": segment_id,
        "assignment": assignment,
        "source_file_sha256": source_file_sha256,
        "source_dataset_sha256": source_dataset_sha256,
        "start_open_time": rows[0]["open_time"],
        "end_open_time": rows[-1]["open_time"],
        "end_close_time": rows[-1]["close_time"],
        "candle_count": len(rows),
        "warmup_rows_discarded": 3,
        "terminal_rows_discarded": 1,
        "sample_count": len(samples),
        "feature_definitions": list(_FEATURES),
        "label_definition": "1 when close[t+1] > close[t], otherwise 0",
        "label_horizon_candles": 1,
        "preprocessing_fitted": False,
        "sample_sha256": _sha256(samples),
    }
    metadata["derivation_sha256"] = _derivation_sha256(
        metadata, output["candles"], samples  # type: ignore[arg-type]
    )
    output["ml_segment"] = metadata
    output["samples"] = samples
    return output


def _samples(candles: list[dict[str, object]]) -> list[dict[str, object]]:
    values = [_prices(row) for row in candles]
    samples = []
    for index in range(3, len(candles) - 1):
        current = values[index]
        following = values[index + 1]
        samples.append(
            {
                "feature_window_start_open_time": candles[index - 3]["open_time"],
                "feature_open_time": candles[index]["open_time"],
                "feature_close_time": candles[index]["close_time"],
                "features": {
                    "return_1": str(current[3] / values[index - 1][3] - 1),
                    "return_3": str(current[3] / values[index - 3][3] - 1),
                    "range_fraction": str((current[1] - current[2]) / current[3]),
                },
                "label_open_time": candles[index + 1]["open_time"],
                "label_close_time": candles[index + 1]["close_time"],
                "label_next_close_up": int(following[3] > current[3]),
            }
        )
    return samples


def _segments(candles: object) -> list[list[dict[str, object]]]:
    if not isinstance(candles, list) or not candles:
        raise ValueError("source candles must be a non-empty list")
    segments: list[list[dict[str, object]]] = [[]]
    previous_time: int | None = None
    for row in candles:
        if not isinstance(row, Mapping):
            raise ValueError("every source candle must be an object")
        open_time = row.get("open_time")
        interval = row.get("interval_seconds")
        if isinstance(open_time, bool) or not isinstance(open_time, int):
            raise ValueError("source candle open_time must be an integer")
        if interval != 60:
            raise ValueError("approved derivation requires one-minute candles")
        if previous_time is not None:
            if open_time <= previous_time:
                raise ValueError("source candles must be strictly chronological and unique")
            if open_time != previous_time + 60_000:
                segments.append([])
        segments[-1].append(dict(row))
        previous_time = open_time
    return segments


def _validate_source(dataset: Mapping[str, object], source_file_sha256: str) -> None:
    if not isinstance(dataset, Mapping):
        raise TypeError("source dataset must be a mapping")
    if dataset.get("schema") != "coinswitch-public-candles/v1":
        raise ValueError("unsupported source dataset schema")
    if not (
        dataset.get("public_data_only") is True
        and isinstance(dataset.get("credentials_used"), bool)
        and dataset.get("live_trading") is False
        and dataset.get("training_only") is True
    ):
        raise ValueError("source safety markers are missing or contradictory")
    if not _is_sha256(source_file_sha256):
        raise ValueError("source file SHA-256 is invalid")


def _prices(row: Mapping[str, object]) -> tuple[Decimal, Decimal, Decimal, Decimal]:
    try:
        values = tuple(Decimal(str(row[name])) for name in ("open", "high", "low", "close"))
    except (KeyError, InvalidOperation) as exc:
        raise ValueError("segment candle has invalid OHLC values") from exc
    return values  # type: ignore[return-value]


def _segment_summary(
    segment_id: int,
    rows: list[dict[str, object]],
    assignment: str,
    *,
    discarded: bool,
) -> dict[str, object]:
    return {
        "segment_id": segment_id,
        "assignment": assignment,
        "discarded": discarded,
        "start_open_time": rows[0]["open_time"],
        "end_close_time": rows[-1]["close_time"],
        "candle_count": len(rows),
        "discarded_rows": len(rows) if discarded else 4,
        "sample_count": 0 if discarded else len(rows) - 4,
    }


def _derivation_sha256(
    metadata: Mapping[str, object],
    candles: list[object],
    samples: list[object],
) -> str:
    fields = {
        key: metadata[key]
        for key in (
            "schema",
            "segment_id",
            "assignment",
            "source_file_sha256",
            "source_dataset_sha256",
            "start_open_time",
            "end_open_time",
            "end_close_time",
            "candle_count",
            "warmup_rows_discarded",
            "terminal_rows_discarded",
            "sample_count",
            "feature_definitions",
            "label_definition",
            "label_horizon_candles",
            "preprocessing_fitted",
            "sample_sha256",
        )
    }
    return _sha256({"metadata": fields, "candles": candles, "samples": samples})


def _sha256(value: object) -> str:
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()


def _is_sha256(value: object) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(character in "0123456789abcdef" for character in value)
    )
