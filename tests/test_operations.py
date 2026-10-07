"""Tests for operational status, readiness, and safe startup.

Two properties are being pinned here, and they pull in opposite directions.

**Readiness must be fail-closed.** Every condition the kernel enforces -- an
UNKNOWN order, a latched mismatch, an engaged kill switch, a stale or absent
reconciliation, a venue that cannot be enumerated, a worker that died -- must
make ``ready`` False. The tests below engage each one in isolation, so a
regression that quietly drops one from the predicate shows up as a specific
failure rather than as a system that says it is fine.

**Status must never leak or authorize.** There is no method on the monitor that
submits, cancels, or reconciles, and its output is built from
``redacted_summary()``. ``TestItCannotAct`` asserts the absence of an execution
surface, and ``TestNoSecrets`` asserts that a configured secret does not appear
anywhere in a rendered status.

``test_it_is_not_a_second_gate`` is the load-bearing one: the monitor reports
that the system is un-ready, and the *gateway* is what actually refuses the
order. A status object that could itself block would be a second place the
authorization rule lives.
"""

from __future__ import annotations

import json
import unittest

from tests.harness import ASSET, DEFAULT_QUANTITY, SYMBOL, build_rig
from trading.adapters.operations import (
    BlockedReason,
    ComponentHealth,
    OperationalMonitor,
    OperationalStatus,
    StartupCoordinator,
    StartupReport,
)
from trading.core.authz import Principal, Role
from trading.core.config import REQUIRED_LIVE_CONFIRMATION, TradingConfig
from trading.core.money import USD, Price, Quantity
from trading.core.orders import OrderState
from trading.core.secrets import Secret
from trading.ports.broker import (
    AckOutcome,
    BrokerAck,
    BrokerPort,
    BrokerPositionSnapshot,
)


class BlindBroker(BrokerPort):
    """A venue that cannot enumerate its book, so cannot be fully reconciled.

    Models a real adapter against an exchange with no order-list endpoint. The
    sweep must refuse to pretend it performed a complete reconciliation, and the
    monitor must report the system un-ready because of it.
    """

    def place_order(self, order, *, token) -> BrokerAck:
        token.consume(order_id=order.order_id, clock=ManualClock())
        return BrokerAck(AckOutcome.ACCEPTED, broker_order_id="blind-1")

    def cancel_order(self, order) -> BrokerAck:
        return BrokerAck(AckOutcome.ACCEPTED, message="canceled")

    def fetch_order_state(self, order) -> BrokerAck:
        return BrokerAck(AckOutcome.ACCEPTED, broker_order_id=order.broker_order_id)

    def fetch_positions(self) -> BrokerPositionSnapshot:
        return BrokerPositionSnapshot()


class MonitorCase(unittest.TestCase):
    """A rig with a monitor wired to it."""

    def setUp(self) -> None:
        self.rig = build_rig()
        self.monitor = self.build_monitor()

    def build_monitor(self, **kwargs) -> OperationalMonitor:
        kwargs.setdefault("config", self.rig.config)
        kwargs.setdefault("orders", self.rig.orders)
        kwargs.setdefault("reconciliation", self.rig.reconciliation)
        kwargs.setdefault("kill_switch", self.rig.kill_switch)
        kwargs.setdefault("breakers", self.rig.breakers)
        kwargs.setdefault("modes", self.rig.modes)
        return OperationalMonitor(**kwargs)


# -- the happy path ------------------------------------------------------------


class TestAHealthySystem(MonitorCase):
    def test_a_fresh_paper_system_is_ready(self) -> None:
        status = self.monitor.status()
        self.assertTrue(status.ready, status.blocked_reasons)
        self.assertTrue(status.healthy)

    def test_it_reports_the_mode_and_environment(self) -> None:
        status = self.monitor.status()
        self.assertEqual(status.mode, "paper")
        self.assertFalse(status.live_trading)
        self.assertEqual(status.environment, "development")

    def test_it_counts_orders(self) -> None:
        self.rig.submit()
        status = self.monitor.status()
        self.assertEqual(status.total_order_count, 1)
        self.assertEqual(status.unknown_order_count, 0)

    def test_the_status_is_json_serialisable(self) -> None:
        payload = json.dumps(self.monitor.status().as_details())
        self.assertIn("ready", payload)

    def test_ready_is_the_same_as_the_predicate(self) -> None:
        self.assertEqual(self.monitor.ready(), self.monitor.status().ready)


