"""Offline verification of a built wheel before deployment drills."""

from __future__ import annotations

import hashlib
import re
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
                        if not re.fullmatch(r'Requires-Dist: cryptography>=43; extra == [\"\x27]historical-data[\"\x27]', line):
                            failures.append("wheel unexpectedly declares a runtime dependency")
                if name != "safety-first-trading-app":
                    failures.append("wheel package name is unexpected")
                if version in ('0.3.3rc1','0.3.3rc2','0.3.3rc3'):
                    package = archive.read('trading/__init__.py').decode('utf-8') if 'trading/__init__.py' in names else ''
                    if not re.search(r'__version__\s*=\s*[\"\x27]'+re.escape(version)+r'[\"\x27]', package):
                        failures.append('wheel package/distribution version identity differs')
            if len(entry_names) != 1:
                failures.append("wheel must contain one console entrypoint file")
            else:
                entries = archive.read(entry_names[0]).decode("utf-8")
                if "trading-paper = trading.app.__main__:main" not in entries:
                    failures.append("wheel is missing the trading-paper entrypoint")
                if 'trading/app/paper_account.py' in names and 'trading-paper-account = trading.app.paper_account:main' not in entries:
                    failures.append('wheel is missing the paper account setup entrypoint')
            if "trading/app/__main__.py" not in names:
                failures.append("wheel is missing the runnable application module")
            if version in ('0.2.1','0.2.2','0.2.3','0.2.4','0.2.5','0.2.6','0.3.0','0.3.1','0.3.2','0.3.3rc1','0.3.3rc2','0.3.3rc3'):
                for module in ('paper_account','cost_research','research_acquisition'):
                    if f'trading/app/{module}.py' not in names:
                        failures.append('wheel is missing release module: '+module)
            if version in ('0.2.2','0.2.3','0.2.4','0.2.5','0.2.6','0.3.0','0.3.1','0.3.2','0.3.3rc1','0.3.3rc2','0.3.3rc3'):
                for module in ('trading/adapters/coinswitch/execution_contract.py',
                               'trading/app/dataset_eligibility.py','trading/app/complete_runs.py',
                               'trading/app/native_research.py'):
                    if module not in names:failures.append('wheel is missing completion module: '+module)
            if version in ('0.2.3','0.2.4','0.2.5','0.2.6','0.3.0','0.3.1','0.3.2','0.3.3rc1','0.3.3rc2','0.3.3rc3'):
                for module in ('trading/app/execution_costs.py','trading/adapters/coinswitch/trading_fees.py'):
                    if module not in names:failures.append('wheel is missing cost-preparation module: '+module)
            if version in ('0.2.4','0.2.5','0.2.6','0.3.0','0.3.1','0.3.2','0.3.3rc1','0.3.3rc2','0.3.3rc3') and 'trading/app/depth_costs.py' not in names:
                failures.append('wheel is missing public depth diagnostics')
            if version in ('0.3.0','0.3.1','0.3.2','0.3.3rc1','0.3.3rc2','0.3.3rc3'):
                for module in ('net_expectancy','hourly_expectancy','cross_market_expectancy','adaptive_expectancy',
                               'funding_expectancy','decision_expectancy','flow_expectancy','eth_expectancy','expectancy_registry'):
                    if f'trading/app/{module}.py' not in names:
                        failures.append('wheel is missing expectancy research module: '+module)
            if version in ('0.3.1','0.3.2','0.3.3rc1','0.3.3rc2','0.3.3rc3'):
                for module in ('trading/adapters/persistence/candles.py','trading/app/experience_learning.py',
                               'trading/app/model_registry.py','trading/app/model_monitor.py','trading/app/research_jobs.py',
                               'trading/app/signal_information.py','trading/app/calibrated_expectancy.py','trading/app/event_information.py'):
                    if module not in names:failures.append('wheel is missing ML lifecycle module: '+module)
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
