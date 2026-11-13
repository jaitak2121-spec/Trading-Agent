"""Explicitly approved ML inference for paper trading only."""

from __future__ import annotations

import decimal
import copy
from decimal import Decimal
from typing import Mapping

from ..core.money import FINANCIAL_CONTEXT, ROUND_DOWN, Price
from ..strategy.context import MarketContext
from ..strategy.examples import SMALLEST_PRICE
from ..strategy.indicators import atr
from ..strategy.signals import Signal, SignalDirection, SignalStrategy
from .model_training import (
    predict_direction_probability,
    predict_logistic_probabilities,
)
from .model_deployment import model_identity, validate_paper_deployment
from .ml_research import CONTEXT_CANDLES, features_from_closed_candles

__all__ = ["PaperDirectionModelStrategy"]


class PaperDirectionModelStrategy(SignalStrategy):
    """Long-only model trial that can only run behind the paper broker."""

    name = "ml-direction-paper-trial"

    def __init__(
        self,
        *,
        model: Mapping[str, object],
        evaluation: Mapping[str, object],
        confirmation: str,
        symbol: str,
        atr_period: int = 14,
        approval_check=None,
        exchange: str | None = None,
    ) -> None:
        validate_paper_deployment(model, evaluation, confirmation)
        if not isinstance(symbol, str) or not symbol.strip():
            raise ValueError("symbol must be a non-empty string")
        if symbol.strip().upper() != model["symbol"]:
            raise ValueError("paper symbol must match the model training symbol")
        if exchange is not None and exchange.lower() != model['exchange']:
            raise ValueError('paper exchange must match the model training exchange')
        if isinstance(atr_period, bool) or not isinstance(atr_period, int):
            raise TypeError("atr_period must be an int")
        if atr_period <= 0:
            raise ValueError("atr_period must be positive")
        self._model = copy.deepcopy(dict(model))
        self._v2 = model.get('schema') == 'offline-logistic-classifier/v2'
        self._approval_check = approval_check
        self._evaluation = copy.deepcopy(dict(evaluation))
        self._symbol = symbol.strip().upper()
        self._atr_period = atr_period
        self._model_hash = model_identity(model)
        self.last_prediction = None
        self.name = f"ml-direction-paper-trial:{self._model_hash[:12]}"

    @property
    def required_bars(self) -> int:
        return max(CONTEXT_CANDLES if self._v2 else 4, self._atr_period + 1)

    @property
    def model_metadata(self):
        return {'model_sha256':self._model_hash, 'schema':self._model.get('schema'),
                'training_configuration':self._model.get('training_configuration',self._model.get('configuration')),
                'horizon_minutes':self._model.get('horizon_minutes',1),
                'features':list(self._model['features']),
                'symbol':self._model['symbol'],'exchange':self._model['exchange'],
                'paper_inference_start_ms':self._model.get('paper_inference_start_ms'),
                'trained_at':self._model.get('trained_at'),
                'evaluation_metrics':self._evaluation.get('metrics'),
                'paper_only':True}

    @property
    def monitor_reference(self):
        return copy.deepcopy(self._model)

    def generate(self, context: MarketContext) -> list[Signal]:
        if self._approval_check is not None:
            self._approval_check()
        self.last_prediction = None
        quote = context.quote(self._symbol)
        bars = context.candles(self._symbol)
        if quote is None or len(bars) < self.required_bars:
            return []
        if any(bar.interval_seconds != 60 for bar in bars[-self.required_bars:]):
            return []
        close = bars[-1].close.amount
        features = [
            float(close / bars[-2].close.amount - 1),
            float(close / bars[-4].close.amount - 1),
            float(bars[-1].range / close),
        ]
        horizon_seconds = 60
        if self._v2:
            if int(bars[-1].open_time.timestamp()*1000) < self._model['paper_inference_start_ms']:
                return []
            horizon = self._model['horizon_minutes']
            if (int(bars[-1].open_time.timestamp()) // 60) % horizon:
                return []
            generated = features_from_closed_candles(bars, symbol=self._symbol)
            if generated is None:
                return []
            features = [generated[name] for name in self._model['features']]
            probability = predict_logistic_probabilities(self._model, [features])[0]
            horizon_seconds = horizon * 60
        else:
            probability = predict_direction_probability(self._model, features)
        threshold = float(self._model["threshold"])
        evidence = {
            "model_sha256": self._model_hash,
            "evaluation_dataset_sha256": str(
                self._evaluation.get("evaluation_dataset_sha256", self._evaluation.get('confirmation_artifact_sha256'))
            ),
            "up_probability": f"{probability:.6f}",
            "threshold": f"{threshold:.6f}",
            "bar_open_time": bars[-1].open_time.isoformat(),
        }
        self.last_prediction = {**evidence, 'features': features,
                                'at': context.as_of.isoformat(), 'symbol': self._symbol,
                                'reference_close':str(close), 'horizon_seconds':horizon_seconds,
                                'prediction_id':f'{self._model_hash}:{self._symbol}:{bars[-1].open_time.isoformat()}'}
        if probability < threshold:
            if context.is_flat(self._symbol):
                return []
            return [
                Signal(
                    strategy_name=self.name,
                    signal_id=self._signal_id(context, SignalDirection.EXIT),
                    symbol=self._symbol,
                    direction=SignalDirection.EXIT,
                    reference_price=quote.mid,
                    as_of=context.as_of,
                    rationale="the approved paper model fell below its exit threshold",
                    evidence=evidence,
                    warnings=("paper-only ML trial; never approved for live execution",),
                )
            ]
        if not context.is_flat(self._symbol):
            return []
        volatility = atr(bars, self._atr_period)
        if volatility is None or volatility == 0:
            return []
        reference = quote.mid
        with decimal.localcontext(FINANCIAL_CONTEXT):
            risk = volatility * Decimal("2")
            stop_amount = reference.amount - risk
            target_amount = reference.amount + risk * Decimal("2")
        if stop_amount <= SMALLEST_PRICE:
            return []
        stop = Price.rounded(stop_amount, reference.currency, rounding=ROUND_DOWN)
        target = Price.rounded(target_amount, reference.currency, rounding=ROUND_DOWN)
        if not stop < reference or not target > reference:
            return []
        evidence[f"atr_{self._atr_period}"] = str(volatility)
        return [
            Signal(
                strategy_name=self.name,
                signal_id=self._signal_id(context, SignalDirection.LONG),
                symbol=self._symbol,
                direction=SignalDirection.LONG,
                reference_price=reference,
                as_of=context.as_of,
                rationale=(
                    "the independently evaluated paper model exceeded its entry "
                    "threshold; stop and target use fixed ATR risk multiples"
                ),
                conviction=Decimal(str(probability)),
                stop_loss=stop,
                take_profit=target,
                evidence=evidence,
                warnings=("paper-only ML trial; never approved for live execution",),
            )
        ]

    def _signal_id(
        self, context: MarketContext, direction: SignalDirection
    ) -> str:
        bar_time = context.candles(self._symbol)[-1].open_time.isoformat()
        return f"{self.name}:{self._symbol}:{bar_time}:{direction.value}"
