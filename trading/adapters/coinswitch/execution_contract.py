"""CoinSwitch Spot execution contract over injected I/O, never a live transport.

No host, socket, signing key, credentials or runtime wiring is provided. The
default port refuses every call. Tests supply scripted responses; the existing
gateway remains responsible for risk capabilities, durable orders and recovery.
"""
from abc import ABC, abstractmethod
from dataclasses import dataclass
from decimal import Decimal
import hashlib
import json
from types import MappingProxyType
from uuid import uuid5,NAMESPACE_URL

from ..sandbox import SandboxBroker,SandboxTransport,SandboxResponse,SandboxRequest,TransportMalformed,TransportDisconnected,TransportRateLimited
from ...ports.broker import BrokerAck,AckOutcome
from .spot_contract import CoinSwitchSpotResponseTranslator
from .trading_fees import parse_trading_fee


@dataclass(frozen=True)
class SpotRequest:
    method: str
    path: str
    query: tuple=()
    body: tuple=()

    def __post_init__(self):
        if self.method not in ('GET','POST','DELETE') or self.path not in (
            '/trade/api/v2/order','/trade/api/v2/orders',
            '/trade/api/v2/user/portfolio','/trade/api/v2/tradingFee'):
            raise ValueError('unsupported Spot contract route')

    def body_bytes(self):
        """Encode exact JSON numeric tokens without a float round-trip."""
        def encode(value):
            if isinstance(value,Decimal):
                if not value.is_finite():raise ValueError('nonfinite request number')
                return format(value,'f')
            return json.dumps(value,separators=(',',':'))
        return ('{'+','.join(json.dumps(k)+':'+encode(v) for k,v in sorted(self.body))+'}').encode()

    def signing_payload(self,epoch_ms):
        """Prepare documented bytes only; no key or signer is provided."""
        if type(epoch_ms) is not int or epoch_ms<0:raise ValueError('invalid epoch')
        # CoinSwitch signs the decoded query, not its URL-encoded wire form.
        # Fixed route paths and ordered key/value pairs need no network utility.
        path=self.path+('?'+'&'.join(str(k)+'='+str(v) for k,v in self.query) if self.query else '')
        return (self.method+path+str(epoch_ms)).encode()


@dataclass(frozen=True)
class SpotResponse:
    status: int
    body: bytes


class SpotExchangePort(ABC):
    """An exchange-contract seam. No production implementation is shipped."""
    @abstractmethod
    def request(self,request:SpotRequest)->SpotResponse:
        raise NotImplementedError


class DisabledSpotExchange(SpotExchangePort):
    def request(self,request):
        raise TransportDisconnected('actual CoinSwitch execution is disabled')


def native_client_id(exchange,key):
    return str(uuid5(NAMESPACE_URL,'trading-agent/coinswitch/'+exchange+'/'+key))


