"""Read-only CoinSwitch Spot candle backfill and dataset integrity checks."""

from __future__ import annotations

import hashlib
import json
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation

from ..adapters.coinswitch.historical_candles import (
    CandleFetchResult,
    CoinSwitchCandleTransportError,
)

_CANDLES_PATH = "/trade/api/v2/candles"


class HistoricalCandleError(RuntimeError):
    """CoinSwitch refused or returned unusable historical candle data."""


def recover_historical_candles(
    dataset: Mapping[str, object],
    *,
    fetch: Callable[[Mapping[str, str]], bytes | CandleFetchResult],
    input_file_sha256: str,
    chunk_candles: int = 1440,
    confirmations: int = 2,
    checkpoint: Callable[[Mapping[str, object]], object] | None = None,
) -> tuple[dict[str, object], CandleDatasetValidation]:
    """Refetch only missing ranges and checkpoint a separate recovered artifact."""
    if len(input_file_sha256) != 64:
        raise ValueError("input_file_sha256 must be a SHA-256 hex digest")
    if chunk_candles < 1 or confirmations < 1:
        raise ValueError("chunk_candles and confirmations must be positive")
    symbol = _text(dataset.get("symbol"), "symbol").upper()
    exchange = _text(dataset.get("exchange"), "exchange").lower()
    source = dataset.get("source")
    rows = dataset.get("candles")
    if dataset.get("schema") != "coinswitch-public-candles/v1" or not isinstance(source, Mapping):
        raise ValueError("unsupported recovery source dataset")
    if not isinstance(rows, list):
        raise ValueError("recovery source candles must be a list")
    interval_minutes = _integer(source.get("interval_minutes"), "source interval_minutes")
    interval_ms = interval_minutes * 60_000
    start = _integer(source.get("requested_start_time"), "source requested_start_time")
    end = _integer(source.get("requested_end_time"), "source requested_end_time")
    normalized = [_stored_candle(row, symbol=symbol) for row in rows]
    candles, duplicates, reordered = _deduplicate(normalized)
    if duplicates or reordered:
        raise ValueError("recovery source must already be unique and chronological")

    prior = source.get("recovery")
    if isinstance(prior, Mapping):
        original_file_hash = _text(prior.get("original_file_sha256"), "original_file_sha256")
        original_candle_hash = _text(prior.get("original_candle_sha256"), "original_candle_sha256")
        original_missing = _ranges(prior.get("original_missing_ranges"))
        ledger = [dict(item) for item in prior.get("requests", []) if isinstance(item, Mapping)]
    else:
        original_file_hash = input_file_sha256
        original_candle_hash = _validate_rows(candles, duplicate_count=0, reordered=False).candle_sha256
        original_missing = _missing_ranges(candles, start, end, interval_ms)
        ledger = []

    def snapshot() -> tuple[dict[str, object], CandleDatasetValidation]:
        output, report = _recovery_dataset(
            dataset, candles=candles, ledger=ledger,
            original_file_hash=original_file_hash,
            original_candle_hash=original_candle_hash,
            original_missing=original_missing,
            input_file_hash=input_file_sha256,
            confirmations=confirmations,
        )
        if checkpoint is not None:
            checkpoint(output)
        return output, report

    snapshot()  # Makes an interrupted first request resumable too.
    windows = _split_ranges(original_missing, interval_ms, chunk_candles)
    for range_start, range_end in windows:
        if not _has_missing(candles, range_start, range_end, interval_ms):
            continue
        successes = sum(
            item.get("requested_start_time") == range_start
            and item.get("requested_end_time") == range_end
            and item.get("outcome") == "success"
            for item in ledger
        )
        while successes < confirmations and _has_missing(
            candles, range_start, range_end, interval_ms
        ):
            parameters = {
                "exchange": exchange,
                "symbol": symbol,
                "interval": str(interval_minutes),
                "start_time": str(range_start),
                "end_time": str(range_end),
            }
            try:
                fetched = fetch(parameters)
            except CoinSwitchCandleTransportError as exc:
                ledger.append({
                    "sequence": len(ledger) + 1,
                    "requested_start_time": range_start,
                    "requested_end_time": range_end,
                    "http_statuses": list(exc.statuses),
                    "attempts": len(exc.statuses),
                    "retry_count": max(0, len(exc.statuses) - 1),
                    "response_count": 0,
                    "response_sha256": None,
                    "outcome": "transport_error",
                })
                snapshot()
                raise
            if isinstance(fetched, bytes):
                result = CandleFetchResult(fetched, (200,))
            elif isinstance(fetched, CandleFetchResult):
                result = fetched
            else:
                raise TypeError("recovery fetch must return bytes or CandleFetchResult")
            returned = _response_candles(
                result.body,
                symbol=symbol,
                interval_minutes=interval_minutes,
                range_start=range_start,
                range_end=range_end,
                acquired_at=time.time_ns() // 1_000_000,
            )
            before = len(candles)
            candles, ignored, _ = _deduplicate(candles + returned)
            ledger.append({
                "sequence": len(ledger) + 1,
                "requested_start_time": range_start,
                "requested_end_time": range_end,
                "http_statuses": list(result.statuses),
                "attempts": result.attempts,
                "retry_count": max(0, result.attempts - 1),
                "response_count": len(returned),
                "new_candle_count": len(candles) - before,
                "duplicates_ignored": ignored,
                "response_sha256": hashlib.sha256(result.body).hexdigest(),
                "outcome": "success",
            })
            successes += 1
            snapshot()
    return snapshot()


