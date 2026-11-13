"""Fixed research baselines; never selected automatically by the paper app."""
from decimal import Decimal

from .signals import SignalStrategy, Signal, SignalDirection
from .indicators import atr
from ..core.money import Price


class HistoricalRuleStrategy(SignalStrategy):
    required_bars = 31

    def __init__(self, rule):
        if rule not in {'momentum', 'mean-reversion', 'volatility-filtered-momentum'}:
            raise ValueError('unknown fixed research rule')
        self.rule = rule
        self.name = 'research-' + rule

    @property
    def configuration(self):
        return {'rule':self.rule, 'lookback':30, 'threshold':'0.005', 'atr_period':14,
                'stop_atr_multiple':'2', 'reward_multiple':'2', 'range_filter':'0.01'}

    def generate(self, context):
        signals = []
        for symbol in context.tradable_symbols:
            bars = context.candles(symbol)
            if len(bars) < self.required_bars:
                continue
            close = bars[-1].close.amount
            momentum = close / bars[-31].close.amount - 1
            mean = sum((bar.close.amount for bar in bars[-30:]), Decimal(0)) / 30
            deviation = close / mean - 1
            enter = momentum > Decimal('0.005')
            exit_rule = momentum <= 0
            if self.rule == 'mean-reversion':
                enter, exit_rule = deviation < Decimal('-0.005'), deviation >= 0
            if self.rule == 'volatility-filtered-momentum':
                enter = enter and bars[-1].range / close < Decimal('0.01')
            direction = None
            if context.is_flat(symbol) and enter:
                direction = SignalDirection.LONG
            elif not context.is_flat(symbol) and exit_rule:
                direction = SignalDirection.EXIT
            if direction is None:
                continue
            reference = context.price(symbol)
            volatility = atr(bars, 14)
            if volatility is None or volatility <= 0 or reference.amount <= 2 * volatility:
                continue
            options = {}
            if direction == SignalDirection.LONG:
                options = {'stop_loss': Price.rounded(reference.amount-2*volatility, reference.currency),
                           'take_profit': Price.rounded(reference.amount+4*volatility, reference.currency)}
            signals.append(Signal(strategy_name=self.name,
                signal_id=f'{self.name}:{symbol}:{bars[-1].open_time.isoformat()}:{direction.value}',
                symbol=symbol, direction=direction, reference_price=reference,
                as_of=context.as_of, rationale='fixed development research rule',
                evidence={'momentum_30':str(momentum), 'deviation_sma_30':str(deviation)}, **options))
        return signals
