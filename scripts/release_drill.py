#!/usr/bin/env python3
"""Build, verify, install, start, stop, restart, and recovery-test a wheel."""

from __future__ import annotations

import argparse
import json
import os
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request
import venv
import zipfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from trading.app.release import verify_release_wheel  # noqa: E402


def run(command: list[str], **kwargs) -> subprocess.CompletedProcess[str]:
    return subprocess.run(command, check=True, text=True, **kwargs)


def free_loopback_port() -> int:
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        return int(listener.getsockname()[1])


def start_health_stop(
    command: list[str], *, port: int, env: dict[str, str], min_passes: int = 19
) -> None:
    process = subprocess.Popen(
        command,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        text=True,
        env=env,
    )
    deadline = time.monotonic() + 15
    last_snapshot: dict[str, object] | None = None
    try:
        while time.monotonic() < deadline:
            if process.poll() is not None:
                error = process.stderr.read() if process.stderr else ""
                raise RuntimeError(f"installed dashboard exited early: {error}")
            try:
                with urllib.request.urlopen(
                    f"http://127.0.0.1:{port}/api/snapshot", timeout=0.5
                ) as response:
                    last_snapshot = json.loads(response.read())
                    if (
                        last_snapshot["supervisor"]["healthy"]
                        and last_snapshot["operations"]["ready"]
                        and last_snapshot["supervisor"]["passes"] >= min_passes
                        and not last_snapshot["portfolio"]["positions"]
                    ):
                        break
            except Exception:
                time.sleep(0.1)
        else:
            detail = (
                "no snapshot"
                if last_snapshot is None
                else json.dumps(
                    {
                        "supervisor": last_snapshot.get("supervisor"),
                        "operations": last_snapshot.get("operations"),
                    },
                    sort_keys=True,
                )
            )
            raise RuntimeError(
                "installed dashboard did not become healthy: " + detail
            )
        process.terminate()
        if process.wait(10) != 0:
            raise RuntimeError("installed dashboard did not stop cleanly")
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(5)


def start_snapshot_condition_stop(
    command: list[str],
    *,
    port: int,
    env: dict[str, str],
    description: str,
    condition,
    graceful: bool = True,
) -> None:
    process = subprocess.Popen(
        command,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        text=True,
        env=env,
    )
    deadline = time.monotonic() + 15
    last_snapshot = None
    try:
        while time.monotonic() < deadline:
            if process.poll() is not None:
                error = process.stderr.read() if process.stderr else ""
                raise RuntimeError(f"installed dashboard exited early: {error}")
            try:
                with urllib.request.urlopen(
                    f"http://127.0.0.1:{port}/api/snapshot", timeout=0.5
                ) as response:
                    last_snapshot = json.loads(response.read())
                    if condition(last_snapshot):
                        break
            except Exception:
                time.sleep(0.05)
        else:
            detail = None
            if isinstance(last_snapshot, dict):
                detail = {
                    "supervisor": last_snapshot.get("supervisor"),
                    "operations": last_snapshot.get("operations"),
                    "portfolio": last_snapshot.get("portfolio"),
                }
            raise RuntimeError(
                f"installed dashboard never reached {description}: "
                + json.dumps(detail, sort_keys=True)
            )
        if graceful:
            process.terminate()
            if process.wait(10) != 0:
                raise RuntimeError("installed dashboard did not stop cleanly")
        else:
            process.kill()
            process.wait(10)
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(5)


