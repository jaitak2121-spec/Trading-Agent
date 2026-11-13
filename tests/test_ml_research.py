"""Causal, deterministic checks for the second ML research pipeline."""

from __future__ import annotations

import copy
import hashlib
import json
import math
import unittest

from trading.app.ml_research import (
    CONTEXT_CANDLES,
    FEATURE_NAMES,
    baseline_probabilities,
    binary_classification_metrics,
    block_bootstrap_confidence_intervals,
    confirmation_evaluation_phrase,
    evaluate_research_model,
    evaluate_frozen_confirmation,
    fit_final_development_model,
    fit_research_logistic,
    generate_causal_dataset,
    generate_walk_forward_manifest,
    preregistered_experiment_manifest,
    run_walk_forward_candidate,
    seal_confirmation_period,
)
from trading.app.historical_candles import validate_candle_dataset


MINUTE_MS = 60_000
DAY_MS = 86_400_000


def source(count: int = 2_000, *, gap_at: int | None = None) -> dict[str, object]:
    candles = []
    price = 30_000.0
    for index in range(count):
        if index == gap_at:
            continue
        price += ((index % 19) - 9) * 0.07
        open_ = price - ((index % 5) - 2) * 0.03
        high = max(open_, price) + 0.5
        low = min(open_, price) - 0.4
        candles.append(
            {
                "symbol": "BTC/USDT",
                "open_time": index * MINUTE_MS,
                "close_time": (index + 1) * MINUTE_MS,
                "interval_seconds": 60,
                "open": str(open_),
                "high": str(high),
                "low": str(low),
                "close": str(price),
                "volume": str(10 + index % 23),
                "source": "test",
            }
        )
    result = {
        "schema": "coinswitch-public-candles/v1",
        "symbol": "BTC/USDT",
        "exchange": "c2c1",
        "public_data_only": True,
        "credentials_used": True,
        "live_trading": False,
        "training_only": True,
        "candles": candles,
        "source":{'provider':'CoinSwitch','access':'authenticated-read-only','interval_minutes':1,
                  'requested_start_time':0,'requested_end_time':count*MINUTE_MS},
    }
    result['integrity']=validate_candle_dataset(result).as_details()
    return result


