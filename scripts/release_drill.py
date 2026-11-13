#!/usr/bin/env python3
"""Build, verify, install, start, stop, restart, and recovery-test a wheel."""

from __future__ import annotations

import argparse
from dataclasses import asdict
import json
import os
import shlex
import socket
import subprocess
import sys
import tempfile
import time
import tomllib
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


def require_startup_ready(port: int, pid: int) -> None:
    with urllib.request.urlopen(f'http://127.0.0.1:{port}/readyz', timeout=.5) as response:
        value = json.load(response)
    if value.get('ready') is not True or value.get('pid') != pid:
        raise RuntimeError('installed dashboard readiness belongs to another process')


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
                        require_startup_ready(port, process.pid)
                        break
                    time.sleep(0.05)
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
                        require_startup_ready(port, process.pid)
                        break
                    time.sleep(0.05)
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
        run([str(python), "-m", "pip", "install", "--no-index", "--no-deps", str(wheel)])
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
            code=(f'import sys;sys.path.insert(0, {str(site_packages)!r});'
                  'from trading.app.__main__ import main;raise SystemExit(main())')
            console.write_text('#!/bin/sh\nexec '+shlex.quote(str(python))+' -c '
                               +shlex.quote(code)+' "$@"\n',encoding='utf-8')
            console.chmod(0o755)
        account_console = binary / ('trading-paper-account.cmd' if os.name == 'nt' else 'trading-paper-account')
        account_console.write_text(console.read_text().replace('trading.app.__main__', 'trading.app.paper_account')
                                   .replace('-m trading.app ', '-m trading.app.paper_account '),encoding='utf-8')
        account_console.chmod(0o755)
        return python, console


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--python", default=sys.executable)
    parser.add_argument("--dist", default=str(ROOT / "dist"))
    parser.add_argument('--evidence',help='new JSON path for installed-wheel verification results')
    args = parser.parse_args()
    if args.evidence and Path(args.evidence).exists():
        parser.error('release evidence already exists; choose a new path')

    dist = Path(args.dist).resolve()
    dist.mkdir(parents=True, exist_ok=True)
    # Fixed wheel ZIP timestamps; callers may choose another recorded epoch.
    build_env = dict(os.environ)
    build_env.setdefault('SOURCE_DATE_EPOCH', '1794528000')
    run(
        [
            args.python,
            "-m",
            "pip",
            "wheel",
            str(ROOT),
            "--no-deps",
            "--no-index",
            "--no-build-isolation",
            "--wheel-dir",
            str(dist),
        ], env=build_env
    )
    version = tomllib.loads((ROOT/'pyproject.toml').read_text())['project']['version']
    wheel = dist/f'safety_first_trading_app-{version}-py3-none-any.whl'
    if not wheel.is_file():
        raise RuntimeError("wheel build produced no release artifact")
    artifact = verify_release_wheel(wheel)
    if not artifact.passed:
        raise RuntimeError("release artifact verification failed: " + "; ".join(artifact.failures))

    with tempfile.TemporaryDirectory(prefix="trading-release-drill-") as directory:
        root = Path(directory)
        environment = root / "venv"
        python, console = install_isolated(wheel, environment)
        env = dict(os.environ)
        env.pop('PYTHONPATH',None)
        env.pop('PYTHONHOME',None)
        env["PYTHONUNBUFFERED"] = "1"
        run([str(console), "--help"],stdout=subprocess.DEVNULL,env=env,cwd=root)
        account_console=console.with_name('trading-paper-account'+console.suffix)
        account_path=root/'custom-account.sqlite3'
        created=run([str(account_console),'create','--state-db',str(account_path),
            '--cash','1234.56','--currency','USD'],capture_output=True,env=env,cwd=root)
        account=json.loads(created.stdout)
        if account['state']!='paused' or account['starting_cash']!='1234.56' or account['live_trading']:
            raise RuntimeError('installed account setup returned an invalid account')
        port=free_loopback_port()
        start_snapshot_condition_stop([str(console),'--dashboard','--port',str(port),
            '--state-db',str(account_path)],port=port,env=env,
            description='custom balance and paused new paper account',
            condition=lambda s: s['application']['bot_state']=='paused'
                and s['portfolio']['cash']=='1234.56' and not s['orders'])
        port = free_loopback_port()
        start_health_stop(
            [
                str(console),
                "--dashboard",
                "--port",
                str(port),
                "--poll-interval",
                "0.01",
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
            description="restored inventory and paused bot",
            condition=lambda snapshot: (
                snapshot['application']['bot_state'] == 'paused'
                and bool(snapshot['portfolio']['positions'])
                and 'position_mismatch' not in snapshot['operations']['blocked_reasons']
            ),
        )
        port = free_loopback_port()
        start_health_stop(
            dashboard_command + ["--port", str(port), '--resume-paper'],
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

    if args.evidence:
        with Path(args.evidence).open('x') as output:
            json.dump({'schema':'installed-paper-release/v1','artifact':asdict(artifact),
                'passed':True,'checks':['installed CLI help','paused custom account and preserved balance',
                    'HTTP dashboard health','crash inventory recovery','explicit resume',
                    'clean shutdown','restart drill','owned-process startup readiness'],
                'source_date_epoch':build_env['SOURCE_DATE_EPOCH'],'live_trading':False,
                'real_orders':False,'fixtures':'temporary synthetic paper sessions'},output,indent=2)
            output.write('\n')
    print(f"release drill: PASS\nwheel: {wheel}\nsha256: {artifact.sha256}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
