"""Offline model training stays chronological, explicit, and paper-only."""

from __future__ import annotations

import contextlib
from datetime import datetime, timedelta, timezone
import hashlib
import io
import json
import tempfile
import unittest
from pathlib import Path

from trading.app.model_training import (
    diagnose_direction_model_robustness,
    direction_model_sha256,
    evaluate_direction_model,
    paper_model_approval_phrase,
    predict_direction_probability,
    split_direction_datasets,
    train_direction_model,
    train_validated_direction_model,
    validate_paper_model_trial,
)
from trading.app.historical_candles import validate_candle_dataset
from trading.app.model_strategy import PaperDirectionModelStrategy
from trading.core.clock import UTC
from trading.core.marketdata import Candle, MarketSnapshot, Quote
from trading.core.money import USDT, Money, Price, Quantity
from trading.strategy import MarketContext, SignalDirection


def dataset(count: int = 300, *, start_time: int = 0) -> dict[str, object]:
    candles = []
    price = 100
    for index in range(count):
        price += 1 if (index // 5) % 2 == 0 else -1
        candles.append(
            {
                "open_time": start_time + index * 60_000,
                "interval_seconds": 60,
                "open": str(price),
                "high": str(price + 1),
                "low": str(price - 1),
                "close": str(price),
                "volume": "1",
            }
        )
    return {
        "schema": "coinswitch-public-candles/v1",
        "symbol": "BTC/USDT",
        "exchange": "c2c1",
        "public_data_only": True,
        "credentials_used": False,
        "live_trading": False,
        "training_only": True,
        "candles": candles,
    }


def marked_dataset(
    assignment: str, segment_id: int, *, start_time: int
) -> dict[str, object]:
    captured = dataset(start_time=start_time)
    captured["integrity"] = validate_candle_dataset(captured).as_details()
    captured["ml_segment"] = {
        "schema": "contiguous-ml-segment/v1",
        "assignment": assignment,
        "segment_id": segment_id,
        "preprocessing_fitted": False,
        "source_file_sha256": "a" * 64,
        "source_dataset_sha256": "b" * 64,
    }
    return captured


class DirectionModelTrainingTest(unittest.TestCase):
    def test_training_cadence_must_match_one_minute_paper_inference(self):
        captured=dataset()
        for i,row in enumerate(captured['candles']):
            row['open_time']=i*300000
            row['interval_seconds']=300
        with self.assertRaisesRegex(ValueError,'one-minute'):
            train_direction_model(captured)
        self.assertEqual(train_direction_model(dataset()).model['interval_seconds'],60)

    def test_splits_one_capture_into_non_overlapping_model_datasets(self) -> None:
        captured = dataset()
        captured["integrity"] = validate_candle_dataset(captured).as_details()
        training, evaluation = split_direction_datasets(captured)

        self.assertEqual(len(training["candles"]), 150)
        self.assertEqual(len(evaluation["candles"]), 150)
        self.assertEqual(training["segment"], "training")
        self.assertEqual(evaluation["segment"], "evaluation")
        self.assertTrue(training["integrity"]["passed"])
        self.assertTrue(evaluation["integrity"]["passed"])
        self.assertNotEqual(
            training["integrity"]["candle_sha256"],
            captured["integrity"]["candle_sha256"],
        )
        model = train_direction_model(training).model
        self.assertIsInstance(evaluate_direction_model(model, evaluation).accuracy, float)
        with self.assertRaisesRegex(ValueError, "at least 240"):
            split_direction_datasets(dataset(239))

    def test_trains_on_past_and_keeps_candidate_unapproved(self) -> None:
        report = train_direction_model(dataset())

        self.assertGreater(report.validation_accuracy, report.baseline_accuracy)
        self.assertTrue(report.eligible_for_paper_validation)
        self.assertEqual(report.model["schema"], "offline-direction-model/v1")
        self.assertFalse(report.model["approved"])
        self.assertTrue(report.model["paper_only"])
        self.assertFalse(report.model["self_promoting"])
        self.assertEqual(
            report.model["training_configuration"],
            {
                "training_fraction": 0.7,
                "iterations": 400,
                "learning_rate": 0.1,
                "l2_penalty": 0.001,
                "target_horizon_candles": 1,
                "random_seed": 0,
                "randomness_used": False,
            },
        )

    def test_validated_training_persists_and_predicts_deterministically(self) -> None:
        training = marked_dataset("training", 1, start_time=0)
        validation = marked_dataset("validation", 3, start_time=30_000_000)
        timestamp = datetime(2026, 11, 11, tzinfo=timezone.utc)

        first, first_validation = train_validated_direction_model(
            training, validation, trained_at=timestamp
        )
        second, second_validation = train_validated_direction_model(
            training, validation, trained_at=timestamp
        )
        restored = json.loads(json.dumps(first.model))
        features = [0.01, -0.02, 0.03]

        self.assertEqual(first.model, second.model)
        self.assertEqual(first_validation.evidence, second_validation.evidence)
        self.assertEqual(
            predict_direction_probability(first.model, features),
            predict_direction_probability(restored, features),
        )
        self.assertEqual(first.model["validation_dataset_sha256"], first_validation.evidence["evaluation_dataset_sha256"])
        self.assertIn("model_content_sha256", first.model)
        self.assertIn("roc_auc", first_validation.evidence["metrics"])

    def test_validated_training_enforces_feature_and_dataset_provenance(self) -> None:
        training = marked_dataset("training", 1, start_time=0)
        validation = marked_dataset("validation", 3, start_time=30_000_000)
        report, _ = train_validated_direction_model(training, validation)
        incompatible = dict(report.model)
        incompatible["features"] = ["return_1"]

        with self.assertRaisesRegex(ValueError, "features are invalid"):
            predict_direction_probability(incompatible, [0.1, 0.2, 0.3])
        validation["ml_segment"]["segment_id"] = 7  # type: ignore[index]
        validation["ml_segment"]["assignment"] = "test"  # type: ignore[index]
        with self.assertRaisesRegex(ValueError, "validation segment 3"):
            train_validated_direction_model(training, validation)

    def test_robustness_diagnosis_is_frozen_deterministic_and_gap_safe(self) -> None:
        training = marked_dataset("training", 1, start_time=0)
        validation = marked_dataset("validation", 3, start_time=30_000_000)
        robustness_5 = marked_dataset("robustness", 5, start_time=60_000_000)
        robustness_6 = marked_dataset("robustness", 6, start_time=90_000_000)
        trained, _ = train_validated_direction_model(
            training,
            validation,
            trained_at=datetime(2026, 11, 11, tzinfo=timezone.utc),
        )
        frozen_model = json.loads(json.dumps(trained.model))

        first = diagnose_direction_model_robustness(
            trained.model, training, validation, robustness_5, robustness_6
        )
        second = diagnose_direction_model_robustness(
            trained.model, training, validation, robustness_5, robustness_6
        )

        self.assertEqual(first, second)
        self.assertEqual(trained.model, frozen_model)
        self.assertEqual(set(first["evaluations"]), {"segment_3", "segment_5", "segment_6"})
        self.assertFalse(first["threshold_optimized"])
        self.assertFalse(first["model_retrained"])
        self.assertFalse(first["final_test_accessed"])
        for evaluation in first["evaluations"].values():
            self.assertIn("positive_prediction_rate", evaluation["metrics"])
            self.assertIn("feature_distribution", evaluation)
            self.assertIn("brier_score", evaluation["calibration"])

    def test_robustness_diagnosis_rejects_final_test_provenance(self) -> None:
        training = marked_dataset("training", 1, start_time=0)
        validation = marked_dataset("validation", 3, start_time=30_000_000)
        robustness_5 = marked_dataset("robustness", 5, start_time=60_000_000)
        final_test = marked_dataset("test", 7, start_time=90_000_000)
        trained, _ = train_validated_direction_model(training, validation)

        with self.assertRaisesRegex(ValueError, "robustness segment 6"):
            diagnose_direction_model_robustness(
                trained.model, training, validation, robustness_5, final_test
            )

    def test_rejects_short_or_unsafe_datasets(self) -> None:
        short = dataset(20)
        with self.assertRaisesRegex(ValueError, "at least 120"):
            train_direction_model(short)
        unsafe = dataset()
        unsafe["live_trading"] = True
        with self.assertRaisesRegex(ValueError, "safety markers"):
            train_direction_model(unsafe)
        gapped = dataset()
        gapped["candles"][10]["open_time"] += 60_000  # type: ignore[index]
        with self.assertRaisesRegex(ValueError, "chronological"):
            train_direction_model(gapped)
        invalid_volume = dataset()
        invalid_volume["candles"][10]["volume"] = "-1"  # type: ignore[index]
        with self.assertRaisesRegex(ValueError, "volume"):
            train_direction_model(invalid_volume)

    def test_authenticated_public_candles_remain_training_eligible(self) -> None:
        captured = dataset()
        captured["credentials_used"] = True

        report = train_direction_model(captured)

        self.assertGreater(report.training_samples, 0)

    def test_evaluates_only_on_a_distinct_dataset(self) -> None:
        training = dataset()
        model = train_direction_model(training).model
        evaluation = evaluate_direction_model(
            model, dataset(start_time=30_000_000)
        )

        self.assertGreater(evaluation.accuracy, evaluation.baseline_accuracy)
        self.assertTrue(evaluation.eligible_for_paper_trial)
        self.assertFalse(evaluation.evidence["approved"])
        self.assertFalse(evaluation.evidence["live_trading"])
        with self.assertRaisesRegex(ValueError, "must differ"):
            evaluate_direction_model(model, training)
        with self.assertRaisesRegex(ValueError, "must not overlap"):
            evaluate_direction_model(model, dataset(start_time=60_000))
        later_model = train_direction_model(dataset(start_time=30_000_000)).model
        with self.assertRaisesRegex(ValueError, "must begin after"):
            evaluate_direction_model(later_model, dataset())
        wrong_market = dataset(start_time=30_000_000)
        wrong_market["symbol"] = "ETH/USDT"
        with self.assertRaisesRegex(ValueError, "market must match"):
            evaluate_direction_model(model, wrong_market)

    def test_paper_trial_requires_exact_model_bound_approval(self) -> None:
        model = train_direction_model(dataset()).model
        evidence = evaluate_direction_model(
            model, dataset(start_time=30_000_000)
        ).evidence

        validate_paper_model_trial(
            model, evidence, paper_model_approval_phrase(model)
        )
        with self.assertRaisesRegex(ValueError, "confirmation must exactly match"):
            validate_paper_model_trial(model, evidence, "APPROVE PAPER MODEL")
        changed = dict(model)
        changed["bias"] = float(changed["bias"]) + 0.01
        with self.assertRaisesRegex(ValueError, "not eligible"):
            validate_paper_model_trial(
                changed, evidence, paper_model_approval_phrase(changed)
            )

    def test_approved_model_emits_only_an_explained_paper_signal(self) -> None:
        model = train_direction_model(dataset()).model
        model["weights"] = [0.0, 0.0, 0.0]
        model["bias"] = 10.0
        model_hash = direction_model_sha256(model)
        evidence = {
            "schema": "offline-direction-evaluation/v1",
            "model_sha256": model_hash,
            "evaluation_dataset_sha256": "a" * 64,
            "samples": 100,
            "eligible_for_paper_trial": True,
            "approved": False,
            "paper_only": True,
            "live_trading": False,
            "self_promoting": False,
        }
        strategy = PaperDirectionModelStrategy(
            model=model,
            evaluation=evidence,
            confirmation=paper_model_approval_phrase(model),
            symbol="BTC/USDT",
        )
        now = datetime(2026, 1, 1, 1, 0, tzinfo=UTC)
        bars = []
        for index in range(20):
            value = 100 + index
            bars.append(
                Candle(
                    symbol="BTC/USDT",
                    open_time=now - timedelta(minutes=20 - index),
                    interval_seconds=60,
                    open=Price(value, USDT),
                    high=Price(value + 1, USDT),
                    low=Price(value - 1, USDT),
                    close=Price(value, USDT),
                    volume=Quantity("1", "BTC"),
                    source="test",
                )
            )
        quote = Quote(
            symbol="BTC/USDT",
            bid=Price("119.9", USDT),
            ask=Price("120.1", USDT),
            as_of=now,
            source="test",
        )
        context = MarketContext(
            as_of=now,
            equity=Money("10000", USDT),
            snapshot=MarketSnapshot(now, {"BTC/USDT": quote}),
            history={"BTC/USDT": bars},
        )

        signals = strategy.generate(context)

        self.assertEqual(len(signals), 1)
        self.assertIs(signals[0].direction, SignalDirection.LONG)
        self.assertEqual(
            signals[0].strategy_name,
            f"ml-direction-paper-trial:{model_hash[:12]}",
        )
        self.assertEqual(signals[0].evidence["model_sha256"], model_hash)
        self.assertIn("paper-only", signals[0].warnings[0])

    def test_cli_writes_model_atomically(self) -> None:
        from trading.app.__main__ import main

        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "candles.json"
            output = Path(directory) / "model.json"
            source.write_text(json.dumps(dataset()), encoding="utf-8")
            with contextlib.redirect_stdout(io.StringIO()):
                code = main(
                    ["--train-model", str(source), "--model-output", str(output)]
                )
            model = json.loads(output.read_text(encoding="utf-8"))

        self.assertEqual(code, 0)
        self.assertFalse(model["approved"])
        self.assertTrue(model["eligible_for_paper_validation"])

    def test_cli_writes_provenance_bound_validation_evidence(self) -> None:
        from trading.app.__main__ import main

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            training_path = root / "training.json"
            validation_path = root / "validation.json"
            model_path = root / "model.json"
            evidence_path = root / "evidence.json"
            training_path.write_text(
                json.dumps(marked_dataset("training", 1, start_time=0)),
                encoding="utf-8",
            )
            validation_path.write_text(
                json.dumps(marked_dataset("validation", 3, start_time=30_000_000)),
                encoding="utf-8",
            )
            with contextlib.redirect_stdout(io.StringIO()):
                code = main(
                    [
                        "--train-model",
                        str(training_path),
                        "--model-output",
                        str(model_path),
                        "--validation-data",
                        str(validation_path),
                        "--validation-output",
                        str(evidence_path),
                    ]
                )
            model = json.loads(model_path.read_text())
            evidence = json.loads(evidence_path.read_text())
            model_file_sha256 = hashlib.sha256(model_path.read_bytes()).hexdigest()

        self.assertEqual(code, 0)
        self.assertEqual(evidence["model_sha256"], direction_model_sha256(model))
        self.assertEqual(
            evidence["model_artifact_sha256"],
            model_file_sha256,
        )
        self.assertIn("confusion_matrix", evidence["metrics"])

    def test_cli_diagnoses_only_approved_segments_and_never_opens_test(self) -> None:
        from trading.app.__main__ import main

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            datasets = {
                "training": marked_dataset("training", 1, start_time=0),
                "validation": marked_dataset("validation", 3, start_time=30_000_000),
                "robustness_5": marked_dataset(
                    "robustness", 5, start_time=60_000_000
                ),
                "robustness_6": marked_dataset(
                    "robustness", 6, start_time=90_000_000
                ),
            }
            records = {}
            for key, captured in datasets.items():
                path = root / f"{key}.json"
                path.write_text(json.dumps(captured), encoding="utf-8")
                records[key] = {
                    "path": path.name,
                    "file_sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                }
            records["test"] = {
                "path": "must-not-be-opened.json",
                "file_sha256": "0" * 64,
            }
            manifest_path = root / "manifest.json"
            manifest_path.write_text(
                json.dumps({"artifacts": records}), encoding="utf-8"
            )
            trained, _ = train_validated_direction_model(
                datasets["training"], datasets["validation"]
            )
            model_path = root / "model.json"
            model_path.write_text(json.dumps(trained.model), encoding="utf-8")
            evidence_path = root / "robustness.json"

            with contextlib.redirect_stdout(io.StringIO()):
                code = main(
                    [
                        "--diagnose-model",
                        str(model_path),
                        "--segment-manifest",
                        str(manifest_path),
                        "--diagnostic-output",
                        str(evidence_path),
                    ]
                )
            evidence = json.loads(evidence_path.read_text())

        self.assertEqual(code, 0)
        self.assertFalse(evidence["final_test_accessed"])
        self.assertEqual(
            set(evidence["evaluations"]), {"segment_3", "segment_5", "segment_6"}
        )

    def test_cli_writes_out_of_sample_evidence(self) -> None:
        from trading.app.__main__ import main

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            training_path = root / "training.json"
            evaluation_path = root / "evaluation.json"
            model_path = root / "model.json"
            evidence_path = root / "evidence.json"
            training_path.write_text(json.dumps(dataset()), encoding="utf-8")
            evaluation_path.write_text(
                json.dumps(dataset(start_time=30_000_000)), encoding="utf-8"
            )
            with contextlib.redirect_stdout(io.StringIO()):
                main(
                    [
                        "--train-model",
                        str(training_path),
                        "--model-output",
                        str(model_path),
                    ]
                )
                code = main(
                    [
                        "--evaluate-model",
                        str(model_path),
                        "--evaluation-data",
                        str(evaluation_path),
                        "--evaluation-output",
                        str(evidence_path),
                    ]
                )
            evidence = json.loads(evidence_path.read_text(encoding="utf-8"))

        self.assertEqual(code, 0)
        self.assertEqual(evidence["schema"], "offline-direction-evaluation/v1")
        self.assertFalse(evidence["approved"])


if __name__ == "__main__":
    unittest.main()
