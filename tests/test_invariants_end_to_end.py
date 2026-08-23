"""The thirteen invariants, asserted on a fully wired system.

Every other test module proves something about one component. This one proves the
*system* keeps its promises, which is a different claim: a chain of correct links
can still fail at a join. One class per invariant, named after it, so a failure
points straight at the promise that broke.

Two rules keep this from becoming a slower copy of ``test_gateway.py``:

1. **Assert against the venue and the audit trail, not against internals.**
   ``rig.broker.placement_count`` and ``rig.broker.duplicate_keys`` are what the
   outside world saw. A refusal that satisfied the gateway's own bookkeeping but
   still reached the venue is the failure worth catching, and only an external
   observer can catch it.
2. **Prefer multi-step scenarios.** A single call exercises one gate; a sequence
   exercises the interaction between gates, latches, and accumulated state. The
   interesting bugs live in the second category.

``test_gateway.py`` owns the per-gate refusals and the chain ordering; this module
owns the end-to-end consequences.
"""

from __future__ import annotations

import unittest
from decimal import Decimal

from tests.harness import ASSET, DEFAULT_PRICE, SYMBOL, build_rig, equity
from trading.core.audit import AuditOutcome
from trading.core.authz import Action, Principal, Role, is_authorized
from trading.core.config import REQUIRED_LIVE_CONFIRMATION, RiskConfig, TradingConfig
from trading.core.errors import (
    ConfigurationError,
    KillSwitchEngaged,
    PositionMismatch,
    RiskLimitExceeded,
    SafetyViolation,
    UnauthorizedAction,
    UnknownOrderStateBlocked,
)
from trading.core.gateway import ExecutionGate, ExecutionOutcome
from trading.core.modes import TradingMode
from trading.core.money import USD, Money, Quantity
from trading.core.orders import OrderIntent, OrderSide, OrderState
from trading.core.secrets import Secret, global_redactor
from trading.ports.broker import AckOutcome, BrokerAck
from trading.strategy import MarketView, Strategy, StrategyRunner

LEAKABLE = "e2e_api_key_9f8e7d6c5b4a3921"


def floats_in(value, path="details"):
    """Every float reachable inside ``value``, with the path that found it."""
    found = []
    if isinstance(value, float):
        found.append(path)
    elif isinstance(value, dict):
        for k, v in value.items():
            found += floats_in(v, f"{path}.{k}")
    elif isinstance(value, (list, tuple)):
        for i, v in enumerate(value):
            found += floats_in(v, f"{path}[{i}]")
    return found


class SystemCase(unittest.TestCase):
    """A wired system, plus assertions phrased as what an outsider observed."""

    def setUp(self):
        self.rig = build_rig()

    def tearDown(self):
        # Cheap, so every scenario in this module doubles as a check that the
        # audit trail survived it intact (INVARIANT 13).
        self.rig.audit.verify()

    def assertVenueSawNothing(self):
        self.assertEqual(
            self.rig.broker.placement_count,
            0,
            "a refused order still reached the venue",
        )

    def assertVenueSaw(self, count):
        self.assertEqual(self.rig.broker.placement_count, count)

    def assertNoDuplicateReachedTheVenue(self):
        self.assertEqual(
            self.rig.broker.duplicate_keys,
            frozenset(),
            "the venue saw the same idempotency key twice",
        )


# ---------------------------------------------------------------------------
# INVARIANT 1 -- LIVE_TRADING defaults to FALSE
# ---------------------------------------------------------------------------


class TestInvariant1LiveIsOptIn(unittest.TestCase):
    """The default must be the safe one, with no argument supplied."""

    def test_a_bare_config_is_not_live(self):
        self.assertFalse(TradingConfig().live_trading)

    def test_a_bare_config_starts_disabled_and_cannot_execute(self):
        rig = build_rig(mode=TradingMode.DISABLED)
        self.assertIs(rig.modes.mode, TradingMode.DISABLED)
        result = rig.submit()
        self.assertTrue(result.is_refused)
        self.assertEqual(rig.broker.placement_count, 0)

    def test_enabling_live_needs_more_than_a_flag(self):
        """A single boolean must not be enough to reach a real venue."""
        with self.assertRaises(ConfigurationError):
            TradingConfig(live_trading=True)

    def test_the_confirmation_phrase_must_match_exactly(self):
        with self.assertRaises(ConfigurationError):
            TradingConfig(live_trading=True, live_confirmation="yes")

    def test_with_both_it_is_permitted(self):
        config = TradingConfig(
            live_trading=True, live_confirmation=REQUIRED_LIVE_CONFIRMATION
        )
        self.assertTrue(config.live_trading)


