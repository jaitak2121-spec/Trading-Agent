"""Native CoinSwitch contract exercises only mocked I/O and real safety gates."""
import datetime as dt
from decimal import Decimal
import json
import tempfile
import unittest

from trading.adapters.coinswitch.execution_contract import (CoinSwitchContractBroker,SpotExchangePort,
    SpotResponse,SpotRequest,native_client_id)
from trading.adapters.sandbox import TransportMalformed,TransportDisconnected
from trading.app.composition import build_paper_stack
from trading.app.order_policy import VenueMarketSpecification
from trading.core.authz import mint_execution_token
from trading.core.clock import ManualClock
from trading.core.config import RiskConfig
from trading.core.errors import SafetyViolation
from trading.core.money import Money,Price,Quantity,USDT
from trading.core.orders import Order,OrderIntent,OrderSide,OrderType,OrderState
from trading.ports.broker import AckOutcome


class MockExchange(SpotExchangePort):
    def __init__(self):
        self.calls=[];self.state='OPEN';self.filled='0';self.fail=None
        self.exchange='c2c1';self.balance='10000';self.client=None;self.closed=[];self.extra=None

    def record(self):
        return {'order_id':'venue-1','symbol':'BTC/USDT','side':'BUY','exchange':self.exchange,
            'orig_qty':'0.0010','executed_qty':self.filled,'average_price':'50000' if Decimal(self.filled)>0 else '0',
            'status':self.state,'client_order_id':self.client}

    def request(self,request):
        self.calls.append(request)
        if request.path.endswith('/portfolio'):
            data=[{'currency':'USDT','main_balance':self.balance,'blocked_balance_order':'0'},
                  {'currency':'BTC','main_balance':'1','blocked_balance_order':'0'}]
        elif request.path.endswith('/tradingFee'):
            data={'c2c1':{'BTC':{'maker_fee_after_discount':'0.001','taker_fee_after_discount':'0.001'}}}
        elif request.path.endswith('/orders'):
            data={'orders':[self.record()] if dict(request.query)['open']=='true' and self.client else self.closed}
        elif request.method=='POST':
            self.client=dict(request.body)['client_order_id']
            if self.fail:raise TimeoutError('SECRET IN CLIENT ERROR')
            if self.extra:return self.extra
            data=self.record()
        else:
            data=self.record()
        return SpotResponse(200,json.dumps({'data':data}).encode())


