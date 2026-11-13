"""CoinSwitch public Spot Socket.IO translation and fail-closed behavior."""

from __future__ import annotations

import json
import threading
import unittest

from trading.adapters.coinswitch.spot_stream import (
    CoinSwitchSpotStream,
    _WebSocketConnection,
)
from trading.adapters.marketdata import FeedDisconnected, FeedUnavailable


BOOK = {
    "s": "BTC,INR",
    "timestamp": 1673255767122,
    "bids": [["1471201.000000000000", "0.005870"]],
    "asks": [["1484400.000000000000", "0.022109"]],
}
CANDLE = {
    "o": "1470000",
    "h": "1490000",
    "l": "1460000",
    "c": "1480000",
    "volume": "0.30468",
    "end_time": "1699423980000",
    "symbol": "BTC,INR",
    "interval": "1",
}
TRADE_1 = {
    "E": 1699423921000,
    "p": 100.25,
    "q": 0.10,
    "s": "BTC,INR",
    "t": 1,
    "e": "coinswitchx",
}


class ScriptedConnection:
    def __init__(self, messages) -> None:
        self.messages = list(messages)
        self.sent: list[str] = []
        self.closed = False
        self.exhausted = threading.Event()

    def recv_text(self) -> str:
        if self.messages:
            return self.messages.pop(0)
        self.exhausted.set()
        raise FeedDisconnected("script exhausted")

    def send_text(self, text: str) -> None:
        self.sent.append(text)

    def close(self) -> None:
        self.closed = True


class BlockingConnection(ScriptedConnection):
    def __init__(self, messages) -> None:
        super().__init__(messages)
        self._closed_event = threading.Event()

    def recv_text(self) -> str:
        if self.messages:
            return self.messages.pop(0)
        self.exhausted.set()
        self._closed_event.wait(2)
        raise FeedDisconnected("connection closed")

    def close(self) -> None:
        super().close()
        self._closed_event.set()


def packet(event: str, payload: object) -> str:
    return "42/coinswitchx," + json.dumps([event, payload], separators=(",", ":"))


