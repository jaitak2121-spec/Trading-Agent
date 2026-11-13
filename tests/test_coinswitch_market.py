"""Offline CoinSwitch trade-info translation into paper-order constraints."""

from __future__ import annotations

import base64
import hashlib
import json
import unittest
from unittest.mock import Mock
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from trading.app import (
    CoinSwitchMarketMetadataMalformed,
    CoinSwitchTradeInfoTranslator,
)
from trading.core.money import INR, USDT, Money
from trading.core.secrets import Secret
from trading.adapters.coinswitch.historical_candles import CoinSwitchTradeInfoReader,CoinSwitchCandleTransportError


def trade_info(
    *,
    exchange: str = "coinswitchx",
    symbol: str = "BTC/INR",
    minimum: object = "150",
    maximum: object = "2500000",
    base: object = 6,
    quote: object = 2,
    limit: object = 0,
) -> bytes:
    return json.dumps(
        {
            "data": {
                exchange: {
                    symbol: {
                        "quote": {"min": minimum, "max": maximum},
                        "precision": {
                            "base": base,
                            "quote": quote,
                            "limit": limit,
                        },
                    }
                }
            }
        },
        separators=(",", ":"),
    ).encode()


def capture(
    response: bytes,
    *,
    captured_at: str = "2026-11-03T10:00:00Z",
    exchange: str = "coinswitchx",
    symbol: str = "BTC/INR",
    checksum: str | None = None,
) -> bytes:
    return json.dumps(
        {
            "schema": "coinswitch-trade-info-capture/v1",
            "source": "/trade/api/v2/tradeInfo",
            "captured_at": captured_at,
            "exchange": exchange,
            "symbol": symbol,
            "response_body_base64": base64.b64encode(response).decode(),
            "response_sha256": checksum or hashlib.sha256(response).hexdigest(),
        },
        separators=(",", ":"),
    ).encode()