class ExecutionContractTests(unittest.TestCase):
    def test_exact_body_encoding_and_documented_signing_bytes(self):
        request=SpotRequest('POST','/trade/api/v2/order',body=(('quantity',Decimal('.00000001')),('price',Decimal('100000.12345678'))))
        decoded=json.loads(request.body_bytes(),parse_float=Decimal)
        self.assertEqual(decoded['quantity'],Decimal('.00000001'))
        self.assertEqual(decoded['price'],Decimal('100000.12345678'))
        self.assertEqual(request.signing_payload(123),b'POST/trade/api/v2/order123')
        query=SpotRequest('GET','/trade/api/v2/orders',query=(('symbols','BTC/USDT,ETH/USDT'),))
        self.assertEqual(query.signing_payload(123),b'GET/trade/api/v2/orders?symbols=BTC/USDT,ETH/USDT123')
        with self.assertRaises(ValueError):SpotRequest('POST','https://coinswitch.co/trade/api/v2/order')

    def setUp(self):
        self.clock=ManualClock()
        risk=RiskConfig(base_currency=USDT,max_order_notional=Money('100',USDT),
            max_position_notional=Money('250',USDT),max_gross_exposure=Money('500',USDT),
            max_daily_loss=Money('50',USDT))
        self.stack=build_paper_stack(starting_cash=Money('10000',USDT),risk=risk,clock=self.clock)
        self.market=VenueMarketSpecification(price_decimals=2,quantity_step=Decimal('.00001'),
            min_notional=Money('10',USDT),max_notional=Money('1000',USDT),metadata_source='mock-trade-info',
            captured_at=self.clock.now(),valid_until=self.clock.now()+dt.timedelta(hours=1))
        self.port=MockExchange()
        self.broker=self.make_broker()
        self.stack.gateway._broker=self.broker
        self.stack.feed.publish('BTC/USDT',Price('50000',USDT),Price('50000',USDT))

    def make_broker(self,port=True):
        return CoinSwitchContractBroker(clock=self.clock,orders=self.stack.orders,market=self.market,
            symbol='BTC/USDT',exchange='c2c1',port=self.port if port else None)

    def intent(self,quantity='.001',signal='fixed'):
        return OrderIntent('strategy',signal,'BTC/USDT',OrderSide.BUY,Quantity(quantity,'BTC'),
            order_type=OrderType.LIMIT,limit_price=Price('50000',USDT))

    def submit(self,**kwargs):
        return self.stack.gateway.submit(self.intent(**kwargs),proposer=self.stack.strategy_id,
            mark_prices={'BTC/USDT':Price('50000',USDT)})

    def test_native_request_gateway_and_uuid_determinism(self):
        result=self.submit()
        self.assertEqual(result.order.state,OrderState.ACCEPTED)
        writes=[r for r in self.port.calls if r.method=='POST']
        self.assertEqual(len(writes),1)
        body=dict(writes[0].body)
        self.assertEqual(body['type'],'limit');self.assertEqual(body['exchange'],'c2c1')
        self.assertEqual(body['client_order_id'],native_client_id('c2c1',result.order.idempotency_key))
        self.assertNotEqual(body['client_order_id'],native_client_id('c2c2',result.order.idempotency_key))
        self.submit()
        self.assertEqual(len([r for r in self.port.calls if r.method=='POST']),1)

    def test_risk_and_pause_block_before_exchange(self):
        self.submit(quantity='1')
        self.assertEqual(self.port.calls,[])
        self.stack.bot.transition('paused',self.stack.operator_id,reason='test')
        self.assertEqual(self.submit(signal='paused').outcome,'refused')
        self.assertEqual(self.port.calls,[])

    def test_timeout_no_retry_redacted_and_restart_recovers_by_stable_client_id(self):
        self.port.fail=True
        result=self.submit()
        self.assertEqual(result.order.state,OrderState.UNKNOWN)
        self.assertNotIn('SECRET',result.ack.message)
        self.port.fail=None
        self.port.state='PARTIALLY_EXECUTED';self.port.filled='.0004'
        restarted=self.make_broker()
        self.stack.gateway._broker=restarted
        ack=restarted.fetch_order_state(result.order)
        self.assertEqual(ack.filled_quantity.amount,Decimal('.0004'))
        self.assertEqual(len([r for r in self.port.calls if r.method=='POST']),1)

    def test_cumulative_partial_sync_does_not_double_book(self):
        result=self.submit()
        self.port.state='PARTIALLY_EXECUTED';self.port.filled='.0004'
        self.stack.gateway.sync_order(result.order,operator=self.stack.operator_id)
        self.stack.gateway.sync_order(result.order,operator=self.stack.operator_id)
        self.assertEqual(self.stack.portfolio.position('BTC/USDT',asset='BTC').quantity.amount,Decimal('.0004'))
        self.port.state='EXECUTED';self.port.filled='.001'
        self.stack.gateway.sync_order(result.order,operator=self.stack.operator_id)
        self.assertEqual(self.stack.portfolio.position('BTC/USDT',asset='BTC').quantity.amount,Decimal('.001'))

    def test_partial_then_cancel_preserves_fill(self):
        result=self.submit()
        self.port.state='CANCELLED';self.port.filled='.0004'
        self.stack.gateway.cancel(result.order,operator=self.stack.operator_id)
        self.assertEqual(result.order.state,OrderState.CANCELED)
        self.assertEqual(self.stack.portfolio.position('BTC/USDT',asset='BTC').quantity.amount,Decimal('.0004'))
        delete=[r for r in self.port.calls if r.method=='DELETE'][0]
        self.assertEqual(dict(delete.body),{'order_id':'venue-1'})

    def test_foreign_exchange_or_overfill_remains_unknown(self):
        self.port.exchange='c2c2'
        result=self.submit()
        self.assertEqual(result.order.state,OrderState.UNKNOWN)

    def test_undersized_funding_no_write(self):
        self.port.balance='50'
        result=self.submit()
        self.assertEqual(result.order.state,OrderState.UNKNOWN)
        self.assertFalse(any(r.method=='POST' for r in self.port.calls))

    def test_invalid_effective_fee_prevents_native_placement(self):
        original=self.port.request
        def invalid_fee(request):
            if request.path.endswith('/tradingFee'):
                self.port.calls.append(request)
                return SpotResponse(200,json.dumps({'data':{'c2c1':{'BTC':{
                    'maker_fee_after_discount':'-0.001','taker_fee_after_discount':'0.001'}}}}).encode())
            return original(request)
        self.port.request=invalid_fee
        self.assertEqual(self.submit().order.state,OrderState.UNKNOWN)
        self.assertFalse(any(r.method=='POST' for r in self.port.calls))

    def test_expired_metadata_no_io_and_default_port_disabled(self):
        self.clock.advance(3600)
        self.submit()
        self.assertEqual(self.port.calls,[])
        with self.assertRaises(TransportDisconnected):self.make_broker(False).contract.balances()

    def test_absence_and_truncated_inventory_never_prove_rejection(self):
        order=Order(self.intent(),clock=self.clock)
        with self.assertRaises(TransportMalformed):self.broker.fetch_order_state(order)
        self.port.client='unrelated'
        self.port.closed=[self.port.record()]*500
        with self.assertRaises(TransportMalformed):self.broker.fetch_order_inventory()

    def test_account_holdings_not_fabricated_as_strategy_positions(self):
        self.assertEqual(self.broker.contract.balances()['USDT'],(Decimal('10000'),Decimal('0')))
        with self.assertRaises(TransportMalformed):self.broker.fetch_positions()
        checked=self.broker.contract.reconcile_account_wallet({'USDT':Decimal('10000'),'BTC':Decimal('1')})
        self.assertTrue(checked['matched'])
        self.assertFalse(checked['strategy_allocation_verified'])
        with self.assertRaises(TransportMalformed):
            self.broker.contract.reconcile_account_wallet({'USDT':Decimal('10000')})

    def test_replayed_token_never_reaches_port(self):
        order=Order(self.intent(),clock=self.clock)
        token=mint_execution_token(self.stack.gateway_id,order_id=order.order_id,
            idempotency_key=order.idempotency_key,clock=self.clock)
        self.broker.place_order(order,token=token)
        before=len(self.port.calls)
        with self.assertRaises(SafetyViolation):self.broker.place_order(order,token=token)
        self.assertEqual(before,len(self.port.calls))

    def test_server_rate_limit_malformed_and_overfill_do_not_retry(self):
        for response in (SpotResponse(500,b'{}'),SpotResponse(429,b'{}'),SpotResponse(200,b'bad-json')):
            with self.subTest(response=response):
                self.setUp()
                self.port.extra=response
                self.assertEqual(self.submit().order.state,OrderState.UNKNOWN)
                self.assertEqual(len([r for r in self.port.calls if r.method=='POST']),1)
        self.setUp()
        self.port.state='EXECUTED';self.port.filled='.002'
        self.assertEqual(self.submit().order.state,OrderState.UNKNOWN)

    def test_pending_cancel_is_not_terminal(self):
        result=self.submit()
        self.port.state='CANCELLATION_RAISED';self.port.filled='.0004'
        self.stack.gateway.cancel(result.order,operator=self.stack.operator_id)
        self.assertFalse(result.order.state.is_terminal)
        self.assertEqual(result.order.filled_quantity.amount,Decimal('.0004'))

    def test_restart_restores_unknown_and_resolves_without_new_placement(self):
        with tempfile.TemporaryDirectory() as folder:
            path=folder+'/contract-test.sqlite3'
            original=build_paper_stack(starting_cash=Money('10000',USDT),risk=self.stack.config.risk,
                clock=self.clock,sqlite_path=path)
            port=MockExchange();port.fail=True
            original.gateway._broker=CoinSwitchContractBroker(clock=self.clock,orders=original.orders,
                market=self.market,symbol='BTC/USDT',exchange='c2c1',port=port)
            original.feed.publish('BTC/USDT',Price('50000',USDT),Price('50000',USDT))
            result=original.gateway.submit(self.intent(),proposer=original.strategy_id,
                mark_prices={'BTC/USDT':Price('50000',USDT)})
            self.assertEqual(result.order.state,OrderState.UNKNOWN)
            original.persistence.close()
            restored=build_paper_stack(starting_cash=Money('10000',USDT),risk=self.stack.config.risk,
                clock=self.clock,sqlite_path=path,run_startup=False)
            try:
                self.assertEqual(restored.bot.state,'paused')
                order=restored.orders.all_orders()[0]
                self.assertEqual(order.state,OrderState.UNKNOWN)
                port.fail=None;port.state='PARTIALLY_EXECUTED';port.filled='.0004'
                restored.gateway._broker=CoinSwitchContractBroker(clock=self.clock,orders=restored.orders,
                    market=self.market,symbol='BTC/USDT',exchange='c2c1',port=port)
                restored.gateway.resolve_unknown(order,operator=restored.operator_id)
                self.assertFalse(order.is_unknown)
                self.assertEqual(restored.portfolio.position('BTC/USDT',asset='BTC').quantity.amount,Decimal('.0004'))
                self.assertEqual(len([r for r in port.calls if r.method=='POST']),1)
            finally:restored.persistence.close()
