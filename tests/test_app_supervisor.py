"""Scheduling, failure visibility, and shutdown for the paper supervisor."""

from __future__ import annotations

import json
import threading
import unittest

from trading.app import PaperServiceSupervisor
from trading.core.errors import ConfigurationError, SafetyViolation
from trading.core.secrets import REDACTED, Secret


class CountingService:
    def __init__(self) -> None:
        self.cycles = 0
        self.last_cycle = None
        self.called = threading.Event()

    def poll_once(self):
        self.cycles += 1
        self.called.set()
        return None


class FailingService(CountingService):
    def __init__(self, message: str) -> None:
        super().__init__()
        self.message = message

    def poll_once(self):
        self.called.set()
        raise RuntimeError(self.message)


class BlockingService(CountingService):
    def __init__(self) -> None:
        super().__init__()
        self.release = threading.Event()

    def poll_once(self):
        self.called.set()
        self.release.wait(2)
        self.cycles += 1
        return None


class PaperServiceSupervisorTest(unittest.TestCase):
    def test_runs_immediately_and_stops_cleanly(self) -> None:
        service = CountingService()
        supervisor = PaperServiceSupervisor(service, interval_seconds=10)

        supervisor.start()
        self.assertTrue(service.called.wait(1))
        supervisor.stop()

        status = supervisor.status()
        self.assertFalse(status.running)
        self.assertEqual(status.passes, 1)
        self.assertIsNotNone(status.started_at)
        self.assertIsNotNone(status.stopped_at)
        self.assertIsNone(status.last_error)
        json.dumps(status.as_details())

    def test_worker_failure_stops_and_remains_visible_with_secrets_redacted(self) -> None:
        secret_text = "supervisor-secret-value-123456"
        Secret(secret_text)
        service = FailingService(f"transport leaked {secret_text}")
        supervisor = PaperServiceSupervisor(service, interval_seconds=1)

        supervisor.start()
        self.assertTrue(service.called.wait(1))
        for _ in range(100):
            if not supervisor.is_running:
                break
            threading.Event().wait(0.005)

        status = supervisor.status()
        self.assertFalse(status.running)
        self.assertIn(REDACTED, status.last_error or "")
        self.assertNotIn(secret_text, status.last_error or "")
        self.assertFalse(status.healthy)

    def test_starting_a_second_worker_is_refused(self) -> None:
        service = BlockingService()
        supervisor = PaperServiceSupervisor(service)
        supervisor.start()
        self.assertTrue(service.called.wait(1))
        try:
            with self.assertRaises(SafetyViolation):
                supervisor.start()
        finally:
            service.release.set()
            supervisor.stop()

    def test_stop_is_idempotent_before_and_after_a_run(self) -> None:
        service = CountingService()
        supervisor = PaperServiceSupervisor(service)
        supervisor.stop()
        supervisor.start()
        self.assertTrue(service.called.wait(1))
        supervisor.stop()
        supervisor.stop()

    def test_rejects_invalid_configuration(self) -> None:
        with self.assertRaises(TypeError):
            PaperServiceSupervisor(object())  # type: ignore[arg-type]
        for value in (0, -1, float("nan")):
            with self.subTest(value=value):
                with self.assertRaises(ConfigurationError):
                    PaperServiceSupervisor(CountingService(), interval_seconds=value)


if __name__ == "__main__":
    unittest.main()
