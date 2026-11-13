"""Synthetic isolated accounts only; no production runtime or real orders."""
import datetime as dt
from decimal import Decimal
from pathlib import Path
import tempfile
import unittest
from types import SimpleNamespace

from trading.app.composition import build_paper_stack
from trading.app.paper_service import RealtimePaperService
from trading.app.order_policy import MarketableLimitPolicy,VenueMarketSpecification,bounded_exit_quantity
from trading.adapters.marketdata import CandleIngestor,CandleNormalizer,InMemoryCandleStore,MarketDataIngestor,QuoteNormalizer
from trading.core.clock import ManualClock,UTC
from trading.core.config import RiskConfig
from trading.core.errors import SafetyViolation
from trading.core.money import Money,Price,Quantity,USD
from trading.core.orders import OrderIntent,OrderSide
from trading.strategy import SignalStrategy,Signal,SignalDirection
from tests.test_paper_service import ScriptedSource

SYMBOL='BTCUSD';BASE=dt.datetime(2024,1,1,tzinfo=UTC)


class OneExit(SignalStrategy):
    name='exit-fixture'
    def __init__(self,emit=True):self.emit=emit
    def generate(self,context):
        if not self.emit:return []
        self.emit=False
        return [exit_signal(context.as_of)]


def exit_signal(at):
    return Signal(strategy_name='exit-fixture',signal_id='one-original-exit',symbol=SYMBOL,
        direction=SignalDirection.EXIT,reference_price=Price('100',USD),as_of=at,rationale='fixture close')


def stack_for(path=None):
    return build_paper_stack(starting_cash=Money('10000',USD),sqlite_path=path,clock=ManualClock(BASE),fee_bps=10,
        risk=RiskConfig(max_order_notional=Money('100',USD),max_position_notional=Money('1000',USD),
            max_gross_exposure=Money('1000',USD),max_open_orders=5,max_orders_per_minute=100))


def service_for(stack,emit=True,market=None):
    store=InMemoryCandleStore()
    quotes=ScriptedSource([[{'symbol':SYMBOL,'bid':'100','ask':'100'}]]*4)
    bars=ScriptedSource([[{'symbol':SYMBOL,'open_time':(BASE+dt.timedelta(minutes=i)).isoformat(),
        'interval_seconds':60,'open':'100','high':'101','low':'99','close':'100','volume':'1'}] for i in range(4)])
    return RealtimePaperService(stack,OneExit(emit),candles=store,instruments={SYMBOL:'BTC'},
        quote_ingestor=MarketDataIngestor(source=quotes,normalizer=QuoteNormalizer(clock=stack.clock),feed=stack.feed,clock=stack.clock),
        candle_ingestor=CandleIngestor(source=bars,normalizer=CandleNormalizer(clock=stack.clock,volume_asset='BTC'),store=store,clock=stack.clock),
        order_policy=MarketableLimitPolicy(protection_bps=0,market=market))


def seed(stack,quantity='1.01'):
    stack.feed.publish(SYMBOL,Price('50',USD),Price('50',USD))
    result=stack.gateway.submit(OrderIntent('fixture','entry',SYMBOL,OrderSide.BUY,Quantity(quantity,'BTC')),
        proposer=stack.strategy_id,mark_prices={SYMBOL:Price('50',USD)})
    assert result.is_executed
    stack.feed.publish(SYMBOL,Price('100',USD),Price('100',USD))


def execute(service,stack):
    return service._execute(exit_signal(stack.clock.now()),equity=Money('10000',USD),marks={SYMBOL:Price('100',USD)})