# ---------------------------------------------------------------------------
# INVARIANT 2 -- no execution while live trading is disabled
# ---------------------------------------------------------------------------


class TestInvariant2NoLiveWithoutAuthorization(SystemCase):
    def test_a_paper_rig_cannot_walk_to_live(self):
        with self.assertRaises(SafetyViolation):
            self.rig.go_live()
        self.assertIs(self.rig.modes.mode, TradingMode.PAPER)

    def test_paper_orders_still_work(self):
        """The refusal must be specific to LIVE, not a blanket stop."""
        self.assertTrue(self.rig.submit().is_executed)

    def test_reaching_live_is_not_enough_to_send_a_live_order(self):
        """Being in LIVE mode still leaves INVARIANT 6 unsatisfied.

        A system that has never reconciled cannot prove its positions match the
        venue's, so the first live order is refused even though every mode and
        authorisation check passed. Two independent conditions, not one.
        """
        rig = build_rig(live_authorized=True)
        rig.go_live()
        self.assertIs(rig.modes.mode, TradingMode.LIVE)
        result = rig.submit()
        self.assertTrue(result.is_refused)
        self.assertEqual(result.gate, ExecutionGate.RECONCILIATION)
        self.assertEqual(rig.broker.placement_count, 0)
        rig.audit.verify()

    def test_a_live_authorized_reconciled_rig_can_trade(self):
        rig = build_rig(live_authorized=True)
        rig.go_live()
        rig.reconciliation.reconcile(rig.broker.fetch_positions().positions)
        self.assertTrue(rig.submit().is_executed)
        rig.audit.verify()

    def test_halting_stops_execution_immediately(self):
        self.rig.modes.halt(actor="operator-1", reason="e2e")
        result = self.rig.submit()
        self.assertTrue(result.is_refused)
        self.assertVenueSawNothing()


# ---------------------------------------------------------------------------
# INVARIANT 3 -- strategy code cannot execute
# ---------------------------------------------------------------------------


class Eager(Strategy):
    """Proposes one order every time it is asked. Defines no execution method."""

    name = "eager"

    def __init__(self):
        self._n = 0

    def propose(self, view):
        self._n += 1
        return [
            OrderIntent(
                strategy_id=self.name,
                signal_id=f"e2e-{self._n}",
                symbol=SYMBOL,
                side=OrderSide.BUY,
                quantity=Quantity("0.001", ASSET),
            )
        ]


class TestInvariant3StrategyCannotExecute(SystemCase):
    def view(self):
        return MarketView(
            as_of=self.rig.clock.now(),
            equity=equity(),
            prices={SYMBOL: DEFAULT_PRICE},
            positions={},
        )

    def runner(self):
        return StrategyRunner(
            Eager(),
            identity=self.rig.strategy_id,
            audit=self.rig.audit,
            clock=self.rig.clock,
        )

    def test_a_strategy_run_reaches_no_venue_by_itself(self):
        """Proposing is inert. Only a gateway submission can place anything."""
        intents = self.runner().propose(self.view())
        self.assertEqual(len(intents), 1)
        self.assertVenueSawNothing()

    def test_the_same_intent_reaches_the_venue_only_via_the_gateway(self):
        intent = self.runner().propose(self.view())[0]
        self.assertVenueSawNothing()
        self.assertTrue(self.rig.submit(intent).is_executed)
        self.assertVenueSaw(1)

    def test_a_strategy_principal_cannot_execute(self):
        self.assertFalse(is_authorized(self.rig.strategy_id, Action.EXECUTE_ORDER))

    def test_a_strategy_principal_cannot_approve_risk(self):
        self.assertFalse(is_authorized(self.rig.strategy_id, Action.APPROVE_ORDER))

    def test_a_strategy_cannot_drive_the_broker_directly(self):
        """No token, so the venue refuses before looking at the order."""
        order_ish = object()
        with self.assertRaises(TypeError):
            self.rig.broker.place_order(order_ish)
        self.assertVenueSawNothing()

    def test_defining_an_execution_method_fails_at_class_definition(self):
        with self.assertRaises(SafetyViolation) as ctx:
            class Sneaky(Strategy):
                name = "sneaky"

                def propose(self, view):
                    return []

                def place_order(self, intent):  # pragma: no cover
                    return None

        self.assertIn("INVARIANT 3", str(ctx.exception))

    def test_the_gateway_refuses_a_non_strategy_proposer(self):
        result = self.rig.submit(proposer=self.rig.operator_id)
        self.assertTrue(result.is_refused)
        self.assertEqual(result.gate, ExecutionGate.AUTHORIZATION)
        self.assertVenueSawNothing()


