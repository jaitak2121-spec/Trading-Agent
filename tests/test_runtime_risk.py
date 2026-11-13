"""Runtime policy boundary, bypass and recovery tests."""
import datetime as dt
import tempfile
import unittest
from decimal import Decimal

from trading.app.composition import build_paper_stack
from trading.core.config import RiskConfig, TradingConfig
from trading.core.errors import RiskLimitExceeded, ConfigurationError
from trading.core.marketdata import Candle
from trading.core.money import Money, Price, Quantity, USD
from trading.core.orders import OrderIntent, OrderSide, Order, OrderState
from trading.core.risk import RiskLimit


def intent(side=OrderSide.BUY, quantity='0.1', symbol='BTCUSD', signal='decision'):
    return OrderIntent('test', signal, symbol, side, Quantity(quantity, 'BTC'))


class RuntimeRiskTest(unittest.TestCase):
    def approve(self, stack, order=None, *, equity=None, positions=None):
        return stack.risk.approve(order or intent(), positions=positions or {},
                                  mark_prices={'BTCUSD': Price('100', USD), 'ETHUSD': Price('100', USD)},
                                  equity=equity)

    def candle(self, stack, high='101'):
        return Candle(symbol='BTCUSD', open=Price('100', USD), high=Price(high, USD),
                      low=Price('99', USD), close=Price('100', USD), volume=Quantity('1', 'BTC'),
                      open_time=stack.clock.now()-dt.timedelta(seconds=60), interval_seconds=60,
                      source='test')

    def test_drawdown_boundary_persists_and_allows_reduction(self):
        config = RiskConfig(max_drawdown_fraction=Decimal('0.1'))
        with tempfile.TemporaryDirectory() as directory:
            path = directory+'/paper.db'
            stack = build_paper_stack(sqlite_path=path, risk=config, starting_cash=Money('100', USD))
            stack.risk.observe_equity(Money('100', USD))
            self.approve(stack, equity=Money('90.01', USD))
            with self.assertRaises(RiskLimitExceeded):
                self.approve(stack, equity=Money('90', USD))
            self.approve(stack, intent(OrderSide.SELL), equity=None,
                         positions={'BTCUSD': Quantity('0.1', 'BTC')})
            stack.persistence.close()
            restored = build_paper_stack(sqlite_path=path, risk=config)
            try:
                with self.assertRaises(RiskLimitExceeded):
                    self.approve(restored, equity=Money('90', USD))
            finally:
                restored.persistence.close()

    def test_missing_equity_and_reversal_are_refused(self):
        stack = build_paper_stack(risk=RiskConfig(max_drawdown_fraction=Decimal('0.1')))
        with self.assertRaises(RiskLimitExceeded):
            self.approve(stack)
        with self.assertRaises(RiskLimitExceeded):
            self.approve(stack, intent(OrderSide.SELL, '0.2'),
                         positions={'BTCUSD': Quantity('0.1', 'BTC')})

    def test_pending_orders_reserve_position_capacity(self):
        stack = build_paper_stack(risk=RiskConfig(max_positions=1))
        order = Order(intent(symbol='ETHUSD'), clock=stack.clock)
        order.transition_to(OrderState.PENDING_NEW, reason='pending test')
        stack.orders.add(order)
        with self.assertRaises(RiskLimitExceeded):
            self.approve(stack)
        self.approve(stack, intent(symbol='ETHUSD'))

    def test_cooldown_boundary_and_restart(self):
        with tempfile.TemporaryDirectory() as directory:
            path = directory+'/paper.db'
            config = RiskConfig(trade_cooldown_seconds=60)
            stack = build_paper_stack(sqlite_path=path, risk=config)
            stack.orders.add(Order(intent(), clock=stack.clock))
            with self.assertRaises(RiskLimitExceeded):
                self.approve(stack)
            stack.persistence.close()
            restored = build_paper_stack(sqlite_path=path, risk=config)
            try:
                with self.assertRaises(RiskLimitExceeded):
                    self.approve(restored)
                restored.clock.advance(seconds=60)
                self.approve(restored)
            finally:
                restored.persistence.close()

    def test_volatility_missing_boundary_stale_and_restart(self):
        stack = build_paper_stack(risk=RiskConfig(max_candle_range_fraction=Decimal('0.02')))
        with self.assertRaises(RiskLimitExceeded):
            self.approve(stack)
        stack.risk.observe_candle(self.candle(stack))
        with self.assertRaises(RiskLimitExceeded):
            self.approve(stack)
        stack.risk.observe_candle(self.candle(stack, '100'))
        self.approve(stack)
        stack.clock.advance(seconds=121)
        with self.assertRaises(RiskLimitExceeded):
            self.approve(stack)

    def test_gateway_cannot_skip_volatility_policy(self):
        stack = build_paper_stack(risk=RiskConfig(max_candle_range_fraction=Decimal('0.1')))
        stack.feed.publish('BTCUSD', Price('99', USD), Price('101', USD))
        result = stack.gateway.submit(intent(), proposer=stack.strategy_id)
        self.assertNotEqual(result.outcome, 'executed')
        self.assertEqual(stack.broker.placement_count, 0)

    def test_configuration_round_trip_and_invalid_values(self):
        config = TradingConfig.from_mapping({'max_drawdown_fraction':'0.1', 'max_positions':'2',
                                             'trade_cooldown_seconds':'60', 'max_candle_range_fraction':'0.05'})
        self.assertEqual(config.risk.max_positions, 2)
        for values in [{'max_positions':True}, {'trade_cooldown_seconds':-1},
                       {'max_drawdown_fraction':Decimal('NaN')}, {'max_candle_range_fraction':0.1}]:
            with self.assertRaises(ConfigurationError):
                RiskConfig(**values)
