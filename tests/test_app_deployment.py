"""Local packaging, profile, and process-shutdown deployment contracts."""

from __future__ import annotations

import contextlib
import io
import signal
import tempfile
import tomllib
import unittest
from pathlib import Path

from trading.app import GracefulShutdown
from trading.core.config import TradingConfig


ROOT = Path(__file__).resolve().parents[1]


class DeploymentContractTest(unittest.TestCase):
    def test_package_has_dependency_free_console_entrypoint(self) -> None:
        with (ROOT / "pyproject.toml").open("rb") as handle:
            project = tomllib.load(handle)["project"]

        self.assertEqual(project["requires-python"], ">=3.11")
        self.assertEqual(project["dependencies"], [])
        self.assertEqual(
            project["scripts"]["trading-paper"],
            "trading.app.__main__:main",
        )

    def test_example_profile_is_valid_paper_only_and_contains_no_secrets(self) -> None:
        profile = TradingConfig.from_toml_file(
            str(ROOT / "config" / "paper.example.toml")
        )

        self.assertFalse(profile.live_trading)
        self.assertFalse(profile.is_live_authorized)
        self.assertIsNone(profile.api_key)
        self.assertIsNone(profile.api_secret)
        self.assertEqual(profile.environment, "paper")

    def test_graceful_shutdown_is_idempotent_and_restores_signal_handlers(self) -> None:
        previous = signal.getsignal(signal.SIGTERM)
        shutdown = GracefulShutdown()

        with shutdown:
            self.assertNotEqual(signal.getsignal(signal.SIGTERM), previous)
            shutdown._handle(signal.SIGTERM, None)  # exercise installed handler
            shutdown.request("later request must not replace first reason")
            self.assertTrue(shutdown.requested)
            self.assertEqual(shutdown.reason, "SIGTERM")

        self.assertEqual(signal.getsignal(signal.SIGTERM), previous)

    def test_cli_rejects_a_live_configuration_profile(self) -> None:
        from trading.app.__main__ import main

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "unsafe.toml"
            path.write_text(
                "[trading]\n"
                "live_trading = true\n"
                'live_confirmation = "I_UNDERSTAND_THIS_TRADES_REAL_MONEY"\n',
                encoding="utf-8",
            )
            with contextlib.redirect_stderr(io.StringIO()):
                with self.assertRaises(SystemExit) as caught:
                    main(["--config", str(path)])

        self.assertEqual(caught.exception.code, 2)


if __name__ == "__main__":
    unittest.main()
