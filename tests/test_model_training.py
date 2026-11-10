"""Offline model training stays chronological, explicit, and paper-only."""

from __future__ import annotations

import contextlib
from datetime import datetime, timedelta
import io
import json
import tempfile
import unittest
from pathlib import Path

from trading.app.model_training import (
    direction_model_sha256,
    evaluate_direction_model,
    paper_model_approval_phrase,
    split_direction_datasets,
    train_direction_model,
    validate_paper_model_trial,
)
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


class DirectionModelTrainingTest(unittest.TestCase):
    def test_splits_one_capture_into_non_overlapping_model_datasets(self) -> None:
        training, evaluation = split_direction_datasets(dataset())

        self.assertEqual(len(training["candles"]), 150)
        self.assertEqual(len(evaluation["candles"]), 150)
        self.assertEqual(training["segment"], "training")
        self.assertEqual(evaluation["segment"], "evaluation")
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