class PaperExitChunkTests(unittest.TestCase):
    def test_venue_minimum_tail_through_service_and_dashboard_accounting(self):
        from trading.adapters.http.dashboard import OperatorDashboard
        from trading.app.supervisor import SupervisorStatus
        stack=stack_for();seed(stack);market=VenueMarketSpecification(price_decimals=2,quantity_step=Decimal('.01'),
            min_notional=Money('10',USD),max_notional=Money('100',USD))
        service=service_for(stack,market=market)
        dashboard=OperatorDashboard(stack,SimpleNamespace(status=lambda:SupervisorStatus(False,0,1)))
        first=execute(service,stack);self.assertEqual(Decimal(first.quantity),Decimal('.91'))
        position=dashboard.snapshot()['portfolio']
        self.assertEqual(Decimal(position['positions'][0]['quantity']),Decimal('.10'))
        expected=Decimal(position['starting_cash'])+Decimal(position['realized_pnl'])+Decimal(position['unrealized_pnl'])
        self.assertLessEqual(abs(Decimal(position['equity'])-expected),Decimal('.01'))
        execute(service,stack);final=dashboard.snapshot()['portfolio']
        cash_deltas=sum((Decimal(r.details['cash_delta']) for r in stack.audit.records() if r.action=='portfolio.fill'),Decimal(0))
        self.assertEqual(Decimal(final['cash']),Decimal(final['starting_cash'])+cash_deltas)
        self.assertEqual(Decimal(final['cash']),Decimal(final['equity']))
        self.assertEqual(Decimal(final['equity']),Decimal(final['starting_cash'])+Decimal(final['realized_pnl']))
        self.assertEqual(final['positions'],[])

    def test_cumulative_partial_replay_never_rebooks_fees_or_inventory(self):
        stack=stack_for();seed(stack);service=service_for(stack)
        stack.broker.set_depth(SYMBOL,Quantity('.4','BTC'));result=execute(service,stack)
        order=stack.orders.get(result.order_id)
        before=(stack.portfolio.cash,stack.portfolio.fees,stack.portfolio.snapshot()[SYMBOL])
        for _ in range(3):stack.gateway.sync_order(order,operator=stack.operator_id)
        self.assertEqual(before,(stack.portfolio.cash,stack.portfolio.fees,stack.portfolio.snapshot()[SYMBOL]))
        self.assertEqual(sum(r.action=='portfolio.fill' for r in stack.audit.records()),2)

    def test_all_inactive_states_block_pending_continuations(self):
        for state in ('paused','stopped','emergency_stopped'):
            with self.subTest(state=state):
                stack=stack_for();seed(stack);service=service_for(stack);execute(service,stack)
                stack.bot.transition(state,stack.operator_id,reason='isolated inactive-state test')
                before=(len(stack.orders.all_orders()),stack.broker.placement_count,stack.portfolio.cash)
                service.collect_only()
                with self.assertRaises(SafetyViolation):service.poll_once()
                self.assertEqual(execute(service,stack).outcome,'refused')
                self.assertEqual(before,(len(stack.orders.all_orders()),stack.broker.placement_count,stack.portfolio.cash))
                self.assertEqual(stack.bot.state,state)

    def test_blocked_dust_restores_and_strategy_identity_cannot_rearm_it(self):
        with tempfile.TemporaryDirectory() as folder:
            db=Path(folder)/'blocked.db';stack=stack_for(db);seed(stack,'.01')
            market=VenueMarketSpecification(price_decimals=2,quantity_step=Decimal('.01'),
                min_notional=Money('10',USD),max_notional=Money('100',USD))
            service=service_for(stack,market=market);execute(service,stack);stack.persistence.close()
            restored=stack_for(db)
            try:
                service=service_for(restored,market=market);self.assertTrue(service._exit_plans[SYMBOL]['blocked'])
                count=sum(r.action=='paper.exit_blocked' for r in restored.audit.records())
                execute(service,restored)
                self.assertEqual(sum(r.action=='paper.exit_blocked' for r in restored.audit.records()),count)
                with self.assertRaises(SafetyViolation):service.retry_exit(SYMBOL,restored.strategy_id,reason='unauthorized fixture')
                self.assertTrue(service._exit_plans[SYMBOL]['blocked']);self.assertEqual(len(restored.orders.all_orders()),1)
            finally:restored.persistence.close()

    def test_expired_metadata_prevents_remaining_chunk_without_new_order(self):
        stack=stack_for();seed(stack)
        market=VenueMarketSpecification(price_decimals=2,quantity_step=Decimal('.01'),min_notional=Money('10',USD),
            max_notional=Money('100',USD),captured_at=BASE,valid_until=BASE+dt.timedelta(seconds=90))
        service=service_for(stack,market=market);stack.clock.advance(60);service.poll_once()
        self.assertEqual(stack.portfolio.snapshot()[SYMBOL].amount,Decimal('.10'))
        before=stack.broker.placement_count;stack.clock.advance(60);cycle=service.poll_once()
        self.assertEqual(cycle.skipped_reason,'venue market metadata is stale')
        self.assertEqual(stack.broker.placement_count,before)

    def test_stale_quote_does_not_display_fabricated_equity_or_unrealized_pnl(self):
        from trading.adapters.http.dashboard import OperatorDashboard
        from trading.app.supervisor import SupervisorStatus
        stack=stack_for();seed(stack)
        dashboard=OperatorDashboard(stack,SimpleNamespace(status=lambda:SupervisorStatus(False,0,1)))
        self.assertIsNotNone(dashboard.snapshot()['portfolio']['equity'])
        stack.clock.advance(601);p=dashboard.snapshot()['portfolio']
        self.assertIsNone(p['equity']);self.assertIsNone(p['unrealized_pnl'])
        self.assertIsNone(p['positions'][0]['mark_price'])
        self.assertEqual(Decimal(p['cash']),stack.portfolio.cash.amount)

    def test_one_strategy_exit_continues_to_flat_with_caps_and_fees(self):
        stack=stack_for();seed(stack);service=service_for(stack)
        stack.clock.advance(60);first=service.poll_once()
        self.assertEqual(first.outcomes[0].quantity,'1.00000000')
        stack.clock.advance(60);second=service.poll_once()
        self.assertEqual(second.outcomes[0].quantity,'0.01000000')
        self.assertTrue(stack.portfolio.snapshot()[SYMBOL].is_zero)
        self.assertEqual(stack.portfolio.cash,stack.portfolio.starting_cash+stack.portfolio.realized())
        self.assertGreater(stack.portfolio.fees.amount,0)
        self.assertFalse(service._exit_plans)
        for record in stack.audit.records():
            if record.action=='risk_approved':self.assertLessEqual(Decimal(record.details['order_notional']),Decimal('100'))
        before=stack.broker.placement_count;self.assertEqual(execute(service,stack).outcome,'not_sized')
        self.assertEqual(stack.broker.placement_count,before)

    def test_pending_partial_exit_is_not_duplicated_and_cancelled_remainder_is_safe(self):
        stack=stack_for();seed(stack);service=service_for(stack)
        stack.broker.set_depth(SYMBOL,Quantity('.4','BTC'));first=execute(service,stack)
        self.assertEqual(stack.portfolio.snapshot()[SYMBOL].amount,Decimal('.61'))
        order=stack.orders.get(first.order_id);self.assertTrue(stack.orders.open_orders())
        before=stack.broker.placement_count;self.assertEqual(execute(service,stack).outcome,'not_sized')
        self.assertEqual(stack.broker.placement_count,before)
        stack.gateway.cancel(order,operator=stack.operator_id)
        stack.broker.set_depth(SYMBOL,Quantity('10','BTC'));execute(service,stack)
        self.assertTrue(stack.portfolio.snapshot()[SYMBOL].is_zero)
        self.assertEqual(order.filled_quantity.amount,Decimal('.4'))
        self.assertFalse(stack.broker.duplicate_keys)

    def test_precision_and_small_tail_are_split_without_rounding_up(self):
        market=VenueMarketSpecification(price_decimals=2,quantity_step=Decimal('.01'),
            min_notional=Money('10',USD),max_notional=Money('100',USD))
        q=bounded_exit_quantity(Quantity('1.01','BTC'),ceiling=Money('100',USD),valuation_price=Price('100',USD),
            limit_price=Price('100',USD),market=market)
        self.assertEqual(q.amount,Decimal('.91'))
        self.assertEqual(bounded_exit_quantity(Quantity('1.00000001','BTC'),ceiling=Money('100',USD),
            valuation_price=Price('100',USD)).amount,Decimal('1'))
        with self.assertRaises(ValueError):bounded_exit_quantity(Quantity('.00000001','BTC'),
            ceiling=Money('100',USD),valuation_price=Price('100',USD),market=market)
        fine=VenueMarketSpecification(price_decimals=2,quantity_step=Decimal('.00000001'),
            min_notional=Money('10',USD),max_notional=Money('100',USD))
        with self.assertRaises(ValueError):bounded_exit_quantity(Quantity('.09999999','BTC'),
            ceiling=Money('100',USD),valuation_price=Price('100',USD),market=fine)

    def test_untradeable_dust_blocks_once_and_requires_audited_review(self):
        stack=stack_for();seed(stack,'.01')
        market=VenueMarketSpecification(price_decimals=2,quantity_step=Decimal('.01'),
            min_notional=Money('10',USD),max_notional=Money('100',USD))
        service=service_for(stack,market=market);before=stack.broker.placement_count
        self.assertEqual(execute(service,stack).outcome,'not_sized')
        execute(service,stack)
        self.assertEqual(sum(r.action=='paper.exit_blocked' for r in stack.audit.records()),1)
        self.assertEqual(stack.broker.placement_count,before)
        with self.assertRaises(SafetyViolation):service.retry_exit(SYMBOL,stack.operator_id,reason='review')
        stack.bot.transition('paused',stack.operator_id,reason='fixture review')
        service.retry_exit(SYMBOL,stack.operator_id,reason='fixture reviewed')
        self.assertEqual(stack.bot.state,'paused');self.assertEqual(stack.broker.placement_count,before)

    def test_emergency_stop_and_insufficient_inventory_do_not_sell(self):
        stack=stack_for();seed(stack);service=service_for(stack);before=stack.broker.placement_count
        stack.bot.transition('emergency_stopped',stack.operator_id,reason='fixture stop')
        with self.assertRaises(SafetyViolation):service.poll_once()
        result=execute(service,stack);self.assertEqual(result.outcome,'refused')
        self.assertTrue(stack.kill_switch.is_engaged);self.assertEqual(stack.broker.placement_count,before)
        other=stack_for();seed(other,'.01')
        result=other.gateway.submit(OrderIntent('fixture','oversell',SYMBOL,OrderSide.SELL,Quantity('.02','BTC')),
            proposer=other.strategy_id,mark_prices={SYMBOL:Price('100',USD)})
        self.assertEqual(result.outcome,'refused');self.assertEqual(other.portfolio.snapshot()[SYMBOL].amount,Decimal('.01'))

    def test_restart_between_chunks_restores_exit_without_another_strategy_signal(self):
        with tempfile.TemporaryDirectory() as folder:
            db=Path(folder)/'paper.db';stack=stack_for(db);seed(stack);service=service_for(stack);execute(service,stack)
            self.assertEqual(stack.portfolio.snapshot()[SYMBOL].amount,Decimal('.01'));stack.persistence.close()
            restored=stack_for(db)
            try:
                self.assertNotEqual(restored.bot.state,'running')
                restored.bot.transition('running',restored.operator_id,reason='isolated fixture recovery')
                service=service_for(restored,emit=False);restored.clock.advance(60)
                service.poll_once()
                self.assertTrue(restored.portfolio.snapshot()[SYMBOL].is_zero)
                self.assertFalse(service._exit_plans)
                self.assertEqual(len(restored.orders.all_orders()),3)
            finally:restored.persistence.close()

    def test_restart_preserves_partial_fill_inventory_and_pending_exit(self):
        with tempfile.TemporaryDirectory() as folder:
            db=Path(folder)/'partial.db';stack=stack_for(db);seed(stack);service=service_for(stack)
            stack.broker.set_depth(SYMBOL,Quantity('.4','BTC'));outcome=execute(service,stack)
            order_id=outcome.order_id;fees=stack.portfolio.fees;stack.persistence.close()
            restored=stack_for(db)
            try:
                restored.bot.transition('running',restored.operator_id,reason='isolated partial fixture recovery')
                service=service_for(restored,emit=False)
                self.assertEqual(restored.portfolio.snapshot()[SYMBOL].amount,Decimal('.61'))
                self.assertEqual(restored.portfolio.fees,fees)
                self.assertEqual(execute(service,restored).outcome,'not_sized')
                order=restored.orders.get(order_id)
                self.assertEqual(order.filled_quantity.amount,Decimal('.4'))
                restored.gateway.cancel(order,operator=restored.operator_id)
                restored.feed.publish(SYMBOL,Price('100',USD),Price('100',USD))
                restored.broker.set_depth(SYMBOL,Quantity('10','BTC'));execute(service,restored)
                self.assertTrue(restored.portfolio.snapshot()[SYMBOL].is_zero)
                self.assertFalse(restored.broker.duplicate_keys)
            finally:restored.persistence.close()
