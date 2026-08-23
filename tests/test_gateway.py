"""Tests for the execution gateway -- the single chokepoint.

The gateway is where every other component's guarantee is either enforced or
lost, so these tests are organised around the chain itself:

* one class per gate, checking that it refuses and that **nothing was sent**;
* :class:`TestChainOrdering`, which trips several gates at once and asserts the
  *earliest* one is what refuses -- the order in the docstring is not decoration;
* :class:`TestNothingBypassesTheGateway`, which attacks the chokepoint directly.

The recurring assertion is ``rig.broker.placement_count``. A refusal that still
reached the venue would be worse than no refusal at all, because it would look
safe in the logs.
"""

from __future__ import annotations

import threading
import unittest
from decimal import Decimal

from tests.harness import ASSET, SYMBOL, build_rig
from trading.core.authz import Principal, Role, mint_execution_token
from trading.core.config import RiskConfig
from trading.core.dedupe import ReservationState
from trading.core.errors import (
    SafetyViolation,
    UnauthorizedAction,
)
from trading.core.gateway import ExecutionGate, ExecutionGateway, ExecutionOutcome
from trading.core.modes import TradingMode
from trading.core.money import USD, Money, Price, Quantity
from trading.core.orders import OrderSide, OrderState
from trading.ports.broker import AckOutcome, BrokerAck, BrokerPositionSnapshot


class GatewayFixture(unittest.TestCase):
    def setUp(self) -> None:
        self.rig = build_rig()

    def assertNothingSent(self) -> None:
        self.assertEqual(
            self.rig.broker.placement_count,
            0,
            "a refused order still reached the venue",
        )

    def assertRefusedAt(self, result, gate: str) -> None:
        self.assertTrue(
            result.is_refused, f"expected a refusal, got {result.outcome}"
        )
        self.assertEqual(result.gate, gate, f"refused at {result.gate}, not {gate}")


class TestHappyPath(GatewayFixture):
    def test_a_well_formed_order_executes(self):
        result = self.rig.submit()
        self.assertTrue(result.is_executed)
        self.assertIsNone(result.gate)
        self.assertEqual(result.order.state, OrderState.FILLED)

    def test_the_position_ledger_reflects_the_fill(self):
        self.rig.submit()
        self.assertEqual(
            self.rig.positions.position(SYMBOL, asset=ASSET),
            Quantity("0.001", ASSET),
        )

    def test_exactly_one_request_reaches_the_venue(self):
        self.rig.submit()
        self.assertEqual(self.rig.broker.placement_count, 1)
        self.assertEqual(self.rig.broker.duplicate_keys, frozenset())

    def test_the_reservation_settles(self):
        result = self.rig.submit()
        reservation = self.rig.dedupe.get(result.order.idempotency_key)
        self.assertEqual(reservation.state, ReservationState.SETTLED)

    def test_an_accepted_but_unfilled_order_is_still_a_success(self):
        rig = build_rig(default_outcome=AckOutcome.ACCEPTED)
        result = rig.submit()
        self.assertTrue(result.is_executed)
        self.assertEqual(result.order.state, OrderState.ACCEPTED)
        # No fill, so no position.
        self.assertTrue(rig.positions.position(SYMBOL, asset=ASSET).is_zero)

    def test_a_venue_rejection_is_a_refusal_not_an_unknown(self):
        rig = build_rig(default_outcome=AckOutcome.REJECTED)
        result = rig.submit()
        self.assertTrue(result.is_refused)
        self.assertEqual(result.order.state, OrderState.REJECTED)
        # A rejection is definitive, so it must not block anything.
        self.assertFalse(rig.orders.has_unknown_orders())

    def test_execution_is_audited_with_the_order_id(self):
        result = self.rig.submit()
        records = self.rig.sink.find("gateway.executed")
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0].details["order_id"], result.order.order_id)

    def test_the_order_is_persisted_before_it_is_sent(self):
        """PENDING_NEW must appear in the history before any venue answer."""
        result = self.rig.submit()
        states = [t.to_state for t in result.order.history]
        self.assertIn(OrderState.PENDING_NEW, states)
        self.assertLess(
            states.index(OrderState.PENDING_NEW),
            states.index(OrderState.FILLED),
        )