# -- every blocker, one at a time ----------------------------------------------


class TestFailClosedReadiness(MonitorCase):
    """Each of these alone must make the system un-ready."""

    def reason_sets(self) -> set[str]:
        return set(self.monitor.blocked_reasons())

    def test_an_unknown_order_blocks_readiness(self) -> None:
        self.rig.broker.script(
            BrokerAck(__import__("trading.ports.broker", fromlist=["AckOutcome"]).AckOutcome.UNCERTAIN)
        )
        result = self.rig.submit()
        self.assertEqual(result.outcome, "unknown")
        self.assertIn(BlockedReason.UNKNOWN_ORDERS, self.reason_sets())

    def test_an_engaged_kill_switch_blocks_readiness(self) -> None:
        self.rig.kill_switch.engage(self.rig.operator_id, reason="test")
        self.assertIn(BlockedReason.KILL_SWITCH_ENGAGED, self.reason_sets())

    def test_a_position_mismatch_blocks_readiness(self) -> None:
        self.rig.positions.set_position(SYMBOL, Quantity("1", ASSET))
        self.rig.reconciliation.reconcile({SYMBOL: Quantity.zero(ASSET)})
        self.assertTrue(self.rig.reconciliation.has_mismatch)
        self.assertIn(BlockedReason.POSITION_MISMATCH, self.reason_sets())

    def test_a_venue_that_cannot_be_enumerated_blocks_readiness(self) -> None:
        """A venue that cannot list its orders cannot be reconciled in full."""
        from trading.adapters.reconciliation import ReconciliationCoordinator

        rig = build_rig(broker=BlindBroker())
        coord = ReconciliationCoordinator(
            gateway=rig.gateway,
            orders=rig.orders,
            broker=rig.broker,
            reconciliation=rig.reconciliation,
            audit=rig.audit,
            clock=rig.clock,
            identity=Principal("sys-1", Role.SYSTEM),
        )
        monitor = self.build_monitor(
            config=rig.config,
            orders=rig.orders,
            reconciliation=rig.reconciliation,
            kill_switch=rig.kill_switch,
            breakers=rig.breakers,
            modes=rig.modes,
            coordinator=coord,
        )
        self.assertFalse(coord.supports_venue_inventory)
        self.assertIn(BlockedReason.INVENTORY_UNAVAILABLE, set(monitor.blocked_reasons()))

    def test_an_open_breaker_blocks_readiness(self) -> None:
        for _ in range(3):
            self.rig.breaker.record_failure()
        self.assertIn(BlockedReason.BREAKER_OPEN, self.reason_sets())

    def test_a_dead_worker_blocks_readiness(self) -> None:
        class DeadWorker:
            is_running = False
            passes = 3
            last_error = RuntimeError("worker blew up")

        monitor = self.build_monitor(lifecycle=DeadWorker())
        self.assertIn(BlockedReason.WORKER_DIED, set(monitor.blocked_reasons()))

    def test_a_worker_that_never_ran_blocks_readiness(self) -> None:
        class IdleWorker:
            is_running = False
            passes = 0
            last_error = None

        monitor = self.build_monitor(lifecycle=IdleWorker())
        self.assertIn(BlockedReason.WORKER_NOT_RUNNING, set(monitor.blocked_reasons()))

    def test_a_failed_startup_recovery_blocks_readiness(self) -> None:
        failed = StartupReport(error="recovery exploded")
        monitor = self.build_monitor(recovery=failed)
        self.assertIn(
            BlockedReason.STARTUP_RECOVERY_FAILED, set(monitor.blocked_reasons())
        )

    def test_a_stale_reconciliation_blocks_readiness_when_live(self) -> None:
        rig = build_rig(live_authorized=True, max_staleness_seconds=10.0)
        rig.reconciliation.reconcile({})  # one clean pass
        rig.clock.advance(60)
        monitor = self.build_monitor(
            config=rig.config,
            orders=rig.orders,
            reconciliation=rig.reconciliation,
            kill_switch=rig.kill_switch,
            breakers=rig.breakers,
            modes=rig.modes,
        )
        self.assertIn(BlockedReason.RECONCILIATION_STALE, set(monitor.blocked_reasons()))

    def test_never_reconciled_blocks_readiness_when_live(self) -> None:
        rig = build_rig(live_authorized=True)
        monitor = self.build_monitor(
            config=rig.config,
            orders=rig.orders,
            reconciliation=rig.reconciliation,
            kill_switch=rig.kill_switch,
            breakers=rig.breakers,
            modes=rig.modes,
        )
        self.assertIn(
            BlockedReason.RECONCILIATION_NEVER_RAN, set(monitor.blocked_reasons())
        )

    def test_a_clean_system_after_a_clear_is_ready_again(self) -> None:
        """Un-ready is a state, not a one-way door."""
        self.rig.positions.set_position(SYMBOL, Quantity("1", ASSET))
        self.rig.reconciliation.reconcile({SYMBOL: Quantity.zero(ASSET)})
        self.assertFalse(self.monitor.ready())

        # The operator decides the venue is right, adopts it, and *then* clears.
        # clear_mismatch re-verifies rather than taking the operator's word for
        # it, so the two records have to actually agree first.
        self.rig.reconciliation.adopt_broker_positions(
            self.rig.operator_id,
            reason="venue is authoritative",
            broker_positions={SYMBOL: Quantity.zero(ASSET)},
        )
        self.rig.reconciliation.clear_mismatch(
            self.rig.operator_id,
            reason="ledger now agrees with the venue",
            broker_positions={SYMBOL: Quantity.zero(ASSET)},
        )
        self.assertTrue(self.monitor.ready(), self.monitor.blocked_reasons())