@dataclass(frozen=True, slots=True)
class CandleDatasetValidation:
    candle_count: int
    duplicate_count: int
    reordered: bool
    gap_count: int
    first_open_time: int | None
    last_open_time: int | None
    candle_sha256: str
    failures: tuple[str, ...] = ()

    @property
    def passed(self) -> bool:
        return not self.failures

    def as_details(self) -> dict[str, object]:
        return {
            "passed": self.passed,
            "candle_count": self.candle_count,
            "duplicates_removed": self.duplicate_count,
            "reordered": self.reordered,
            "gap_count": self.gap_count,
            "first_open_time": self.first_open_time,
            "last_open_time": self.last_open_time,
            "candle_sha256": self.candle_sha256,
            "failures": list(self.failures),
        }

    def render(self) -> str:
        verdict = "PASS" if self.passed else "FAIL"
        lines = [
            f"candle dataset validation: {verdict}",
            f"candles           : {self.candle_count}",
            f"duplicates removed: {self.duplicate_count}",
            f"reordered         : {str(self.reordered).lower()}",
            f"gaps              : {self.gap_count}",
            f"candle sha256     : {self.candle_sha256}",
        ]
        lines.extend(f"failure           : {failure}" for failure in self.failures)
        return "\n".join(lines)


def download_historical_candles(
    *,
    fetch: Callable[[Mapping[str, str]], bytes],
    exchange: str,
    symbol: str,
    interval_minutes: int,
    start_time_ms: int,
    end_time_ms: int,
    chunk_candles: int = 1440,
    acquired_at_ms: int | None = None,
) -> tuple[dict[str, object], CandleDatasetValidation]:
    """Download, normalize and integrity-check a closed historical range."""
    exchange = _text(exchange, "exchange").lower()
    symbol = _text(symbol, "symbol").upper()
    if exchange not in {"coinswitchx", "c2c1", "c2c2"}:
        raise ValueError("unsupported CoinSwitch Spot exchange")
    if symbol.count("/") != 1:
        raise ValueError("symbol must use BASE/QUOTE form")
    for name, value in (
        ("interval_minutes", interval_minutes),
        ("start_time_ms", start_time_ms),
        ("end_time_ms", end_time_ms),
        ("chunk_candles", chunk_candles),
    ):
        if isinstance(value, bool) or not isinstance(value, int):
            raise TypeError(f"{name} must be an int")
    if interval_minutes <= 0 or chunk_candles <= 0:
        raise ValueError("interval_minutes and chunk_candles must be positive")
    interval_ms = interval_minutes * 60_000
    if start_time_ms < 0 or end_time_ms <= start_time_ms:
        raise ValueError("historical time range is invalid")
    if start_time_ms % interval_ms or end_time_ms % interval_ms:
        raise ValueError("historical time range must align to the candle interval")
    acquired = acquired_at_ms if acquired_at_ms is not None else time.time_ns() // 1_000_000
    if end_time_ms > acquired:
        raise ValueError("historical end time must not be in the future")

    raw_rows: list[dict[str, object]] = []
    response_hashes: list[str] = []
    cursor = start_time_ms
    while cursor < end_time_ms:
        chunk_end = min(end_time_ms, cursor + chunk_candles * interval_ms)
        parameters = {
            "exchange": exchange,
            "symbol": symbol,
            "interval": str(interval_minutes),
            "start_time": str(cursor),
            "end_time": str(chunk_end),
        }
        body = fetch(parameters)
        if not isinstance(body, bytes):
            raise TypeError("historical candle transport must return bytes")
        response_hashes.append(hashlib.sha256(body).hexdigest())
        raw_rows.extend(
            _response_candles(
                body,
                symbol=symbol,
                interval_minutes=interval_minutes,
                range_start=cursor,
                range_end=chunk_end,
                acquired_at=acquired,
            )
        )
        cursor = chunk_end

    candles, duplicates, reordered = _deduplicate(raw_rows)
    validation = _validate_rows(
        candles, duplicate_count=duplicates, reordered=reordered
    )
    boundary_failures = []
    if candles and int(candles[0]["open_time"]) != start_time_ms:
        boundary_failures.append("dataset does not begin at the requested start time")
    if candles and int(candles[-1]["close_time"]) != end_time_ms:
        boundary_failures.append("dataset does not end at the requested end time")
    expected_count = (end_time_ms - start_time_ms) // interval_ms
    if len(candles) != expected_count:
        boundary_failures.append(
            f"dataset has {len(candles)} candles; requested range requires {expected_count}"
        )
    if boundary_failures:
        validation = CandleDatasetValidation(
            validation.candle_count,
            validation.duplicate_count,
            validation.reordered,
            validation.gap_count,
            validation.first_open_time,
            validation.last_open_time,
            validation.candle_sha256,
            validation.failures + tuple(boundary_failures),
        )
    dataset: dict[str, object] = {
        "schema": "coinswitch-public-candles/v1",
        "symbol": symbol,
        "exchange": exchange,
        "public_data_only": True,
        "credentials_used": True,
        "live_trading": False,
        "training_only": True,
        "source": {
            "provider": "CoinSwitch",
            "endpoint": _CANDLES_PATH,
            "access": "authenticated-read-only",
            "interval_minutes": interval_minutes,
            "requested_start_time": start_time_ms,
            "requested_end_time": end_time_ms,
            "acquired_at": acquired,
            "response_sha256": response_hashes,
        },
        "candles": candles,
        "integrity": validation.as_details(),
    }
    return dataset, validation


