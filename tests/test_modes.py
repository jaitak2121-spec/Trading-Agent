"""Tests for the trading-mode state machine.

Covers INVARIANT 11 (invalid transitions rejected) and the mode half of
INVARIANT 2 (no execution when live trading is disabled).
"""

from __future__ import annotations

import unittest

from trading.core.audit import AuditLog, InMemoryAuditSink
from trading.core.clock import ManualClock
from trading.core.config import REQUIRED_LIVE_CONFIRMATION, TradingConfig
from trading.core.errors import InvalidModeTransition, LiveTradingDisabled
from trading.core.modes import ALLOWED_TRANSITIONS, TradingMode, TradingModeMachine


def build(config: TradingConfig | None = None, initial=TradingMode.DISABLED):
    cfg = config if config is not None else TradingConfig()
    sink = InMemoryAuditSink()
    log = AuditLog(sink, clock=ManualClock())
    return TradingModeMachine(cfg, log, initial=initial), sink


LIVE_CONFIG = TradingConfig(
    live_trading=True, live_confirmation=REQUIRED_LIVE_CONFIRMATION
)


class TestDefaultMode(unittest.TestCase):
    def test_default_mode_is_disabled(self):
        machine, _ = build()
        self.assertIs(machine.mode, TradingMode.DISABLED)

    def test_disabled_does_not_allow_execution(self):
        machine, _ = build()
        self.assertFalse(machine.mode.allows_execution)
        with self.assertRaises(LiveTradingDisabled):
            machine.require_execution_allowed()

    def test_cannot_construct_directly_into_live(self):
        with self.assertRaises(InvalidModeTransition):
            build(LIVE_CONFIG, initial=TradingMode.LIVE)

    def test_rejects_non_mode_initial(self):
        cfg = TradingConfig()
        log = AuditLog(InMemoryAuditSink(), clock=ManualClock())
        with self.assertRaises(TypeError):
            TradingModeMachine(cfg, log, initial="paper")  # type: ignore[arg-type]

    def test_rejects_non_config(self):
        log = AuditLog(InMemoryAuditSink(), clock=ManualClock())
        with self.assertRaises(TypeError):
            TradingModeMachine({}, log)  # type: ignore[arg-type]


class TestValidTransitions(unittest.TestCase):
    def test_disabled_to_paper(self):
        machine, _ = build()
        machine.transition_to(TradingMode.PAPER, actor="op")
        self.assertIs(machine.mode, TradingMode.PAPER)

    def test_disabled_to_backtest(self):
        machine, _ = build()
        machine.transition_to(TradingMode.BACKTEST, actor="op")
        self.assertIs(machine.mode, TradingMode.BACKTEST)

    def test_paper_to_live_with_authorized_config(self):
        machine, _ = build(LIVE_CONFIG)
        machine.transition_to(TradingMode.PAPER, actor="op")
        machine.transition_to(TradingMode.LIVE, actor="op")
        self.assertIs(machine.mode, TradingMode.LIVE)

    def test_live_back_to_paper(self):
        machine, _ = build(LIVE_CONFIG)
        machine.transition_to(TradingMode.PAPER, actor="op")
        machine.transition_to(TradingMode.LIVE, actor="op")
        machine.transition_to(TradingMode.PAPER, actor="op")
        self.assertIs(machine.mode, TradingMode.PAPER)

    def test_every_mode_can_halt_in_one_step(self):
        for start in (TradingMode.DISABLED, TradingMode.BACKTEST, TradingMode.PAPER, TradingMode.LIVE):
            with self.subTest(start=start):
                self.assertIn(TradingMode.HALTED, ALLOWED_TRANSITIONS[start])

    def test_halt_from_live(self):
        machine, _ = build(LIVE_CONFIG)
        machine.transition_to(TradingMode.PAPER, actor="op")
        machine.transition_to(TradingMode.LIVE, actor="op")
        machine.halt(actor="op", reason="drawdown")
        self.assertIs(machine.mode, TradingMode.HALTED)

    def test_self_transition_is_a_noop(self):
        machine, sink = build()
        machine.transition_to(TradingMode.DISABLED, actor="op")
        self.assertIs(machine.mode, TradingMode.DISABLED)
        self.assertEqual(len(sink.find("mode_transition_noop")), 1)

    def test_transition_counter_increments(self):
        machine, _ = build()
        self.assertEqual(machine.snapshot().transitions, 0)
        machine.transition_to(TradingMode.PAPER, actor="op")
        self.assertEqual(machine.snapshot().transitions, 1)


