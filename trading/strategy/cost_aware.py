"""Fixed slower research hypotheses, not production strategy replacements."""
from decimal import Decimal
from datetime import datetime, timezone

from ..core.marketdata import Candle
from ..core.money import Price, Quantity
from .context import MarketContext
from .indicators import atr, sma
from .signals import Signal, SignalDirection, SignalStrategy


def aggregate_complete_minutes(candles, minutes):
    """Aggregate only complete UTC-aligned buckets of genuine minute candles."""
    if type(minutes) is not int or minutes not in (5,15,30):
        raise ValueError('supported research frames are 5, 15, 30 minutes')
    groups={}
    previous=None
    market=None
    for candle in candles:
        if candle.interval_seconds != 60 or candle.open_time.second or candle.open_time.microsecond:
            raise ValueError('aggregation requires aligned one-minute candles')
        identity=(candle.symbol,candle.close.currency,candle.volume.asset,candle.source)
        if market is not None and identity != market:
            raise ValueError('aggregation cannot mix markets or sources')
        market=identity
        if previous is not None and candle.open_time <= previous:
            raise ValueError('aggregation requires unique chronological candles')
        previous=candle.open_time
        stamp=int(candle.open_time.timestamp())
        groups.setdefault(stamp//(minutes*60)*(minutes*60),[]).append(candle)
    result=[]
    for start,group in groups.items():
        if len(group) != minutes or any(int(c.open_time.timestamp()) != start+i*60 for i,c in enumerate(group)):
            continue
        first,last=group[0],group[-1]
        result.append(Candle(symbol=first.symbol,open_time=datetime.fromtimestamp(start,timezone.utc),
            interval_seconds=minutes*60,open=first.open,close=last.close,
            high=max((c.high for c in group),key=lambda p:p.amount),
            low=min((c.low for c in group),key=lambda p:p.amount),
            volume=Quantity(sum((c.volume.amount for c in group),Decimal(0)),first.volume.asset,max_scale=18),
            source='aggregated:'+first.source))
    return tuple(result)


class CostAwareRule(SignalStrategy):
    required_bars=51

    def __init__(self, rule):
        if rule not in ('breakout','trend-pullback'):
            raise ValueError('unknown cost-aware research rule')
        self.rule=rule
        self.name='cost-research-'+rule

    @property
    def configuration(self):
        return {'rule':self.rule,'trend_bars':50,'pullback_bars':20,'breakout_bars':20,
                'exit_channel_bars':10,'atr_period':14,'stop_atr_multiple':'2',
                'pullback_target_atr_multiple':'4','minimum_stop_fraction':'0.0052',
                'cost_gate':'2 ATR must cover twice the fixed base 26bp round-trip assumption'}

    def generate(self,context):
        signals=[]
        for symbol in context.tradable_symbols:
            bars=context.candles(symbol)
            if len(bars) < self.required_bars:
                continue
            close=bars[-1].close.amount
            trend=sma(bars,50).amount
            prior_trend=sma(bars[:-1],50).amount
            volatility=atr(bars,14)
            if volatility is None or volatility <= 0:
                continue
            if self.rule == 'breakout':
                entry=close > max(c.high.amount for c in bars[-21:-1])
                exit_rule=close < min(c.low.amount for c in bars[-11:-1])
            else:
                fast=sma(bars,20).amount
                prior_fast=sma(bars[:-1],20).amount
                entry=bars[-2].close.amount <= prior_fast and close > fast and fast > trend
                exit_rule=close < trend
            direction=None
            if not context.is_flat(symbol) and exit_rule:
                direction=SignalDirection.EXIT
            elif (context.is_flat(symbol) and entry and close > trend and trend > prior_trend
                  and 2*volatility/close >= Decimal('0.0052')):
                direction=SignalDirection.LONG
            if direction is None:
                continue
            reference=context.price(symbol)
            options={}
            if direction is SignalDirection.LONG:
                if reference.amount <= 2*volatility:
                    continue
                options['stop_loss']=Price.rounded(reference.amount-2*volatility,reference.currency)
                if self.rule == 'trend-pullback':
                    options['take_profit']=Price.rounded(reference.amount+4*volatility,reference.currency)
            signals.append(Signal(strategy_name=self.name,
                signal_id=f'{self.name}:{symbol}:{bars[-1].open_time.isoformat()}:{direction.value}',
                symbol=symbol,direction=direction,reference_price=reference,as_of=context.as_of,
                rationale='preregistered slower cost-aware research hypothesis',
                evidence={'atr_14':str(volatility),'trend_50':str(trend)},**options))
        return signals


class SlowerResearchStrategy(SignalStrategy):
    """Decide at completed coarse bars; execution still uses native minute bars."""
    def __init__(self,rule,minutes):
        if type(minutes) is not int or minutes not in (5,15,30):
            raise ValueError('unsupported research frame')
        self.inner=CostAwareRule(rule)
        self.minutes=minutes
        self.name=f'{self.inner.name}-{minutes}m'
        self.inner.name=self.name

    @property
    def required_bars(self):
        return self.inner.required_bars*self.minutes+self.minutes-1

    @property
    def configuration(self):
        return {**self.inner.configuration,'decision_minutes':self.minutes,
                'execution_minutes':1,'aggregation':'complete UTC buckets; no filling'}

    def decision_due(self,candle):
        return int(candle.close_time.timestamp()) % (self.minutes*60) == 0

    def generate(self,context):
        history={}
        for symbol in context.tradable_symbols:
            closed=context.candles(symbol)
            if not closed or not self.decision_due(closed[-1]):
                continue
            history[symbol]=aggregate_complete_minutes(closed,self.minutes)
        if not history:
            return []
        coarse=MarketContext(as_of=context.as_of,equity=context.equity,snapshot=context.snapshot,
                             positions=context.positions,history=history,policy=context.policy)
        return self.inner.generate(coarse)
