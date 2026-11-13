"""Explicit single-endpoint fee GET; no orders, balances or credential discovery."""
import hashlib
import json
import time

from .historical_candles import (CandleFetchResult,CoinSwitchCandleTransportError,
    signed_market_get_headers,_https_get)
from ...core.money import to_decimal
from ...core.secrets import Secret


class TradingFeeReadError(CoinSwitchCandleTransportError):
    pass


def parse_trading_fee(body,*,exchange,base_asset,captured_at_ms):
    if exchange not in ('c2c1','c2c2','coinswitchx') or not isinstance(base_asset,str) or not base_asset.isalnum():
        raise ValueError('invalid fee market identity')
    if not isinstance(body,bytes) or type(captured_at_ms) is not int or captured_at_ms<0:
        raise ValueError('fee response requires bytes and integer capture time')
    try:
        row=json.loads(body,parse_float=str)['data'][exchange][base_asset.upper()]
        maker=to_decimal(row['maker_fee_after_discount'],field='effective maker fee')
        taker=to_decimal(row['taker_fee_after_discount'],field='effective taker fee')
        if not 0<=min(maker,taker)<=max(maker,taker)<1:raise ValueError('invalid fee range')
        updated=row.get('timestamp')
        if updated is not None and (type(updated) is not int or updated<0 or updated*1000>captured_at_ms+5000):
            raise ValueError('invalid fee-schedule update time')
    except (KeyError,TypeError,ValueError) as exc:
        raise ValueError('unusable account-specific fee schedule') from None
    result={'schema':'coinswitch-account-fee-snapshot/v1','exchange':exchange,'base_asset':base_asset.upper(),
        'effective_maker_rate':str(maker),'effective_taker_rate':str(taker),
        'effective_maker_bps':str(maker*10000),'effective_taker_bps':str(taker*10000),
        'captured_at_ms':captured_at_ms,'schedule_updated_at_seconds':updated,
        'endpoint':'/trade/api/v2/tradingFee','response_sha256':hashlib.sha256(body).hexdigest(),
        'account_specific':True,'live_trading':False,'orders_executed':False,
        'limitation':'capture-time fee schedule, not evidence of historical fees or future execution quality'}
    result['snapshot_sha256']=hashlib.sha256(json.dumps(result,sort_keys=True,separators=(',',':')).encode()).hexdigest()
    return result


class CoinSwitchFeeReader:
    """One signed GET to the existing reviewed TLS boundary; no retries/redirects."""
    def __init__(self,*,api_key,api_secret,opener=None,signer=None,clock_ms=None):
        if not isinstance(api_key,Secret) or not isinstance(api_secret,Secret):
            raise TypeError('credentials must be Secret values')
        self.key,self.secret=api_key,api_secret
        self.opener=opener or _https_get
        self.signer=signer
        self.clock_ms=clock_ms or (lambda:time.time_ns()//1000000)

    def fetch(self,exchange):
        if exchange not in ('c2c1','c2c2','coinswitchx'):raise ValueError('unsupported exchange')
        path='/trade/api/v2/tradingFee?exchange='+exchange
        epoch=str(self.clock_ms())
        headers=signed_market_get_headers(self.key,self.secret,path,epoch,signer=self.signer)
        try:status,body=self.opener(path,headers,20.0)
        except Exception:
            # HTTP/header exceptions can echo credentials. Preserve no client
            # exception text, even when it is not an OSError subtype.
            raise TradingFeeReadError('fee GET failed at HTTP boundary',statuses=('request_error',)) from None
        if type(status) is not int or not 200<=status<300:
            raise TradingFeeReadError('fee GET refused; no fallback endpoint attempted',statuses=(status,))
        return CandleFetchResult(body,(status,))
