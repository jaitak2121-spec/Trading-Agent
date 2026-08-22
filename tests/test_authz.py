"""Tests for authorization rules and execution tokens.

Covers INVARIANT 3: strategy code cannot directly execute an order.
"""

from __future__ import annotations

import pickle
import unittest

from trading.core.authz import (
    PERMISSIONS,
    Action,
    ExecutionToken,
    Principal,
    Role,
    assert_matrix_invariants,
    authorize,
    is_authorized,
    mint_execution_token,
)
from trading.core.clock import ManualClock
from trading.core.errors import StrategyExecutionForbidden, UnauthorizedAction

STRATEGY = Principal("momentum-v1", Role.STRATEGY)
RISK = Principal("risk-1", Role.RISK_MANAGER)
GATEWAY = Principal("gateway-1", Role.EXECUTION_GATEWAY)
OPERATOR = Principal("alice", Role.OPERATOR)
AUDITOR = Principal("audit-bot", Role.AUDITOR)
SYSTEM = Principal("reconciler", Role.SYSTEM)


class TestStrategyCannotExecute(unittest.TestCase):
    """INVARIANT 3, from every angle we can reach."""

    def test_matrix_denies_execute_to_strategy(self):
        self.assertNotIn(Action.EXECUTE_ORDER, PERMISSIONS[Role.STRATEGY])
        self.assertFalse(is_authorized(STRATEGY, Action.EXECUTE_ORDER))

    def test_authorize_raises_dedicated_exception(self):
        with self.assertRaises(StrategyExecutionForbidden) as ctx:
            authorize(STRATEGY, Action.EXECUTE_ORDER)
        self.assertIn("INVARIANT 3", str(ctx.exception))

    def test_strategy_execution_forbidden_is_an_unauthorized_action(self):
        # So a caller catching UnauthorizedAction cannot miss the worse case.
        self.assertTrue(issubclass(StrategyExecutionForbidden, UnauthorizedAction))

    def test_strategy_cannot_mint_a_token(self):
        with self.assertRaises(StrategyExecutionForbidden):
            mint_execution_token(
                STRATEGY,
                order_id="ORD-1",
                idempotency_key="KEY-1",
                clock=ManualClock(),
            )

    def test_strategy_cannot_approve_its_own_risk_check(self):
        # INVARIANT 4 support: the approver must be a different role.
        self.assertNotIn(Action.APPROVE_ORDER, PERMISSIONS[Role.STRATEGY])
        with self.assertRaises(UnauthorizedAction):
            authorize(STRATEGY, Action.APPROVE_ORDER)

    def test_strategy_may_propose(self):
        authorize(STRATEGY, Action.PROPOSE_ORDER)  # must not raise

    def test_token_cannot_be_constructed_directly(self):
        with self.assertRaises(UnauthorizedAction) as ctx:
            ExecutionToken(
                order_id="ORD-1",
                idempotency_key="KEY-1",
                issued_at=ManualClock().now(),
                ttl_seconds=30,
                issuer="attacker",
                mint_key=object(),
            )
        self.assertIn("INVARIANT 3", str(ctx.exception))

    def test_token_cannot_be_constructed_with_none_key(self):
        with self.assertRaises(UnauthorizedAction):
            ExecutionToken(
                order_id="ORD-1",
                idempotency_key="KEY-1",
                issued_at=ManualClock().now(),
                ttl_seconds=30,
                issuer="attacker",
                mint_key=None,
            )


class TestPermissionMatrix(unittest.TestCase):
    def test_matrix_invariants_hold(self):
        assert_matrix_invariants()  # must not raise

    def test_only_gateway_may_execute(self):
        executors = [r for r, a in PERMISSIONS.items() if Action.EXECUTE_ORDER in a]
        self.assertEqual(executors, [Role.EXECUTION_GATEWAY])

    def test_only_operator_may_release_kill_switch(self):
        releasers = [r for r, a in PERMISSIONS.items() if Action.RELEASE_KILL_SWITCH in a]
        self.assertEqual(releasers, [Role.OPERATOR])

    def test_everyone_operational_may_engage_kill_switch(self):
        # Stopping must never be gated on privilege.
        for principal in (STRATEGY, RISK, GATEWAY, OPERATOR, SYSTEM):
            with self.subTest(principal=principal):
                authorize(principal, Action.ENGAGE_KILL_SWITCH)

    def test_auditor_is_read_only(self):
        self.assertEqual(PERMISSIONS[Role.AUDITOR], frozenset({Action.READ_AUDIT}))
        for action in Action:
            if action is Action.READ_AUDIT:
                continue
            with self.subTest(action=action):
                with self.assertRaises(UnauthorizedAction):
                    authorize(AUDITOR, action)

    def test_only_operator_may_change_mode(self):
        changers = [r for r, a in PERMISSIONS.items() if Action.CHANGE_MODE in a]
        self.assertEqual(changers, [Role.OPERATOR])

    def test_deny_by_default_for_every_role(self):
        # No role holds every action.
        for role, actions in PERMISSIONS.items():
            with self.subTest(role=role):
                self.assertNotEqual(actions, frozenset(Action))

    def test_every_role_present(self):
        for role in Role:
            self.assertIn(role, PERMISSIONS)

    def test_matrix_invariant_check_detects_bad_table(self):
        # Prove the guard actually guards, by breaking the table temporarily.
        import trading.core.authz as authz_module

        original = authz_module.PERMISSIONS
        try:
            broken = dict(original)
            broken[Role.STRATEGY] = frozenset({Action.EXECUTE_ORDER})
            authz_module.PERMISSIONS = broken  # type: ignore[assignment]
            with self.assertRaises(AssertionError):
                authz_module.assert_matrix_invariants()
        finally:
            authz_module.PERMISSIONS = original  # type: ignore[assignment]