class CoinSwitchSpotStreamTest(unittest.TestCase):
    def build(self, events, *, opening=None):
        connection = ScriptedConnection(
            [
                opening or '0{"sid":"abc","pingInterval":25000}',
                "40/coinswitchx,{\"sid\":\"namespace\"}",
                *events,
            ]
        )
        stream = CoinSwitchSpotStream(
            symbol="BTC/INR",
            connection_factory=lambda _host, _path, _timeout: connection,
        )
        return stream, connection

    def test_subscribes_to_documented_book_and_trade_events(self) -> None:
        stream, connection = self.build([])
        stream.start()
        self.assertTrue(connection.exhausted.wait(1))
        stream.stop()

        self.assertEqual(connection.sent[0], "40/coinswitchx,")
        self.assertIn("FETCH_ORDER_BOOK_CS_PRO", connection.sent[1])
        self.assertIn("FETCH_TRADES_CS_PRO", connection.sent[2])
        self.assertIn('"pair":"BTC,INR"', connection.sent[1])

    def test_connects_through_the_documented_exchange_specific_path(self) -> None:
        connection = ScriptedConnection(
            ['0{"sid":"abc"}', "40/c2c1,{}"]
        )
        observed = []
        stream = CoinSwitchSpotStream(
            symbol="BTC/USDT",
            exchange="c2c1",
            connection_factory=lambda host, path, timeout: (
                observed.append((host, path, timeout)) or connection
            ),
        )
        stream.start()
        self.assertTrue(connection.exhausted.wait(1))
        stream.stop()
        self.assertEqual(observed[0][0], "ws.coinswitch.co")
        self.assertEqual(
            observed[0][1],
            "/pro/realtime-rates-socket/spot/c2c1/?EIO=4&transport=websocket",
        )

    def test_translates_order_book_to_the_ingestion_quote_shape(self) -> None:
        acknowledgement = {
            "success": "true",
            "message": "Subscribed to FETCH_ORDER_BOOK_CS_PRO_COINSWITCHX_BTC,INR",
        }
        stream, connection = self.build([
            packet("FETCH_ORDER_BOOK_CS_PRO", acknowledgement),
            packet("FETCH_ORDER_BOOK_CS_PRO", BOOK),
        ])
        stream.start()
        self.assertTrue(connection.exhausted.wait(1))

        messages = stream.quote_source.poll()

        self.assertEqual(
            messages,
            [{
                "symbol": "BTC/INR",
                "bid": "1471201.000000000000",
                "ask": "1484400.000000000000",
                "timestamp": 1673255767122,
            }],
        )
        stream.stop()

    def test_subscription_acknowledgement_is_control_not_market_data(self) -> None:
        acknowledgement = {
            "success": "true",
            "message": "Subscribed to FETCH_TRADES_CS_PRO_COINSWITCHX_BTC,INR",
        }
        stream, connection = self.build([
            packet("FETCH_TRADES_CS_PRO", acknowledgement)
        ])
        stream.start()
        self.assertTrue(connection.exhausted.wait(1))
        with self.assertRaises(FeedDisconnected):
            stream.candle_source.poll()
        self.assertNotIn("unreadable", str(stream.last_error))
        stream.stop()

    def test_aggregates_exact_trade_numbers_into_closed_minute_candle(self) -> None:
        second = dict(TRADE_1, E=1699423930000, p=102.5, q=0.20, t=2)
        next_minute = dict(TRADE_1, E=1699423980000, p=101.0, q=0.05, t=3)
        stream, connection = self.build([
            packet("FETCH_TRADES_CS_PRO", dict(TRADE_1,E=1699423861000,t=0)),
            packet("FETCH_TRADES_CS_PRO", TRADE_1),
            packet("FETCH_TRADES_CS_PRO", second),
            packet("FETCH_TRADES_CS_PRO", next_minute),
        ])
        stream.start()
        self.assertTrue(connection.exhausted.wait(1))

        message = stream.candle_source.poll()[0]

        self.assertEqual(message["open_time"], 1699423920000)
        self.assertEqual(message["open"], "100.25")
        self.assertEqual(message["high"], "102.5")
        self.assertEqual(message["low"], "100.25")
        self.assertEqual(message["close"], "102.5")
        self.assertEqual(message["volume"], "0.3")
        stream.stop()

    def test_startup_trade_bucket_is_not_treated_as_complete(self):
        stream=CoinSwitchSpotStream(symbol='BTC/INR')
        self.assertIsNone(stream._aggregate_trade(TRADE_1))
        self.assertIsNone(stream._aggregate_trade(dict(TRADE_1,E=1699423981000,t=2)))
        self.assertIsNotNone(stream._aggregate_trade(dict(TRADE_1,E=1699424041000,t=3)))
        stream._trade_bar=None  # Same boundary reset performed on reconnect.
        self.assertIsNone(stream._aggregate_trade(dict(TRADE_1,E=1699424101000,t=4)))
        self.assertIsNone(stream._aggregate_trade(dict(TRADE_1,E=1699424161000,t=5)))

    def test_translates_closed_candle_and_derives_open_time(self) -> None:
        stream, connection = self.build([packet("FETCH_CANDLESTICK_CS_PRO", CANDLE)])
        stream.start()
        self.assertTrue(connection.exhausted.wait(1))

        message = stream.candle_source.poll()[0]

        self.assertEqual(message["symbol"], "BTC/INR")
        self.assertEqual(message["interval_seconds"], 60)
        self.assertEqual(message["open_time"], 1699423920000)
        self.assertEqual(message["close"], "1480000")
        stream.stop()

    def test_engine_ping_is_answered_during_namespace_handshake(self) -> None:
        connection = ScriptedConnection(
            ['0{"sid":"abc"}', "2", "40/coinswitchx,{}"]
        )
        stream = CoinSwitchSpotStream(
            symbol="BTC/INR",
            connection_factory=lambda *_args: connection,
        )
        stream.start()
        self.assertTrue(connection.exhausted.wait(1))
        self.assertEqual(connection.sent[:2], ["40/coinswitchx,", "3"])
        stream.stop()

    def test_disconnect_is_retained_and_surfaced_to_both_sources(self) -> None:
        stream, connection = self.build([])
        stream.start()
        self.assertTrue(connection.exhausted.wait(1))

        with self.assertRaises(FeedDisconnected):
            stream.quote_source.poll()
        with self.assertRaises(FeedDisconnected):
            stream.candle_source.poll()
        stream.stop()

    def test_reconnects_resubscribes_and_recovers_after_disconnect(self) -> None:
        first = ScriptedConnection([
            '0{"sid":"one"}', "40/coinswitchx,{}"
        ])
        recovered_book = dict(BOOK, timestamp=1673255767999)
        second = BlockingConnection([
            '0{"sid":"two"}',
            "40/coinswitchx,{}",
            packet("FETCH_ORDER_BOOK_CS_PRO", recovered_book),
        ])
        connections = [first, second]
        stream = CoinSwitchSpotStream(
            symbol="BTC/INR",
            reconnect_initial_seconds=0.001,
            reconnect_max_seconds=0.002,
            connection_factory=lambda *_args: connections.pop(0),
        )
        stream.start()
        self.assertTrue(second.exhausted.wait(1))

        self.assertEqual(stream.successful_reconnects, 1)
        self.assertEqual(stream.reconnect_attempts, 1)
        self.assertEqual(
            stream.quote_source.poll()[0]["timestamp"], 1673255767999
        )
        self.assertIsNone(stream.last_error)
        stream.stop()

    def test_poll_before_start_fails_loudly(self) -> None:
        stream = CoinSwitchSpotStream(symbol="BTC/INR")
        with self.assertRaises(FeedUnavailable):
            stream.quote_source.poll()

    def test_wrong_symbol_and_malformed_event_fail_closed(self) -> None:
        wrong = dict(BOOK, s="ETH,INR")
        stream, connection = self.build([packet("FETCH_ORDER_BOOK_CS_PRO", wrong)])
        stream.start()
        for _ in range(100):
            if stream.last_error is not None:
                break
            threading.Event().wait(0.005)
        self.assertIsNotNone(stream.last_error)
        with self.assertRaises(FeedDisconnected):
            stream.quote_source.poll()
        stream.stop()

    def test_configuration_is_strict(self) -> None:
        for symbol in ("BTCINR", "/INR", "BTC/"):
            with self.subTest(symbol=symbol):
                with self.assertRaises(ValueError):
                    CoinSwitchSpotStream(symbol=symbol)
        with self.assertRaises(ValueError):
            CoinSwitchSpotStream(symbol="BTC/INR", exchange="unknown")
        with self.assertRaises(ValueError):
            CoinSwitchSpotStream(symbol="BTC/USDT", exchange="coinswitchx")
        with self.assertRaises(ValueError):
            CoinSwitchSpotStream(symbol="BTC/INR", exchange="c2c1")
        with self.assertRaises(ValueError):
            CoinSwitchSpotStream(symbol="BTC/INR", timeout_seconds=0)
        with self.assertRaises(ValueError):
            CoinSwitchSpotStream(
                symbol="BTC/INR", reconnect_initial_seconds=0
            )
        with self.assertRaises(ValueError):
            CoinSwitchSpotStream(
                symbol="BTC/INR",
                reconnect_initial_seconds=2,
                reconnect_max_seconds=1,
            )