class MLResearchDatasetTest(unittest.TestCase):
    def test_generates_exact_causal_schema_for_every_horizon(self) -> None:
        for horizon in (5, 15, 30):
            with self.subTest(horizon=horizon):
                dataset = generate_causal_dataset(source(), horizon_minutes=horizon)
                samples = dataset["samples"]
                self.assertEqual(dataset["feature_names"], list(FEATURE_NAMES))
                self.assertEqual(len(samples[0]["features"]), 18)
                self.assertEqual(samples[0]["horizon_minutes"], horizon)
                self.assertEqual(
                    samples[0]["feature_open_time"]
                    - samples[0]["feature_window_start_open_time"],
                    (CONTEXT_CANDLES - 1) * MINUTE_MS,
                )
                for earlier, later in zip(samples, samples[1:]):
                    self.assertEqual(
                        later["feature_open_time"] - earlier["feature_open_time"],
                        horizon * MINUTE_MS,
                    )
                    self.assertLessEqual(
                        earlier["label_close_time"], later["label_open_time"]
                    )

    def test_future_changes_never_change_features(self) -> None:
        captured = source()
        original = generate_causal_dataset(captured, horizon_minutes=5)
        first = original["samples"][0]
        changed = copy.deepcopy(captured)
        future_index = first["feature_open_time"] // MINUTE_MS + 3
        candle = changed["candles"][future_index]
        candle["open"] = candle["close"] = candle["high"] = candle["low"] = "99999"
        changed.pop('integrity',None)
        changed['integrity']=validate_candle_dataset(changed).as_details()
        regenerated = generate_causal_dataset(changed, horizon_minutes=5)

        self.assertEqual(first["features"], regenerated["samples"][0]["features"])
        self.assertNotEqual(original["source_dataset_sha256"], regenerated["source_dataset_sha256"])

    def test_gaps_reset_warmup_and_no_sample_crosses_them(self) -> None:
        gap_at = 1_000
        dataset = generate_causal_dataset(
            source(2_200, gap_at=gap_at), horizon_minutes=15
        )

        self.assertEqual(len(dataset["segments"]), 2)
        for sample in dataset["samples"]:
            gap = gap_at * MINUTE_MS
            self.assertFalse(
                sample["feature_window_start_open_time"] < gap
                < sample["label_close_time"]
            )
        second = next(sample for sample in dataset["samples"] if sample["segment_id"] == 2)
        self.assertGreaterEqual(
            second["feature_open_time"],
            (gap_at + CONTEXT_CANDLES) * MINUTE_MS,
        )

    def test_feature_values_and_zero_range_are_explicit(self) -> None:
        captured = source()
        first = generate_causal_dataset(captured, horizon_minutes=5)["samples"][0]
        index = first["feature_open_time"] // MINUTE_MS
        candles = captured["candles"]
        expected_return = float(candles[index]["close"]) / float(candles[index - 5]["close"]) - 1

        self.assertAlmostEqual(first["features"]["return_5"], expected_return)
        self.assertEqual(first["features"]["zero_range"], 0.0)
        self.assertAlmostEqual(
            first["features"]["signed_body_fraction"],
            (float(candles[index]["close"]) - float(candles[index]["open"]))
            / (float(candles[index]["high"]) - float(candles[index]["low"])),
        )
        flat = copy.deepcopy(captured)
        flat_candle = flat["candles"][index]
        flat_candle["open"] = flat_candle["high"] = flat_candle["low"] = flat_candle["close"]
        flat.pop('integrity',None)
        flat['integrity']=validate_candle_dataset(flat).as_details()
        flat_first = generate_causal_dataset(flat, horizon_minutes=5)["samples"][0]
        self.assertEqual(flat_first["features"]["zero_range"], 1.0)
        self.assertEqual(flat_first["features"]["signed_body_fraction"], 0.0)

    def test_generation_and_hashes_are_reproducible(self) -> None:
        captured = source()
        first = generate_causal_dataset(captured, horizon_minutes=30)
        second = generate_causal_dataset(captured, horizon_minutes=30)
        self.assertEqual(first, second)
        self.assertEqual(
            first["dataset_sha256"],
            hashlib.sha256(
                json.dumps(
                    {key: value for key, value in first.items() if key != "dataset_sha256"},
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode()
            ).hexdigest(),
        )

    def test_stored_source_hash_mismatch_is_rejected(self) -> None:
        captured = source()
        captured["integrity"] = validate_candle_dataset(captured).as_details()
        captured["candles"][500]["close"] = captured["candles"][500]["open"]
        with self.assertRaisesRegex(ValueError, "stored candle hash"):
            generate_causal_dataset(captured, horizon_minutes=5)

    def test_walk_forward_is_deterministic_and_confirmation_stays_sealed(self) -> None:
        captured = source(8 * 24 * 60)
        dataset = generate_causal_dataset(captured, horizon_minutes=30)
        development, confirmation = seal_confirmation_period(
            dataset,
            confirmation_start_ms=6 * DAY_MS,
            range_end_ms=8 * DAY_MS,
        )
        arguments = {
            "range_start_ms": 0,
            "range_end_ms": 8 * DAY_MS,
            "training_days": 2,
            "validation_days": 1,
            "step_days": 1,
            "confirmation_days": 2,
        }
        first = generate_walk_forward_manifest(development, **arguments)
        second = generate_walk_forward_manifest(development, **arguments)

        self.assertEqual(first, second)
        self.assertEqual(len(first["folds"]), 4)
        self.assertTrue(first["confirmation"]["sealed"])
        self.assertFalse(first["confirmation"]["evaluated"])
        self.assertFalse(first["final_test"]["accessed"])
        self.assertTrue(confirmation["sealed"])
        self.assertFalse(confirmation["evaluated"])
        self.assertNotIn(confirmation["samples"][0], development["samples"])
        self.assertEqual(
            development["sealed_confirmation"]["artifact_sha256"],
            confirmation["artifact_sha256"],
        )
        for fold in first["folds"]:
            self.assertLessEqual(
                fold["training_end_exclusive"], fold["validation_start"]
            )
            self.assertEqual(
                fold["preprocessing_policy"],
                "fit on this fold's training rows only",
            )
        result = run_walk_forward_candidate(
            development,
            first,
            {
                "horizon_minutes": 30,
                "feature_set": "price_baseline_3",
                "features": ["return_1", "return_5", "range_fraction"],
                "l2_penalty": 0.001,
            },
            preregistration_sha256="a" * 64,
            bootstrap_iterations=20,
        )
        self.assertEqual(len(result["folds"]), 4)
        self.assertFalse(result["acceptance_criteria"]["required_six_folds"])
        self.assertFalse(result["accepted"])
        self.assertFalse(result["confirmation_loaded"])
        self.assertFalse(result["confirmation_evaluated"])
        self.assertFalse(result["final_test_accessed"])
        candidate = result["candidate_configuration"]
        result["acceptance_criteria"] = {
            key: True for key in result["acceptance_criteria"]
        }
        result["accepted"] = True
        result.pop("result_sha256")
        result["result_sha256"] = hashlib.sha256(
            json.dumps(result, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
        preregistration = {"candidates": [candidate]}
        preregistration["manifest_sha256"] = hashlib.sha256(
            json.dumps(
                preregistration, sort_keys=True, separators=(",", ":")
            ).encode()
        ).hexdigest()
        selection = {
            "schema": "frozen-development-candidate/v2",
            "candidate_id": result["candidate_id"],
            "candidate_configuration": candidate,
            "development_result_sha256": result["result_sha256"],
            "development_artifact_sha256": development["artifact_sha256"],
            "preregistration_sha256": preregistration["manifest_sha256"],
            "development_accepted": True,
            "new_model_fitted_after_selection": False,
            "confirmation_loaded": False,
            "confirmation_evaluated": False,
            "final_test_accessed": False,
        }
        selection["freeze_sha256"] = hashlib.sha256(
            json.dumps(selection, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
        final_model = fit_final_development_model(
            development, selection, result, preregistration
        )
        phrase = confirmation_evaluation_phrase(
            final_model, development["sealed_confirmation"]
        )
        self.assertEqual(
            final_model["development_samples_used"], len(development["samples"])
        )
        self.assertEqual(final_model["confirmation_rows_used_for_fitting"], 0)
        self.assertIn(final_model["model_sha256"], phrase)
        self.assertIn(
            development["sealed_confirmation"]["artifact_sha256"], phrase
        )
        with self.assertRaisesRegex(ValueError, "authorization phrase"):
            evaluate_frozen_confirmation(
                final_model,
                development,
                confirmation,
                result,
                preregistration,
                phrase + "x",
                bootstrap_iterations=20,
            )
        evidence = evaluate_frozen_confirmation(
            final_model,
            development,
            confirmation,
            result,
            preregistration,
            phrase,
            bootstrap_iterations=20,
        )
        self.assertEqual(evidence["samples"], len(confirmation["samples"]))
        self.assertTrue(evidence["confirmation_evaluated_once"])
        self.assertFalse(evidence["model_retrained"])
        self.assertFalse(evidence["threshold_optimized"])
        self.assertFalse(evidence["final_test_accessed"])


class MLResearchModelTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.dataset = generate_causal_dataset(source(4_000), horizon_minutes=5)
        cls.training = cls.dataset["samples"][:300]
        cls.validation = cls.dataset["samples"][300:500]

    def test_training_is_deterministic_and_scaling_uses_training_only(self) -> None:
        first = fit_research_logistic(
            self.training, feature_names=FEATURE_NAMES, l2_penalty=0.001
        )
        second = fit_research_logistic(
            self.training, feature_names=FEATURE_NAMES, l2_penalty=0.001
        )
        changed_validation = copy.deepcopy(self.validation)
        changed_validation[0]["features"]["return_1"] = 1000
        third = fit_research_logistic(
            self.training, feature_names=FEATURE_NAMES, l2_penalty=0.001
        )

        self.assertEqual(first, second)
        self.assertEqual(first, third)
        self.assertEqual(first["configuration"]["preprocessing_fit"], "training_rows_only")
        self.assertFalse(first["threshold_optimized"])
        self.assertFalse(first["confirmation_evaluated"])
        self.assertFalse(first["final_test_accessed"])
        self.assertIn("roc_auc", evaluate_research_model(first, changed_validation))

    def test_baselines_and_metrics_include_required_comparisons(self) -> None:
        labels = [sample["label"] for sample in self.training]
        baselines = baseline_probabilities(
            labels, self.validation, horizon_minutes=5
        )
        self.assertEqual(
            set(baselines),
            {"majority", "constant_prevalence", "momentum", "contrarian"},
        )
        metrics = binary_classification_metrics(
            [0, 0, 1, 1], [0.1, 0.4, 0.35, 0.8]
        )
        self.assertEqual(metrics["roc_auc"], 0.75)
        self.assertEqual(metrics["balanced_accuracy"], 0.75)
        self.assertAlmostEqual(metrics["average_precision"], 0.833333, places=6)
        self.assertEqual(metrics["brier_score"], 0.158125)
        self.assertFalse(metrics["threshold_optimized"])

    def test_block_bootstrap_is_seeded_and_reproducible(self) -> None:
        labels = [sample["label"] for sample in self.validation]
        probabilities = [0.7 if label else 0.3 for label in labels]
        first = block_bootstrap_confidence_intervals(
            labels, probabilities, block_size=12, iterations=50, seed=7
        )
        second = block_bootstrap_confidence_intervals(
            labels, probabilities, block_size=12, iterations=50, seed=7
        )
        self.assertEqual(first, second)
        self.assertEqual(first["roc_auc_95_interval"], [1.0, 1.0])

    def test_preregistration_is_small_hashed_and_unfitted(self) -> None:
        source_hash = "a" * 64
        manifest = preregistered_experiment_manifest(
            source_dataset_sha256=source_hash,
            walk_forward_manifest_sha256=["b" * 64, "c" * 64, "d" * 64],
        )
        self.assertEqual(len(manifest["candidates"]), 12)
        self.assertFalse(manifest["model_fitted"])
        self.assertTrue(manifest["confirmation"]["sealed"])
        self.assertFalse(manifest["final_test"]["accessed"])
        self.assertTrue(math.isclose(manifest["threshold"], 0.5))


if __name__ == "__main__":
    unittest.main()
