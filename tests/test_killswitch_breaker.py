"""Tests for the kill switch and circuit breakers.

Covers INVARIANT 10: the kill switch prevents new orders.
"""

from __future__ import annotations

import os
import tempfile
import unittest

from trading.core.audit import AuditLog, InMemoryAuditSink
from trading.core.authz import Principal, Role
from trading.core.breaker import BreakerRegistry, BreakerState, CircuitBreaker
from trading.core.clock import ManualClock
from trading.core.errors import (
    CircuitBreakerOpen,
    KillSwitchEngaged,
    SafetyViolation,
    UnauthorizedAction,
)
from trading.core.killswitch import KillSwitch, file_presence_probe

OPERATOR = Principal("alice", Role.OPERATOR)
STRATEGY = Principal("momentum-v1", Role.STRATEGY)
GATEWAY = Principal("gateway-1", Role.EXECUTION_GATEWAY)
AUDITOR = Principal("audit-bot", Role.AUDITOR)


class KillSwitchFixture(unittest.TestCase):
    def setUp(self):
        self.clock = ManualClock()
        self.sink = InMemoryAuditSink()
        self.audit = AuditLog(self.sink, clock=self.clock)

    def make(self, **kwargs) -> KillSwitch:
        return KillSwitch(self.audit, clock=self.clock, **kwargs)


class TestKillSwitchBasics(KillSwitchFixture):
    def test_starts_disengaged(self):
        ks = self.make()
        self.assertFalse(ks.is_engaged)
        ks.require_not_engaged()  # must not raise

    def test_engage_blocks(self):
        ks = self.make()
        ks.engage(OPERATOR, reason="manual stop")
        self.assertTrue(ks.is_engaged)
        with self.assertRaises(KillSwitchEngaged) as ctx:
            ks.require_not_engaged()
        self.assertIn("INVARIANT 10", str(ctx.exception))

    def test_reason_and_actor_recorded(self):
        ks = self.make()
        ks.engage(OPERATOR, reason="drawdown breach")
        self.assertEqual(ks.reason, "drawdown breach")
        self.assertEqual(ks.engaged_by, "alice")

    def test_engage_is_audited(self):
        ks = self.make()
        ks.engage(OPERATOR, reason="manual stop")
        records = self.sink.find("kill_switch_engaged")
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0].details["cause"], "manual")

    def test_engage_requires_a_reason(self):
        ks = self.make()
        with self.assertRaises(ValueError):
            ks.engage(OPERATOR, reason="")
        with self.assertRaises(ValueError):
            ks.engage(OPERATOR, reason="   ")

    def test_double_engage_is_a_noop_but_audited(self):
        ks = self.make()
        ks.engage(OPERATOR, reason="first")
        ks.engage(GATEWAY, reason="second")
        self.assertEqual(ks.reason, "first")
        self.assertEqual(len(self.sink.find("kill_switch_engaged")), 1)
        self.assertEqual(len(self.sink.find("kill_switch_engage_noop")), 1)


class TestKillSwitchAuthority(KillSwitchFixture):
    def test_strategy_may_engage(self):
        # Stopping is never gated on privilege.
        ks = self.make()
        ks.engage(STRATEGY, reason="anomaly detected")
        self.assertTrue(ks.is_engaged)

    def test_gateway_may_engage(self):
        ks = self.make()
        ks.engage(GATEWAY, reason="broker errors")
        self.assertTrue(ks.is_engaged)

    def test_auditor_may_not_engage(self):
        ks = self.make()
        with self.assertRaises(UnauthorizedAction):
            ks.engage(AUDITOR, reason="curious")

    def test_only_operator_may_release(self):
        ks = self.make()
        ks.engage(OPERATOR, reason="stop")
        for principal in (STRATEGY, GATEWAY, AUDITOR):
            with self.subTest(principal=principal):
                with self.assertRaises(UnauthorizedAction):
                    ks.release(principal, reason="resume")
        self.assertTrue(ks.is_engaged)

    def test_operator_can_release(self):
        ks = self.make()
        ks.engage(STRATEGY, reason="anomaly")
        ks.release(OPERATOR, reason="investigated, all clear")
        self.assertFalse(ks.is_engaged)
        ks.require_not_engaged()

    def test_release_requires_a_reason(self):
        ks = self.make()
        ks.engage(OPERATOR, reason="stop")
        with self.assertRaises(ValueError):
            ks.release(OPERATOR, reason="")

    def test_release_is_audited(self):
        ks = self.make()
        ks.engage(OPERATOR, reason="stop")
        ks.release(OPERATOR, reason="all clear")
        records = self.sink.find("kill_switch_released")
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0].details["previous_reason"], "stop")