# ---------------------------------------------------------------------------
# INVARIANT 4 -- risk runs before execution
# ---------------------------------------------------------------------------


class TestInvariant4RiskPrecedesExecution(SystemCase):
    def test_a_risk_refusal_never_reaches_the_venue(self):
        rig = build_rig(risk=RiskConfig(max_order_notional=Money("1.00", USD)))
        result = rig.submit()
        self.assertTrue(result.is_refused)
        self.assertEqual(rig.broker.placement_count, 0)
        rig.audit.verify()

    def test_the_risk_record_precedes_the_placement_record(self):
        """Ordering in the trail is the evidence that the check ran first."""
        self.rig.submit()
        actions = self.rig.actions()
        self.assertIn("risk_approved", actions)
        self.assertIn("gateway.executed", actions)
        self.assertLess(
            actions.index("risk_approved"),
            actions.index("gateway.executed"),
            "the order was placed before risk approved it",
        )

    def test_a_risk_refusal_is_recorded_and_nothing_is_placed(self):
        rig = build_rig(risk=RiskConfig(max_order_notional=Money("1.00", USD)))
        rig.submit()
        actions = rig.actions()
        self.assertIn("risk_refused", actions)
        self.assertNotIn("gateway.executed", actions)
        rig.audit.verify()

    def test_the_gateway_identity_cannot_also_approve_risk(self):
        self.assertFalse(is_authorized(self.rig.gateway_id, Action.APPROVE_ORDER))

    def test_the_risk_identity_cannot_execute(self):
        self.assertFalse(is_authorized(self.rig.risk_id, Action.EXECUTE_ORDER))

    def test_wiring_a_gateway_that_could_self_approve_fails(self):
        """The separation is checked at construction, not per order."""
        from trading.core.gateway import ExecutionGateway

        omnipotent = Principal("god-1", Role.OPERATOR)
        with self.assertRaises((SafetyViolation, UnauthorizedAction)):
            ExecutionGateway(
                identity=omnipotent,
                broker=self.rig.broker,
                orders=self.rig.orders,
                positions=self.rig.positions,
                reconciliation=self.rig.reconciliation,
                risk=self.rig.risk,
                dedupe=self.rig.dedupe,
                kill_switch=self.rig.kill_switch,
                breakers=self.rig.breakers,
                modes=self.rig.modes,
                config=self.rig.config,
                audit=self.rig.audit,
                clock=self.rig.clock,
            )


# ---------------------------------------------------------------------------
# INVARIANT 5 -- an UNKNOWN order blocks new orders until reconciled
# ---------------------------------------------------------------------------


