"""Atomic evidence artifacts for finite public market-data observations."""

from __future__ import annotations

import json
import os
import tempfile
from collections.abc import Mapping, Sequence
from datetime import datetime
from pathlib import Path

__all__ = [
    "write_json_atomically",
    "write_observation_dataset",
    "write_observation_evidence",
]


def write_observation_dataset(
    path: str | os.PathLike[str],
    *,
    symbol: str,
    exchange: str,
    candles: Sequence[Mapping[str, object]],
    started_at: datetime,
    finished_at: datetime,
) -> Path:
    """Atomically preserve exact public candles for offline research only."""
    _validate_times(started_at, finished_at)
    if not isinstance(symbol, str) or not symbol.strip():
        raise ValueError("symbol must be a non-empty string")
    if not isinstance(exchange, str) or not exchange.strip():
        raise ValueError("exchange must be a non-empty string")
    rows = []
    for candle in candles:
        if not isinstance(candle, Mapping):
            raise TypeError("every candle must be a mapping")
        rows.append(dict(candle))
    return write_json_atomically(
        path,
        {
            "schema": "coinswitch-public-candles/v1",
            "started_at": started_at.isoformat(),
            "finished_at": finished_at.isoformat(),
            "symbol": symbol,
            "exchange": exchange,
            "public_data_only": True,
            "credentials_used": False,
            "live_trading": False,
            "training_only": True,
            "candles": rows,
        },
    )


def write_observation_evidence(
    path: str | os.PathLike[str],
    *,
    report: Mapping[str, object],
    started_at: datetime,
    finished_at: datetime,
) -> Path:
    """Atomically persist a credential-free CoinSwitch observation result."""
    if not isinstance(report, Mapping):
        raise TypeError("report must be a mapping")
    _validate_times(started_at, finished_at)
    return write_json_atomically(
        path,
        {
            "schema": "coinswitch-public-observation/v1",
            "started_at": started_at.isoformat(),
            "finished_at": finished_at.isoformat(),
            "elapsed_seconds": (finished_at - started_at).total_seconds(),
            "public_data_only": True,
            "credentials_used": False,
            "live_trading": False,
            "report": dict(report),
        },
    )


def _validate_times(started_at: datetime, finished_at: datetime) -> None:
    for name, value in (("started_at", started_at), ("finished_at", finished_at)):
        if not isinstance(value, datetime) or value.tzinfo is None:
            raise TypeError(f"{name} must be a timezone-aware datetime")
    if finished_at < started_at:
        raise ValueError("finished_at must not precede started_at")


def write_json_atomically(
    path: str | os.PathLike[str], payload: Mapping[str, object]
) -> Path:
    """Write one JSON object by same-directory replacement."""
    if not isinstance(payload, Mapping):
        raise TypeError("payload must be a mapping")
    destination = Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary_name: str | None = None
    try:
        with tempfile.NamedTemporaryFile(
            "w",
            encoding="utf-8",
            dir=destination.parent,
            prefix=f".{destination.name}.",
            suffix=".tmp",
            delete=False,
        ) as handle:
            temporary_name = handle.name
            json.dump(payload, handle, sort_keys=True, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_name, destination)
        temporary_name = None
    finally:
        if temporary_name is not None:
            try:
                os.unlink(temporary_name)
            except FileNotFoundError:
                pass
    return destination