def validate_candle_dataset(
    dataset: Mapping[str, object], *, minimum_candles: int = 1
) -> CandleDatasetValidation:
    """Validate a stored CoinSwitch dataset without changing or training it."""
    if not isinstance(dataset, Mapping):
        raise TypeError("dataset must be a mapping")
    if dataset.get("schema") != "coinswitch-public-candles/v1":
        raise ValueError("unsupported candle dataset schema")
    if not (
        dataset.get("public_data_only") is True
        and isinstance(dataset.get("credentials_used"), bool)
        and dataset.get("live_trading") is False
        and dataset.get("training_only") is True
    ):
        raise ValueError("dataset safety markers are missing or contradictory")
    rows = dataset.get("candles")
    if not isinstance(rows, list):
        raise ValueError("dataset candles must be a list")
    symbol = _text(dataset.get("symbol"), "symbol").upper()
    normalized = [_stored_candle(row, symbol=symbol) for row in rows]
    candles, duplicates, reordered = _deduplicate(normalized)
    validation = _validate_rows(
        candles,
        duplicate_count=duplicates,
        reordered=reordered,
        minimum_candles=minimum_candles,
    )
    storage_failures = []
    if reordered:
        storage_failures.append("stored candles are not in chronological order")
    if duplicates:
        storage_failures.append("stored dataset contains duplicate candles")
    if storage_failures:
        validation = CandleDatasetValidation(
            validation.candle_count,
            validation.duplicate_count,
            validation.reordered,
            validation.gap_count,
            validation.first_open_time,
            validation.last_open_time,
            validation.candle_sha256,
            validation.failures + tuple(storage_failures),
        )
    stored = dataset.get("integrity")
    if isinstance(stored, Mapping):
        if stored.get("passed") is not True:
            validation = CandleDatasetValidation(
                validation.candle_count,
                validation.duplicate_count,
                validation.reordered,
                validation.gap_count,
                validation.first_open_time,
                validation.last_open_time,
                validation.candle_sha256,
                validation.failures + ("stored integrity report did not pass",),
            )
        expected = stored.get("candle_sha256")
        if expected is not None and expected != validation.candle_sha256:
            validation = CandleDatasetValidation(
                validation.candle_count,
                validation.duplicate_count,
                validation.reordered,
                validation.gap_count,
                validation.first_open_time,
                validation.last_open_time,
                validation.candle_sha256,
                validation.failures + ("stored candle hash does not match content",),
            )
    return validation