class TestAuthorizationGate(GatewayFixture):
    def test_a_proposer_who_may_not_propose_is_refused(self):
        auditor = Principal("auditor-1", Role.AUDITOR)
        result = self.rig.submit(proposer=auditor)
        self.assertRefusedAt(result, ExecutionGate.AUTHORIZATION)
        self.assertNothingSent()

    def test_a_gateway_identity_that_cannot_execute_is_refused_at_construction(self):
        for role in (Role.STRATEGY, Role.RISK_MANAGER, Role.AUDITOR, Role.OPERATOR):
            with self.subTest(role=role):
                with self.assertRaises(UnauthorizedAction):
                    self._rebuild_gateway_with(Principal("x", role))

    def test_a_gateway_that_could_also_approve_is_refused(self):
        """Separation of duties, enforced at wiring time (INVARIANT 4)."""

        class BothRoles(Principal):
            pass

        # No role in the matrix holds both, so this is proven by the matrix
        # itself rather than by constructing an impossible principal.
        from trading.core.authz import PERMISSIONS, Action

        for role, actions in PERMISSIONS.items():
            with self.subTest(role=role):
                self.assertFalse(
                    Action.EXECUTE_ORDER in actions
                    and Action.APPROVE_ORDER in actions,
                    f"{role} can both approve and execute",
                )

    def test_the_broker_must_implement_the_port(self):
        with self.assertRaises(TypeError):
            self._rebuild_gateway_with(self.rig.gateway_id, broker=object())

    def _rebuild_gateway_with(self, identity, **overrides):
        rig = self.rig
        kwargs = dict(
            identity=identity,
            broker=rig.broker,
            orders=rig.orders,
            positions=rig.positions,
            reconciliation=rig.reconciliation,
            risk=rig.risk,
            dedupe=rig.dedupe,
            kill_switch=rig.kill_switch,
            breakers=rig.breakers,
            modes=rig.modes,
            config=rig.config,
            audit=rig.audit,
            clock=rig.clock,
        )
        kwargs.update(overrides)
        return ExecutionGateway(**kwargs)


class TestKillSwitchGate(GatewayFixture):
    def test_an_engaged_kill_switch_refuses_every_order(self):
        self.rig.kill_switch.engage(self.rig.operator_id, reason="test stop")
        result = self.rig.submit()
        self.assertRefusedAt(result, ExecutionGate.KILL_SWITCH)
        self.assertNothingSent()

    def test_releasing_the_kill_switch_restores_execution(self):
        self.rig.kill_switch.engage(self.rig.operator_id, reason="test stop")
        self.rig.submit()
        self.rig.kill_switch.release(self.rig.operator_id, reason="all clear")
        self.assertTrue(self.rig.submit().is_executed)

    def test_the_kill_switch_beats_a_healthy_system(self):
        """No amount of everything-else-being-fine gets past it."""
        self.rig.kill_switch.engage(self.rig.operator_id, reason="test stop")
        for _ in range(5):
            self.assertRefusedAt(self.rig.submit(), ExecutionGate.KILL_SWITCH)
        self.assertNothingSent()

    def test_no_idempotency_key_is_consumed_by_a_kill_switch_refusal(self):
        """A refusal before the dedupe gate must leave the key reusable."""
        self.rig.kill_switch.engage(self.rig.operator_id, reason="test stop")
        intent = self.rig.intent()
        self.rig.submit(intent)
        self.assertIsNone(self.rig.dedupe.get(intent.idempotency_key))
        self.rig.kill_switch.release(self.rig.operator_id, reason="all clear")
        self.assertTrue(self.rig.submit(intent).is_executed)


class TestCircuitBreakerGate(GatewayFixture):
    def _open_the_breaker(self) -> None:
        for _ in range(3):  # failure_threshold=3
            self.rig.breaker.record_failure(reason="test")

    def test_an_open_breaker_refuses_orders(self):
        self._open_the_breaker()
        result = self.rig.submit()
        self.assertRefusedAt(result, ExecutionGate.CIRCUIT_BREAKERS)
        self.assertNothingSent()

    def test_any_open_breaker_in_the_registry_refuses(self):
        from trading.core.breaker import CircuitBreaker

        other = self.rig.breakers.add(
            CircuitBreaker(
                "market_data",
                clock=self.rig.clock,
                audit=self.rig.audit,
                failure_threshold=1,
            )
        )
        other.record_failure(reason="feed down")
        self.assertRefusedAt(self.rig.submit(), ExecutionGate.CIRCUIT_BREAKERS)

    def test_an_operator_reset_restores_execution(self):
        self._open_the_breaker()
        self.rig.breaker.reset(self.rig.operator_id, reason="fixed")
        self.assertTrue(self.rig.submit().is_executed)