class TestInvariant5UnknownBlocks(SystemCase):
    def setUp(self):
        super().setUp()
        self.rig.broker.script(BrokerAck(AckOutcome.UNCERTAIN), lands_at_venue=True)

    def test_an_uncertain_ack_becomes_an_unknown_order(self):
        result = self.rig.submit()
        self.assertEqual(result.outcome, ExecutionOutcome.UNKNOWN)
        self.assertIs(result.order.state, OrderState.UNKNOWN)

    def test_a_later_order_is_blocked(self):
        self.rig.submit()
        before = self.rig.broker.placement_count
        result = self.rig.submit()
        self.assertTrue(result.is_refused)
        self.assertEqual(result.gate, ExecutionGate.RECONCILIATION)
        self.assertEqual(self.rig.broker.placement_count, before)

    def test_time_alone_does_not_clear_it(self):
        """There is no timeout after which an unknown order is assumed dead."""
        self.rig.submit()
        self.rig.clock.advance(86_400)
        self.assertTrue(self.rig.submit().is_refused)

    def test_only_asking_the_venue_clears_it(self):
        unknown = self.rig.submit().order
        self.rig.gateway.resolve_unknown(unknown, operator=self.rig.operator_id)
        self.assertFalse(unknown.is_unknown)
        self.assertTrue(self.rig.submit().is_executed)

    def test_the_gate_refuses_by_raising_not_by_returning_false(self):
        """The block is the gate's own contract, not the gateway's politeness."""
        self.rig.submit()
        with self.assertRaises(UnknownOrderStateBlocked) as ctx:
            self.rig.reconciliation.require_clean(live=False)
        self.assertIn("INVARIANT 5", str(ctx.exception))

    def test_a_strategy_cannot_resolve_it(self):
        unknown = self.rig.submit().order
        with self.assertRaises(UnauthorizedAction):
            self.rig.gateway.resolve_unknown(unknown, operator=self.rig.strategy_id)
        self.assertTrue(unknown.is_unknown)

    def test_resolution_reports_what_the_venue_actually_had(self):
        """It landed, so the honest answer is ACCEPTED, not REJECTED."""
        unknown = self.rig.submit().order
        ack = self.rig.gateway.resolve_unknown(
            unknown, operator=self.rig.operator_id
        )
        self.assertIs(ack.outcome, AckOutcome.ACCEPTED)


# ---------------------------------------------------------------------------
# INVARIANT 6 -- a position mismatch blocks new orders
# ---------------------------------------------------------------------------


class TestInvariant6MismatchBlocks(SystemCase):
    def force_mismatch(self):
        self.rig.broker.set_venue_position(SYMBOL, Quantity("5", ASSET))
        self.rig.reconciliation.reconcile(
            self.rig.broker.fetch_positions().positions
        )

    def test_a_disagreement_is_detected(self):
        self.force_mismatch()
        self.assertTrue(self.rig.reconciliation.has_mismatch)

    def test_it_blocks_new_orders(self):
        self.force_mismatch()
        result = self.rig.submit()
        self.assertTrue(result.is_refused)
        self.assertEqual(result.gate, ExecutionGate.RECONCILIATION)
        self.assertVenueSawNothing()

    def test_it_latches_even_after_the_venue_agrees_again(self):
        """Agreement returning is not evidence the earlier gap was harmless."""
        self.force_mismatch()
        self.rig.broker.set_venue_position(SYMBOL, Quantity("0", ASSET))
        self.rig.reconciliation.reconcile(
            self.rig.broker.fetch_positions().positions
        )
        self.assertTrue(self.rig.reconciliation.has_mismatch)
        self.assertTrue(self.rig.submit().is_refused)

    def test_an_operator_clears_it_only_with_a_fresh_clean_snapshot(self):
        self.force_mismatch()
        self.rig.broker.set_venue_position(SYMBOL, Quantity("0", ASSET))
        self.rig.reconciliation.clear_mismatch(
            self.rig.operator_id,
            reason="e2e reconciled",
            broker_positions=self.rig.broker.fetch_positions().positions,
        )
        self.assertFalse(self.rig.reconciliation.has_mismatch)
        self.assertTrue(self.rig.submit().is_executed)

    def test_clearing_while_still_dirty_is_refused(self):
        self.force_mismatch()
        with self.assertRaises((PositionMismatch, SafetyViolation)):
            self.rig.reconciliation.clear_mismatch(
                self.rig.operator_id,
                reason="wishful",
                broker_positions=self.rig.broker.fetch_positions().positions,
            )

    def test_a_strategy_cannot_clear_it(self):
        self.force_mismatch()
        self.rig.broker.set_venue_position(SYMBOL, Quantity("0", ASSET))
        with self.assertRaises(UnauthorizedAction):
            self.rig.reconciliation.clear_mismatch(
                self.rig.strategy_id,
                reason="not mine to clear",
                broker_positions=self.rig.broker.fetch_positions().positions,
            )


