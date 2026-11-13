"""Fee reads are explicit, narrowly scoped, and never imply order permission."""
import contextlib
import hashlib
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from trading.adapters.coinswitch.trading_fees import CoinSwitchFeeReader,parse_trading_fee,TradingFeeReadError
from trading.adapters.coinswitch.historical_candles import signed_market_get_headers,CandleFetchResult
from trading.app.execution_costs import main
from trading.core.secrets import Secret


def body(maker='0.0009',taker='0.001'):
    return json.dumps({'data':{'c2c1':{'BTC':{'maker_fee_after_discount':maker,
        'taker_fee_after_discount':taker,'timestamp':100}}}}).encode()


class TradingFeeTests(unittest.TestCase):
    def test_decimal_rate_units_and_provenance(self):
        raw=body();profile=parse_trading_fee(raw,exchange='c2c1',base_asset='BTC',captured_at_ms=200000)
        self.assertEqual(profile['effective_maker_bps'],'9.0000')
        self.assertEqual(profile['effective_taker_bps'],'10.000')
        self.assertEqual(profile['response_sha256'],hashlib.sha256(raw).hexdigest())
        self.assertTrue(profile['account_specific'])
        self.assertFalse(profile['live_trading'])

    def test_missing_wrong_asset_or_invalid_rate_fails_without_guessing(self):
        for raw in (b'bad-json',b'{"data":{}}',body('-0.001'),body('NaN'),body(True),body('1')):
            with self.assertRaises(ValueError):parse_trading_fee(raw,exchange='c2c1',base_asset='BTC',captured_at_ms=200000)
        with self.assertRaises(ValueError):parse_trading_fee(body(),exchange='c2c1',base_asset='ETH',captured_at_ms=200000)

    def test_single_allowlisted_get_and_shared_signing_no_order_method(self):
        calls=[];messages=[]
        def opener(path,headers,timeout):calls.append(path);return 200,body()
        def signer(secret,message):messages.append(message);return 'signature-fixture'
        reader=CoinSwitchFeeReader(api_key=Secret('fixture-public'),api_secret=Secret('fixture-private'),
            opener=opener,signer=signer,clock_ms=lambda:1234)
        self.assertEqual(reader.fetch('c2c1').statuses,(200,))
        self.assertEqual(calls,['/trade/api/v2/tradingFee?exchange=c2c1'])
        self.assertEqual(messages,[b'GET/trade/api/v2/tradingFee?exchange=c2c11234'])
        self.assertFalse(hasattr(reader,'place_order'))
        with self.assertRaises(ValueError):reader.fetch('c2c1&order=bad')
        with self.assertRaises(ValueError):signed_market_get_headers(Secret('fixture'),Secret('fixture'),'/trade/api/v2/order','123',signer=signer)
        self.assertEqual(len(calls),1)

    def test_http_refusals_are_not_retried_or_echoed(self):
        for status in (401,403,429,500):
            calls=[]
            def opener(path,headers,timeout):calls.append(path);return status,b'{"message":"SECRET-FIXTURE"}'
            reader=CoinSwitchFeeReader(api_key=Secret('fixture-public'),api_secret=Secret('fixture-private'),
                opener=opener,signer=lambda *args:'fixture')
            with self.assertRaises(TradingFeeReadError) as error:reader.fetch('c2c1')
            self.assertEqual(error.exception.statuses,(status,))
            self.assertNotIn('SECRET',str(error.exception))
            self.assertEqual(len(calls),1)
        def broken(*args):raise RuntimeError('SECRET-FIXTURE request header')
        reader=CoinSwitchFeeReader(api_key=Secret('fixture-public'),api_secret=Secret('fixture-private'),
            opener=broken,signer=lambda *args:'fixture')
        with self.assertRaises(TradingFeeReadError) as error:reader.fetch('c2c1')
        self.assertNotIn('SECRET',str(error.exception))

    def test_cli_requires_explicit_authorization_and_refuses_existing_output(self):
        with tempfile.TemporaryDirectory() as folder,contextlib.redirect_stderr(io.StringIO()):
            p=Path(folder)/'fee.json'
            with patch('trading.adapters.coinswitch.trading_fees.CoinSwitchFeeReader') as reader:
                with self.assertRaises(SystemExit):main(['capture-fees',str(p)])
                reader.assert_not_called()
                p.write_text('preserve')
                with self.assertRaises(SystemExit):main(['capture-fees',str(p),'--authorize-read-only-fee-lookup'])
                self.assertEqual(p.read_text(),'preserve')
                reader.assert_not_called()

    def test_authorized_mock_capture_is_private_and_contains_no_credentials(self):
        with tempfile.TemporaryDirectory() as folder,contextlib.redirect_stdout(io.StringIO()):
            p=Path(folder)/'private-fees'/'c2c1-BTC.json'
            with patch.dict('os.environ',{'COINSWITCH_API_KEY':'fixture-public-key-value',
                    'COINSWITCH_API_SECRET':'fixture-private-key-value'}),\
                    patch('trading.adapters.coinswitch.trading_fees.CoinSwitchFeeReader') as reader:
                reader.return_value.fetch.return_value=CandleFetchResult(body(),(200,))
                self.assertEqual(main(['capture-fees',str(p),'--authorize-read-only-fee-lookup']),0)
            raw=p.read_text();captured=json.loads(raw)
            self.assertEqual(p.stat().st_mode&0o777,0o600)
            self.assertNotIn('fixture-public-key-value',raw)
            self.assertNotIn('fixture-private-key-value',raw)
            self.assertTrue(captured['credentials_used'])
            self.assertFalse(captured['orders_executed'])
