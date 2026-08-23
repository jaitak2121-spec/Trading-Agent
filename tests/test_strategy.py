"""Tests for the strategy layer.

Covers INVARIANT 3 from the proposing side: a strategy decides *what* to trade
and never *whether* it may. The gateway tests prove the other half -- that an
intent cannot reach a venue without passing the chain. These prove that the
strategy layer never gets a chance to try.

Three separate mechanisms are exercised here, because the separation does not
rest on any one of them:

1. :class:`MarketView` gives a strategy values, never collaborators.
2. :meth:`Strategy.__init_subclass__` rejects an execution method at
   class-definition time, so the mistake cannot even be imported.
3. :class:`StrategyRunner` refuses an instance holding an execution surface.
"""

from __future__ import annotations

import unittest
from datetime import datetime, timezone
from decimal import Decimal

from trading.adapters.memory import SimulatedBroker
from trading.core.audit import AuditCategory, AuditLog, AuditOutcome, InMemoryAuditSink
from trading.core.authz import (
    Action,
    Principal,
    Role,
    is_authorized,
    mint_execution_token,
)
from trading.core.clock import ManualClock
from trading.core.errors import SafetyViolation, UnauthorizedAction
from trading.core.money import USD, Money, Price, Quantity
from trading.core.orders import OrderIntent, OrderSide, OrderType
from trading.ports.broker import BrokerPort
from trading.strategy import MarketView, Strategy, StrategyRunner
from trading.strategy.base import _EXECUTION_ATTRIBUTES

SYMBOL = "BTCUSD"
ASSET = "BTC"
PRICE = Price("50000", USD)
EQUITY = Money(Decimal("10000.00"), USD)


def a_view(**overrides: object) -> MarketView:
    """A well-formed view, with fields replaceable one at a time."""
    fields: dict[str, object] = {
        "as_of": datetime(2026, 1, 1, tzinfo=timezone.utc),
        "equity": EQUITY,
        "prices": {SYMBOL: PRICE},
        "positions": {SYMBOL: Quantity("0.25", ASSET)},
    }
    fields.update(overrides)
    return MarketView(**fields)  # type: ignore[arg-type]


def an_intent(signal_id: str = "sig-1") -> OrderIntent:
    return OrderIntent(
        strategy_id="strat-1",
        signal_id=signal_id,
        symbol=SYMBOL,
        side=OrderSide.BUY,
        order_type=OrderType.MARKET,
        quantity=Quantity("0.001", ASSET),
    )


class Quiet(Strategy):
    """Proposes nothing. The base case."""

    name = "quiet"

    def propose(self, view):
        return []


class Eager(Strategy):
    """Proposes one intent per call, with a fresh signal id each time."""

    name = "eager"

    def __init__(self):
        self._n = 0

    def propose(self, view):
        self._n += 1
        return [an_intent(f"sig-{self._n}")]


class Broken(Strategy):
    """Raises instead of proposing."""

    name = "broken"

    def propose(self, view):
        raise ZeroDivisionError("bad arithmetic in signal generation")


class Confused(Strategy):
    """Returns something that is not an OrderIntent."""

    name = "confused"

    def propose(self, view):
        return ["BUY 1 BTC"]


class RunnerCase(unittest.TestCase):
    """Shared wiring: a strategy identity, an audit log, a manual clock."""

    def setUp(self):
        self.clock = ManualClock()
        self.sink = InMemoryAuditSink()
        self.audit = AuditLog(self.sink, clock=self.clock)
        self.identity = Principal("strategy-1", Role.STRATEGY)

    def runner(self, strategy: Strategy) -> StrategyRunner:
        return StrategyRunner(
            strategy, identity=self.identity, audit=self.audit, clock=self.clock
        )

    def actions(self) -> list[str]:
        return [record.action for record in self.sink.records]


# ---------------------------------------------------------------------------
# MarketView
# ---------------------------------------------------------------------------