# ---------------------------------------------------------------------------
# INVARIANT 7 -- loss and exposure limits cannot be bypassed
# ---------------------------------------------------------------------------


class TestInvariant7LimitsHold(unittest.TestCase):
    """Limits must bind across a *sequence*, not just per order."""

    def test_a_single_oversized_order_is_refused(self):
        rig = build_rig(risk=RiskConfig(max_order_notional=Money("10.00", USD)))
        self.assertTrue(rig.submit().is_refused)
        self.assertEqual(rig.broker.placement_count, 0)
        rig.audit.verify()

    def test_many_small_orders_cannot_creep_past_a_position_limit(self):
        """Each order is legal alone; the accumulated position is not."""
        rig = build_rig(
            risk=RiskConfig(
                max_order_notional=Money("100.00", USD),
                max_position_notional=Money("120.00", USD),
                max_orders_per_minute=100,
                max_open_orders=100,
            )
        )
        executed = 0
        for _ in range(6):
            if rig.submit().is_executed:
                executed += 1
        self.assertLess(executed, 6, "the position limit never bound")
        notional = rig.positions.position(SYMBOL, asset=ASSET).amount * Decimal("50000")
        self.assertLessEqual(notional, Decimal("120.00"))
        rig.audit.verify()

    def test_the_rate_limit_binds(self):
        rig = build_rig(risk=RiskConfig(max_orders_per_minute=2))
        outcomes = [rig.submit().is_executed for _ in range(4)]
        self.assertEqual(outcomes.count(True), 2)
        self.assertEqual(rig.broker.placement_count, 2)
        rig.audit.verify()

    def test_the_rate_window_reopens_with_time(self):
        rig = build_rig(risk=RiskConfig(max_orders_per_minute=2))
        for _ in range(3):
            rig.submit()
        rig.clock.advance(61)
        self.assertTrue(rig.submit().is_executed)
        rig.audit.verify()

    def test_the_risk_engine_refuses_by_raising_not_by_returning_false(self):
        """A breach cannot be ignored by a caller that forgets to check.

        The gateway turns this into a refusal, but the engine's own contract is
        an exception, so a hypothetical second caller cannot accidentally treat
        "no approval" as "approved".
        """
        rig = build_rig(risk=RiskConfig(max_order_notional=Money("1.00", USD)))
        with self.assertRaises(RiskLimitExceeded) as ctx:
            rig.risk.approve(
                rig.intent(), positions={}, mark_prices=rig.prices()
            )
        self.assertIn("INVARIANT 7", str(ctx.exception))
        self.assertEqual(rig.broker.placement_count, 0)
        rig.audit.verify()

    def test_a_missing_price_refuses_rather_than_skipping_the_check(self):
        """An unmeasurable exposure must fail closed."""
        rig = build_rig()
        result = rig.submit(mark_prices={})
        self.assertTrue(result.is_refused)
        self.assertEqual(rig.broker.placement_count, 0)
        rig.audit.verify()


# ---------------------------------------------------------------------------
# INVARIANT 8 -- Decimal only, never float
# ---------------------------------------------------------------------------