class TestInvalidTransitionsRejected(unittest.TestCase):
    """INVARIANT 11."""

    def test_disabled_to_live_is_rejected_even_when_authorized(self):
        # The point of the machine: LIVE is only reachable via PAPER.
        machine, _ = build(LIVE_CONFIG)
        with self.assertRaises(InvalidModeTransition) as ctx:
            machine.transition_to(TradingMode.LIVE, actor="op")
        self.assertIn("not an allowed transition", str(ctx.exception))
        self.assertIs(machine.mode, TradingMode.DISABLED)

    def test_backtest_to_live_is_rejected(self):
        machine, _ = build(LIVE_CONFIG)
        machine.transition_to(TradingMode.BACKTEST, actor="op")
        with self.assertRaises(InvalidModeTransition):
            machine.transition_to(TradingMode.LIVE, actor="op")

    def test_halted_to_live_is_rejected(self):
        machine, _ = build(LIVE_CONFIG)
        machine.halt(actor="op", reason="stop")
        with self.assertRaises(InvalidModeTransition):
            machine.transition_to(TradingMode.LIVE, actor="op")

    def test_halted_to_paper_is_rejected(self):
        machine, _ = build()
        machine.halt(actor="op", reason="stop")
        with self.assertRaises(InvalidModeTransition):
            machine.transition_to(TradingMode.PAPER, actor="op")

    def test_halted_exits_only_to_disabled(self):
        machine, _ = build()
        machine.halt(actor="op", reason="stop")
        self.assertEqual(ALLOWED_TRANSITIONS[TradingMode.HALTED], frozenset({TradingMode.DISABLED}))
        machine.transition_to(TradingMode.DISABLED, actor="op")
        self.assertIs(machine.mode, TradingMode.DISABLED)

    def test_failed_transition_leaves_mode_untouched(self):
        machine, _ = build(LIVE_CONFIG)
        with self.assertRaises(InvalidModeTransition):
            machine.transition_to(TradingMode.LIVE, actor="op")
        self.assertIs(machine.mode, TradingMode.DISABLED)
        self.assertEqual(machine.snapshot().transitions, 0)

    def test_refusal_is_audited(self):
        machine, sink = build(LIVE_CONFIG)
        with self.assertRaises(InvalidModeTransition):
            machine.transition_to(TradingMode.LIVE, actor="op")
        refusals = sink.find("mode_transition_refused")
        self.assertEqual(len(refusals), 1)
        self.assertEqual(refusals[0].details["cause"], "transition_not_allowed")

    def test_non_mode_target_rejected(self):
        machine, _ = build()
        with self.assertRaises(TypeError):
            machine.transition_to("live", actor="op")  # type: ignore[arg-type]

    def test_transition_table_covers_every_mode(self):
        for mode in TradingMode:
            self.assertIn(mode, ALLOWED_TRANSITIONS)

    def test_no_mode_transitions_to_itself_in_the_table(self):
        for mode, targets in ALLOWED_TRANSITIONS.items():
            self.assertNotIn(mode, targets, f"{mode} lists itself as a target")


class TestLiveRequiresConfigAuthorization(unittest.TestCase):
    """INVARIANT 1 + 2 at the mode boundary."""

    def test_paper_to_live_refused_without_config(self):
        machine, _ = build(TradingConfig())
        machine.transition_to(TradingMode.PAPER, actor="op")
        with self.assertRaises(LiveTradingDisabled) as ctx:
            machine.transition_to(TradingMode.LIVE, actor="op")
        self.assertIn("INVARIANT 1", str(ctx.exception))
        self.assertIs(machine.mode, TradingMode.PAPER)

    def test_refusal_cause_is_audited(self):
        machine, sink = build(TradingConfig())
        machine.transition_to(TradingMode.PAPER, actor="op")
        with self.assertRaises(LiveTradingDisabled):
            machine.transition_to(TradingMode.LIVE, actor="op")
        refusals = sink.find("mode_transition_refused")
        self.assertEqual(refusals[-1].details["cause"], "live_not_authorized_by_config")

    def test_require_live_allowed_checks_mode(self):
        machine, _ = build(LIVE_CONFIG)
        machine.transition_to(TradingMode.PAPER, actor="op")
        with self.assertRaises(LiveTradingDisabled):
            machine.require_live_allowed()

    def test_require_live_allowed_passes_in_live(self):
        machine, _ = build(LIVE_CONFIG)
        machine.transition_to(TradingMode.PAPER, actor="op")
        machine.transition_to(TradingMode.LIVE, actor="op")
        machine.require_live_allowed()  # must not raise

    def test_require_live_allowed_rechecks_config_independently(self):
        # Belt and braces: even if the mode says LIVE, an unauthorised config
        # must still block. Simulates config being swapped out underneath.
        machine, _ = build(LIVE_CONFIG)
        machine.transition_to(TradingMode.PAPER, actor="op")
        machine.transition_to(TradingMode.LIVE, actor="op")
        machine._config = TradingConfig()  # noqa: SLF001 - deliberate hostile test
        with self.assertRaises(LiveTradingDisabled):
            machine.require_live_allowed()


class TestExecutionGate(unittest.TestCase):
    def test_execution_blocked_in_disabled(self):
        machine, _ = build()
        with self.assertRaises(LiveTradingDisabled):
            machine.require_execution_allowed()

    def test_execution_blocked_in_backtest(self):
        machine, _ = build()
        machine.transition_to(TradingMode.BACKTEST, actor="op")
        with self.assertRaises(LiveTradingDisabled):
            machine.require_execution_allowed()

    def test_execution_blocked_in_halted(self):
        machine, _ = build()
        machine.halt(actor="op", reason="stop")
        with self.assertRaises(LiveTradingDisabled):
            machine.require_execution_allowed()

    def test_execution_allowed_in_paper(self):
        machine, _ = build()
        machine.transition_to(TradingMode.PAPER, actor="op")
        self.assertIs(machine.require_execution_allowed(), TradingMode.PAPER)

    def test_mode_properties(self):
        self.assertTrue(TradingMode.PAPER.allows_execution)
        self.assertTrue(TradingMode.LIVE.allows_execution)
        self.assertFalse(TradingMode.DISABLED.allows_execution)
        self.assertFalse(TradingMode.BACKTEST.allows_execution)
        self.assertFalse(TradingMode.HALTED.allows_execution)
        self.assertTrue(TradingMode.LIVE.is_live)
        self.assertFalse(TradingMode.PAPER.is_live)

    def test_snapshot_is_serialisable(self):
        machine, _ = build()
        details = machine.snapshot().as_details()
        self.assertEqual(details["mode"], "disabled")
        self.assertFalse(details["live_authorized_by_config"])


if __name__ == "__main__":
    unittest.main()