class TestMarketViewHoldsValuesOnly(unittest.TestCase):
    """INVARIANT 3: a strategy's whole input is numbers, not collaborators."""

    def test_a_well_formed_view_is_accepted(self):
        view = a_view()
        self.assertEqual(view.equity, EQUITY)
        self.assertEqual(view.price(SYMBOL), PRICE)

    def test_equity_must_be_money(self):
        with self.assertRaises(TypeError):
            a_view(equity=Decimal("10000"))
        with self.assertRaises(TypeError):
            a_view(equity=10000)
        with self.assertRaises(TypeError):
            a_view(equity="10000.00")

    def test_a_float_equity_is_rejected(self):
        """INVARIANT 8 at the strategy boundary."""
        with self.assertRaises(TypeError):
            a_view(equity=10000.0)

    def test_prices_must_be_price_objects(self):
        with self.assertRaises(TypeError) as ctx:
            a_view(prices={SYMBOL: Decimal("50000")})
        self.assertIn(SYMBOL, str(ctx.exception))

    def test_a_float_price_is_rejected(self):
        with self.assertRaises(TypeError):
            a_view(prices={SYMBOL: 50000.0})

    def test_positions_must_be_quantity_objects(self):
        with self.assertRaises(TypeError) as ctx:
            a_view(positions={SYMBOL: Decimal("0.25")})
        self.assertIn(SYMBOL, str(ctx.exception))

    def test_a_float_position_is_rejected(self):
        with self.assertRaises(TypeError):
            a_view(positions={SYMBOL: 0.25})

    def test_the_view_is_immutable(self):
        view = a_view()
        for attribute, value in (
            ("equity", Money(Decimal("1.00"), USD)),
            ("prices", {}),
            ("positions", {}),
            ("as_of", datetime(2027, 1, 1, tzinfo=timezone.utc)),
        ):
            with self.subTest(attribute=attribute):
                with self.assertRaises(Exception):
                    setattr(view, attribute, value)

    def test_prices_and_positions_default_to_empty(self):
        view = MarketView(
            as_of=datetime(2026, 1, 1, tzinfo=timezone.utc), equity=EQUITY
        )
        self.assertEqual(dict(view.prices), {})
        self.assertEqual(dict(view.positions), {})

    def test_a_missing_price_is_none_not_a_guess(self):
        """A strategy must handle absence; inventing a price would be worse."""
        self.assertIsNone(a_view().price("ETHUSD"))

    def test_a_missing_position_reads_as_zero_of_the_asked_asset(self):
        held = a_view().position("ETHUSD", asset="ETH")
        self.assertTrue(held.is_zero)
        self.assertEqual(held.asset, "ETH")

    def test_a_held_position_is_returned_unchanged(self):
        held = a_view().position(SYMBOL, asset=ASSET)
        self.assertEqual(held.amount, Decimal("0.25"))
        self.assertEqual(held.asset, ASSET)

    def test_is_flat_is_true_for_an_unknown_symbol(self):
        self.assertTrue(a_view().is_flat("ETHUSD"))

    def test_is_flat_is_true_for_an_explicit_zero(self):
        view = a_view(positions={SYMBOL: Quantity.zero(ASSET)})
        self.assertTrue(view.is_flat(SYMBOL))

    def test_is_flat_is_false_while_a_position_is_held(self):
        self.assertFalse(a_view().is_flat(SYMBOL))

    def test_the_view_exposes_no_execution_surface(self):
        """Nothing reachable on a view can act."""
        surface = {name for name in dir(MarketView) if not name.startswith("_")}
        self.assertEqual(surface & _EXECUTION_ATTRIBUTES, set())
        for name in ("broker", "gateway", "orders", "token", "audit"):
            with self.subTest(attribute=name):
                self.assertFalse(hasattr(a_view(), name))


# ---------------------------------------------------------------------------
# Strategy subclass guard
# ---------------------------------------------------------------------------