class TestInvariant8NoFloats(SystemCase):
    def test_a_full_lifecycle_writes_no_float_into_the_audit_trail(self):
        """A float in the trail would mean a float in a calculation upstream."""
        self.rig.submit()
        self.rig.submit()
        offenders = []
        for record in self.rig.sink.records:
            offenders += floats_in(record.details, f"{record.action}.details")
        self.assertEqual(offenders, [])

    def test_positions_stay_decimal_after_fills(self):
        self.rig.submit()
        self.assertIsInstance(
            self.rig.positions.position(SYMBOL, asset=ASSET).amount, Decimal
        )

    def test_a_float_price_cannot_enter_through_submit(self):
        with self.assertRaises((TypeError, ValueError)):
            self.rig.submit(mark_prices={SYMBOL: 50000.0})
        self.assertVenueSawNothing()

    def test_a_float_quantity_cannot_enter_through_an_intent(self):
        with self.assertRaises((TypeError, ValueError)):
            self.rig.intent(quantity=0.001)

    def test_a_bool_is_not_a_number_here(self):
        """``True`` is an int in Python, which is exactly why it is rejected."""
        with self.assertRaises((TypeError, ValueError)):
            Quantity(True, ASSET)

    def test_money_arithmetic_stays_exact_across_a_sequence(self):
        total = Money("0.00", USD)
        for _ in range(3):
            total = total + Money("0.10", USD)
        self.assertEqual(total, Money("0.30", USD))


# ---------------------------------------------------------------------------
# INVARIANT 9 -- secrets never appear in logs
# ---------------------------------------------------------------------------


class TestInvariant9SecretsStayOut(SystemCase):
    def setUp(self):
        super().setUp()
        global_redactor().forget_all()
        self.secret = Secret(LEAKABLE, label="api_key")

    def tearDown(self):
        global_redactor().forget_all()
        super().tearDown()

    def test_a_full_lifecycle_never_writes_the_secret(self):
        self.rig.submit()
        self.rig.submit()
        self.assertNotIn(LEAKABLE, self.rig.sink.rendered())

    def test_a_secret_placed_in_details_is_redacted(self):
        from trading.core.audit import AuditCategory

        self.rig.audit.record(
            AuditCategory.SYSTEM, "e2e.probe", details={"api_key": self.secret}
        )
        self.assertNotIn(LEAKABLE, self.rig.sink.rendered())

    def test_a_leaked_bare_string_is_still_scrubbed(self):
        """Containment failed; scrubbing is the second line of defence."""
        from trading.core.audit import AuditCategory

        self.rig.audit.record(
            AuditCategory.SYSTEM, "e2e.probe", details={"blob": f"key={LEAKABLE}"}
        )
        self.assertNotIn(LEAKABLE, self.rig.sink.rendered())

    def test_a_secret_in_the_action_name_is_scrubbed(self):
        from trading.core.audit import AuditCategory

        self.rig.audit.record(AuditCategory.SYSTEM, f"login {LEAKABLE}")
        self.assertNotIn(LEAKABLE, self.rig.sink.rendered())

    def test_the_config_does_not_expose_it_in_repr(self):
        config = TradingConfig(api_key=self.secret)
        self.assertNotIn(LEAKABLE, repr(config))

    def test_a_bare_string_credential_is_refused_by_config(self):
        with self.assertRaises(ConfigurationError):
            TradingConfig(api_key=LEAKABLE)

    def test_the_idempotency_key_still_survives(self):
        """Redaction must not eat the identifier reconciliation needs."""
        result = self.rig.submit()
        self.assertIn(result.order.idempotency_key, self.rig.sink.rendered())


# ---------------------------------------------------------------------------
# INVARIANT 10 -- the kill switch prevents new orders
# ---------------------------------------------------------------------------