class FakeSocket:
    def __init__(self, incoming: bytes = b"") -> None:
        self.incoming = bytearray(incoming)
        self.sent = bytearray()
        self.closed = False

    def settimeout(self, _timeout: float) -> None:
        return

    def recv(self, size: int) -> bytes:
        data = bytes(self.incoming[:size])
        del self.incoming[:size]
        return data

    def sendall(self, data: bytes) -> None:
        self.sent.extend(data)

    def close(self) -> None:
        self.closed = True


def server_text_frame(text: str, *, final: bool = True, opcode: int = 1) -> bytes:
    payload = text.encode()
    return bytes(((0x80 if final else 0) | opcode, len(payload))) + payload


class WebSocketFramingTest(unittest.TestCase):
    def test_reads_unmasked_server_text_and_fragmentation(self) -> None:
        incoming = server_text_frame("hel", final=False) + server_text_frame(
            "lo", opcode=0
        )
        connection = _WebSocketConnection(FakeSocket(incoming), timeout_seconds=1)
        self.assertEqual(connection.recv_text(), "hello")

    def test_client_text_frame_is_masked_and_decodes_to_original(self) -> None:
        sock = FakeSocket()
        connection = _WebSocketConnection(sock, timeout_seconds=1)

        connection.send_text("hello")

        frame = bytes(sock.sent)
        self.assertEqual(frame[0], 0x81)
        self.assertTrue(frame[1] & 0x80)
        length = frame[1] & 0x7F
        mask = frame[2:6]
        payload = frame[6 : 6 + length]
        decoded = bytes(value ^ mask[index % 4] for index, value in enumerate(payload))
        self.assertEqual(decoded, b"hello")


if __name__ == "__main__":
    unittest.main()