class TestStrategyClassCannotDefineExecution(unittest.TestCase):
    """INVARIANT 3, enforced at class-definition time."""

    def test_a_strategy_defining_place_order_cannot_be_defined(self):
        with self.assertRaises(SafetyViolation) as ctx:

            class Rogue(Strategy):
                def propose(self, view):
                    return []

                def place_order(self, intent):
                    return None

        self.assertIn("INVARIANT 3", str(ctx.exception))
        self.assertIn("place_order", str(ctx.exception))

    def test_every_forbidden_name_is_rejected(self):
        for forbidden in sorted(_EXECUTION_ATTRIBUTES):
            with self.subTest(attribute=forbidden):
                with self.assertRaises(SafetyViolation):
                    type(
                        "Rogue",
                        (Strategy,),
                        {
                            "propose": lambda self, view: [],
                            forbidden: lambda self, *a, **k: None,
                        },
                    )

    def test_a_non_callable_attribute_with_a_forbidden_name_is_also_rejected(self):
        """The check is on the name, not on callability -- deliberately blunt."""
        with self.assertRaises(SafetyViolation):
            type("Rogue", (Strategy,), {"propose": lambda s, v: [], "submit": 1})

    def test_all_offending_names_are_reported_at_once(self):
        with self.assertRaises(SafetyViolation) as ctx:
            type(
                "Rogue",
                (Strategy,),
                {
                    "propose": lambda s, v: [],
                    "submit": lambda s: None,
                    "execute": lambda s: None,
                },
            )
        message = str(ctx.exception)
        self.assertIn("submit", message)
        self.assertIn("execute", message)

    def test_a_well_behaved_strategy_defines_cleanly(self):
        class Fine(Strategy):
            name = "fine"

            def propose(self, view):
                return []

        self.assertEqual(Fine().name, "fine")

    def test_propose_is_abstract(self):
        class Incomplete(Strategy):
            pass

        with self.assertRaises(TypeError):
            Incomplete()  # type: ignore[abstract]

    def test_name_defaults_to_unnamed(self):
        class Anonymous(Strategy):
            def propose(self, view):
                return []

        self.assertEqual(Anonymous().name, "unnamed")


# ---------------------------------------------------------------------------
# StrategyRunner construction
# ---------------------------------------------------------------------------


class TestRunnerRefusesAnExecutionSurface(RunnerCase):
    """INVARIANT 3: the tripwire for a broker passed into a constructor."""

    def test_a_clean_strategy_is_accepted(self):
        runner = self.runner(Quiet())
        self.assertEqual(runner.strategy_name, "quiet")

    def test_a_strategy_holding_a_broker_is_refused(self):
        class Smuggler(Strategy):
            def __init__(self, broker):
                self.broker = broker

            def propose(self, view):
                return []

        broker = SimulatedBroker(clock=self.clock)
        with self.assertRaises(SafetyViolation) as ctx:
            self.runner(Smuggler(broker))
        message = str(ctx.exception)
        self.assertIn("INVARIANT 3", message)
        self.assertIn("broker", message)

    def test_a_strategy_holding_an_execution_token_is_refused(self):
        gateway_id = Principal("gateway-1", Role.EXECUTION_GATEWAY)
        token = mint_execution_token(
            gateway_id,
            order_id="ORD-1",
            idempotency_key="key-1",
            clock=self.clock,
        )

        class Hoarder(Strategy):
            def __init__(self, token):
                self.token = token

            def propose(self, view):
                return []

        with self.assertRaises(SafetyViolation) as ctx:
            self.runner(Hoarder(token))
        self.assertIn("INVARIANT 3", str(ctx.exception))
        self.assertIn("ExecutionToken", str(ctx.exception))

    def test_a_strategy_holding_a_duck_typed_executor_is_refused(self):
        """Not a BrokerPort, but it walks like one."""

        class LooksLikeAVenue:
            def place_order(self, order, token):
                return None

        class Smuggler(Strategy):
            def __init__(self):
                self.helper = LooksLikeAVenue()

            def propose(self, view):
                return []

        with self.assertRaises(SafetyViolation) as ctx:
            self.runner(Smuggler())
        message = str(ctx.exception)
        self.assertIn("INVARIANT 3", message)
        self.assertIn("place_order", message)
        self.assertIn("helper", message)

    def test_every_forbidden_method_name_trips_the_tripwire(self):
        for forbidden in sorted(_EXECUTION_ATTRIBUTES):
            with self.subTest(attribute=forbidden):
                helper = type(
                    "Helper", (), {forbidden: lambda self, *a, **k: None}
                )()
                strategy = Quiet()
                strategy.helper = helper  # type: ignore[attr-defined]
                with self.assertRaises(SafetyViolation) as ctx:
                    self.runner(strategy)
                self.assertIn(forbidden, str(ctx.exception))

    def test_an_inert_attribute_is_not_mistaken_for_an_executor(self):
        """The tripwire must not fire on ordinary state."""
        strategy = Quiet()
        strategy.lookback = 20  # type: ignore[attr-defined]
        strategy.threshold = Decimal("0.02")  # type: ignore[attr-defined]
        strategy.last_price = PRICE  # type: ignore[attr-defined]
        strategy.label = "submit"  # a *value* named like a method  # type: ignore[attr-defined]
        self.assertEqual(self.runner(strategy).strategy_name, "quiet")

    def test_a_non_strategy_is_refused(self):
        for candidate in (object(), None, "quiet", Quiet):
            with self.subTest(candidate=candidate):
                with self.assertRaises(TypeError):
                    self.runner(candidate)  # type: ignore[arg-type]

    def test_an_identity_that_may_not_propose_is_refused(self):
        for role in (
            Role.RISK_MANAGER,
            Role.EXECUTION_GATEWAY,
            Role.OPERATOR,
            Role.AUDITOR,
            Role.SYSTEM,
        ):
            with self.subTest(role=role.name):
                self.assertFalse(
                    is_authorized(Principal("p", role), Action.PROPOSE_ORDER)
                )
                with self.assertRaises(UnauthorizedAction):
                    StrategyRunner(
                        Quiet(),
                        identity=Principal("p", role),
                        audit=self.audit,
                        clock=self.clock,
                    )

    def test_the_runner_needs_only_the_propose_permission(self):
        """A STRATEGY principal is sufficient, and it cannot execute."""
        self.assertTrue(is_authorized(self.identity, Action.PROPOSE_ORDER))
        self.assertFalse(is_authorized(self.identity, Action.EXECUTE_ORDER))
        self.assertFalse(is_authorized(self.identity, Action.APPROVE_ORDER))
        self.runner(Quiet())