def _response_candles(
    body: bytes,
    *,
    symbol: str,
    interval_minutes: int,
    range_start: int,
    range_end: int,
    acquired_at: int,
) -> list[dict[str, object]]:
    try:
        payload = json.loads(body, parse_float=str)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise HistoricalCandleError("CoinSwitch candles response was not valid JSON") from exc
    if not isinstance(payload, Mapping) or not isinstance(payload.get("data"), list):
        raise HistoricalCandleError("CoinSwitch candles response has no data array")
    output = []
    for value in payload["data"]:
        if not isinstance(value, Mapping):
            raise HistoricalCandleError("CoinSwitch candle entry was not an object")
        row_symbol = _text(value.get("symbol"), "candle symbol").upper()
        interval = _integer(value.get("interval"), "candle interval")
        start = _integer(value.get("start_time"), "candle start_time")
        close_time = _integer(value.get("close_time"), "candle close_time")
        if row_symbol != symbol or interval != interval_minutes:
            raise HistoricalCandleError("CoinSwitch candle identity does not match request")
        if close_time - start != interval_minutes * 60_000:
            raise HistoricalCandleError("CoinSwitch candle has an inconsistent close time")
        if start < range_start or start >= range_end:
            raise HistoricalCandleError("CoinSwitch candle fell outside its requested chunk")
        if close_time > acquired_at:
            raise HistoricalCandleError("CoinSwitch returned an incomplete candle")
        output.append(_prices(value, start, close_time, interval_minutes, symbol))
    return output


def _stored_candle(value: object, *, symbol: str) -> dict[str, object]:
    if not isinstance(value, Mapping):
        raise ValueError("stored candle was not an object")
    start = _integer(value.get("open_time"), "candle open_time")
    interval_seconds = _integer(value.get("interval_seconds"), "candle interval_seconds")
    if interval_seconds <= 0 or interval_seconds % 60:
        raise ValueError("stored candle interval must be whole positive minutes")
    row_symbol = value.get("symbol")
    if row_symbol is not None and _text(row_symbol, "candle symbol").upper() != symbol:
        raise ValueError("stored candle symbol does not match dataset")
    close_time = value.get("close_time", start + interval_seconds * 1000)
    parsed_close_time = _integer(close_time, "candle close_time")
    if parsed_close_time != start + interval_seconds * 1000:
        raise ValueError("stored candle close time is inconsistent")
    try:
        return _prices(
            {
                "o": value.get("open"),
                "h": value.get("high"),
                "l": value.get("low"),
                "c": value.get("close"),
                "volume": value.get("volume"),
            },
            start,
            parsed_close_time,
            interval_seconds // 60,
            symbol,
        )
    except HistoricalCandleError as exc:
        raise ValueError(str(exc)) from exc