class TestKillSwitchLatching(KillSwitchFixture):
    def test_engaged_state_persists_until_released(self):
        ks = self.make()
        ks.engage(STRATEGY, reason="anomaly")
        for _ in range(5):
            self.assertTrue(ks.is_engaged)

    def test_trigger_file_latches_even_after_removal(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "STOP")
            ks = self.make(trigger_path=path)
            self.assertFalse(ks.is_engaged)
            with open(path, "w") as handle:
                handle.write("stop")
            self.assertTrue(ks.is_engaged)
            os.unlink(path)
            # Latched: removing the file does not resume trading by itself.
            self.assertTrue(ks.is_engaged)

    def test_trigger_file_engagement_is_audited(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "STOP")
            ks = self.make(trigger_path=path)
            with open(path, "w") as handle:
                handle.write("stop")
            self.assertTrue(ks.is_engaged)
            records = self.sink.find("kill_switch_engaged")
            self.assertEqual(records[0].details["cause"], "trigger_file")

    def test_release_refused_while_trigger_file_present(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "STOP")
            with open(path, "w") as handle:
                handle.write("stop")
            ks = self.make(trigger_path=path)
            self.assertTrue(ks.is_engaged)
            with self.assertRaises(SafetyViolation) as ctx:
                ks.release(OPERATOR, reason="resume")
            self.assertIn("remove the trigger file first", str(ctx.exception))
            self.assertTrue(ks.is_engaged)

    def test_release_succeeds_after_trigger_file_removed(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "STOP")
            with open(path, "w") as handle:
                handle.write("stop")
            ks = self.make(trigger_path=path)
            self.assertTrue(ks.is_engaged)
            os.unlink(path)
            ks.release(OPERATOR, reason="resume")
            self.assertFalse(ks.is_engaged)


class TestKillSwitchFailsClosed(KillSwitchFixture):
    def test_probe_error_engages_the_switch(self):
        def exploding_probe(path: str) -> bool:
            raise OSError("permission denied")

        ks = self.make(trigger_path="/some/path", presence_probe=exploding_probe)
        # "I cannot tell" must mean "stop".
        self.assertTrue(ks.is_engaged)
        with self.assertRaises(KillSwitchEngaged):
            ks.require_not_engaged()

    def test_probe_error_is_audited_with_cause(self):
        def exploding_probe(path: str) -> bool:
            raise OSError("permission denied")

        ks = self.make(trigger_path="/some/path", presence_probe=exploding_probe)
        self.assertTrue(ks.is_engaged)
        self.assertEqual(
            self.sink.find("kill_switch_engaged")[0].details["cause"], "probe_error"
        )

    def test_release_refused_when_probe_errors(self):
        calls = {"n": 0}

        def flaky_probe(path: str) -> bool:
            calls["n"] += 1
            raise OSError("io error")

        ks = self.make(trigger_path="/some/path", presence_probe=flaky_probe)
        with self.assertRaises(SafetyViolation):
            ks.release(OPERATOR, reason="resume")

    def test_default_probe_distinguishes_missing_from_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            missing = os.path.join(tmp, "nope")
            self.assertFalse(file_presence_probe(missing))
            present = os.path.join(tmp, "yes")
            with open(present, "w") as handle:
                handle.write("x")
            self.assertTrue(file_presence_probe(present))

    def test_no_trigger_path_means_no_file_check(self):
        ks = self.make(trigger_path=None)
        self.assertFalse(ks.is_engaged)
        self.assertIsNone(ks.trigger_path)