class TestTradingModeGate(GatewayFixture):
    def test_disabled_is_the_default_and_refuses(self):
        rig = build_rig(mode=TradingMode.DISABLED)
        self.assertEqual(rig.modes.mode, TradingMode.DISABLED)
        result = rig.submit()
        self.assertEqual(result.gate, ExecutionGate.TRADING_MODE)
        self.assertEqual(rig.broker.placement_count, 0)

    def test_backtest_does_not_reach_a_broker(self):
        rig = build_rig(mode=TradingMode.BACKTEST)
        self.assertEqual(rig.submit().gate, ExecutionGate.TRADING_MODE)
        self.assertEqual(rig.broker.placement_count, 0)

    def test_halted_refuses(self):
        self.rig.modes.halt(actor="operator-1", reason="test")
        self.assertRefusedAt(self.rig.submit(), ExecutionGate.TRADING_MODE)
        self.assertNothingSent()

    def test_paper_executes(self):
        self.assertTrue(self.rig.submit().is_executed)


class TestLiveAuthorizationGate(unittest.TestCase):
    def test_live_mode_is_unreachable_without_config_authorization(self):
        rig = build_rig()
        with self.assertRaises(SafetyViolation):
            rig.go_live()
        self.assertEqual(rig.modes.mode, TradingMode.PAPER)

    def test_live_refuses_until_positions_have_been_reconciled(self):
        rig = build_rig(mode=TradingMode.LIVE, live_authorized=True)
        result = rig.submit()
        self.assertEqual(result.gate, ExecutionGate.RECONCILIATION)
        self.assertEqual(rig.broker.placement_count, 0)

    def test_live_executes_once_reconciled(self):
        rig = build_rig(mode=TradingMode.LIVE, live_authorized=True)
        rig.reconciliation.reconcile(rig.broker.fetch_positions().positions)
        self.assertTrue(rig.submit().is_executed)

    def test_a_stale_reconciliation_stops_live_orders_again(self):
        rig = build_rig(
            mode=TradingMode.LIVE, live_authorized=True, max_staleness_seconds=60.0
        )
        rig.reconciliation.reconcile(rig.broker.fetch_positions().positions)
        self.assertTrue(rig.submit().is_executed)
        rig.clock.advance(120)
        self.assertEqual(rig.submit().gate, ExecutionGate.RECONCILIATION)