class TestInvariant10KillSwitch(SystemCase):
    def test_engaging_stops_new_orders(self):
        self.rig.kill_switch.engage(self.rig.operator_id, reason="e2e")
        result = self.rig.submit()
        self.assertTrue(result.is_refused)
        self.assertEqual(result.gate, ExecutionGate.KILL_SWITCH)
        self.assertVenueSawNothing()

    def test_it_stops_orders_that_were_fine_a_moment_ago(self):
        self.assertTrue(self.rig.submit().is_executed)
        self.rig.kill_switch.engage(self.rig.operator_id, reason="e2e")
        before = self.rig.broker.placement_count
        self.assertTrue(self.rig.submit().is_refused)
        self.assertEqual(self.rig.broker.placement_count, before)

    def test_a_strategy_cannot_release_it(self):
        self.rig.kill_switch.engage(self.rig.operator_id, reason="e2e")
        with self.assertRaises(UnauthorizedAction):
            self.rig.kill_switch.release(self.rig.strategy_id, reason="let me trade")
        self.assertTrue(self.rig.kill_switch.is_engaged)

    def test_an_operator_can_release_it_and_trading_resumes(self):
        self.rig.kill_switch.engage(self.rig.operator_id, reason="e2e")
        self.rig.kill_switch.release(self.rig.operator_id, reason="all clear")
        self.assertFalse(self.rig.kill_switch.is_engaged)
        self.assertTrue(self.rig.submit().is_executed)

    def test_a_strategy_can_still_engage_it(self):
        """Stopping must be available to everyone; only starting is privileged."""
        self.rig.kill_switch.engage(self.rig.strategy_id, reason="panic")
        self.assertTrue(self.rig.kill_switch.is_engaged)

    def test_it_beats_a_risk_refusal_to_the_answer(self):
        """The earliest gate wins, so the reason given is the most urgent one."""
        rig = build_rig(risk=RiskConfig(max_order_notional=Money("1.00", USD)))
        rig.kill_switch.engage(rig.operator_id, reason="e2e")
        self.assertEqual(rig.submit().gate, ExecutionGate.KILL_SWITCH)
        rig.audit.verify()


# ---------------------------------------------------------------------------
# INVARIANT 11 -- invalid mode transitions are rejected
# ---------------------------------------------------------------------------


class TestInvariant11ModeTransitions(SystemCase):
    def test_the_rig_starts_in_paper(self):
        self.assertIs(self.rig.modes.mode, TradingMode.PAPER)

    def test_a_skipped_step_is_refused(self):
        rig = build_rig(mode=TradingMode.DISABLED, live_authorized=True)
        with self.assertRaises(SafetyViolation):
            rig.modes.transition_to(
                TradingMode.LIVE, actor="operator-1", reason="skip paper"
            )
        self.assertIs(rig.modes.mode, TradingMode.DISABLED)
        rig.audit.verify()

    def test_the_reason_for_a_transition_is_recorded(self):
        """A mode change is a safety decision, so the why has to survive it."""
        self.rig.modes.halt(actor="operator-1", reason="market data stale")
        record = next(
            r for r in reversed(self.rig.sink.records) if r.action == "mode_transition"
        )
        self.assertEqual(record.details["reason"], "market data stale")
        self.assertEqual(record.details["to"], TradingMode.HALTED.value)

    def test_a_self_transition_is_a_recorded_no_op_not_an_error(self):
        """"Ensure we are in PAPER" must be safe to call twice."""
        self.rig.modes.transition_to(
            TradingMode.PAPER, actor="operator-1", reason="idempotent"
        )
        self.assertIs(self.rig.modes.mode, TradingMode.PAPER)
        self.assertIn("mode_transition_noop", self.rig.actions())

    def test_halting_is_always_available(self):
        self.assertIs(
            self.rig.modes.halt(actor="operator-1", reason="e2e"),
            TradingMode.HALTED,
        )
        self.assertIs(self.rig.modes.mode, TradingMode.HALTED)

    def test_a_refused_transition_leaves_execution_unchanged(self):
        with self.assertRaises(SafetyViolation):
            self.rig.go_live()
        self.assertTrue(self.rig.submit().is_executed)


# ---------------------------------------------------------------------------
# INVARIANT 12 -- duplicates prevented, or made explicitly UNKNOWN
# ---------------------------------------------------------------------------