class TestReservationLevelUnknown(MonitorCase):
    """Readiness must reflect a durable UNKNOWN reservation, not just orders.

    After a restart with durable reservations but non-durable orders, an UNKNOWN
    reservation survives while its order object does not. The gateway still
    blocks (its reservation-level backstop), so the monitor must report
    un-ready too -- otherwise it would read ready=True off an order store that
    no longer holds the UNKNOWN, and lie about a system the gateway is refusing.
    """

    def test_without_a_dedupe_source_reservations_are_not_consulted(self) -> None:
        # The default build wires no dedupe: the order-level check is the whole
        # surface, and a fresh system is ready.
        self.assertTrue(self.monitor.ready())
        names = {c.name for c in self.monitor.status().components}
        self.assertNotIn("reservations", names)

    def test_an_unknown_reservation_blocks_readiness_when_dedupe_is_wired(self) -> None:
        # An UNKNOWN reservation with NO corresponding UNKNOWN order -- exactly
        # what a restart leaves when orders are not durable.
        self.rig.dedupe.reserve("orphan-key", "ord-gone")
        self.rig.dedupe.mark_submitted("orphan-key")
        self.rig.dedupe.mark_unknown("orphan-key")
        monitor = self.build_monitor(dedupe=self.rig.dedupe)
        status = monitor.status()
        self.assertFalse(status.ready)
        self.assertIn(BlockedReason.UNKNOWN_RESERVATION, status.blocked_reasons)
        # The order-level check did NOT fire: there is no UNKNOWN order object.
        self.assertNotIn(BlockedReason.UNKNOWN_ORDERS, status.blocked_reasons)

    def test_a_clean_dedupe_reports_a_healthy_reservations_component(self) -> None:
        monitor = self.build_monitor(dedupe=self.rig.dedupe)
        status = monitor.status()
        self.assertTrue(status.ready, status.blocked_reasons)
        names = {c.name for c in status.components}
        self.assertIn("reservations", names)


class TestItCannotAct(MonitorCase):
    """The monitor reports. It has no execution surface, and must not grow one."""

    def test_it_exposes_no_submit_or_cancel(self) -> None:
        for forbidden in ("submit", "cancel", "resolve_unknown", "place_order"):
            self.assertFalse(
                hasattr(self.monitor, forbidden),
                f"the operational monitor grew a {forbidden}() method",
            )

    def test_it_holds_no_broker_or_gateway(self) -> None:
        for forbidden in ("_broker", "_gateway"):
            self.assertFalse(hasattr(self.monitor, forbidden))

    def test_a_status_object_cannot_authorize_anything(self) -> None:
        status = self.monitor.status()
        for forbidden in ("submit", "cancel", "reconcile"):
            self.assertFalse(hasattr(status, forbidden))

    def test_it_is_not_a_second_gate(self) -> None:
        """It reports un-ready; the gateway is what refuses."""
        self.rig.kill_switch.engage(self.rig.operator_id, reason="test")
        self.assertFalse(self.monitor.ready())
        result = self.rig.submit()
        self.assertFalse(result.is_executed)
        self.assertEqual(self.rig.broker.placement_count, 0)


