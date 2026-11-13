"""CoinSwitch Spot public order-book and candle Socket.IO stream.

Contract verified against CoinSwitch's official Spot WebSocket documentation
(May 11, 2026): Engine.IO v4 over WebSocket, exchange-specific path
``/pro/realtime-rates-socket/spot/{exchange}``, exchange namespace, and the
``FETCH_ORDER_BOOK_CS_PRO`` / ``FETCH_TRADES_CS_PRO`` events. One-minute
candles are aggregated from exact public trade prints because the documented
candlestick subscription was observed acknowledging without delivering bars.

The adapter is observation-only.  It holds no gateway, broker, execution token,
API key, or order method.  A disconnect is retained and surfaced through both
``MarketDataSource`` views, causing the existing ingestors to fail closed.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import queue
import socket
import ssl
import threading
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from decimal import Decimal
from typing import Protocol

from ..marketdata import (
    FeedDisconnected,
    FeedError,
    FeedTimeout,
    FeedUnavailable,
    MarketDataSource,
    RawMessage,
)

__all__ = ["CoinSwitchSpotStream"]

_HOST = "ws.coinswitch.co"
_PATH_TEMPLATE = (
    "/pro/realtime-rates-socket/spot/{exchange}/?EIO=4&transport=websocket"
)
_BOOK_EVENT = "FETCH_ORDER_BOOK_CS_PRO"
_CANDLE_EVENT = "FETCH_CANDLESTICK_CS_PRO"
_TRADE_EVENT = "FETCH_TRADES_CS_PRO"
_EXCHANGES = frozenset({"coinswitchx", "c2c1", "c2c2"})
_WS_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"


class _TextConnection(Protocol):
    def send_text(self, text: str) -> None: ...
    def recv_text(self) -> str: ...
    def close(self) -> None: ...


class _Source(MarketDataSource):
    def __init__(self, stream: "CoinSwitchSpotStream", kind: str) -> None:
        self._stream = stream
        self._kind = kind

    def poll(self) -> list[RawMessage]:
        return self._stream._poll(self._kind)


@dataclass(slots=True)
class _TradeBar:
    open_time: int
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: Decimal
    coverage_complete: bool = False


class _WebSocketConnection:
    """Minimal RFC 6455 text client sufficient for Engine.IO v4."""

    def __init__(self, sock, *, timeout_seconds: float) -> None:
        self._sock = sock
        self._sock.settimeout(timeout_seconds)
        self._send_lock = threading.Lock()
        self._buffer = bytearray()

    @classmethod
    def connect(
        cls, host: str, path: str, timeout_seconds: float
    ) -> "_WebSocketConnection":
        raw = None
        wrapped = None
        try:
            raw = socket.create_connection((host, 443), timeout=timeout_seconds)
            default_paths = ssl.get_default_verify_paths()
            cafile = default_paths.cafile
            if cafile is None:
                for candidate in ("/etc/ssl/cert.pem", "/etc/pki/tls/cert.pem"):
                    if os.path.isfile(candidate):
                        cafile = candidate
                        break
            context = ssl.create_default_context(cafile=cafile)
            wrapped = context.wrap_socket(raw, server_hostname=host)
            connection = cls(wrapped, timeout_seconds=timeout_seconds)
            connection._upgrade(host, path)
            return connection
        except FeedError:
            if wrapped is not None:
                wrapped.close()
            elif raw is not None:
                raw.close()
            raise
        except TimeoutError as exc:
            if wrapped is not None:
                wrapped.close()
            elif raw is not None:
                raw.close()
            raise FeedTimeout("CoinSwitch WebSocket connection timed out") from exc
        except (OSError, ssl.SSLError) as exc:
            if wrapped is not None:
                wrapped.close()
            elif raw is not None:
                raw.close()
            raise FeedUnavailable(
                f"CoinSwitch WebSocket connection failed: {type(exc).__name__}"
            ) from exc

    def _upgrade(self, host: str, path: str) -> None:
        key = base64.b64encode(os.urandom(16)).decode("ascii")
        request = (
            f"GET {path} HTTP/1.1\r\n"
            f"Host: {host}\r\n"
            "Upgrade: websocket\r\n"
            "Connection: Upgrade\r\n"
            f"Sec-WebSocket-Key: {key}\r\n"
            "Sec-WebSocket-Version: 13\r\n"
            "Origin: https://coinswitch.co\r\n\r\n"
        ).encode("ascii")
        self._sock.sendall(request)
        response = self._receive_headers()
        lines = response.decode("iso-8859-1").split("\r\n")
        if not lines or " 101 " not in f" {lines[0]} ":
            raise FeedUnavailable(
                f"CoinSwitch WebSocket upgrade refused: {lines[0] if lines else 'empty response'}"
            )
        headers: dict[str, str] = {}
        for line in lines[1:]:
            if ":" in line:
                name, value = line.split(":", 1)
                headers[name.strip().lower()] = value.strip()
        expected = base64.b64encode(
            hashlib.sha1((key + _WS_GUID).encode("ascii")).digest()
        ).decode("ascii")
        if headers.get("sec-websocket-accept") != expected:
            raise FeedUnavailable("CoinSwitch WebSocket upgrade signature was invalid")

    def _receive_headers(self) -> bytes:
        marker = b"\r\n\r\n"
        while marker not in self._buffer:
            chunk = self._recv(4096)
            if not chunk:
                raise FeedDisconnected("CoinSwitch closed during WebSocket upgrade")
            self._buffer.extend(chunk)
            if len(self._buffer) > 65536:
                raise FeedDisconnected("CoinSwitch WebSocket headers were too large")
        end = self._buffer.index(marker) + len(marker)
        result = bytes(self._buffer[:end])
        del self._buffer[:end]
        return result

    def _recv(self, size: int) -> bytes:
        try:
            return self._sock.recv(size)
        except TimeoutError as exc:
            raise FeedTimeout("CoinSwitch WebSocket receive timed out") from exc
        except OSError as exc:
            raise FeedDisconnected(
                f"CoinSwitch WebSocket receive failed: {type(exc).__name__}"
            ) from exc

    def _exact(self, size: int) -> bytes:
        while len(self._buffer) < size:
            chunk = self._recv(max(4096, size - len(self._buffer)))
            if not chunk:
                raise FeedDisconnected("CoinSwitch closed the WebSocket")
            self._buffer.extend(chunk)
        result = bytes(self._buffer[:size])
        del self._buffer[:size]
        return result

    def send_text(self, text: str) -> None:
        self._send_frame(0x1, text.encode("utf-8"))

    def _send_frame(self, opcode: int, payload: bytes) -> None:
        mask = os.urandom(4)
        length = len(payload)
        if length < 126:
            header = bytes((0x80 | opcode, 0x80 | length))
        elif length <= 65535:
            header = bytes((0x80 | opcode, 0x80 | 126)) + length.to_bytes(2, "big")
        else:
            header = bytes((0x80 | opcode, 0x80 | 127)) + length.to_bytes(8, "big")
        masked = bytes(value ^ mask[index % 4] for index, value in enumerate(payload))
        try:
            with self._send_lock:
                self._sock.sendall(header + mask + masked)
        except OSError as exc:
            raise FeedDisconnected(
                f"CoinSwitch WebSocket send failed: {type(exc).__name__}"
            ) from exc

    def recv_text(self) -> str:
        fragments = bytearray()
        while True:
            first, second = self._exact(2)
            final = bool(first & 0x80)
            opcode = first & 0x0F
            masked = bool(second & 0x80)
            length = second & 0x7F
            if length == 126:
                length = int.from_bytes(self._exact(2), "big")
            elif length == 127:
                length = int.from_bytes(self._exact(8), "big")
            mask = self._exact(4) if masked else b""
            payload = self._exact(length)
            if masked:
                payload = bytes(
                    value ^ mask[index % 4] for index, value in enumerate(payload)
                )
            if opcode == 0x8:
                raise FeedDisconnected("CoinSwitch closed the WebSocket")
            if opcode == 0x9:
                self._send_frame(0xA, payload)
                continue
            if opcode == 0xA:
                continue
            if opcode not in (0x0, 0x1):
                raise FeedDisconnected(
                    f"CoinSwitch sent unsupported WebSocket opcode {opcode}"
                )
            fragments.extend(payload)
            if final:
                try:
                    return fragments.decode("utf-8")
                except UnicodeDecodeError as exc:
                    raise FeedDisconnected(
                        "CoinSwitch sent non-UTF-8 text data"
                    ) from exc

    def close(self) -> None:
        try:
            self._send_frame(0x8, b"")
        except FeedError:
            pass
        finally:
            try:
                self._sock.close()
            except OSError:
                pass


class CoinSwitchSpotStream:
    """Shared public stream exposed as separate quote and candle sources."""

    def __init__(
        self,
        *,
        symbol: str,
        exchange: str = "coinswitchx",
        timeout_seconds: float = 65.0,
        reconnect_initial_seconds: float = 1.0,
        reconnect_max_seconds: float = 30.0,
        connection_factory: Callable[[str, str, float], _TextConnection] | None = None,
        include_depth: bool = False,
    ) -> None:
        if not isinstance(symbol, str) or symbol.count("/") != 1:
            raise ValueError("symbol must use CoinSwitch REST form BASE/QUOTE")
        base, quote = (part.strip().upper() for part in symbol.split("/", 1))
        if not base or not quote or not base.isalnum() or not quote.isalnum():
            raise ValueError("symbol must contain alphanumeric BASE and QUOTE")
        normalized_exchange = exchange.strip().lower()
        if normalized_exchange not in _EXCHANGES:
            raise ValueError(
                f"exchange must be one of {sorted(_EXCHANGES)}, got {exchange!r}"
            )
        if (quote == "INR") != (normalized_exchange == "coinswitchx"):
            raise ValueError(
                "coinswitchx supports INR pairs; c2c1/c2c2 support USDT pairs"
            )
        timeout = float(timeout_seconds)
        if not timeout > 0:
            raise ValueError("timeout_seconds must be positive")
        reconnect_initial = float(reconnect_initial_seconds)
        reconnect_max = float(reconnect_max_seconds)
        if not reconnect_initial > 0:
            raise ValueError("reconnect_initial_seconds must be positive")
        if reconnect_max < reconnect_initial:
            raise ValueError(
                "reconnect_max_seconds must be at least reconnect_initial_seconds"
            )
        self.symbol = f"{base}/{quote}"
        self.base_asset = base
        self.quote_asset = quote
        self.exchange = normalized_exchange
        self._socket_pair = f"{base},{quote}"
        self._namespace = f"/{normalized_exchange}"
        self._timeout = timeout
        self._reconnect_initial = reconnect_initial
        self._reconnect_max = reconnect_max
        self._factory = connection_factory or _WebSocketConnection.connect
        if type(include_depth) is not bool:
            raise ValueError("include_depth must be boolean")
        self._include_depth = include_depth
        self._quotes: queue.SimpleQueue[RawMessage] = queue.SimpleQueue()
        self._candles: queue.SimpleQueue[RawMessage] = queue.SimpleQueue()
        self.quote_source: MarketDataSource = _Source(self, "quote")
        self.candle_source: MarketDataSource = _Source(self, "candle")
        self._lock = threading.RLock()
        self._stop = threading.Event()
        self._connection: _TextConnection | None = None
        self._thread: threading.Thread | None = None
        self._error: FeedError | None = None
        self._trade_bar: _TradeBar | None = None
        self._reconnect_attempts = 0
        self._successful_reconnects = 0

    @property
    def is_running(self) -> bool:
        with self._lock:
            thread = self._thread
        return thread is not None and thread.is_alive()

    @property
    def last_error(self) -> FeedError | None:
        with self._lock:
            return self._error

    @property
    def reconnect_attempts(self) -> int:
        with self._lock:
            return self._reconnect_attempts

    @property
    def successful_reconnects(self) -> int:
        with self._lock:
            return self._successful_reconnects

    def start(self) -> None:
        with self._lock:
            if self._thread is not None:
                raise RuntimeError("CoinSwitch Spot stream is already running")
            self._error = None
            self._trade_bar = None
            self._reconnect_attempts = 0
            self._successful_reconnects = 0
            self._stop.clear()
        path = _PATH_TEMPLATE.format(exchange=self.exchange)
        connection = self._factory(_HOST, path, self._timeout)
        try:
            self._open_socketio(connection)
        except BaseException:
            connection.close()
            raise
        thread = threading.Thread(
            target=self._connection_loop,
            args=(connection, path),
            name="coinswitch-spot-marketdata",
            daemon=True,
        )
        with self._lock:
            self._connection = connection
            self._thread = thread
        thread.start()

    def _open_socketio(self, connection: _TextConnection) -> None:
        opening = connection.recv_text()
        if not opening.startswith("0{"):
            connection.close()
            raise FeedDisconnected("CoinSwitch did not send an Engine.IO open packet")
        connection.send_text(f"40{self._namespace},")
        for _ in range(10):
            packet = connection.recv_text()
            if packet == "2":
                connection.send_text("3")
                continue
            if packet.startswith(f"40{self._namespace},"):
                break
        else:
            connection.close()
            raise FeedDisconnected("CoinSwitch did not acknowledge the namespace")
        payload = {"event": "subscribe", "pair": self._socket_pair}
        for event in (_BOOK_EVENT, _TRADE_EVENT):
            connection.send_text(
                f"42{self._namespace}," + json.dumps([event, payload], separators=(",", ":"))
            )

    def _connection_loop(self, connection: _TextConnection, path: str) -> None:
        delay = self._reconnect_initial
        try:
            while not self._stop.is_set():
                error = self._consume(connection)
                connection.close()
                with self._lock:
                    if self._connection is connection:
                        self._connection = None
                    self._trade_bar = None
                    if not self._stop.is_set():
                        self._error = error
                if self._stop.is_set():
                    break

                while not self._stop.wait(delay):
                    with self._lock:
                        self._reconnect_attempts += 1
                    replacement = None
                    try:
                        replacement = self._factory(_HOST, path, self._timeout)
                        self._open_socketio(replacement)
                    except FeedError as exc:
                        if replacement is not None:
                            replacement.close()
                        with self._lock:
                            self._error = exc
                        delay = min(delay * 2, self._reconnect_max)
                        continue
                    connection = replacement
                    delay = self._reconnect_initial
                    with self._lock:
                        self._connection = replacement
                        self._error = None
                        self._successful_reconnects += 1
                    break
                else:
                    break
        finally:
            connection.close()
            with self._lock:
                if self._connection is connection:
                    self._connection = None
                if self._thread is threading.current_thread():
                    self._thread = None

    def _consume(self, connection: _TextConnection) -> FeedError:
        try:
            while not self._stop.is_set():
                packet = connection.recv_text()
                if packet == "2":
                    connection.send_text("3")
                    continue
                prefix = f"42{self._namespace},"
                if not packet.startswith(prefix):
                    continue
                # Preserve JSON decimal tokens exactly. CoinSwitch's live trade
                # payload currently sends p/q as JSON numbers despite the docs
                # describing strings; binary float conversion is forbidden.
                decoded = json.loads(packet[len(prefix) :], parse_float=str)
                if not isinstance(decoded, list) or len(decoded) != 2:
                    raise FeedDisconnected("CoinSwitch sent a malformed Socket.IO event")
                event, payload = decoded
                if not isinstance(payload, Mapping):
                    raise FeedDisconnected("CoinSwitch event payload was not an object")
                if self._is_subscription_ack(payload):
                    continue
                if event == _BOOK_EVENT:
                    self._quotes.put(self._decode_book(payload))
                elif event == _CANDLE_EVENT:
                    self._candles.put(self._decode_candle(payload))
                elif event == _TRADE_EVENT:
                    closed = self._aggregate_trade(payload)
                    if closed is not None:
                        self._candles.put(closed)
        except FeedError as exc:
            return exc
        except (ValueError, TypeError, KeyError, IndexError, ArithmeticError) as exc:
            return FeedDisconnected(
                f"CoinSwitch stream message was unreadable: {type(exc).__name__}"
            )
        return FeedDisconnected("CoinSwitch stream stopped")

    @staticmethod
    def _is_subscription_ack(payload: Mapping[str, object]) -> bool:
        return (
            set(payload) == {"success", "message"}
            and str(payload["success"]).lower() == "true"
            and str(payload["message"]).startswith("Subscribed to ")
        )

    def _decode_book(self, payload: Mapping[str, object]) -> RawMessage:
        bids, asks = payload["bids"], payload["asks"]
        if not isinstance(bids, list) or not isinstance(asks, list) or not bids or not asks:
            raise ValueError("order book has no best bid/ask")
        bid, ask = bids[0], asks[0]
        if not isinstance(bid, list) or not isinstance(ask, list):
            raise TypeError("order book levels are not arrays")
        result = {
            "symbol": self._normal_symbol(payload["s"]),
            "bid": bid[0],
            "ask": ask[0],
            "timestamp": payload["timestamp"],
        }
        if self._include_depth:
            result.update(bids=bids, asks=asks, exchange=self.exchange)
        return result

    def _decode_candle(self, payload: Mapping[str, object]) -> RawMessage:
        interval_minutes = int(str(payload["interval"]))
        end_ms = int(str(payload["end_time"]))
        return {
            "symbol": self._normal_symbol(payload["symbol"]),
            "open_time": end_ms - interval_minutes * 60_000,
            "interval_seconds": interval_minutes * 60,
            "open": payload["o"],
            "high": payload["h"],
            "low": payload["l"],
            "close": payload["c"],
            "volume": payload["volume"],
        }

    def _aggregate_trade(self, payload: Mapping[str, object]) -> RawMessage | None:
        self._normal_symbol(payload["s"])
        event_ms = payload["E"]
        if isinstance(event_ms, bool) or not isinstance(event_ms, int):
            raise TypeError("trade event timestamp was not an integer")
        price = Decimal(str(payload["p"]))
        quantity = Decimal(str(payload["q"]))
        if not price.is_finite() or price <= 0:
            raise ValueError("trade price must be finite and positive")
        if not quantity.is_finite() or quantity <= 0:
            raise ValueError("trade quantity must be finite and positive")
        bucket = event_ms - event_ms % 60_000
        current = self._trade_bar
        if current is None:
            self._trade_bar = _TradeBar(
                bucket, price, price, price, price, quantity
            )
            return None
        if bucket < current.open_time:
            raise ValueError("trade timestamps moved backwards across a minute")
        if bucket == current.open_time:
            current.high = max(current.high, price)
            current.low = min(current.low, price)
            current.close = price
            current.volume += quantity
            return None
        closed: RawMessage = {
            "symbol": self.symbol,
            "open_time": current.open_time,
            "interval_seconds": 60,
            "open": str(current.open),
            "high": str(current.high),
            "low": str(current.low),
            "close": str(current.close),
            "volume": str(current.volume),
        }
        self._trade_bar = _TradeBar(
            bucket, price, price, price, price, quantity, True
        )
        # The first observed bucket after startup/reconnect may have begun
        # before subscription. It is not a full OHLCV minute and is never saved.
        return closed if current.coverage_complete else None

    def _normal_symbol(self, value: object) -> str:
        symbol = str(value).replace(",", "/").upper()
        if symbol != self.symbol:
            raise ValueError(
                f"received {symbol!r} while subscribed to {self.symbol!r}"
            )
        return symbol

    def _poll(self, kind: str) -> list[RawMessage]:
        target = self._quotes if kind == "quote" else self._candles
        messages: list[RawMessage] = []
        while True:
            try:
                messages.append(target.get_nowait())
            except queue.Empty:
                break
        if messages:
            return messages
        with self._lock:
            error = self._error
            connection = self._connection
        if error is not None:
            raise error
        if connection is None:
            raise FeedUnavailable("CoinSwitch Spot stream has not been started")
        return []

    def stop(self) -> None:
        self._stop.set()
        with self._lock:
            connection = self._connection
            thread = self._thread
        if connection is not None:
            connection.close()
        if thread is not None:
            thread.join(5.0)
        with self._lock:
            self._connection = None
            if thread is None or not thread.is_alive():
                self._thread = None
