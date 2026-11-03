"""Atomic evidence artifacts for finite public market-data observations."""

from __future__ import annotations

import json
import os
import tempfile
from collections.abc import Mapping
from datetime import datetime
from pathlib import Path

__all__ = ["write_observation_evidence"]


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
    for name, value in (("started_at", started_at), ("finished_at", finished_at)):
        if not isinstance(value, datetime) or value.tzinfo is None:
            raise TypeError(f"{name} must be a timezone-aware datetime")
    if finished_at < started_at:
        raise ValueError("finished_at must not precede started_at")

    destination = Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    evidence = {
        "schema": "coinswitch-public-observation/v1",
        "started_at": started_at.isoformat(),
        "finished_at": finished_at.isoformat(),
        "elapsed_seconds": (finished_at - started_at).total_seconds(),
        "public_data_only": True,
        "credentials_used": False,
        "live_trading": False,
        "report": dict(report),
    }
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
            json.dump(evidence, handle, sort_keys=True, indent=2)
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