class CoinSwitchTradeInfoTranslatorTest(unittest.TestCase):
    def setUp(self) -> None:
        self.translator = CoinSwitchTradeInfoTranslator()

    def test_observed_c2c1_nonzero_limit_adjustment_is_not_silently_ignored(self):
        body=trade_info(exchange='c2c1',symbol='BTC/USDT',minimum='5',maximum='30000',base=5,quote=2,limit=2)
        with self.assertRaisesRegex(ValueError,'non-zero limit precision'):
            self.translator.translate(body=body,exchange='c2c1',symbol='BTC/USDT',currency=USDT)

    def test_metadata_reader_has_one_get_scope_and_no_fallback(self):
        opener=Mock(return_value=(200,b'{"data":{}}'));signer=Mock(return_value='signature-fixture')
        reader=CoinSwitchTradeInfoReader(api_key=Secret('metadata-test-key-value'),api_secret=Secret('metadata-test-secret-value'),
            opener=opener,signer=signer,clock_ms=lambda:123)
        self.assertEqual(reader.fetch(exchange='c2c1',symbol='BTC/USDT').statuses,(200,))
        self.assertEqual(opener.call_args.args[0],'/trade/api/v2/tradeInfo?exchange=c2c1&symbol=BTC/USDT')
        self.assertEqual(signer.call_args.args[1],b'GET/trade/api/v2/tradeInfo?exchange=c2c1&symbol=BTC/USDT123')
        opener.reset_mock();opener.return_value=(401,b'error')
        with self.assertRaises(CoinSwitchCandleTransportError):reader.fetch(exchange='c2c1',symbol='BTC/USDT')
        opener.assert_called_once();opener.reset_mock()
        with self.assertRaises(ValueError):reader.fetch(exchange='c2c1',symbol='BTC/USDT&path=orders')
        opener.assert_not_called()

    def test_metadata_http_exception_cannot_echo_credential_values(self):
        reader=CoinSwitchTradeInfoReader(api_key=Secret('metadata-test-key-value'),api_secret=Secret('metadata-test-secret-value'),
            opener=Mock(side_effect=RuntimeError('metadata-test-secret-value')),signer=lambda *_:'signature-fixture')
        with self.assertRaises(CoinSwitchCandleTransportError) as raised:reader.fetch(exchange='c2c1',symbol='BTC/USDT')
        self.assertNotIn('metadata-test-secret-value',str(raised.exception))

    def test_documented_trade_info_becomes_validated_market_specification(self) -> None:
        market = self.translator.translate(
            body=trade_info(),
            exchange="coinswitchx",
            symbol="BTC/INR",
            currency=INR,
        )

        self.assertEqual(market.price_decimals, 2)
        self.assertEqual(market.quantity_step, Decimal("0.000001"))
        self.assertIsNone(market.valid_until)
        self.assertEqual(market.min_notional, Money("150", INR))
        self.assertEqual(market.max_notional, Money("2500000", INR))

    def test_fresh_capture_verifies_provenance_age_and_payload_integrity(self) -> None:
        market = self.translator.translate_capture(
            capture=capture(trade_info()),
            exchange="coinswitchx",
            symbol="BTC/INR",
            currency=INR,
            now=datetime(2026, 11, 3, 11, tzinfo=timezone.utc),
            max_age=timedelta(hours=2),
        )

        self.assertEqual(market.quantity_step, Decimal("0.000001"))
        self.assertEqual(
            market.valid_until,
            datetime(2026, 11, 3, 12, tzinfo=timezone.utc),
        )

    def test_stale_future_mismatched_and_altered_captures_fail_closed(self) -> None:
        response = trade_info()
        now = datetime(2026, 11, 3, 11, tzinfo=timezone.utc)
        captures = (
            capture(response, captured_at="2026-11-02T10:00:00Z"),
            capture(response, captured_at="2026-11-03T12:00:00Z"),
            capture(response, exchange="c2c1"),
            capture(response, symbol="ETH/INR"),
            capture(response, checksum="0" * 64),
        )
        for value in captures:
            with self.subTest(capture=value):
                with self.assertRaises(CoinSwitchMarketMetadataMalformed):
                    self.translator.translate_capture(
                        capture=value,
                        exchange="coinswitchx",
                        symbol="BTC/INR",
                        currency=INR,
                        now=now,
                        max_age=timedelta(hours=24),
                    )

    def test_numeric_json_bound_is_decoded_without_binary_float(self) -> None:
        body = (
            b'{"data":{"c2c1":{"BTC/USDT":{"quote":'
            b'{"min":0.0009,"max":"10"},"precision":'
            b'{"base":8,"quote":8,"limit":0}}}}}'
        )

        market = self.translator.translate(
            body=body,
            exchange="c2c1",
            symbol="BTC/USDT",
            currency=USDT,
        )

        self.assertEqual(market.min_notional, Money("0.0009", USDT))

    def test_wrong_exchange_symbol_or_quote_currency_is_refused(self) -> None:
        cases = (
            {"exchange": "c2c1", "symbol": "BTC/INR", "currency": INR},
            {"exchange": "coinswitchx", "symbol": "ETH/INR", "currency": INR},
            {"exchange": "coinswitchx", "symbol": "BTC/INR", "currency": USDT},
        )
        for arguments in cases:
            with self.subTest(arguments=arguments):
                with self.assertRaises(CoinSwitchMarketMetadataMalformed):
                    self.translator.translate(body=trade_info(), **arguments)

    def test_unsafe_precision_and_ambiguous_limit_adjustment_fail_closed(self) -> None:
        bodies = (
            trade_info(base=-1),
            trade_info(quote=13),
            trade_info(limit=1),
            trade_info(base="6"),
        )
        for body in bodies:
            with self.subTest(body=body):
                with self.assertRaises(CoinSwitchMarketMetadataMalformed):
                    self.translator.translate(
                        body=body,
                        exchange="coinswitchx",
                        symbol="BTC/INR",
                        currency=INR,
                    )

    def test_missing_or_contradictory_bounds_fail_closed(self) -> None:
        bodies = (
            trade_info(minimum="not-a-number"),
            trade_info(minimum="200", maximum="100"),
            b'{"data":{}}',
            b'[]',
        )
        for body in bodies:
            with self.subTest(body=body):
                with self.assertRaises(CoinSwitchMarketMetadataMalformed):
                    self.translator.translate(
                        body=body,
                        exchange="coinswitchx",
                        symbol="BTC/INR",
                        currency=INR,
                    )


if __name__ == "__main__":
    unittest.main()