class TestPartialWiring(MonitorCase):
    """The monitor is honest about components that were never wired.

    A partially assembled system must not read as healthy merely because the
    checks for the missing parts never ran.
    """

    def test_a_monitor_with_no_lifecycle_says_so(self) -> None:
        monitor = self.build_monitor()
        status = monitor.status()
        lifecycle = [c for c in status.components if c.name == "lifecycle"][0]
        self.assertEqual(lifecycle.detail, "not wired")
        # Not having a poller is normal in a paper rig, so it is not a blocker.
        self.assertNotIn(BlockedReason.WORKER_NOT_RUNNING, status.blocked_reasons)

    def test_a_monitor_with_no_modes_reports_unknown(self) -> None:
        monitor = self.build_monitor(modes=None)
        self.assertEqual(monitor.status().mode, "unknown")

    def test_never_reconciled_renders_as_none_not_zero(self) -> None:
        """"Never" and "0 seconds ago" are different facts."""
        status = self.monitor.status()
        self.assertIsNone(status.seconds_since_clean_reconciliation)
        self.assertIsNone(
            status.as_details()["seconds_since_clean_reconciliation"]
        )


class TestStartupSweepFailure(MonitorCase):
    """A sweep that blows up is reported, and does not crash startup."""

    def test_an_exploding_initial_sweep_is_reported(self) -> None:
        class ExplodingCoordinator:
            supports_venue_inventory = True

            def sweep(self):
                raise RuntimeError("venue unreachable")

        report = StartupCoordinator(
            config=self.rig.config,
            audit=self.rig.audit,
            coordinator=ExplodingCoordinator(),
        ).run(sweep=True)
        stage = [s for s in report.stages if s.name == "initial_sweep"][0]
        self.assertFalse(stage.healthy)
        self.assertTrue(any("initial sweep failed" in f for f in report.recovery_findings))


class TestNoSecrets(MonitorCase):
    def test_a_configured_secret_does_not_appear_in_the_status(self) -> None:
        config = TradingConfig(
            risk=self.rig.config.risk,
            api_key=Secret("SUPER-SECRET-KEY-abcdefghijklmnopqrstuvwxyz012345"),
            api_secret=Secret("SUPER-SECRET-VALUE-abcdefghijklmnopqrstuvwxyz01"),
        )
        monitor = self.build_monitor(config=config)
        rendered = json.dumps(monitor.status().as_details())
        s1 = self.assertNotIn
        s1("SUPER-SECRET-KEY", rendered)
        s1("SUPER-SECRET-VALUE", rendered)

    def test_the_redacted_config_is_what_is_exposed(self) -> None:
        status = self.monitor.status()
        self.assertEqual(status.config, self.rig.config.redacted_summary())


# -- safe startup --------------------------------------------------------------


