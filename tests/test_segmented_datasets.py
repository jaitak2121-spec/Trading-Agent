"""Contiguous ML segments never borrow features or labels across a market gap."""

from __future__ import annotations

import contextlib
import hashlib
import io
import json
import tempfile
import unittest
from pathlib import Path

from trading.app.segmented_datasets import (
    prepare_segmented_datasets,
    validate_segment_dataset,
)


def source_dataset(lengths=(8, 4, 8, 3, 8, 8, 8)) -> dict[str, object]:
    rows = []
    open_time = 0
    price = 100
    for segment_length in lengths:
        for _ in range(segment_length):
            price += 1
            rows.append(
                {
                    "symbol": "BTC/USDT",
                    "open_time": open_time,
                    "close_time": open_time + 60_000,
                    "interval_seconds": 60,
                    "open": str(price),
                    "high": str(price + 1),
                    "low": str(price - 1),
                    "close": str(price),
                    "volume": "1",
                    "source": "test",
                }
            )
            open_time += 60_000
        open_time += 60_000
    return {
        "schema": "coinswitch-public-candles/v1",
        "symbol": "BTC/USDT",
        "exchange": "c2c1",
        "public_data_only": True,
        "credentials_used": True,
        "live_trading": False,
        "training_only": True,
        "candles": rows,
    }


class SegmentedDatasetTest(unittest.TestCase):
    source_hash = "a" * 64

    def test_gap_windows_stay_inside_their_assigned_segment(self) -> None:
        outputs, manifest = prepare_segmented_datasets(
            source_dataset(), source_file_sha256=self.source_hash
        )

        self.assertEqual(
            set(outputs),
            {"training", "validation", "robustness_5", "robustness_6", "test"},
        )
        self.assertEqual(
            [item["segment_id"] for item in manifest["segments"] if item["discarded"]],
            [2, 4],
        )
        for output in outputs.values():
            metadata = output["ml_segment"]
            self.assertEqual(len(output["samples"]), len(output["candles"]) - 4)
            for sample in output["samples"]:
                self.assertGreaterEqual(
                    sample["feature_window_start_open_time"], metadata["start_open_time"]
                )
                self.assertLessEqual(sample["label_close_time"], metadata["end_close_time"])
                self.assertEqual(
                    sample["label_open_time"], sample["feature_open_time"] + 60_000
                )

    def test_label_cannot_cross_or_be_changed_after_derivation(self) -> None:
        outputs, _ = prepare_segmented_datasets(
            source_dataset(), source_file_sha256=self.source_hash
        )
        changed = json.loads(json.dumps(outputs["training"]))
        changed["samples"][-1]["label_open_time"] += 120_000

        with self.assertRaisesRegex(ValueError, "samples do not match"):
            validate_segment_dataset(changed)

    def test_future_label_candle_does_not_enter_current_features(self) -> None:
        original = source_dataset()
        changed = json.loads(json.dumps(original))
        changed["candles"][4].update(  # type: ignore[index]
            {"open": "1", "high": "1", "low": "1", "close": "1"}
        )
        before, _ = prepare_segmented_datasets(
            original, source_file_sha256=self.source_hash
        )
        after, _ = prepare_segmented_datasets(
            changed, source_file_sha256=self.source_hash
        )
        before_sample = before["training"]["samples"][0]
        after_sample = after["training"]["samples"][0]

        self.assertEqual(before_sample["features"], after_sample["features"])
        self.assertNotEqual(
            before_sample["label_next_close_up"], after_sample["label_next_close_up"]
        )

    def test_boundaries_require_unique_ordered_one_minute_candles(self) -> None:
        duplicate = source_dataset()
        duplicate["candles"][1]["open_time"] = 0  # type: ignore[index]
        with self.assertRaisesRegex(ValueError, "strictly chronological and unique"):
            prepare_segmented_datasets(duplicate, source_file_sha256=self.source_hash)

        wrong_segments = source_dataset(lengths=(8, 8, 8))
        with self.assertRaisesRegex(ValueError, "requires 7 segments"):
            prepare_segmented_datasets(wrong_segments, source_file_sha256=self.source_hash)

    def test_derivation_and_cli_files_are_reproducible(self) -> None:
        source = source_dataset()
        first = prepare_segmented_datasets(source, source_file_sha256=self.source_hash)
        second = prepare_segmented_datasets(source, source_file_sha256=self.source_hash)
        self.assertEqual(first, second)

        from trading.app.__main__ import main

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source_path = root / "source.json"
            output_dir = root / "segments"
            source_bytes = json.dumps(source, sort_keys=True).encode()
            source_path.write_bytes(source_bytes)
            with contextlib.redirect_stdout(io.StringIO()):
                code = main(
                    [
                        "--prepare-segmented-model-data",
                        str(source_path),
                        "--segment-output-dir",
                        str(output_dir),
                    ]
                )
            manifest = json.loads((output_dir / "manifest.json").read_text())
            preserved_source = source_path.read_bytes()

        self.assertEqual(code, 0)
        self.assertEqual(preserved_source, source_bytes)
        self.assertFalse(manifest["preprocessing"]["fitted"])
        self.assertFalse(manifest["training_performed"])
        self.assertEqual(
            manifest["source_file_sha256"], hashlib.sha256(source_bytes).hexdigest()
        )


if __name__ == "__main__":
    unittest.main()
