"""Read-only operator snapshot and local HTTP dashboard."""

from __future__ import annotations

import datetime as dt
import http.client
import json
import tempfile
import threading
import unittest

from trading.app import (
    DashboardServer,
    OperatorDashboard,
    PaperServiceSupervisor,
    SupervisorStatus,
    VenueMarketSpecification,
    build_paper_stack,
)
from trading.core.clock import UTC, ManualClock
from trading.core.errors import ConfigurationError, SafetyViolation
from trading.core.money import Money, USD


class IdleService:
    cycles = 0
    last_cycle = None

    def poll_once(self):
        return None


def build_dashboard():
    stack = build_paper_stack()
    supervisor = PaperServiceSupervisor(IdleService(), interval_seconds=10)
    dashboard = OperatorDashboard(stack, supervisor, strategy_name="test-strategy")
    return dashboard, supervisor


class OperatorDashboardTest(unittest.TestCase):
    def test_persisted_snapshot_waits_for_the_active_transaction(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            stack = build_paper_stack(sqlite_path=f"{directory}/state.sqlite3")
            supervisor = PaperServiceSupervisor(IdleService())
            dashboard = OperatorDashboard(stack, supervisor)
            finished = threading.Event()

            with stack.persistence.transaction():
                worker = threading.Thread(
                    target=lambda: (dashboard.snapshot(), finished.set()),
                    daemon=True,
                )
                worker.start()
                self.assertFalse(finished.wait(0.05))
            self.assertTrue(finished.wait(1))
            worker.join(1)
            stack.persistence.close()

    def test_snapshot_is_json_safe_redacted_operational_state(self) -> None:
        dashboard, _ = build_dashboard()

        snapshot = dashboard.snapshot()

        self.assertEqual(snapshot["application"]["mode"], "paper")
        self.assertFalse(snapshot["application"]["live_trading"])
        self.assertEqual(snapshot["application"]["strategy"], "test-strategy")
        self.assertEqual(snapshot["portfolio"]["currency"], "USD")
        self.assertIn("operations", snapshot)
        self.assertIn("orders", snapshot)
        self.assertIn("audit", snapshot)
        json.dumps(snapshot)

    def test_audit_limit_is_applied_to_the_tail(self) -> None:
        stack = build_paper_stack()
        supervisor = PaperServiceSupervisor(IdleService())
        dashboard = OperatorDashboard(stack, supervisor, audit_limit=1)

        records = dashboard.snapshot()["audit"]

        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["seq"], stack.audit.count)

    def test_configuration_validation(self) -> None:
        dashboard, _ = build_dashboard()
        with self.assertRaises(ConfigurationError):
            DashboardServer(dashboard, host="0.0.0.0")
        with self.assertRaises(ValueError):
            DashboardServer(dashboard, port=70000)
        with self.assertRaises(TypeError):
            OperatorDashboard("bad", PaperServiceSupervisor(IdleService()))  # type: ignore[arg-type]

    def test_market_data_reconnect_state_is_redacted_and_observable(self) -> None:
        class MarketData:
            symbol = "BTC/USDT"
            exchange = "c2c1"
            is_running = True
            reconnect_attempts = 3
            successful_reconnects = 2
            last_error = TimeoutError("secret transport detail")

        stack = build_paper_stack()
        supervisor = PaperServiceSupervisor(IdleService())
        dashboard = OperatorDashboard(
            stack, supervisor, market_data=MarketData()
        )

        state = dashboard.snapshot()["market_data"]

        self.assertEqual(state["reconnect_attempts"], 3)
        self.assertEqual(state["successful_reconnects"], 2)
        self.assertEqual(state["last_error"], "TimeoutError")
        self.assertNotIn("secret", json.dumps(state))

    def test_market_metadata_warns_then_blocks_readiness_at_expiry(self) -> None:
        class MarketData:
            symbol = "BTC/USDT"
            exchange = "c2c1"
            is_running = True
            reconnect_attempts = 0
            successful_reconnects = 0
            last_error = None

        captured = dt.datetime(2026, 1, 1, tzinfo=UTC)
        clock = ManualClock(captured)
        market = VenueMarketSpecification(
            price_decimals=2,
            quantity_step="0.000001",  # type: ignore[arg-type]
            min_notional=Money("1.00", USD),
            max_notional=Money("1000.00", USD),
            metadata_source="/trade/api/v2/tradeInfo",
            captured_at=captured,
            valid_until=captured + dt.timedelta(hours=24),
        )
        stack = build_paper_stack(clock=clock)
        supervisor = PaperServiceSupervisor(IdleService())
        dashboard = OperatorDashboard(
            stack,
            supervisor,
            market_data=MarketData(),
            market_specification=market,
            clock=clock,
        )

        fresh = dashboard.snapshot()
        self.assertEqual(fresh["market_metadata"]["state"], "fresh")
        self.assertTrue(fresh["operations"]["ready"])
        self.assertEqual(fresh["alerts"], [])

        clock.set_wall_clock(captured + dt.timedelta(hours=19))
        warning = dashboard.snapshot()
        self.assertEqual(warning["market_metadata"]["state"], "warning")
        self.assertTrue(warning["operations"]["ready"])
        self.assertEqual(
            [alert["kind"] for alert in warning["alerts"]],
            ["market_metadata_expiring"],
        )

        clock.set_wall_clock(captured + dt.timedelta(hours=25))
        expired = dashboard.snapshot()
        self.assertEqual(expired["market_metadata"]["state"], "expired")
        self.assertFalse(expired["operations"]["ready"])
        self.assertIn(
            "market_metadata_expired",
            expired["operations"]["blocked_reasons"],
        )
        self.assertEqual(
            [alert["kind"] for alert in expired["alerts"]],
            [
                "market_metadata_expiring",
                "readiness_blocked",
                "market_metadata_expired",
            ],
        )

    def test_feed_and_supervisor_transitions_emit_redacted_alerts_once(self) -> None:
        class MarketData:
            symbol = "BTC/USDT"
            exchange = "c2c1"
            is_running = True
            reconnect_attempts = 0
            successful_reconnects = 0
            last_error = None

        class MutableSupervisor:
            value = SupervisorStatus(
                running=True, passes=1, interval_seconds=1.0
            )

            def status(self):
                return self.value

        market_data = MarketData()
        supervisor = MutableSupervisor()
        dashboard = OperatorDashboard(
            build_paper_stack(), supervisor, market_data=market_data
        )
        self.assertEqual(dashboard.snapshot()["alerts"], [])

        market_data.is_running = False
        market_data.last_error = TimeoutError("secret transport detail")
        supervisor.value = SupervisorStatus(
            running=False,
            passes=1,
            interval_seconds=1.0,
            last_error="redacted worker failure",
        )
        failed = dashboard.snapshot()["alerts"]
        self.assertEqual(
            [alert["kind"] for alert in failed],
            ["supervisor_failed", "market_data_disconnected"],
        )
        self.assertNotIn("secret", json.dumps(failed))
        self.assertEqual(dashboard.snapshot()["alerts"], failed)

        market_data.is_running = True
        market_data.last_error = None
        supervisor.value = SupervisorStatus(
            running=True, passes=2, interval_seconds=1.0
        )
        recovered = dashboard.snapshot()["alerts"]
        self.assertEqual(
            [alert["kind"] for alert in recovered[-2:]],
            ["supervisor_recovered", "market_data_recovered"],
        )