class BreakerFixture(unittest.TestCase):
    def setUp(self):
        self.clock = ManualClock()
        self.sink = InMemoryAuditSink()
        self.audit = AuditLog(self.sink, clock=self.clock)

    def make(self, **kwargs) -> CircuitBreaker:
        params = {
            "clock": self.clock,
            "audit": self.audit,
            "failure_threshold": 3,
            "reset_timeout_seconds": 60.0,
            "successes_to_close": 2,
        }
        params.update(kwargs)
        return CircuitBreaker("broker", **params)


class TestBreakerStateMachine(BreakerFixture):
    def test_starts_closed(self):
        b = self.make()
        self.assertIs(b.state, BreakerState.CLOSED)
        b.require_closed()

    def test_opens_at_threshold(self):
        b = self.make()
        b.record_failure()
        b.record_failure()
        self.assertIs(b.state, BreakerState.CLOSED)
        b.record_failure()
        self.assertIs(b.state, BreakerState.OPEN)

    def test_open_refuses_calls(self):
        b = self.make()
        for _ in range(3):
            b.record_failure()
        with self.assertRaises(CircuitBreakerOpen) as ctx:
            b.require_closed()
        self.assertIn("is open", str(ctx.exception))

    def test_success_resets_failure_count(self):
        b = self.make()
        b.record_failure()
        b.record_failure()
        b.record_success()
        b.record_failure()
        b.record_failure()
        self.assertIs(b.state, BreakerState.CLOSED)

    def test_promotes_to_half_open_after_cooldown(self):
        b = self.make()
        for _ in range(3):
            b.record_failure()
        self.assertIs(b.state, BreakerState.OPEN)
        self.clock.advance(59)
        self.assertIs(b.state, BreakerState.OPEN)
        self.clock.advance(1)
        self.assertIs(b.state, BreakerState.HALF_OPEN)

    def test_half_open_allows_limited_trials(self):
        b = self.make(half_open_max_calls=1)
        for _ in range(3):
            b.record_failure()
        self.clock.advance(60)
        b.require_closed()  # reserves the single trial slot
        with self.assertRaises(CircuitBreakerOpen) as ctx:
            b.require_closed()
        self.assertIn("half-open", str(ctx.exception))

    def test_half_open_failure_reopens_and_restarts_cooldown(self):
        b = self.make()
        for _ in range(3):
            b.record_failure()
        self.clock.advance(60)
        self.assertIs(b.state, BreakerState.HALF_OPEN)
        b.require_closed()
        b.record_failure(reason="still broken")
        self.assertIs(b.state, BreakerState.OPEN)
        self.clock.advance(59)
        self.assertIs(b.state, BreakerState.OPEN)
        self.clock.advance(1)
        self.assertIs(b.state, BreakerState.HALF_OPEN)

    def test_requires_multiple_successes_to_close(self):
        b = self.make(successes_to_close=2)
        for _ in range(3):
            b.record_failure()
        self.clock.advance(60)
        b.require_closed()
        b.record_success()
        self.assertIs(b.state, BreakerState.HALF_OPEN)
        b.require_closed()
        b.record_success()
        self.assertIs(b.state, BreakerState.CLOSED)

    def test_closing_resets_counters(self):
        b = self.make(successes_to_close=1)
        for _ in range(3):
            b.record_failure()
        self.clock.advance(60)
        b.require_closed()
        b.record_success()
        snap = b.snapshot()
        self.assertIs(snap.state, BreakerState.CLOSED)
        self.assertEqual(snap.consecutive_failures, 0)

    def test_allows_call_does_not_consume_a_slot(self):
        b = self.make(half_open_max_calls=1)
        for _ in range(3):
            b.record_failure()
        self.clock.advance(60)
        self.assertTrue(b.allows_call())
        self.assertTrue(b.allows_call())
        b.require_closed()
        self.assertFalse(b.allows_call())

    def test_opened_count_tracks_trips(self):
        b = self.make()
        for _ in range(3):
            b.record_failure()
        self.clock.advance(60)
        b.require_closed()
        b.record_failure()
        self.assertEqual(b.snapshot().opened_count, 2)