def install_isolated(wheel: Path, environment: Path) -> tuple[Path, Path]:
    """Install with pip, or unpack a verified pure wheel if ensurepip is absent."""
    try:
        venv.EnvBuilder(with_pip=True, clear=True).create(environment)
        binary = environment / ("Scripts" if os.name == "nt" else "bin")
        python = binary / ("python.exe" if os.name == "nt" else "python")
        console = binary / (
            "trading-paper.exe" if os.name == "nt" else "trading-paper"
        )
        run([str(python), "-m", "pip", "install", "--no-deps", str(wheel)])
        return python, console
    except subprocess.CalledProcessError:
        binary = environment / ("Scripts" if os.name == "nt" else "bin")
        site_packages = environment / "site-packages"
        binary.mkdir(parents=True, exist_ok=True)
        site_packages.mkdir(parents=True, exist_ok=True)
        python = Path(sys.executable)
        with zipfile.ZipFile(wheel) as archive:
            archive.extractall(site_packages)
        if os.name == "nt":
            console = binary / "trading-paper.cmd"
            console.write_text(
                f'@set PYTHONPATH={site_packages}\r\n'
                f'@"{python}" -m trading.app %*\r\n',
                encoding="utf-8",
            )
        else:
            console = binary / "trading-paper"
            console.write_text(
                f"#!{python}\nimport sys\n"
                f"sys.path.insert(0, {str(site_packages)!r})\n"
                "from trading.app.__main__ import main\n"
                "raise SystemExit(main())\n",
                encoding="utf-8",
            )
            console.chmod(0o755)
        return python, console


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--python", default=sys.executable)
    parser.add_argument("--dist", default=str(ROOT / "dist"))
    args = parser.parse_args()

    dist = Path(args.dist).resolve()
    dist.mkdir(parents=True, exist_ok=True)
    run(
        [
            args.python,
            "-m",
            "pip",
            "wheel",
            str(ROOT),
            "--no-deps",
            "--no-build-isolation",
            "--wheel-dir",
            str(dist),
        ]
    )
    wheels = sorted(dist.glob("safety_first_trading_app-*.whl"), key=lambda p: p.stat().st_mtime)
    if not wheels:
        raise RuntimeError("wheel build produced no release artifact")
    wheel = wheels[-1]
    artifact = verify_release_wheel(wheel)
    if not artifact.passed:
        raise RuntimeError("release artifact verification failed: " + "; ".join(artifact.failures))

    with tempfile.TemporaryDirectory(prefix="trading-release-drill-") as directory:
        root = Path(directory)
        environment = root / "venv"
        python, console = install_isolated(wheel, environment)
        run([str(console), "--help"], stdout=subprocess.DEVNULL)

        state = root / "installed-state.sqlite3"
        env = dict(os.environ)
        env["PYTHONUNBUFFERED"] = "1"
        for _ in range(2):
            port = free_loopback_port()
            start_health_stop(
                [
                    str(console),
                    "--dashboard",
                    "--port",
                    str(port),
                    "--poll-interval",
                    "0.01",
                    "--state-db",
                    str(state),
                ],
                port=port,
                env=env,
            )

        interrupted = root / "interrupted.sqlite3"
        dashboard_command = [
            str(console),
            "--dashboard",
            "--poll-interval",
            "0.01",
            "--state-db",
            str(interrupted),
        ]
        port = free_loopback_port()
        start_snapshot_condition_stop(
            dashboard_command + ["--port", str(port)],
            port=port,
            env=env,
            description="an open paper position",
            condition=lambda snapshot: bool(snapshot["portfolio"]["positions"]),
            graceful=False,
        )
        port = free_loopback_port()
        start_snapshot_condition_stop(
            dashboard_command + ["--port", str(port)],
            port=port,
            env=env,
            description="the expected restart reconciliation block",
            condition=lambda snapshot: "position_mismatch"
            in snapshot["operations"]["blocked_reasons"],
        )
        run(
            [
                str(console),
                "--recover-paper-position",
                str(interrupted),
                "--confirm-paper-position-reset",
                "I_ACCEPT_PAPER_VENUE_POSITION_RESET",
                "--paper-recovery-reason",
                "installed release drill intentionally interrupted an open position",
            ]
        )
        port = free_loopback_port()
        start_health_stop(
            dashboard_command + ["--port", str(port)],
            port=port,
            env=env,
        )
        run(
            [
                str(console),
                "--restart-drill",
                str(root / "recovery.sqlite3"),
                "--restart-drill-cycles",
                "19",
            ]
        )

    print(f"release drill: PASS\nwheel: {wheel}\nsha256: {artifact.sha256}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