class TestPrincipalValidation(unittest.TestCase):
    def test_empty_id_rejected(self):
        with self.assertRaises(ValueError):
            Principal("", Role.OPERATOR)
        with self.assertRaises(ValueError):
            Principal("   ", Role.OPERATOR)

    def test_non_role_rejected(self):
        with self.assertRaises(TypeError):
            Principal("bob", "operator")  # type: ignore[arg-type]

    def test_str_shows_id_and_role(self):
        self.assertEqual(str(OPERATOR), "alice[operator]")

    def test_authorize_type_checks(self):
        with self.assertRaises(TypeError):
            is_authorized("alice", Action.EXECUTE_ORDER)  # type: ignore[arg-type]
        with self.assertRaises(TypeError):
            is_authorized(OPERATOR, "execute_order")  # type: ignore[arg-type]


class TestExecutionToken(unittest.TestCase):
    def setUp(self):
        self.clock = ManualClock()

    def mint(self, order_id="ORD-1", key="KEY-1", ttl=30):
        return mint_execution_token(
            GATEWAY,
            order_id=order_id,
            idempotency_key=key,
            clock=self.clock,
            ttl_seconds=ttl,
        )

    def test_gateway_can_mint(self):
        token = self.mint()
        self.assertEqual(token.order_id, "ORD-1")
        self.assertEqual(token.idempotency_key, "KEY-1")
        self.assertEqual(token.issuer, "gateway-1")
        self.assertFalse(token.is_consumed)

    def test_consume_succeeds_once(self):
        token = self.mint()
        token.consume(order_id="ORD-1", clock=self.clock)
        self.assertTrue(token.is_consumed)

    def test_consume_twice_is_rejected(self):
        token = self.mint()
        token.consume(order_id="ORD-1", clock=self.clock)
        with self.assertRaises(UnauthorizedAction) as ctx:
            token.consume(order_id="ORD-1", clock=self.clock)
        self.assertIn("single-use", str(ctx.exception))

    def test_token_bound_to_its_order(self):
        token = self.mint(order_id="ORD-1")
        with self.assertRaises(UnauthorizedAction) as ctx:
            token.consume(order_id="ORD-2", clock=self.clock)
        self.assertIn("issued for order ORD-1", str(ctx.exception))
        self.assertFalse(token.is_consumed)

    def test_expired_token_is_rejected(self):
        token = self.mint(ttl=30)
        self.clock.advance(31)
        self.assertTrue(token.is_expired(self.clock))
        with self.assertRaises(UnauthorizedAction) as ctx:
            token.consume(order_id="ORD-1", clock=self.clock)
        self.assertIn("expired", str(ctx.exception))

    def test_token_valid_up_to_ttl(self):
        token = self.mint(ttl=30)
        self.clock.advance(30)
        self.assertFalse(token.is_expired(self.clock))
        token.consume(order_id="ORD-1", clock=self.clock)

    def test_clock_moved_backwards_invalidates_token(self):
        # A negative elapsed time means something is wrong with time itself;
        # refuse rather than trust it.
        import datetime as dt

        token = self.mint()
        self.clock.set_wall_clock(self.clock.now() - dt.timedelta(seconds=10))
        self.assertTrue(token.is_expired(self.clock))

    def test_token_ids_are_unique(self):
        ids = {self.mint(order_id=f"ORD-{i}").token_id for i in range(50)}
        self.assertEqual(len(ids), 50)

    def test_token_cannot_be_pickled(self):
        with self.assertRaises(TypeError):
            pickle.dumps(self.mint())

    def test_repr_does_not_leak_full_token_id(self):
        token = self.mint()
        self.assertNotIn(token.token_id, repr(token))
        self.assertIn("ORD-1", repr(token))

    def test_mint_validates_arguments(self):
        with self.assertRaises(ValueError):
            mint_execution_token(GATEWAY, order_id="", idempotency_key="K", clock=self.clock)
        with self.assertRaises(ValueError):
            mint_execution_token(GATEWAY, order_id="O", idempotency_key="", clock=self.clock)
        with self.assertRaises(ValueError):
            mint_execution_token(
                GATEWAY, order_id="O", idempotency_key="K", clock=self.clock, ttl_seconds=0
            )
        with self.assertRaises(ValueError):
            mint_execution_token(
                GATEWAY, order_id="O", idempotency_key="K", clock=self.clock, ttl_seconds=True
            )

    def test_other_roles_cannot_mint(self):
        for principal in (RISK, OPERATOR, AUDITOR, SYSTEM):
            with self.subTest(principal=principal):
                with self.assertRaises(UnauthorizedAction):
                    mint_execution_token(
                        principal,
                        order_id="ORD-1",
                        idempotency_key="KEY-1",
                        clock=self.clock,
                    )

    def test_concurrent_consume_yields_exactly_one_success(self):
        import threading

        token = self.mint()
        successes = []
        failures = []
        barrier = threading.Barrier(8)

        def attempt():
            barrier.wait()
            try:
                token.consume(order_id="ORD-1", clock=self.clock)
                successes.append(1)
            except UnauthorizedAction:
                failures.append(1)

        threads = [threading.Thread(target=attempt) for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(len(successes), 1)
        self.assertEqual(len(failures), 7)


if __name__ == "__main__":
    unittest.main()
