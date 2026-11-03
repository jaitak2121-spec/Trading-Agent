"""Atomic, redacted evidence artifacts for unattended paper soaks."""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from collections.abc import Mapping
from datetime import datetime
from pathlib import Path

__all__ = ["write_soak_evidence"]


def write_soak_evidence(
    path: str | os.PathLike[str],
    *,
    report: Mapping[str, object],
    started_at: datetime,
    finished_at: datetime,
    state_db: str | os.PathLike[str] | None = None,
) -> Path:
    """Write one complete soak result atomically, with optional DB checksum."""
    if not isinstance(report, Mapping):
        raise TypeError("report must be a mapping")
    for name, value in (("started_at", started_at), ("finished_at", finished_at)):
        if not isinstance(value, datetime) or value.tzinfo is None:
            raise TypeError(f"{name} must be a timezone-aware datetime")
    if finished_at < started_at:
        raise ValueError("finished_at must not precede started_at")

    destination = Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    database: dict[str, object] | None = None
    if state_db is not None:
        database_path = Path(state_db)
        digest = hashlib.sha256()
        try:
            with database_path.open("rb") as handle:
                for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                    digest.update(chunk)
            database = {
                "filename": database_path.name,
                "size_bytes": database_path.stat().st_size,
                "sha256": digest.hexdigest(),
            }
        except OSError as exc:
            raise ValueError("state database could not be checksummed") from exc

    evidence = {
        "schema": "paper-soak-evidence/v1",
        "started_at": started_at.isoformat(),
        "finished_at": finished_at.isoformat(),
        "elapsed_seconds": (finished_at - started_at).total_seconds(),
        "mode": "paper",
        "live_trading": False,
        "report": dict(report),
        "state_database": database,
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