class TestSafeStartup(unittest.TestCase):
    def setUp(self) -> None:
        self.rig = build_rig()

    def coordinator(self, **kwargs) -> StartupCoordinator:
        kwargs.setdefault("config", self.rig.config)
        kwargs.setdefault("audit", self.rig.audit)
        return StartupCoordinator(**kwargs)

    def test_a_clean_system_starts(self) -> None:
        report = self.coordinator().run(sweep=False)
        self.assertTrue(report.ok, report.findings if hasattr(report, "findings") else report)

    def test_startup_is_audited(self) -> None:
        self.coordinator().run(sweep=False)
        self.assertIn("startup_completed", self.rig.actions())

    def test_startup_does_not_enable_live_trading(self) -> None:
        self.coordinator().run(sweep=False)
        self.assertFalse(self.rig.config.is_live_authorized)

    def test_startup_does_not_submit_anything(self) -> None:
        self.coordinator().run(sweep=False)
        self.assertEqual(self.rig.broker.placement_count, 0)

    def test_live_without_confirmation_is_refused(self) -> None:
        """Constructed directly, since TradingConfig would refuse the phrase check."""
        config = TradingConfig.__new__(TradingConfig)
        object.__setattr__(config, "live_trading", True)
        object.__setattr__(config, "live_confirmation", "")
        object.__setattr__(config, "environment", "production")
        object.__setattr__(config, "risk", self.rig.config.risk)
        report = StartupCoordinator(
            config=config, audit=self.rig.audit
        ).run(sweep=False)
        self.assertFalse(report.ok)
        self.assertIsNotNone(report.error)

    def test_an_unrecognised_environment_is_refused(self) -> None:
        config = TradingConfig.__new__(TradingConfig)
        object.__setattr__(config, "live_trading", False)
        object.__setattr__(config, "live_confirmation", "")
        object.__setattr__(config, "environment", "debug-personal")
        object.__setattr__(config, "risk", self.rig.config.risk)
        report = StartupCoordinator(
            config=config, audit=self.rig.audit
        ).run(sweep=False)
        self.assertFalse(report.ok)

    def test_live_in_a_non_production_environment_is_refused(self) -> None:
        config = TradingConfig.__new__(TradingConfig)
        object.__setattr__(config, "live_trading", True)
        object.__setattr__(config, "live_confirmation", REQUIRED_LIVE_CONFIRMATION)
        object.__setattr__(config, "environment", "staging")
        object.__setattr__(config, "risk", self.rig.config.risk)
        report = StartupCoordinator(
            config=config, audit=self.rig.audit
        ).run(sweep=False)
        self.assertFalse(report.ok)

    def test_recovery_findings_are_reported_not_resolved(self) -> None:
        """An ambiguous order is surfaced and left alone."""
        from trading.adapters.memory import SimulatedBroker
        from trading.adapters.persistence import ReservationRepository
        from trading.adapters.recovery import RestartRecoveryCoordinator
        from trading.ports.broker import AckOutcome

        clock = self.rig.clock
        venue = SimulatedBroker(clock=clock, default_outcome=AckOutcome.UNCERTAIN)
        rig = build_rig(broker=venue, clock=clock)
        result = rig.submit()
        self.assertEqual(result.outcome, "unknown")

        recovery = RestartRecoveryCoordinator(
            gateway=rig.gateway,
            orders=rig.orders,
            reservations=ReservationRepository(),
        )
        report = StartupCoordinator(
            config=rig.config, audit=rig.audit, recovery=recovery
        ).run(sweep=False)
        self.assertTrue(report.recovery_findings)
        order = rig.orders.all_orders()[0]
        self.assertIs(order.state, OrderState.UNKNOWN, "startup resolved an UNKNOWN")

    def test_a_failing_recovery_scan_stops_startup(self) -> None:
        class ExplodingRecovery:
            def scan(self):
                raise RuntimeError("persistence unavailable")

        report = self.coordinator(recovery=ExplodingRecovery()).run(sweep=False)
        self.assertFalse(report.ok)
        self.assertIn("recovery scan failed", report.error or "")

    def test_startup_can_take_an_initial_sweep(self) -> None:
        from trading.adapters.reconciliation import ReconciliationCoordinator

        coord = ReconciliationCoordinator(
            gateway=self.rig.gateway,
            orders=self.rig.orders,
            broker=self.rig.broker,
            reconciliation=self.rig.reconciliation,
            audit=self.rig.audit,
            clock=self.rig.clock,
            identity=Principal("sys-1", Role.SYSTEM),
        )
        report = self.coordinator(coordinator=coord).run(sweep=True)
        self.assertTrue(report.ok, report.error)
        self.assertEqual(coord.sweeps, 1)

    def test_a_blocked_sweep_is_reported_and_does_not_stop_startup(self) -> None:
        """Being un-ready is a status, not a startup failure."""

        class BlockedCoordinator:
            def __init__(self):
                self._sweeps = 0
                self._report = None

            @property
            def sweeps(self):
                return self._sweeps

            @property
            def last_report(self):
                return self._report

            @property
            def supports_venue_inventory(self):
                return True

            def sweep(self):
                from trading.adapters.reconciliation import (
                    FindingKind,
                    FindingSeverity,
                    ReconciliationFinding,
                    VenueSweepReport,
                )

                self._sweeps += 1
                report = VenueSweepReport(
                    at="now",
                    findings=(
                        ReconciliationFinding(
                            FindingKind.VENUE_ORDER_UNKNOWN_LOCALLY,
                            FindingSeverity.BLOCKING,
                            "a ghost order",
                        ),
                    ),
                )
                self._report = report
                return report

        report = self.coordinator(coordinator=BlockedCoordinator()).run(sweep=True)
        # Startup completed -- it did the sweep. The system is un-ready, and the
        # monitor is what says so.
        stage = [s for s in report.stages if s.name == "initial_sweep"][0]
        self.assertFalse(stage.healthy)
        self.assertIn("venue_order_unknown_locally", stage.detail)


if __name__ == "__main__":
    unittest.main()
