"""Offline translation of documented CoinSwitch Spot order responses.

There is deliberately no HTTP client, base URL, credential, signer, or order
method in this module. CoinSwitch documents only its production Spot host; no
sandbox/testnet contract is published. This translator prepares the safe,
testable half of an eventual transport without making production reachable.

JSON financial numbers are decoded with ``parse_float=str`` before translation,
so a venue token such as ``0.0009`` never passes through binary floating point.
The output is the generic shape consumed by :class:`SandboxBroker`.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from decimal import Decimal, InvalidOperation

from ..sandbox import SandboxResponse, TransportMalformed

__all__ = ["CoinSwitchSpotResponseTranslator"]


_STATUS = {
    "OPEN": "open",
    "PARTIALLY_EXECUTED": "partially_filled",
    "EXECUTED": "filled",
    "CANCELLED": "canceled",
    "EXPIRED": "expired",
    "DISCARDED": "rejected",
}
_INTERMEDIATE = frozenset({"CANCELLATION_RAISED", "EXPIRATION_RAISED"})
_ORDER_ACTIONS = frozenset({"place_order", "cancel_order", "fetch_order_state"})


class CoinSwitchSpotResponseTranslator:
    """Translate CoinSwitch's documented envelopes without performing I/O."""

    def translate(
        self, *, action: str, status_code: int, body: bytes
    ) -> SandboxResponse:
        if isinstance(status_code, bool) or not isinstance(status_code, int):
            raise TypeError("status_code must be an int")
        if not isinstance(body, bytes):
            raise TypeError("body must be bytes")
        try:
            decoded = json.loads(body, parse_float=str)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise TransportMalformed("CoinSwitch response was not valid JSON") from exc
        if not isinstance(decoded, Mapping):
            raise TransportMalformed("CoinSwitch response envelope was not an object")

        # Preserve definite HTTP refusals for SandboxBroker to interpret. The
        # error message is observational only and never changes the outcome.
        if not 200 <= status_code < 300:
            message = decoded.get("message")
            return SandboxResponse(
                status_code,
                {"message": message} if isinstance(message, str) else {},
            )

        data = decoded.get("data")
        if action in _ORDER_ACTIONS:
            if not isinstance(data, Mapping):
                raise TransportMalformed("CoinSwitch response has no order object")
            return SandboxResponse(status_code, self._order(data))
        if action == "fetch_orders":
            if not isinstance(data, Mapping) or not isinstance(data.get("orders"), list):
                raise TransportMalformed("CoinSwitch response has no orders array")
            return SandboxResponse(
                status_code,
                {"orders": [self._order(item) for item in data["orders"]]},
            )
        if action == "fetch_positions":
            raise TransportMalformed(
                "CoinSwitch portfolio is account-wide holdings, not a per-exchange "
                "position snapshot; refusing to manufacture reconciliation data"
            )
        raise TransportMalformed(f"unsupported CoinSwitch action {action!r}")

    def _order(self, value: object) -> dict[str, object]:
        if not isinstance(value, Mapping):
            raise TransportMalformed("CoinSwitch order entry was not an object")
        order_id = self._text(value, "order_id")
        symbol = self._text(value, "symbol").upper()
        if symbol.count("/") != 1:
            raise TransportMalformed(f"order {order_id} has malformed symbol {symbol!r}")
        base_asset, quote_currency = symbol.split("/", 1)
        if not base_asset or not quote_currency:
            raise TransportMalformed(f"order {order_id} has malformed symbol {symbol!r}")
        side = self._text(value, "side").lower()
        if side not in {"buy", "sell"}:
            raise TransportMalformed(f"order {order_id} has malformed side {side!r}")

        ordered = self._number(value, "orig_qty")
        filled = self._number(value, "executed_qty")
        average = self._number(value, "average_price")
        if ordered <= 0 or filled < 0 or filled > ordered:
            raise TransportMalformed(
                f"order {order_id} has contradictory ordered/filled quantities"
            )
        raw_status = self._text(value, "status").upper()
        if raw_status == "OPEN" and filled != 0:
            raise TransportMalformed(f"order {order_id} is OPEN but reports fills")
        if raw_status == "PARTIALLY_EXECUTED" and not 0 < filled < ordered:
            raise TransportMalformed(
                f"order {order_id} has contradictory PARTIALLY_EXECUTED quantity"
            )
        if raw_status == "EXECUTED" and filled != ordered:
            raise TransportMalformed(
                f"order {order_id} is EXECUTED without its full quantity"
            )
        if raw_status == "DISCARDED" and filled != 0:
            raise TransportMalformed(
                f"order {order_id} is DISCARDED but reports fills"
            )
        if raw_status in _INTERMEDIATE:
            status = "partially_filled" if filled > 0 else "open"
        else:
            status = _STATUS.get(raw_status)
        if status is None:
            raise TransportMalformed(
                f"order {order_id} has unknown CoinSwitch status {raw_status!r}"
            )
        if filled > 0 and average <= 0:
            raise TransportMalformed(
                f"order {order_id} reports fills without a positive average price"
            )

        translated: dict[str, object] = {
            "order_id": order_id,
            "symbol": symbol,
            "side": side,
            "status": status,
            "quantity": str(ordered),
            "filled_quantity": str(filled),
            "average_price": str(average),
            "base_asset": base_asset,
            "quote_currency": quote_currency,
        }
        client_order_id = value.get("client_order_id")
        if client_order_id is not None:
            if not isinstance(client_order_id, str) or not client_order_id:
                raise TransportMalformed("client_order_id must be a non-empty string")
            translated["client_order_id"] = client_order_id
        return translated

    @staticmethod
    def _text(value: Mapping[str, object], field: str) -> str:
        item = value.get(field)
        if not isinstance(item, str) or not item.strip():
            raise TransportMalformed(f"CoinSwitch order is missing {field!r}")
        return item.strip()

    @staticmethod
    def _number(value: Mapping[str, object], field: str) -> Decimal:
        item = value.get(field)
        if isinstance(item, bool) or not isinstance(item, (str, int, Decimal)):
            raise TransportMalformed(f"CoinSwitch order has unusable {field!r}")
        try:
            result = Decimal(str(item))
        except InvalidOperation as exc:
            raise TransportMalformed(
                f"CoinSwitch order has non-numeric {field!r}"
            ) from exc
        if not result.is_finite():
            raise TransportMalformed(f"CoinSwitch order has non-finite {field!r}")
        return result