# ---------------------------------------------------------------------------
# StrategyRunner.propose
# ---------------------------------------------------------------------------


class TestRunnerProposes(RunnerCase):
    def test_intents_are_returned_to_the_caller(self):
        intents = self.runner(Eager()).propose(a_view())
        self.assertEqual(len(intents), 1)
        self.assertIsInstance(intents[0], OrderIntent)

    def test_the_result_is_a_plain_list(self):
        self.assertIsInstance(self.runner(Eager()).propose(a_view()), list)

    def test_proposing_nothing_is_legitimate(self):
        self.assertEqual(self.runner(Quiet()).propose(a_view()), [])

    def test_proposing_nothing_audits_nothing(self):
        self.runner(Quiet()).propose(a_view())
        self.assertEqual(self.actions(), [])

    def test_every_proposal_is_audited(self):
        runner = self.runner(Eager())
        runner.propose(a_view())
        runner.propose(a_view())
        self.assertEqual(self.actions(), ["strategy.proposed"] * 2)

    def test_the_proposal_record_is_a_signal_not_an_order(self):
        """Nothing exists yet, so the category must not say ORDER."""
        self.runner(Eager()).propose(a_view())
        record = self.sink.records[0]
        self.assertEqual(record.category, AuditCategory.SIGNAL.value)
        self.assertEqual(record.outcome, AuditOutcome.INFO.value)

    def test_the_proposal_record_names_the_proposing_identity(self):
        self.runner(Eager()).propose(a_view())
        self.assertEqual(self.sink.records[0].actor, "strategy-1")

    def test_the_proposal_record_carries_the_idempotency_key(self):
        intents = self.runner(Eager()).propose(a_view())
        details = self.sink.records[0].details
        self.assertEqual(details["idempotency_key"], intents[0].idempotency_key)
        self.assertEqual(details["symbol"], SYMBOL)
        self.assertEqual(details["side"], OrderSide.BUY.value)

    def test_the_proposal_record_carries_no_float(self):
        """INVARIANT 8 holds in the signal trail too."""
        self.runner(Eager()).propose(a_view())
        for key, value in self.sink.records[0].details.items():
            with self.subTest(key=key):
                self.assertNotIsInstance(value, float)

    def test_proposals_are_audited_before_the_caller_sees_them(self):
        """INVARIANT 13: the record exists by the time propose() returns."""
        runner = self.runner(Eager())
        self.assertEqual(len(self.sink), 0)
        runner.propose(a_view())
        self.assertEqual(len(self.sink), 1)

    def test_the_view_must_be_a_market_view(self):
        runner = self.runner(Eager())
        for candidate in (None, {}, "view", a_view):
            with self.subTest(candidate=candidate):
                with self.assertRaises(TypeError):
                    runner.propose(candidate)  # type: ignore[arg-type]

    def test_a_non_intent_in_the_output_is_refused(self):
        with self.assertRaises(SafetyViolation) as ctx:
            self.runner(Confused()).propose(a_view())
        self.assertIn("OrderIntent", str(ctx.exception))
        self.assertIn("confused", str(ctx.exception))

    def test_a_non_intent_in_the_output_audits_nothing(self):
        """Refuse the whole batch: a half-audited proposal is worse than none."""
        with self.assertRaises(SafetyViolation):
            self.runner(Confused()).propose(a_view())
        self.assertEqual(self.actions(), [])

    def test_a_broken_strategy_is_audited_as_an_error_and_re_raised(self):
        with self.assertRaises(ZeroDivisionError):
            self.runner(Broken()).propose(a_view())
        record = self.sink.records[0]
        self.assertEqual(record.action, "strategy.failed")
        self.assertEqual(record.category, AuditCategory.SIGNAL.value)
        self.assertEqual(record.outcome, AuditOutcome.ERROR.value)
        self.assertEqual(record.details["strategy"], "broken")
        self.assertIn("bad arithmetic", record.details["error"])

    def test_a_broken_strategy_proposes_nothing(self):
        with self.assertRaises(ZeroDivisionError):
            self.runner(Broken()).propose(a_view())
        self.assertNotIn("strategy.proposed", self.actions())

    def test_strategy_name_is_reported(self):
        self.assertEqual(self.runner(Broken()).strategy_name, "broken")