class CoinSwitchExecutionContract(SandboxTransport):
    """Native request translation with explicit identity and completeness checks."""
    def __init__(self,*,clock,orders,market,symbol,exchange,port=None):
        if exchange not in ('coinswitchx','c2c1','c2c2') or symbol.count('/')!=1:
            raise ValueError('invalid CoinSwitch market identity')
        if symbol.split('/')[1]!=market.min_notional.currency.code:
            raise ValueError('market currency mismatch')
        if (exchange=='coinswitchx') != symbol.endswith('/INR'):
            raise ValueError('exchange/quote currency mismatch')
        if exchange!='coinswitchx' and not symbol.endswith('/USDT'):
            raise ValueError('unsupported Spot quote currency')
        if port is not None and not isinstance(port,SpotExchangePort):
            raise TypeError('port must implement SpotExchangePort')
        self.clock,self.orders,self.market=clock,orders,market
        self.symbol,self.exchange=symbol,exchange
        self.port=port or DisabledSpotExchange()
        self.translator=CoinSwitchSpotResponseTranslator()
        self._placements=[]

    def _request(self,method,path,query=(),body=()):
        try:
            response=self.port.request(SpotRequest(method,path,tuple(query),tuple(body)))
        except Exception:
            # Raw client exceptions can contain headers/secrets. Never echo them.
            raise TransportDisconnected('exchange-contract request failed; reconcile before retry') from None
        if not isinstance(response,SpotResponse) or type(response.status) is not int or not isinstance(response.body,bytes):
            raise TransportMalformed('invalid exchange-contract response')
        return response

    @staticmethod
    def _data(response):
        if not 200<=response.status<300:
            raise TransportMalformed('exchange read failed with HTTP '+str(response.status))
        try:return json.loads(response.body,parse_float=str)['data']
        except (ValueError,KeyError,TypeError):raise TransportMalformed('malformed exchange envelope') from None

    def balances(self):
        data=self._data(self._request('GET','/trade/api/v2/user/portfolio'))
        if not isinstance(data,list):raise TransportMalformed('portfolio requires an asset array')
        result={}
        for row in data:
            asset=row.get('currency') if isinstance(row,dict) else None
            if not isinstance(asset,str) or not asset or asset in result:
                raise TransportMalformed('invalid or duplicate portfolio asset')
            free=self.translator._number(row,'main_balance')
            locked=self.translator._number(row,'blocked_balance_order')
            if min(free,locked)<0:raise TransportMalformed('negative Spot balance')
            result[asset]=(free,locked)
        return MappingProxyType(result)

    def _funding(self,payload):
        balances=self.balances()
        fee_response=self._request('GET','/trade/api/v2/tradingFee',(('exchange',self.exchange),))
        self._data(fee_response)
        base,quote=self.symbol.split('/')
        try:
            fees=parse_trading_fee(fee_response.body,exchange=self.exchange,base_asset=base,
                captured_at_ms=int(self.clock.now().timestamp()*1000))
        except ValueError:raise TransportMalformed('invalid account-specific fees') from None
        rate=max(Decimal(fees['effective_maker_rate']),Decimal(fees['effective_taker_rate']))
        quantity=Decimal(payload['quantity'])
        asset,needed=(quote,quantity*Decimal(payload['limit_price'])*(1+rate)) if payload['side']=='buy' else (base,quantity*(1+rate))
        if asset not in balances or balances[asset][0]<needed:
            raise TransportMalformed('insufficient unblocked balance including conservative fee reserve')

    def reconcile_account_wallet(self,expected_totals):
        """Verify a whole-account ledger; never infer strategy-owned positions."""
        observed=self.balances()
        if any(not isinstance(v,Decimal) or not v.is_finite() or v<0 for v in expected_totals.values()):
            raise ValueError('expected wallet totals must be finite nonnegative Decimal values')
        for asset in set(observed)|set(expected_totals):
            if sum(observed.get(asset,(Decimal(0),Decimal(0))))!=expected_totals.get(asset,Decimal(0)):
                raise TransportMalformed('account-wide wallet reconciliation mismatch')
        report={'scope':'whole_account_only','matched':True,'as_of':self.clock.now().isoformat(),
            'balances':{asset:{'available':str(v[0]),'blocked':str(v[1])} for asset,v in observed.items()},
            'strategy_allocation_verified':False,'live_enabled':False}
        report['sha256']=hashlib.sha256(json.dumps(report,sort_keys=True,separators=(',',':')).encode()).hexdigest()
        return report

    def _validate_order(self,request):
        p=request.payload
        now=self.clock.now()
        m=self.market
        if (m.captured_at is None or m.valid_until is None or not m.metadata_source
                or not m.captured_at<=now<m.valid_until):
            raise TransportMalformed('missing, future or expired market metadata')
        if p.get('symbol')!=self.symbol or p.get('order_type')!='limit' or p.get('side') not in ('buy','sell'):
            raise TransportMalformed('only scoped Spot LIMIT orders supported')
        if p.get('base_asset')!=self.symbol.split('/')[0] or p.get('quote_currency')!=m.min_notional.currency.code:
            raise TransportMalformed('order asset/currency mismatch')
        quantity=self.translator._number(p,'quantity')
        price=self.translator._number(p,'limit_price')
        if quantity<=0 or price<=0 or quantity%m.quantity_step or price%Decimal(1).scaleb(-m.price_decimals):
            raise TransportMalformed('order precision/size invalid; no implicit rounding')
        if not m.min_notional.amount<=quantity*price<=m.max_notional.amount:
            raise TransportMalformed('order notional outside venue limits')

    def _normalize(self,raw,expected=None):
        translated=self.translator._order(raw)
        if raw.get('exchange')!=self.exchange or translated['symbol']!=self.symbol:
            raise TransportMalformed('exchange/order market mismatch')
        keys={native_client_id(self.exchange,o.idempotency_key):o.idempotency_key for o in self.orders.all_orders()}
        if expected is not None:
            local=[o for o in self.orders.all_orders() if o.idempotency_key==expected.idempotency_key]
            if len(local)>1:raise TransportMalformed('ambiguous local order identity')
            side=local[0].side.value if local else expected.payload.get('side',translated['side'])
            size=local[0].intent.quantity.amount if local else Decimal(expected.payload.get('quantity',translated['quantity']))
            if (translated['side']!=expected.payload.get('side',translated['side'])
                    or translated['side']!=side or Decimal(translated['quantity'])!=size):
                raise TransportMalformed('response order identity/quantity mismatch')
            wanted=native_client_id(self.exchange,expected.idempotency_key)
            if raw.get('client_order_id') not in (None,wanted):
                raise TransportMalformed('response client identity mismatch')
            translated['client_order_id']=expected.idempotency_key
        elif translated.get('client_order_id') in keys:
            translated['client_order_id']=keys[translated['client_order_id']]
        return translated

    def _inventory(self):
        combined=[]
        ids=set()
        clients=set()
        for opened in ('true','false'):
            data=self._data(self._request('GET','/trade/api/v2/orders',
                (('open',opened),('exchanges',self.exchange),('symbols',self.symbol),('count','500'))))
            rows=data.get('orders') if isinstance(data,dict) else None
            if not isinstance(rows,list) or len(rows)>=500:
                raise TransportMalformed('missing or potentially truncated order inventory')
            for raw in rows:
                normalized=self._normalize(raw)
                identity=normalized['order_id'];client=normalized.get('client_order_id')
                if identity in ids or (client is not None and client in clients):
                    raise TransportMalformed('duplicate or ambiguous order inventory')
                ids.add(identity)
                if client is not None:clients.add(client)
                combined.append(normalized)
        return combined

    def send(self,request):
        action=request.action
        if action=='place_order':
            self._validate_order(request)
            # Fixed conservative local burst budget. No sleeping/retrying writes.
            now=self.clock.monotonic_seconds()
            self._placements=[t for t in self._placements if now-t<10]
            if len(self._placements)>=90:raise TransportRateLimited('local placement budget exhausted')
            self._funding(request.payload)
            self._placements.append(now)
            body={k:request.payload[k] for k in ('symbol','side','quantity')}
            body['quantity']=Decimal(body['quantity'])
            body.update(type='limit',price=Decimal(request.payload['limit_price']),exchange=self.exchange,
                client_order_id=native_client_id(self.exchange,request.idempotency_key),expiry_period=90)
            response=self._request('POST','/trade/api/v2/order',body=body.items())
        elif action in ('cancel_order','fetch_order_state'):
            identity=request.payload.get('broker_order_id')
            if not identity:
                matches=[o for o in self._inventory() if o.get('client_order_id')==request.idempotency_key]
                if len(matches)!=1:raise TransportMalformed('order absence is not proof of rejection; keep UNKNOWN')
                if action=='fetch_order_state':return SandboxResponse(200,matches[0])
                identity=matches[0]['order_id']
            response=self._request('DELETE' if action=='cancel_order' else 'GET','/trade/api/v2/order',
                query=(('order_id',identity),) if action=='fetch_order_state' else (),
                body=(('order_id',identity),) if action=='cancel_order' else ())
            if response.status==404:raise TransportMalformed('unresolved order absence; keep UNKNOWN')
        elif action=='fetch_orders':return SandboxResponse(200,{'orders':self._inventory()})
        elif action=='fetch_positions':
            raise TransportMalformed('account-wide balances cannot establish strategy allocations; verified isolation/reconciliation required')
        else:raise TransportMalformed('unsupported execution-contract action')
        if not 200<=response.status<300:
            # Never propagate untrusted venue error text or classify uncertain
            # server/rate-limit failures as fills. The existing broker interprets status.
            return SandboxResponse(response.status,{})
        data=self._data(response)
        normalized=self._normalize(data,request)
        if action!='place_order' and identity!=normalized['order_id']:
            raise TransportMalformed('broker order identity mismatch')
        return SandboxResponse(response.status,normalized)


class CoinSwitchContractBroker(SandboxBroker):
    """Offline native contract broker. Not wired into any application launcher."""
    def __init__(self,*,clock,orders,market,symbol,exchange,port=None):
        self.contract=CoinSwitchExecutionContract(clock=clock,orders=orders,market=market,
            symbol=symbol,exchange=exchange,port=port)
        super().__init__(clock=clock,transport=self.contract,quote_currency=market.min_notional.currency,
                         id_prefix='OFFLINE-COINSWITCH')

    def cancel_order(self,order):
        # A successful DELETE may still mean CANCELLATION_RAISED. Never let its
        # acknowledgement retire a remaining partial order; gateway's subsequent
        # authoritative GET applies terminal status and cumulative fills.
        try:
            self.contract.send(SandboxRequest(action='cancel_order',order_id=order.order_id,
                idempotency_key=order.idempotency_key,payload={'broker_order_id':order.broker_order_id or ''}))
        except Exception:
            return BrokerAck(AckOutcome.UNCERTAIN,message='cancel outcome requires authoritative order lookup')
        return BrokerAck(AckOutcome.UNCERTAIN,message='cancel requested; authoritative order lookup required')