def _prices(
    value: Mapping[str, object],
    start: int,
    close_time: int,
    interval_minutes: int,
    symbol: str,
) -> dict[str, object]:
    try:
        open_, high, low, close = (
            Decimal(str(value[name])) for name in ("o", "h", "l", "c")
        )
        volume = Decimal(str(value["volume"]))
    except (KeyError, InvalidOperation) as exc:
        raise HistoricalCandleError("candle has invalid OHLCV values") from exc
    if any(not item.is_finite() or item <= 0 for item in (open_, high, low, close)):
        raise HistoricalCandleError("candle OHLC values must be finite and positive")
    if not volume.is_finite() or volume < 0:
        raise HistoricalCandleError("candle volume must be finite and non-negative")
    if low > min(open_, close) or high < max(open_, close) or low > high:
        raise HistoricalCandleError("candle OHLC values are inconsistent")
    return {
        "symbol": symbol,
        "open_time": start,
        "close_time": close_time,
        "interval_seconds": interval_minutes * 60,
        "open": str(open_),
        "high": str(high),
        "low": str(low),
        "close": str(close),
        "volume": str(volume),
        "source": "coinswitch-rest-candles",
    }


def _deduplicate(
    rows: list[dict[str, object]],
) -> tuple[list[dict[str, object]], int, bool]:
    times = [int(row["open_time"]) for row in rows]
    reordered = times != sorted(times)
    unique: dict[int, dict[str, object]] = {}
    duplicates = 0
    for row in rows:
        open_time = int(row["open_time"])
        previous = unique.get(open_time)
        if previous is None:
            unique[open_time] = row
        elif previous == row:
            duplicates += 1
        else:
            raise HistoricalCandleError(
                f"conflicting duplicate candle at open_time {open_time}"
            )
    return [unique[key] for key in sorted(unique)], duplicates, reordered


def _validate_rows(
    rows: list[dict[str, object]],
    *,
    duplicate_count: int,
    reordered: bool,
    minimum_candles: int = 1,
) -> CandleDatasetValidation:
    gaps = 0
    for previous, current in zip(rows, rows[1:]):
        if current["interval_seconds"] != previous["interval_seconds"]:
            gaps += 1
            continue
        expected = int(previous["open_time"]) + int(previous["interval_seconds"]) * 1000
        if int(current["open_time"]) != expected:
            gaps += 1
    failures = []
    if len(rows) < minimum_candles:
        failures.append(f"dataset contains fewer than {minimum_candles} candles")
    if gaps:
        failures.append(f"dataset contains {gaps} candle gap(s)")
    encoded = json.dumps(rows, sort_keys=True, separators=(",", ":")).encode()
    return CandleDatasetValidation(
        candle_count=len(rows),
        duplicate_count=duplicate_count,
        reordered=reordered,
        gap_count=gaps,
        first_open_time=int(rows[0]["open_time"]) if rows else None,
        last_open_time=int(rows[-1]["open_time"]) if rows else None,
        candle_sha256=hashlib.sha256(encoded).hexdigest(),
        failures=tuple(failures),
    )


def _ranges(value: object) -> list[tuple[int, int]]:
    if not isinstance(value, list):
        raise ValueError("original_missing_ranges must be a list")
    output = []
    for item in value:
        if not isinstance(item, Mapping):
            raise ValueError("missing range must be an object")
        start = _integer(item.get("start_time"), "missing range start_time")
        end = _integer(item.get("end_time"), "missing range end_time")
        if end <= start:
            raise ValueError("missing range end must follow start")
        output.append((start, end))
    return output


def _missing_ranges(
    candles: list[dict[str, object]], start: int, end: int, interval_ms: int
) -> list[tuple[int, int]]:
    present = {int(row["open_time"]) for row in candles}
    missing = [stamp for stamp in range(start, end, interval_ms) if stamp not in present]
    return _compact_timestamps(missing, interval_ms)


def _compact_timestamps(stamps: list[int], interval_ms: int) -> list[tuple[int, int]]:
    if not stamps:
        return []
    ranges = []
    start = previous = stamps[0]
    for stamp in stamps[1:]:
        if stamp != previous + interval_ms:
            ranges.append((start, previous + interval_ms))
            start = stamp
        previous = stamp
    ranges.append((start, previous + interval_ms))
    return ranges


def _split_ranges(
    ranges: list[tuple[int, int]], interval_ms: int, chunk_candles: int
) -> list[tuple[int, int]]:
    output = []
    width = interval_ms * chunk_candles
    for start, end in ranges:
        cursor = start
        while cursor < end:
            chunk_end = min(end, cursor + width)
            output.append((cursor, chunk_end))
            cursor = chunk_end
    return output