# ---------------------------------------------------------------------------
# The runner is not a back door
# ---------------------------------------------------------------------------


class TestRunnerCannotExecute(RunnerCase):
    """INVARIANT 3: the runner is the proposing side, and only that."""

    def test_the_runner_exposes_no_execution_method(self):
        surface = {name for name in dir(StrategyRunner) if not name.startswith("_")}
        self.assertEqual(surface & _EXECUTION_ATTRIBUTES, set())
        self.assertEqual(surface, {"propose", "strategy_name"})

    def test_the_runner_holds_no_broker_and_no_gateway(self):
        runner = self.runner(Eager())
        for name in ("broker", "gateway", "orders", "positions", "risk"):
            with self.subTest(attribute=name):
                self.assertFalse(hasattr(runner, name))
        held = {type(value).__name__ for value in vars(runner).values()}
        self.assertNotIn("SimulatedBroker", held)
        self.assertNotIn("ExecutionGateway", held)

    def test_the_runners_identity_cannot_mint_a_token(self):
        with self.assertRaises(UnauthorizedAction):
            mint_execution_token(
                self.identity,
                order_id="ORD-1",
                idempotency_key="key-1",
                clock=self.clock,
            )

    def test_an_intent_carries_no_way_to_act(self):
        intent = self.runner(Eager()).propose(a_view())[0]
        surface = {name for name in dir(intent) if not name.startswith("_")}
        self.assertEqual(surface & _EXECUTION_ATTRIBUTES, set())
        for name in ("broker", "gateway", "token"):
            with self.subTest(attribute=name):
                self.assertFalse(hasattr(intent, name))

    def test_the_strategy_module_imports_nothing_that_can_execute(self):
        """No gateway and no concrete broker is reachable from the module.

        ``test_core_purity.py`` enforces the layering rule across the kernel by
        AST. This checks the narrower claim this module is about: even the names
        bound inside :mod:`trading.strategy.base` give a strategy nothing it
        could send an order through. ``BrokerPort`` and ``ExecutionToken`` are
        expected here -- the runner imports them in order to *reject* them.
        """
        import ast
        import pathlib

        import trading.strategy.base as base

        tree = ast.parse(pathlib.Path(base.__file__).read_text(encoding="utf-8"))
        imported: list[str] = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported.extend(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom):
                imported.append("." * node.level + (node.module or ""))

        for name in imported:
            self.assertNotIn("adapters", name, f"strategy reaches an adapter: {name}")
            self.assertNotIn("gateway", name, f"strategy reaches the gateway: {name}")

        # Nothing bound in the module is a usable broker. The port ABC is, and
        # must be; an instantiable implementation would not be.
        for name, value in vars(base).items():
            if not isinstance(value, type) or not issubclass(value, BrokerPort):
                continue
            self.assertTrue(
                bool(getattr(value, "__abstractmethods__", frozenset())),
                f"{name} in trading.strategy.base is a usable broker",
            )


if __name__ == "__main__":
    unittest.main()