class TestBreakerUsesMonotonicClock(BreakerFixture):
    def test_wall_clock_jumping_backwards_does_not_shorten_cooldown(self):
        import datetime as dt

        b = self.make()
        for _ in range(3):
            b.record_failure()
        self.assertIs(b.state, BreakerState.OPEN)
        # A backwards NTP correction of an hour must not affect the cooldown.
        self.clock.set_wall_clock(self.clock.now() - dt.timedelta(hours=1))
        self.assertIs(b.state, BreakerState.OPEN)
        self.clock.advance(60)
        self.assertIs(b.state, BreakerState.HALF_OPEN)

    def test_wall_clock_jumping_forwards_does_not_end_cooldown_early(self):
        import datetime as dt

        b = self.make()
        for _ in range(3):
            b.record_failure()
        self.clock.set_wall_clock(self.clock.now() + dt.timedelta(hours=1))
        self.assertIs(b.state, BreakerState.OPEN)


class TestBreakerValidationAndReset(BreakerFixture):
    def test_constructor_validation(self):
        with self.assertRaises(ValueError):
            CircuitBreaker("", clock=self.clock, audit=self.audit)
        with self.assertRaises(ValueError):
            self.make(failure_threshold=0)
        with self.assertRaises(ValueError):
            self.make(reset_timeout_seconds=0)
        with self.assertRaises(ValueError):
            self.make(successes_to_close=0)
        with self.assertRaises(ValueError):
            self.make(half_open_max_calls=0)

    def test_operator_can_reset(self):
        b = self.make()
        for _ in range(3):
            b.record_failure()
        b.reset(OPERATOR, reason="dependency fixed")
        self.assertIs(b.state, BreakerState.CLOSED)
        b.require_closed()

    def test_non_operator_cannot_reset(self):
        b = self.make()
        for _ in range(3):
            b.record_failure()
        for principal in (STRATEGY, GATEWAY, AUDITOR):
            with self.subTest(principal=principal):
                with self.assertRaises(UnauthorizedAction):
                    b.reset(principal, reason="try")
        self.assertIs(b.state, BreakerState.OPEN)

    def test_reset_requires_reason(self):
        b = self.make()
        with self.assertRaises(ValueError):
            b.reset(OPERATOR, reason="")

    def test_transitions_are_audited(self):
        b = self.make(successes_to_close=1)
        for _ in range(3):
            b.record_failure()
        self.clock.advance(60)
        self.assertIs(b.state, BreakerState.HALF_OPEN)
        b.require_closed()
        b.record_success()
        self.assertEqual(len(self.sink.find("breaker_opened")), 1)
        self.assertEqual(len(self.sink.find("breaker_half_open")), 1)
        self.assertEqual(len(self.sink.find("breaker_closed")), 1)


class TestBreakerRegistry(BreakerFixture):
    def test_require_all_closed_passes_when_all_closed(self):
        reg = BreakerRegistry()
        reg.add(self.make())
        reg.add(CircuitBreaker("market_data", clock=self.clock, audit=self.audit))
        reg.require_all_closed()

    def test_require_all_closed_fails_if_any_open(self):
        reg = BreakerRegistry()
        b = reg.add(self.make())
        reg.add(CircuitBreaker("market_data", clock=self.clock, audit=self.audit))
        for _ in range(3):
            b.record_failure()
        with self.assertRaises(CircuitBreakerOpen) as ctx:
            reg.require_all_closed()
        self.assertIn("broker", str(ctx.exception))

    def test_duplicate_name_rejected(self):
        reg = BreakerRegistry()
        reg.add(self.make())
        with self.assertRaises(ValueError):
            reg.add(self.make())

    def test_get_unknown_raises(self):
        with self.assertRaises(KeyError):
            BreakerRegistry().get("nope")

    def test_names_and_snapshots(self):
        reg = BreakerRegistry()
        reg.add(self.make())
        reg.add(CircuitBreaker("market_data", clock=self.clock, audit=self.audit))
        self.assertEqual(reg.names(), ["broker", "market_data"])
        self.assertEqual(len(reg.snapshots()), 2)


if __name__ == "__main__":
    unittest.main()
