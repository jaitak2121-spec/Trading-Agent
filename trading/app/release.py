"""Offline verification of a built wheel before deployment drills."""

from __future__ import annotations

import hashlib
import zipfile
from dataclasses import dataclass
from pathlib import Path

__all__ = ["ReleaseArtifactReport", "verify_release_wheel"]


@dataclass(frozen=True, slots=True)
class ReleaseArtifactReport:
    path: str
    sha256: str
    size_bytes: int
    package_name: str
    version: str
    failures: tuple[str, ...] = ()

    @property
    def passed(self) -> bool:
        return not self.failures


def verify_release_wheel(path: str | Path) -> ReleaseArtifactReport:
    """Verify checksum, metadata, entrypoint, and dependency-free contents."""
    wheel = Path(path)
    try:
        payload = wheel.read_bytes()
    except OSError as exc:
        return ReleaseArtifactReport(
            path=str(wheel),
            sha256="",
            size_bytes=0,
            package_name="",
            version="",
            failures=(f"wheel is unreadable: {type(exc).__name__}",),
        )
    digest = hashlib.sha256(payload).hexdigest()
    failures: list[str] = []
    name = version = ""
    try:
        with zipfile.ZipFile(wheel) as archive:
            names = archive.namelist()
            metadata_names = [item for item in names if item.endswith(".dist-info/METADATA")]
            entry_names = [item for item in names if item.endswith(".dist-info/entry_points.txt")]
            if len(metadata_names) != 1:
                failures.append("wheel must contain exactly one METADATA file")
            else:
                metadata = archive.read(metadata_names[0]).decode("utf-8")
                for line in metadata.splitlines():
                    if line.startswith("Name: "):
                        name = line[6:]
                    elif line.startswith("Version: "):
                        version = line[9:]
                    elif line.startswith("Requires-Dist: "):
                        failures.append("wheel unexpectedly declares a runtime dependency")
                if name != "safety-first-trading-app":
                    failures.append("wheel package name is unexpected")
            if len(entry_names) != 1:
                failures.append("wheel must contain one console entrypoint file")
            else:
                entries = archive.read(entry_names[0]).decode("utf-8")
                if "trading-paper = trading.app.__main__:main" not in entries:
                    failures.append("wheel is missing the trading-paper entrypoint")
            if "trading/app/__main__.py" not in names:
                failures.append("wheel is missing the runnable application module")
    except (OSError, zipfile.BadZipFile, UnicodeDecodeError) as exc:
        failures.append(f"wheel is unreadable: {type(exc).__name__}")

    return ReleaseArtifactReport(
        path=str(wheel),
        sha256=digest,
        size_bytes=len(payload),
        package_name=name,
        version=version,
        failures=tuple(dict.fromkeys(failures)),
    )