class DashboardHTTPTest(unittest.TestCase):
    def setUp(self) -> None:
        dashboard, self.supervisor = build_dashboard()
        self.server = DashboardServer(dashboard, port=0)
        try:
            self.server.start()
        except PermissionError as exc:
            raise unittest.SkipTest(
                "environment forbids loopback socket binding"
            ) from exc
        address = self.server.address
        assert address is not None
        self.connection = http.client.HTTPConnection(*address, timeout=2)

    def tearDown(self) -> None:
        self.connection.close()
        self.server.stop()
        self.supervisor.stop()

    def test_serves_dashboard_and_snapshot(self) -> None:
        self.connection.request("GET", "/")
        response = self.connection.getresponse()
        body = response.read().decode("utf-8")
        self.assertEqual(response.status, 200)
        self.assertIn("Trading App", body)
        self.assertIn("<th>Filled / ordered</th>", body)
        self.assertIn("d.market_data.running&&!d.market_data.last_error", body)
        self.assertEqual(response.getheader("Cache-Control"), "no-store")

        self.connection.request("GET", "/api/snapshot")
        response = self.connection.getresponse()
        payload = json.loads(response.read())
        self.assertEqual(response.status, 200)
        self.assertEqual(payload["application"]["mode"], "paper")

    def test_health_is_unavailable_when_supervisor_is_not_running(self) -> None:
        self.connection.request("GET", "/healthz")
        response = self.connection.getresponse()
        payload = json.loads(response.read())
        self.assertEqual(response.status, 503)
        self.assertFalse(payload["healthy"])

    def test_mutation_is_not_exposed_and_unknown_routes_are_404(self) -> None:
        self.connection.request("POST", "/api/orders", body=b"{}")
        response = self.connection.getresponse()
        self.assertEqual(response.status, 405)
        self.assertEqual(json.loads(response.read())["error"], "dashboard is read-only")

        self.connection.request("GET", "/missing")
        response = self.connection.getresponse()
        self.assertEqual(response.status, 404)
        response.read()

    def test_starting_twice_is_refused(self) -> None:
        with self.assertRaises(SafetyViolation):
            self.server.start()


if __name__ == "__main__":
    unittest.main()