def _has_missing(
    candles: list[dict[str, object]], start: int, end: int, interval_ms: int
) -> bool:
    present = {int(row["open_time"]) for row in candles if start <= int(row["open_time"]) < end}
    return any(stamp not in present for stamp in range(start, end, interval_ms))


def _range_details(
    ranges: list[tuple[int, int]], interval_ms: int
) -> list[dict[str, int]]:
    return [
        {
            "start_time": start,
            "end_time": end,
            "timestamp_count": (end - start) // interval_ms,
        }
        for start, end in ranges
    ]


def _recovery_dataset(
    dataset: Mapping[str, object],
    *,
    candles: list[dict[str, object]],
    ledger: list[dict[str, object]],
    original_file_hash: str,
    original_candle_hash: str,
    original_missing: list[tuple[int, int]],
    input_file_hash: str,
    confirmations: int,
) -> tuple[dict[str, object], CandleDatasetValidation]:
    source = dataset["source"]
    assert isinstance(source, Mapping)
    interval_ms = _integer(source.get("interval_minutes"), "source interval_minutes") * 60_000
    start = _integer(source.get("requested_start_time"), "source requested_start_time")
    end = _integer(source.get("requested_end_time"), "source requested_end_time")
    present = {int(row["open_time"]) for row in candles}
    original_stamps = [
        stamp
        for range_start, range_end in original_missing
        for stamp in range(range_start, range_end, interval_ms)
    ]
    recovered = [stamp for stamp in original_stamps if stamp in present]
    unavailable = []
    unresolved = []
    for stamp in original_stamps:
        if stamp in present:
            continue
        successful_observations = sum(
            item.get("outcome") == "success"
            and int(item["requested_start_time"]) <= stamp < int(item["requested_end_time"])
            for item in ledger
        )
        (unavailable if successful_observations >= confirmations else unresolved).append(stamp)

    validation = _validate_rows(candles, duplicate_count=0, reordered=False)
    expected = (end - start) // interval_ms
    failures = list(validation.failures)
    if not candles or int(candles[0]["open_time"]) != start:
        failures.append("dataset does not begin at the requested start time")
    if not candles or int(candles[-1]["close_time"]) != end:
        failures.append("dataset does not end at the requested end time")
    if len(candles) != expected:
        failures.append(f"dataset has {len(candles)} candles; requested range requires {expected}")
    validation = CandleDatasetValidation(
        validation.candle_count, 0, False, validation.gap_count,
        validation.first_open_time, validation.last_open_time,
        validation.candle_sha256, tuple(dict.fromkeys(failures)),
    )
    classification = {
        "recovered": _range_details(_compact_timestamps(recovered, interval_ms), interval_ms),
        "consistently_unavailable": _range_details(
            _compact_timestamps(unavailable, interval_ms), interval_ms
        ),
        "unresolved": _range_details(_compact_timestamps(unresolved, interval_ms), interval_ms),
        "counts": {
            "recovered": len(recovered),
            "consistently_unavailable": len(unavailable),
            "unresolved": len(unresolved),
        },
    }
    recovery = {
        "original_file_sha256": original_file_hash,
        "original_candle_sha256": original_candle_hash,
        "resume_input_file_sha256": input_file_hash,
        "original_missing_ranges": _range_details(original_missing, interval_ms),
        "timestamp_classification": classification,
        "requests": ledger,
        "request_ledger_sha256": hashlib.sha256(
            json.dumps(ledger, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest(),
    }
    source_copy = dict(source)
    source_copy["recovery"] = recovery
    output = dict(dataset)
    output["source"] = source_copy
    output["candles"] = candles
    output["integrity"] = validation.as_details() | {
        "expected_candle_count": expected,
        "missing_timestamp_count": expected - len(candles),
        "contiguous_segment_count": validation.gap_count + (1 if candles else 0),
    }
    return output, validation


def _text(value: object, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be a non-empty string")
    return value.strip()


def _integer(value: object, name: str) -> int:
    if isinstance(value, bool):
        raise ValueError(f"{name} must be an integer")
    try:
        converted = int(str(value))
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{name} must be an integer") from exc
    return converted
