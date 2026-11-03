"""Strict offline translation of CoinSwitch Spot trade-info metadata.

The translator accepts a captured ``/trade/api/v2/tradeInfo`` response. It
does not perform I/O, sign requests, accept credentials, or make the production
API reachable. Numeric JSON tokens are decoded as strings so venue values
never pass through binary floating point.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import json
from collections.abc import Mapping
from dataclasses import replace
from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation

from ..core.money import Currency, Money
from .order_policy import VenueMarketSpecification

__all__ = [
    "CoinSwitchMarketMetadataMalformed",
    "CoinSwitchTradeInfoTranslator",
]

_CAPTURE_SCHEMA = "coinswitch-trade-info-capture/v1"
_TRADE_INFO_SOURCE = "/trade/api/v2/tradeInfo"


class CoinSwitchMarketMetadataMalformed(ValueError):
    """A captured CoinSwitch market-metadata response is unsafe to use."""


class CoinSwitchTradeInfoTranslator:
    """Translate one documented trade-info record into order constraints."""

    def translate_capture(
        self,
        *,
        capture: bytes,
        exchange: str,
        symbol: str,
        currency: Currency,
        now: datetime,
        max_age: timedelta = timedelta(hours=24),
    ) -> VenueMarketSpecification:
        """Verify a versioned offline capture before translating its payload."""
        if not isinstance(capture, bytes):
            raise TypeError("capture must be bytes")
        if not isinstance(exchange, str) or not exchange.strip():
            raise TypeError("exchange must be a non-empty string")
        if not isinstance(symbol, str) or not symbol.strip():
            raise TypeError("symbol must be a non-empty string")
        if not isinstance(now, datetime) or now.tzinfo is None:
            raise TypeError("now must be a timezone-aware datetime")
        if not isinstance(max_age, timedelta) or max_age <= timedelta(0):
            raise ValueError("max_age must be a positive timedelta")
        try:
            decoded = json.loads(capture)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise CoinSwitchMarketMetadataMalformed(
                "CoinSwitch metadata capture was not valid JSON"
            ) from exc
        if not isinstance(decoded, Mapping):
            raise CoinSwitchMarketMetadataMalformed(
                "CoinSwitch metadata capture was not an object"
            )
        if decoded.get("schema") != _CAPTURE_SCHEMA:
            raise CoinSwitchMarketMetadataMalformed(
                "CoinSwitch metadata capture has an unsupported schema"
            )
        if decoded.get("source") != _TRADE_INFO_SOURCE:
            raise CoinSwitchMarketMetadataMalformed(
                "CoinSwitch metadata capture has an unexpected source"
            )

        expected_exchange = exchange.strip().lower()
        expected_symbol = symbol.strip().upper()
        if decoded.get("exchange") != expected_exchange:
            raise CoinSwitchMarketMetadataMalformed(
                "CoinSwitch metadata capture exchange does not match runtime"
            )
        if decoded.get("symbol") != expected_symbol:
            raise CoinSwitchMarketMetadataMalformed(
                "CoinSwitch metadata capture symbol does not match runtime"
            )

        captured_text = decoded.get("captured_at")
        if not isinstance(captured_text, str):
            raise CoinSwitchMarketMetadataMalformed(
                "CoinSwitch metadata capture has no UTC captured_at"
            )
        try:
            captured_at = datetime.fromisoformat(
                captured_text.removesuffix("Z") + "+00:00"
                if captured_text.endswith("Z")
                else captured_text
            )
        except ValueError as exc:
            raise CoinSwitchMarketMetadataMalformed(
                "CoinSwitch metadata captured_at is invalid"
            ) from exc
        if captured_at.tzinfo is None or captured_at.utcoffset() != timedelta(0):
            raise CoinSwitchMarketMetadataMalformed(
                "CoinSwitch metadata captured_at must be UTC"
            )
        age = now - captured_at
        if age < timedelta(0):
            raise CoinSwitchMarketMetadataMalformed(
                "CoinSwitch metadata capture is future-dated"
            )
        if age > max_age:
            raise CoinSwitchMarketMetadataMalformed(
                "CoinSwitch metadata capture is stale"
            )

        encoded = decoded.get("response_body_base64")
        checksum = decoded.get("response_sha256")
        if not isinstance(encoded, str) or not isinstance(checksum, str):
            raise CoinSwitchMarketMetadataMalformed(
                "CoinSwitch metadata capture has no payload integrity fields"
            )
        try:
            response_body = base64.b64decode(encoded, validate=True)
        except (ValueError, binascii.Error) as exc:
            raise CoinSwitchMarketMetadataMalformed(
                "CoinSwitch metadata capture payload is not valid base64"
            ) from exc
        actual_checksum = hashlib.sha256(response_body).hexdigest()
        if len(checksum) != 64 or not hmac.compare_digest(
            actual_checksum, checksum.lower()
        ):
            raise CoinSwitchMarketMetadataMalformed(
                "CoinSwitch metadata capture payload checksum does not match"
            )
        specification = self.translate(
            body=response_body,
            exchange=expected_exchange,
            symbol=expected_symbol,
            currency=currency,
        )
        return replace(
            specification,
            metadata_source=_TRADE_INFO_SOURCE,
            captured_at=captured_at,
            valid_until=captured_at + max_age,
        )

    def translate(
        self,
        *,
        body: bytes,
        exchange: str,
        symbol: str,
        currency: Currency,
    ) -> VenueMarketSpecification:
        if not isinstance(body, bytes):
            raise TypeError("body must be bytes")
        if not isinstance(exchange, str) or not exchange.strip():
            raise TypeError("exchange must be a non-empty string")
        if not isinstance(symbol, str) or not symbol.strip():
            raise TypeError("symbol must be a non-empty string")
        if not isinstance(currency, Currency):
            raise TypeError("currency must be a Currency")

        selected_exchange = exchange.strip().lower()
        selected_symbol = symbol.strip().upper()
        if selected_exchange not in {"coinswitchx", "c2c1", "c2c2"}:
            raise CoinSwitchMarketMetadataMalformed(
                f"unsupported CoinSwitch exchange {selected_exchange!r}"
            )
        if selected_symbol.count("/") != 1:
            raise CoinSwitchMarketMetadataMalformed("symbol must be BASE/QUOTE")
        base_asset, quote_code = selected_symbol.split("/", 1)
        if not base_asset or quote_code != currency.code:
            raise CoinSwitchMarketMetadataMalformed(
                "metadata symbol quote does not match the configured currency"
            )

        try:
            decoded = json.loads(
                body,
                parse_float=str,
                parse_constant=self._reject_constant,
            )
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise CoinSwitchMarketMetadataMalformed(
                "CoinSwitch trade-info capture was not valid JSON"
            ) from exc
        if not isinstance(decoded, Mapping):
            raise CoinSwitchMarketMetadataMalformed(
                "CoinSwitch trade-info envelope was not an object"
            )

        data = self._mapping(decoded.get("data"), "data")
        markets = self._mapping(
            data.get(selected_exchange), f"data.{selected_exchange}"
        )
        market = self._mapping(
            markets.get(selected_symbol),
            f"data.{selected_exchange}.{selected_symbol}",
        )
        quote = self._mapping(market.get("quote"), "quote")
        precision = self._mapping(market.get("precision"), "precision")

        base_decimals = self._precision(precision, "base", maximum=18)
        quote_decimals = self._precision(precision, "quote", maximum=12)
        limit_adjustment = self._precision(precision, "limit", maximum=12)
        if limit_adjustment != 0:
            raise CoinSwitchMarketMetadataMalformed(
                "non-zero limit precision adjustment is not safely specified"
            )

        minimum = self._number(quote, "min")
        maximum = self._number(quote, "max")
        try:
            return VenueMarketSpecification(
                price_decimals=quote_decimals,
                quantity_step=Decimal(1).scaleb(-base_decimals),
                min_notional=Money(minimum, currency),
                max_notional=Money(maximum, currency),
            )
        except (TypeError, ValueError, ArithmeticError) as exc:
            raise CoinSwitchMarketMetadataMalformed(
                "CoinSwitch trade-info constraints are contradictory"
            ) from exc

    @staticmethod
    def _reject_constant(value: str) -> None:
        raise CoinSwitchMarketMetadataMalformed(
            f"CoinSwitch trade-info contains non-finite JSON token {value!r}"
        )

    @staticmethod
    def _mapping(value: object, field: str) -> Mapping[str, object]:
        if not isinstance(value, Mapping):
            raise CoinSwitchMarketMetadataMalformed(
                f"CoinSwitch trade-info is missing object {field!r}"
            )
        return value

    @staticmethod
    def _precision(
        value: Mapping[str, object], field: str, *, maximum: int
    ) -> int:
        item = value.get(field)
        if isinstance(item, bool) or not isinstance(item, int):
            raise CoinSwitchMarketMetadataMalformed(
                f"CoinSwitch precision {field!r} must be an integer"
            )
        if not 0 <= item <= maximum:
            raise CoinSwitchMarketMetadataMalformed(
                f"CoinSwitch precision {field!r} is outside the safe range"
            )
        return item

    @staticmethod
    def _number(value: Mapping[str, object], field: str) -> Decimal:
        item = value.get(field)
        if isinstance(item, bool) or not isinstance(item, (str, int, Decimal)):
            raise CoinSwitchMarketMetadataMalformed(
                f"CoinSwitch quote bound {field!r} is not numeric"
            )
        try:
            result = Decimal(str(item))
        except InvalidOperation as exc:
            raise CoinSwitchMarketMetadataMalformed(
                f"CoinSwitch quote bound {field!r} is not numeric"
            ) from exc
        if not result.is_finite():
            raise CoinSwitchMarketMetadataMalformed(
                f"CoinSwitch quote bound {field!r} is not finite"
            )
        return result
