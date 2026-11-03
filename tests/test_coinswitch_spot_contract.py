"""CoinSwitch Spot order-contract translation without endpoint or credentials."""

from __future__ import annotations

import json
import unittest

from trading.adapters.coinswitch import CoinSwitchSpotResponseTranslator
from trading.adapters.sandbox import (
    SandboxBroker,
    SandboxRequest,
    SandboxResponse,
    SandboxTransport,
    TransportMalformed,
)
from trading.core.authz import Principal, Role, mint_execution_token
from trading.core.clock import ManualClock
from trading.core.money import Price, Quantity, USDT
from trading.core.orders import Order, OrderIntent, OrderSide, OrderType
from trading.ports.broker import AckOutcome


def order_record(**changes) -> dict[str, object]:
    value: dict[str, object] = {
        "order_id": "927db2d9-643c-47a3-9c0f-78741ac95cf5",
        "symbol": "BTC/USDT",
        "price": 26000,
        "average_price": 0,
        "orig_qty": 0.0009,
        "executed_qty": 0,
        "status": "OPEN",
        "side": "SELL",
        "exchange": "c2c1",
        "order_source": "API_TRADING",
        "created_time": 1689661638000,
        "updated_time": 1689661639000,
        "client_order_id": "decision-key",
    }
    value.update(changes)
    return value


def envelope(data: object) -> bytes:
    return json.dumps({"data": data}, separators=(",", ":")).encode()


class CoinSwitchSpotResponseTranslatorTest(unittest.TestCase):
    def setUp(self) -> None:
        self.translator = CoinSwitchSpotResponseTranslator()

    def test_translates_documented_order_shape_without_binary_float(self) -> None:
        response = self.translator.translate(
            action="place_order", status_code=200, body=envelope(order_record())
        )

        self.assertEqual(response.body["quantity"], "0.0009")
        self.assertEqual(response.body["filled_quantity"], "0")
        self.assertEqual(response.body["status"], "open")
        self.assertEqual(response.body["base_asset"], "BTC")
        self.assertEqual(response.body["quote_currency"], "USDT")

    def test_maps_every_documented_terminal_and_nonterminal_status(self) -> None:
        cases = {
            "OPEN": "open",
            "PARTIALLY_EXECUTED": "partially_filled",
            "EXECUTED": "filled",
            "CANCELLED": "canceled",
            "EXPIRED": "expired",
            "DISCARDED": "rejected",
            "CANCELLATION_RAISED": "open",
            "EXPIRATION_RAISED": "open",
        }
        for raw, expected in cases.items():
            filled = (
                "0.0004" if raw == "PARTIALLY_EXECUTED"
                else "0.0009" if raw == "EXECUTED"
                else "0"
            )
            average = "26000" if filled != "0" else "0"
            with self.subTest(status=raw):
                translated = self.translator.translate(
                    action="fetch_order_state",
                    status_code=200,
                    body=envelope(
                        order_record(
                            status=raw,
                            executed_qty=filled,
                            average_price=average,
                        )
                    ),
                )
                self.assertEqual(translated.body["status"], expected)

    def test_intermediate_cancel_with_a_fill_remains_partially_filled(self) -> None:
        translated = self.translator.translate(
            action="cancel_order",
            status_code=200,
            body=envelope(
                order_record(
                    status="CANCELLATION_RAISED",
                    executed_qty="0.0004",
                    average_price="25999.50",
                )
            ),
        )
        self.assertEqual(translated.body["status"], "partially_filled")

    def test_translates_inventory_and_preserves_client_order_id(self) -> None:
        translated = self.translator.translate(
            action="fetch_orders",
            status_code=200,
            body=envelope({"orders": [order_record()]}),
        )
        self.assertEqual(len(translated.body["orders"]), 1)
        self.assertEqual(
            translated.body["orders"][0]["client_order_id"], "decision-key"
        )

    def test_definite_http_refusal_is_preserved_for_broker_policy(self) -> None:
        translated = self.translator.translate(
            action="place_order",
            status_code=422,
            body=b'{"message":"Input price must be of type number"}',
        )
        self.assertEqual(translated.status_code, 422)
        self.assertEqual(translated.body["message"], "Input price must be of type number")

    def test_refuses_contradictions_unknown_status_and_account_holdings(self) -> None:
        bad_records = (
            order_record(status="MYSTERY"),
            order_record(orig_qty="0.0009", executed_qty="0.001"),
            order_record(executed_qty="0.0001", average_price="0"),
        )
        for record in bad_records:
            with self.subTest(record=record):
                with self.assertRaises(TransportMalformed):
                    self.translator.translate(
                        action="fetch_order_state",
                        status_code=200,
                        body=envelope(record),
                    )
        with self.assertRaises(TransportMalformed):
            self.translator.translate(
                action="fetch_positions", status_code=200, body=envelope([])
            )

    def test_preserves_terminal_cancel_with_cumulative_partial_fill(self) -> None:
        translated = self.translator.translate(
            action="fetch_order_state",
            status_code=200,
            body=envelope(
                order_record(
                    status="CANCELLED",
                    executed_qty="0.0001",
                    average_price="26000",
                )
            ),
        )
        self.assertEqual(translated.body["status"], "canceled")
        self.assertEqual(translated.body["filled_quantity"], "0.0001")


class OneCoinSwitchResponse(SandboxTransport):
    def __init__(self) -> None:
        self.request: SandboxRequest | None = None
        self.translator = CoinSwitchSpotResponseTranslator()

    def send(self, request: SandboxRequest) -> SandboxResponse:
        self.request = request
        return self.translator.translate(
            action=request.action,
            status_code=200,
            body=envelope(order_record(side="BUY")),
        )


class CoinSwitchContractThroughSandboxBrokerTest(unittest.TestCase):
    def test_limit_order_contract_reaches_existing_translation_boundary(self) -> None:
        clock = ManualClock()
        transport = OneCoinSwitchResponse()
        broker = SandboxBroker(
            clock=clock, transport=transport, quote_currency=USDT
        )
        intent = OrderIntent(
            strategy_id="strategy",
            signal_id="signal",
            symbol="BTC/USDT",
            side=OrderSide.BUY,
            quantity=Quantity("0.0009", "BTC"),
            order_type=OrderType.LIMIT,
            limit_price=Price("26000", USDT),
        )
        order = Order(intent, clock=clock)
        token = mint_execution_token(
            Principal("gateway", Role.EXECUTION_GATEWAY),
            order_id=order.order_id,
            idempotency_key=order.idempotency_key,
            clock=clock,
        )

        acknowledgement = broker.place_order(order, token=token)

        self.assertIs(acknowledgement.outcome, AckOutcome.ACCEPTED)
        assert transport.request is not None
        self.assertEqual(transport.request.payload["order_type"], "limit")
        self.assertEqual(transport.request.payload["limit_price"], "26000")
        self.assertEqual(transport.request.idempotency_key, order.idempotency_key)


if __name__ == "__main__":
    unittest.main()