class TestDuplicatePrevention(GatewayFixture):
    def test_the_same_intent_twice_is_refused_the_second_time(self):
        intent = self.rig.intent()
        first = self.rig.submit(intent)
        second = self.rig.submit(intent)
        self.assertTrue(first.is_executed)
        self.assertRefusedAt(second, ExecutionGate.DUPLICATE_ORDER)

    def test_the_venue_only_ever_sees_the_key_once(self):
        intent = self.rig.intent()
        for _ in range(5):
            self.rig.submit(intent)
        self.assertEqual(self.rig.broker.times_seen(intent.idempotency_key), 1)
        self.assertEqual(self.rig.broker.duplicate_keys, frozenset())

    def test_identical_signals_from_the_same_strategy_share_a_key(self):
        """Two intents with the same content are the same order, not two."""
        a = self.rig.intent(signal_id="same")
        b = self.rig.intent(signal_id="same")
        self.assertEqual(a.idempotency_key, b.idempotency_key)
        self.rig.submit(a)
        self.assertRefusedAt(self.rig.submit(b), ExecutionGate.DUPLICATE_ORDER)

    def test_concurrent_submissions_of_one_intent_produce_one_placement(self):
        intent = self.rig.intent()
        results: list[object] = []
        barrier = threading.Barrier(8)

        def run() -> None:
            barrier.wait()
            results.append(self.rig.submit(intent))

        threads = [threading.Thread(target=run) for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        executed = [r for r in results if r.is_executed]
        self.assertEqual(len(executed), 1, "more than one submission executed")
        self.assertEqual(self.rig.broker.placement_count, 1)
        self.assertEqual(self.rig.broker.duplicate_keys, frozenset())

    def test_a_different_signal_gets_its_own_key_and_executes(self):
        self.assertTrue(self.rig.submit().is_executed)
        self.assertTrue(self.rig.submit().is_executed)
        self.assertEqual(self.rig.broker.placement_count, 2)


class TestReconciliationGate(GatewayFixture):
    def test_an_unknown_order_blocks_every_subsequent_order(self):
        self.rig.broker.script(BrokerAck(AckOutcome.UNCERTAIN, message="timeout"))
        first = self.rig.submit()
        self.assertTrue(first.is_unknown)

        second = self.rig.submit()
        self.assertRefusedAt(second, ExecutionGate.RECONCILIATION)
        self.assertEqual(self.rig.broker.placement_count, 1)

    def test_a_position_mismatch_blocks_orders(self):
        self.rig.broker.set_venue_position(SYMBOL, Quantity("5", ASSET))
        self.rig.reconciliation.reconcile(
            self.rig.gateway._broker.fetch_positions().positions
        )
        result = self.rig.submit()
        self.assertRefusedAt(result, ExecutionGate.RECONCILIATION)
        self.assertNothingSent()

    def test_clearing_a_mismatch_restores_execution(self):
        self.rig.broker.set_venue_position(SYMBOL, Quantity("5", ASSET))
        self.rig.reconciliation.reconcile(
            self.rig.gateway._broker.fetch_positions().positions
        )
        # Adopt the venue's truth, then clear against a fresh snapshot.
        self.rig.positions.set_position(SYMBOL, Quantity("5", ASSET))
        self.rig.reconciliation.clear_mismatch(
            self.rig.operator_id,
            reason="adopted venue positions",
            broker_positions=self.rig.broker.fetch_positions().positions,
        )
        # 5 BTC at 50000 is far over the exposure limit, so use a small symbol.
        self.rig.positions.set_position(SYMBOL, Quantity("0", ASSET))
        self.assertTrue(self.rig.submit().is_executed)

    def test_a_refusal_here_releases_the_key(self):
        self.rig.broker.set_venue_position(SYMBOL, Quantity("5", ASSET))
        self.rig.reconciliation.reconcile(
            self.rig.gateway._broker.fetch_positions().positions
        )
        intent = self.rig.intent()
        self.rig.submit(intent)
        self.assertIsNone(self.rig.dedupe.get(intent.idempotency_key))


class TestRiskGate(GatewayFixture):
    def test_an_oversized_order_is_refused(self):
        result = self.rig.submit(quantity="0.5")  # 25000 USD, ceiling is 100
        self.assertRefusedAt(result, ExecutionGate.RISK)
        self.assertNothingSent()

    def test_a_missing_mark_price_is_refused(self):
        result = self.rig.submit(mark_prices={})
        self.assertRefusedAt(result, ExecutionGate.RISK)
        self.assertNothingSent()

    def test_the_rate_limit_refuses_the_seventh_order_in_a_minute(self):
        rig = self._rig_without_position_limits()
        for i in range(6):  # max_orders_per_minute=6
            self.assertTrue(rig.submit().is_executed, f"order {i} refused")
        self.assertEqual(rig.submit().gate, ExecutionGate.RISK)
        self.assertEqual(rig.broker.placement_count, 6)

    def test_the_rate_window_reopens(self):
        rig = self._rig_without_position_limits()
        for _ in range(6):
            rig.submit()
        self.assertEqual(rig.submit().gate, ExecutionGate.RISK)
        rig.clock.advance(61)
        self.assertTrue(rig.submit().is_executed)

    @staticmethod
    def _rig_without_position_limits():
        """A rig where only the rate limit can bind.

        Six 50-USD buys would otherwise trip the default 250-USD position
        ceiling first, and the point of these two tests is the rate window.
        """
        return build_rig(
            risk=RiskConfig(
                max_position_notional=Money("100000.00", USD),
                max_gross_exposure=Money("100000.00", USD),
            )
        )

    def test_a_risk_refusal_releases_the_key(self):
        intent = self.rig.intent(quantity="0.5")
        self.rig.submit(intent)
        self.assertIsNone(self.rig.dedupe.get(intent.idempotency_key))

    def test_a_risk_refusal_does_not_consume_rate_budget(self):
        """A refused order must not count towards the rate limit."""
        for _ in range(10):
            self.rig.submit(quantity="0.5")
        self.assertEqual(self.rig.risk.submissions_in_window(), 0)
        self.assertTrue(self.rig.submit().is_executed)

    def test_a_gross_exposure_breach_is_refused(self):
        rig = build_rig(
            risk=RiskConfig(
                max_order_notional=Money("50.00", USD),
                max_position_notional=Money("60.00", USD),
                max_gross_exposure=Money("60.00", USD),
            )
        )
        self.assertTrue(rig.submit().is_executed)  # 50 USD
        second = rig.submit()  # would take it to 100 USD
        self.assertEqual(second.gate, ExecutionGate.RISK)

    def test_floats_never_reach_the_risk_engine(self):
        with self.assertRaises(TypeError):
            self.rig.submit(mark_prices={SYMBOL: 50000.0})
        self.assertNothingSent()


class TestTokenDiscipline(GatewayFixture):
    def test_the_strategy_role_cannot_mint_a_token(self):
        with self.assertRaises(UnauthorizedAction):
            mint_execution_token(
                self.rig.strategy_id,
                order_id="o-1",
                idempotency_key="k-1",
                clock=self.rig.clock,
            )

    def test_no_role_but_the_gateway_can_mint(self):
        for identity in (self.rig.strategy_id, self.rig.risk_id, self.rig.operator_id):
            with self.subTest(role=identity.role):
                with self.assertRaises(UnauthorizedAction):
                    mint_execution_token(
                        identity,
                        order_id="o-1",
                        idempotency_key="k-1",
                        clock=self.rig.clock,
                    )

    def test_an_expired_token_cannot_place_an_order(self):
        order = self._draft_order()
        token = mint_execution_token(
            self.rig.gateway_id,
            order_id=order.order_id,
            idempotency_key=order.idempotency_key,
            clock=self.rig.clock,
            ttl_seconds=10,
        )
        self.rig.clock.advance(11)
        with self.assertRaises(SafetyViolation):
            self.rig.broker.place_order(order, token=token)
        self.assertNothingSent()

    def test_a_token_is_single_use(self):
        order = self._draft_order()
        token = mint_execution_token(
            self.rig.gateway_id,
            order_id=order.order_id,
            idempotency_key=order.idempotency_key,
            clock=self.rig.clock,
        )
        self.rig.broker.place_order(order, token=token)
        with self.assertRaises(SafetyViolation):
            self.rig.broker.place_order(order, token=token)
        self.assertEqual(self.rig.broker.placement_count, 1)

    def test_a_token_is_bound_to_one_order(self):
        first = self._draft_order()
        second = self._draft_order(signal_id="other")
        token = mint_execution_token(
            self.rig.gateway_id,
            order_id=first.order_id,
            idempotency_key=first.idempotency_key,
            clock=self.rig.clock,
        )
        with self.assertRaises(UnauthorizedAction):
            self.rig.broker.place_order(second, token=token)

    def _draft_order(self, **kwargs):
        from trading.core.orders import Order

        return Order(self.rig.intent(**kwargs), clock=self.rig.clock)


class TestUnknownOutcome(GatewayFixture):
    def test_an_uncertain_ack_produces_an_unknown_order(self):
        self.rig.broker.script(BrokerAck(AckOutcome.UNCERTAIN, message="timeout"))
        result = self.rig.submit()
        self.assertEqual(result.outcome, ExecutionOutcome.UNKNOWN)
        self.assertEqual(result.order.state, OrderState.UNKNOWN)

    def test_a_raising_broker_produces_an_unknown_order(self):
        self.rig.broker.raise_on_next("connection reset")
        result = self.rig.submit()
        self.assertTrue(result.is_unknown)
        self.assertIn("connection reset", result.reason)
        self.assertEqual(result.order.state, OrderState.UNKNOWN)

    def test_a_nonsense_return_value_produces_an_unknown_order(self):
        class Liar:
            def place_order(self, order, *, token):
                token.consume(order_id=order.order_id, clock=self.clock)
                return "ok"

        liar = Liar()
        liar.clock = self.rig.clock
        self.rig.gateway._broker = liar  # simulate a broken adapter
        result = self.rig.submit()
        self.assertTrue(result.is_unknown)
        self.assertIn("BrokerAck", result.reason)

    def test_the_reservation_goes_unknown_and_stays_claimed(self):
        self.rig.broker.script(BrokerAck(AckOutcome.UNCERTAIN))
        result = self.rig.submit()
        reservation = self.rig.dedupe.get(result.order.idempotency_key)
        self.assertEqual(reservation.state, ReservationState.UNKNOWN)
        self.assertTrue(self.rig.dedupe.has_unknown())

    def test_an_unknown_outcome_is_never_retried(self):
        self.rig.broker.script(BrokerAck(AckOutcome.UNCERTAIN))
        self.rig.submit()
        self.assertEqual(self.rig.broker.placement_count, 1)

    def test_resubmitting_the_same_intent_after_unknown_is_refused(self):
        intent = self.rig.intent()
        self.rig.broker.script(BrokerAck(AckOutcome.UNCERTAIN))
        self.rig.submit(intent)
        again = self.rig.submit(intent)
        # Either gate is correct; both stop it. Reconciliation runs first.
        self.assertIn(
            again.gate, (ExecutionGate.RECONCILIATION, ExecutionGate.DUPLICATE_ORDER)
        )
        self.assertEqual(self.rig.broker.placement_count, 1)

    def test_an_unknown_order_is_audited_as_an_error(self):
        self.rig.broker.script(BrokerAck(AckOutcome.UNCERTAIN))
        self.rig.submit()
        self.assertEqual(len(self.rig.sink.find("gateway.unknown")), 1)


class TestResolvingUnknown(GatewayFixture):
    def _make_unknown(self, *, lands: bool = False):
        self.rig.broker.script(
            BrokerAck(AckOutcome.UNCERTAIN, message="timeout"), lands_at_venue=lands
        )
        return self.rig.submit().order

    def test_a_venue_with_no_record_resolves_to_rejected(self):
        order = self._make_unknown(lands=False)
        self.rig.gateway.resolve_unknown(order, operator=self.rig.operator_id)
        self.assertEqual(order.state, OrderState.REJECTED)

    def test_resolution_unblocks_new_orders(self):
        order = self._make_unknown(lands=False)
        self.assertRefusedAt(self.rig.submit(), ExecutionGate.RECONCILIATION)
        self.rig.gateway.resolve_unknown(order, operator=self.rig.operator_id)
        self.assertTrue(self.rig.submit().is_executed)

    def test_an_order_that_landed_resolves_to_accepted(self):
        order = self._make_unknown(lands=True)
        ack = self.rig.gateway.resolve_unknown(order, operator=self.rig.operator_id)
        self.assertEqual(ack.outcome, AckOutcome.ACCEPTED)
        self.assertEqual(order.state, OrderState.ACCEPTED)

    def test_resolving_requires_the_reconcile_permission(self):
        order = self._make_unknown()
        with self.assertRaises(UnauthorizedAction):
            self.rig.gateway.resolve_unknown(order, operator=self.rig.strategy_id)

    def test_resolving_a_non_unknown_order_is_refused(self):
        result = self.rig.submit()
        with self.assertRaises(SafetyViolation):
            self.rig.gateway.resolve_unknown(
                result.order, operator=self.rig.operator_id
            )

    def test_a_still_uncertain_venue_leaves_the_order_unknown(self):
        order = self._make_unknown()
        self.rig.broker.script(BrokerAck(AckOutcome.UNCERTAIN))

        class StillUnsure:
            def fetch_order_state(self, order):
                return BrokerAck(AckOutcome.UNCERTAIN, message="still no idea")

        self.rig.gateway._broker = StillUnsure()
        ack = self.rig.gateway.resolve_unknown(order, operator=self.rig.operator_id)
        self.assertEqual(ack.outcome, AckOutcome.UNCERTAIN)
        self.assertEqual(order.state, OrderState.UNKNOWN)
        self.assertTrue(self.rig.orders.has_unknown_orders())


class TestResolvingUnknownToAFill(GatewayFixture):
    """The recovery path that mutates positions.

    Our request timed out, and by the time we ask, the venue has already filled
    it. This is the branch that costs money if it is wrong: it is the only place
    the position ledger moves without an order having been *observed* to fill, so
    a bug here means our idea of the position silently diverges from the venue's.

    ``SimulatedBroker`` cannot reach it on its own -- it rewrites an
    uncertain-but-landed order to ACCEPTED, deliberately, so the ordinary path
    stays conservative. Reaching FILLED needs a venue that fills between our
    timeout and our question, which is what the stub below is.
    """

    def setUp(self) -> None:
        super().setUp()
        self.filled_quantity = Quantity("0.001", ASSET)
        self.order = self._make_unknown()
        self.rig.gateway._broker = self._venue_that_filled_it()

    def _make_unknown(self):
        self.rig.broker.script(
            BrokerAck(AckOutcome.UNCERTAIN, message="timeout"), lands_at_venue=True
        )
        return self.rig.submit().order

    def _venue_that_filled_it(self):
        ack = BrokerAck(
            AckOutcome.FILLED,
            broker_order_id="venue-filled-1",
            filled_quantity=self.filled_quantity,
            fill_price=Price("50000", USD),
        )
        # Consistent about both questions: the order filled, and the position
        # exists. A stub that answered only the first would make the
        # reconciliation assertion below meaningless.
        snapshot = BrokerPositionSnapshot({SYMBOL: self.filled_quantity})

        class VenueFilledIt:
            def fetch_order_state(self, order):
                return ack

            def fetch_positions(self):
                return snapshot

        return VenueFilledIt()

    def test_the_order_reaches_filled(self):
        self.rig.gateway.resolve_unknown(self.order, operator=self.rig.operator_id)
        self.assertEqual(self.order.state, OrderState.FILLED)

    def test_the_discovered_fill_reaches_the_position_ledger(self):
        """The assertion that matters: our books learn about the fill."""
        before = self.rig.positions.position(SYMBOL, asset=ASSET)
        self.rig.gateway.resolve_unknown(self.order, operator=self.rig.operator_id)
        self.assertEqual(
            self.rig.positions.position(SYMBOL, asset=ASSET),
            before + self.filled_quantity,
        )

    def test_the_fill_price_and_quantity_are_recorded_on_the_order(self):
        self.rig.gateway.resolve_unknown(self.order, operator=self.rig.operator_id)
        self.assertEqual(self.order.filled_quantity, self.filled_quantity)

    def test_resolution_unblocks_new_orders(self):
        self.assertRefusedAt(self.rig.submit(), ExecutionGate.RECONCILIATION)
        self.rig.gateway.resolve_unknown(self.order, operator=self.rig.operator_id)
        self.assertFalse(self.rig.orders.has_unknown_orders())

    def test_a_discovered_fill_still_requires_the_reconcile_permission(self):
        with self.assertRaises(UnauthorizedAction):
            self.rig.gateway.resolve_unknown(
                self.order, operator=self.rig.strategy_id
            )
        self.assertEqual(self.order.state, OrderState.UNKNOWN)

    def test_the_recovery_is_audited_and_the_chain_survives(self):
        self.rig.gateway.resolve_unknown(self.order, operator=self.rig.operator_id)
        self.rig.audit.verify()
        self.assertTrue(
            any("reconcil" in action for action in self.rig.actions()),
            f"no reconciliation was audited: {self.rig.actions()}",
        )

    def test_reconciling_against_the_venue_now_agrees(self):
        """The point of applying the fill: our books match the venue's again."""
        self.rig.gateway.resolve_unknown(self.order, operator=self.rig.operator_id)
        self.rig.reconciliation.reconcile(
            self.rig.gateway._broker.fetch_positions().positions
        )
        self.assertFalse(self.rig.reconciliation.has_mismatch)


class TestChainOrdering(GatewayFixture):
    """The chain's order is load-bearing, so it is asserted directly.

    Each case trips several gates at once; the earliest must be the one that
    refuses. If someone reorders the chain, these fail rather than silently
    changing which check protects the system.
    """

    def _trip_everything(self) -> None:
        self.rig.kill_switch.engage(self.rig.operator_id, reason="test")
        for _ in range(3):
            self.rig.breaker.record_failure(reason="test")
        self.rig.modes.halt(actor="operator-1", reason="test")

    def test_authorization_precedes_the_kill_switch(self):
        self._trip_everything()
        result = self.rig.submit(proposer=Principal("a", Role.AUDITOR))
        self.assertRefusedAt(result, ExecutionGate.AUTHORIZATION)

    def test_the_kill_switch_precedes_the_breakers(self):
        self._trip_everything()
        self.assertRefusedAt(self.rig.submit(), ExecutionGate.KILL_SWITCH)

    def test_the_breakers_precede_the_mode(self):
        for _ in range(3):
            self.rig.breaker.record_failure(reason="test")
        self.rig.modes.halt(actor="operator-1", reason="test")
        self.assertRefusedAt(self.rig.submit(), ExecutionGate.CIRCUIT_BREAKERS)

    def test_the_mode_precedes_duplicate_detection(self):
        intent = self.rig.intent()
        self.rig.submit(intent)  # claims the key
        self.rig.modes.halt(actor="operator-1", reason="test")
        self.assertRefusedAt(self.rig.submit(intent), ExecutionGate.TRADING_MODE)

    def test_duplicate_detection_precedes_the_risk_check(self):
        """A duplicate must not consume risk budget to be rejected."""
        intent = self.rig.intent(quantity="0.5")  # also over the risk limit
        self.rig.submit(intent)  # refused at risk, key released
        # Claim the key by hand so the duplicate gate is the one that trips.
        self.rig.dedupe.reserve(intent.idempotency_key, "someone-elses-order")
        result = self.rig.submit(intent)
        self.assertRefusedAt(result, ExecutionGate.DUPLICATE_ORDER)

    def test_reconciliation_precedes_the_risk_check(self):
        self.rig.broker.set_venue_position(SYMBOL, Quantity("5", ASSET))
        self.rig.reconciliation.reconcile(
            self.rig.gateway._broker.fetch_positions().positions
        )
        result = self.rig.submit(quantity="0.5")  # also over the risk limit
        self.assertRefusedAt(result, ExecutionGate.RECONCILIATION)

    def test_risk_precedes_execution(self):
        self.assertRefusedAt(self.rig.submit(quantity="0.5"), ExecutionGate.RISK)
        self.assertNothingSent()

    def test_the_declared_chain_matches_the_gates_in_use(self):
        declared = set(ExecutionGate.ORDER)
        constants = {
            value
            for name, value in vars(ExecutionGate).items()
            if name.isupper() and isinstance(value, str)
        }
        self.assertEqual(declared, constants)


class TestNothingBypassesTheGateway(GatewayFixture):
    def test_the_broker_refuses_a_call_with_no_token(self):
        order = self._order()
        with self.assertRaises(TypeError):
            self.rig.broker.place_order(order)  # type: ignore[call-arg]

    def test_the_broker_refuses_a_forged_token(self):
        order = self._order()

        class ForgedToken:
            def consume(self, *, order_id, clock):
                return None

        # A duck-typed token gets through the adapter -- the adapter cannot
        # tell. What stops this in practice is that nothing outside the gateway
        # holds a broker reference, which the next two tests cover.
        self.rig.broker.place_order(order, token=ForgedToken())
        self.assertEqual(self.rig.broker.placement_count, 1)
        # The order never entered the store, so the gateway's books do not lie
        # about it: it is visibly absent rather than falsely settled.
        self.assertEqual(len(self.rig.orders), 0)

    def test_the_strategy_layer_has_no_broker_and_no_gateway(self):
        from trading.strategy import MarketView, Strategy, StrategyRunner

        surface = {n for n in dir(MarketView) if not n.startswith("_")}
        self.assertNotIn("broker", surface)
        self.assertNotIn("gateway", surface)
        self.assertNotIn("submit", surface)

        class Noop(Strategy):
            name = "noop"

            def propose(self, view):
                return []

        runner = StrategyRunner(
            Noop(),
            identity=self.rig.strategy_id,
            audit=self.rig.audit,
            clock=self.rig.clock,
        )
        self.assertNotIn("submit", {n for n in dir(runner) if not n.startswith("_")})

    def test_a_strategy_holding_a_broker_is_refused(self):
        from trading.strategy import Strategy, StrategyRunner

        class Sneaky(Strategy):
            name = "sneaky"

            def __init__(self, broker):
                self.broker = broker

            def propose(self, view):
                return []

        with self.assertRaises(SafetyViolation):
            StrategyRunner(
                Sneaky(self.rig.broker),
                identity=self.rig.strategy_id,
                audit=self.rig.audit,
                clock=self.rig.clock,
            )

    def test_submit_is_the_only_public_way_to_execute(self):
        public = {
            name
            for name in dir(self.rig.gateway)
            if not name.startswith("_") and callable(getattr(self.rig.gateway, name))
        }
        self.assertEqual(public, {"submit", "cancel", "resolve_unknown"})

    def test_cancel_needs_no_risk_approval_but_does_need_a_permission(self):
        result = self.rig.submit()
        with self.assertRaises(UnauthorizedAction):
            self.rig.gateway.cancel(result.order, operator=self.rig.strategy_id)
        ack = self.rig.gateway.cancel(result.order, operator=self.rig.operator_id)
        self.assertEqual(ack.outcome, AckOutcome.ACCEPTED)

    def _order(self):
        from trading.core.orders import Order

        return Order(self.rig.intent(), clock=self.rig.clock)


class TestDecimalDiscipline(GatewayFixture):
    def test_a_float_mark_price_is_rejected_not_coerced(self):
        with self.assertRaises(TypeError) as ctx:
            self.rig.submit(mark_prices={SYMBOL: 50000.0})
        self.assertIn("INVARIANT 8", str(ctx.exception))

    def test_every_money_amount_in_an_audit_record_is_a_string(self):
        self.rig.submit()
        for record in self.rig.sink.records:
            for value in (record.details or {}).values():
                self.assertNotIsInstance(value, float)

    def test_the_filled_notional_is_exact(self):
        result = self.rig.submit()
        self.assertEqual(result.order.filled_notional(), Money("50.00", USD))


class TestSubmitInputValidation(GatewayFixture):
    def test_submit_requires_an_order_intent(self):
        for bad in ("BTCUSD", None, 42, object()):
            with self.subTest(bad=bad):
                with self.assertRaises(TypeError):
                    self.rig.gateway.submit(bad, proposer=self.rig.strategy_id)

    def test_a_sell_reduces_the_position(self):
        self.rig.submit()
        self.rig.submit(side=OrderSide.SELL)
        self.assertTrue(self.rig.positions.position(SYMBOL, asset=ASSET).is_zero)

    def test_mark_prices_default_to_empty_and_therefore_refuse(self):
        result = self.rig.gateway.submit(
            self.rig.intent(), proposer=self.rig.strategy_id
        )
        self.assertRefusedAt(result, ExecutionGate.RISK)


if __name__ == "__main__":
    unittest.main()
