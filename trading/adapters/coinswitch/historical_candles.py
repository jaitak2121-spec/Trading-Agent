"""GET-only CoinSwitch market-data transports; never orders or balances."""

from __future__ import annotations

import http.client
import random
import time
import urllib.parse
from collections.abc import Callable, Mapping
from dataclasses import dataclass

from ...core.secrets import Secret

_CANDLES_PATH = "/trade/api/v2/candles"


class CoinSwitchCandleTransportError(RuntimeError):
    """The read-only historical candle request could not be completed."""

    def __init__(self, message: str, *, statuses: tuple[int | str, ...] = ()) -> None:
        super().__init__(message)
        self.statuses = statuses


@dataclass(frozen=True, slots=True)
class CandleFetchResult:
    """Response plus credential-free transport evidence."""

    body: bytes
    statuses: tuple[int | str, ...]

    @property
    def attempts(self) -> int:
        return len(self.statuses)


class CoinSwitchCandleTransport:
    """Authenticated transport with no method except historical candle reads."""

    def __init__(
        self,
        *,
        api_key: Secret,
        api_secret: Secret,
        timeout_seconds: float = 20.0,
        max_attempts: int = 4,
        clock_ms: Callable[[], int] | None = None,
        wait: Callable[[float], object] | None = None,
        opener: Callable[[str, Mapping[str, str], float], tuple[int, bytes]] | None = None,
        signer: Callable[[Secret, bytes], str] | None = None,
    ) -> None:
        if not isinstance(api_key, Secret) or not isinstance(api_secret, Secret):
            raise TypeError("CoinSwitch credentials must be Secret values")
        if max_attempts < 1:
            raise ValueError("max_attempts must be positive")
        self._api_key = api_key
        self._api_secret = api_secret
        self._timeout = float(timeout_seconds)
        self._max_attempts = max_attempts
        self._clock_ms = clock_ms or (lambda: time.time_ns() // 1_000_000)
        self._wait = wait or time.sleep
        self._opener = opener or _https_get
        self._signer = signer or _ed25519_signature

    def fetch(self, parameters: Mapping[str, str]) -> bytes:
        return self.fetch_with_details(parameters).body

    def fetch_with_details(self, parameters: Mapping[str, str]) -> CandleFetchResult:
        query = urllib.parse.urlencode(parameters)
        path = urllib.parse.unquote_plus(f"{_CANDLES_PATH}?{query}")
        last_status: int | None = None
        statuses: list[int | str] = []
        for attempt in range(self._max_attempts):
            epoch = str(self._clock_ms())
            try:
                status, body = self._opener(
                    path, self._headers(path, epoch), self._timeout
                )
                statuses.append(status)
                if 200 <= status < 300:
                    return CandleFetchResult(body, tuple(statuses))
                last_status = status
                if status != 429 and not 500 <= status < 600:
                    raise CoinSwitchCandleTransportError(
                        f"CoinSwitch candles request failed with HTTP {status}",
                        statuses=tuple(statuses),
                    )
            except (ConnectionError, OSError, TimeoutError) as exc:
                statuses.append("network_error")
                last_status = None
                if attempt + 1 == self._max_attempts:
                    raise CoinSwitchCandleTransportError(
                        "CoinSwitch candles request failed at the network boundary",
                        statuses=tuple(statuses),
                    ) from exc
            if attempt + 1 < self._max_attempts:
                self._wait((2**attempt) * random.uniform(0.75, 1.25))
        raise CoinSwitchCandleTransportError(
            "CoinSwitch candles request exhausted retries"
            + (f" after HTTP {last_status}" if last_status is not None else ""),
            statuses=tuple(statuses),
        )

    def _headers(self, path: str, epoch: str) -> dict[str, str]:
        return signed_market_get_headers(self._api_key,self._api_secret,path,epoch,signer=self._signer)


class CoinSwitchTradeInfoReader:
    """Explicit single signed metadata GET, no discovery/retry/redirect/order API."""
    def __init__(self,*,api_key,api_secret,opener=None,signer=None,clock_ms=None):
        if not isinstance(api_key,Secret) or not isinstance(api_secret,Secret):raise TypeError('credentials must be Secret values')
        self._key,self._secret=api_key,api_secret
        self._opener=opener or _https_get;self._signer=signer
        self._clock=clock_ms or (lambda:time.time_ns()//1000000)

    def fetch(self,*,exchange,symbol):
        if exchange not in ('c2c1','c2c2','coinswitchx') or not isinstance(symbol,str) or len(symbol.split('/'))!=2 or not all(p.isalnum() for p in symbol.split('/')):
            raise ValueError('explicit supported exchange and BASE/QUOTE symbol required')
        path='/trade/api/v2/tradeInfo?exchange='+exchange+'&symbol='+symbol.upper()
        headers=signed_market_get_headers(self._key,self._secret,path,str(self._clock()),signer=self._signer)
        try:status,body=self._opener(path,headers,20.0)
        except Exception:raise CoinSwitchCandleTransportError('metadata GET failed at HTTP boundary',statuses=('request_error',)) from None
        if type(status) is not int or not 200<=status<300:
            raise CoinSwitchCandleTransportError('metadata GET refused; no fallback requested',statuses=(status,))
        return CandleFetchResult(body,(status,))


def _https_get(
    path: str, headers: Mapping[str, str], timeout: float
) -> tuple[int, bytes]:
    connection = http.client.HTTPSConnection("coinswitch.co", timeout=timeout)
    try:
        connection.request("GET", path, headers=dict(headers))
        response = connection.getresponse()
        return response.status, response.read()
    finally:
        connection.close()


def _ed25519_signature(secret: Secret, message: bytes) -> str:
    try:
        from cryptography.hazmat.primitives.asymmetric import ed25519
    except ImportError as exc:  # pragma: no cover - depends on optional extra
        raise CoinSwitchCandleTransportError(
            "historical download requires: pip install -e '.[historical-data]'"
        ) from exc
    try:
        seed = bytes.fromhex(secret.reveal())
        private_key = ed25519.Ed25519PrivateKey.from_private_bytes(seed)
    except (TypeError, ValueError) as exc:
        raise CoinSwitchCandleTransportError(
            "COINSWITCH_API_SECRET is not a valid 32-byte hex Ed25519 seed"
        ) from exc
    return private_key.sign(message).hex()


def signed_market_get_headers(api_key,api_secret,path,epoch,*,signer=None):
    """Shared GET-only signing for explicit candle/fee readers, never orders."""
    if not isinstance(api_key,Secret) or not isinstance(api_secret,Secret):
        raise TypeError('credentials must be Secret values')
    if path.split('?',1)[0] not in (_CANDLES_PATH,'/trade/api/v2/tradingFee','/trade/api/v2/tradeInfo'):
        raise ValueError('authenticated market GET endpoint is not allowlisted')
    if not isinstance(epoch,str) or not epoch.isdigit():raise ValueError('invalid GET epoch')
    signature=(signer or _ed25519_signature)(api_secret,f'GET{path}{epoch}'.encode())
    return {'Content-Type':'application/json','X-AUTH-APIKEY':api_key.reveal(),
        'X-AUTH-SIGNATURE':signature,'X-AUTH-EPOCH':epoch}