class TestInvariant12NoDuplicates(SystemCase):
    def test_resubmitting_the_same_intent_reaches_the_venue_once(self):
        intent = self.rig.intent()
        self.assertTrue(self.rig.submit(intent).is_executed)
        second = self.rig.submit(intent)
        self.assertTrue(second.is_refused)
        self.assertEqual(second.gate, ExecutionGate.DUPLICATE_ORDER)
        self.assertVenueSaw(1)
        self.assertNoDuplicateReachedTheVenue()

    def test_a_hostile_sequence_produces_no_duplicate_at_the_venue(self):
        """Interleaved retries, refusals, and an UNKNOWN, all in one run."""
        intent = self.rig.intent()
        self.rig.submit(intent)
        self.rig.submit(intent)
        self.rig.broker.script(BrokerAck(AckOutcome.UNCERTAIN), lands_at_venue=True)
        unknown = self.rig.submit().order
        self.rig.submit(intent)
        self.rig.gateway.resolve_unknown(unknown, operator=self.rig.operator_id)
        self.rig.submit(intent)
        self.assertNoDuplicateReachedTheVenue()

    def test_a_transport_failure_becomes_unknown_not_a_silent_retry(self):
        self.rig.broker.raise_on_next()
        result = self.rig.submit()
        self.assertEqual(result.outcome, ExecutionOutcome.UNKNOWN)
        self.assertVenueSaw(1)

    def test_after_a_transport_failure_the_next_order_is_blocked(self):
        self.rig.broker.raise_on_next()
        self.rig.submit()
        before = self.rig.broker.placement_count
        self.assertTrue(self.rig.submit().is_refused)
        self.assertEqual(self.rig.broker.placement_count, before)

    def test_distinct_intents_are_not_confused_for_duplicates(self):
        for _ in range(3):
            self.assertTrue(self.rig.submit().is_executed)
        self.assertVenueSaw(3)
        self.assertNoDuplicateReachedTheVenue()


# ---------------------------------------------------------------------------
# INVARIANT 13 -- every safety decision is recorded before it takes effect
# ---------------------------------------------------------------------------


class TestInvariant13AuditPrecedesEffect(SystemCase):
    def test_a_refusal_is_recorded(self):
        rig = build_rig(risk=RiskConfig(max_order_notional=Money("1.00", USD)))
        rig.submit()
        self.assertTrue(
            any(r.outcome == AuditOutcome.REFUSED for r in rig.sink.records),
            "a refusal left no trace",
        )
        rig.audit.verify()

    def test_the_record_exists_even_though_an_exception_propagated(self):
        self.rig.kill_switch.engage(self.rig.operator_id, reason="e2e")
        with self.assertRaises(KillSwitchEngaged):
            self.rig.kill_switch.require_not_engaged()
        self.assertTrue(self.rig.sink.records)

    def test_the_chain_verifies_after_a_mixed_run(self):
        self.rig.submit()
        self.rig.kill_switch.engage(self.rig.operator_id, reason="e2e")
        self.rig.submit()
        self.rig.kill_switch.release(self.rig.operator_id, reason="clear")
        self.rig.submit()
        self.rig.audit.verify()

    def test_rewriting_a_record_is_detected(self):
        """Append-only in practice, not just by convention."""
        import dataclasses

        self.rig.submit()
        records = self.rig.sink.records
        tampered = [dataclasses.replace(records[0], action="rewritten")] + records[1:]
        with self.assertRaises(ValueError):
            self.rig.audit.verify(tampered)

    def test_dropping_a_record_is_detected(self):
        """Deleting the inconvenient middle of the trail must not verify."""
        for _ in range(2):
            self.rig.submit()
        records = self.rig.sink.records
        self.assertGreater(len(records), 2)
        with self.assertRaises(ValueError):
            self.rig.audit.verify(records[:1] + records[2:])

    def test_reordering_records_is_detected(self):
        for _ in range(2):
            self.rig.submit()
        records = self.rig.sink.records
        swapped = list(records)
        swapped[0], swapped[1] = swapped[1], swapped[0]
        with self.assertRaises(ValueError):
            self.rig.audit.verify(swapped)

    def test_the_decision_is_recorded_before_the_order_reaches_the_venue(self):
        """A crash mid-flight leaves evidence of intent, which is what
        reconciliation needs in order to ask the venue the right question."""
        self.rig.submit()
        actions = self.rig.actions()
        self.assertIn("idempotency_key_reserved", actions)
        self.assertIn("gateway.executed", actions)
        self.assertLess(
            actions.index("idempotency_key_reserved"),
            actions.index("gateway.executed"),
            "the order was sent before its intent was recorded",
        )


if __name__ == "__main__":
    unittest.main()
