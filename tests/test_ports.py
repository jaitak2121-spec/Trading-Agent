"""Tests for the port seam itself.

The hexagonal boundary is only real if it is enforced. A port that nothing
implements is decoration: it drifts from the class it is supposed to describe,
and the drift is invisible until Stage 2 writes a second implementation against
a stale interface.

Two of the four ports cannot use inheritance to declare conformance --
``trading.ports.broker`` imports ``trading.core.orders`` at runtime, so an
``OrderStore(OrderRepositoryPort)`` base class would close an import cycle. They
use ``ABCMeta.register`` instead, which grants the ``issubclass`` relationship
and enforces nothing. This module supplies the enforcement, for all four ports
uniformly, and it is stronger than inheritance would be: inheritance notices a
*missing* method, while these tests also notice a *changed* one.
"""

from __future__ import annotations

import ast
import inspect
import unittest
from decimal import Decimal

from trading.adapters.memory import SimulatedBroker, StaticMarketData
from trading.core.clock import ManualClock
from trading.core.money import USD, Price
from trading.core.orders import OrderStore
from trading.core.reconciliation import PositionLedger
from trading.ports import (
    BrokerPort,
    MarketDataPort,
    OrderRepositoryPort,
    PositionRepositoryPort,
)

#: Every port, paired with the Stage 1 class that implements it. A port missing
#: from this list is a port nothing is holding to account, which is the exact
#: failure this module exists to prevent -- see
#: ``test_every_port_has_an_implementation``.
PORT_IMPLEMENTATIONS = [
    (OrderRepositoryPort, OrderStore),
    (PositionRepositoryPort, PositionLedger),
    (BrokerPort, SimulatedBroker),
    (MarketDataPort, StaticMarketData),
]


def abstract_methods(port: type) -> list[str]:
    return sorted(getattr(port, "__abstractmethods__", frozenset()))


class TestPortsAreAbstract(unittest.TestCase):
    """A port must not be instantiable and must not carry behaviour."""

    def test_no_port_can_be_instantiated(self):
        for port, _impl in PORT_IMPLEMENTATIONS:
            with self.subTest(port=port.__name__):
                with self.assertRaises(TypeError):
                    port()

    def test_every_port_declares_at_least_one_abstract_method(self):
        """An ABC with no abstract methods is instantiable and enforces nothing."""
        for port, _impl in PORT_IMPLEMENTATIONS:
            with self.subTest(port=port.__name__):
                self.assertTrue(
                    abstract_methods(port),
                    f"{port.__name__} declares nothing, so it constrains nothing",
                )

    def test_no_abstract_method_carries_an_implementation(self):
        """A port method may hold a docstring or ``...``, never logic.

        Behaviour on a port is behaviour that cannot be swapped out, which
        defeats the point of having the seam.
        """
        for port, _impl in PORT_IMPLEMENTATIONS:
            for name in abstract_methods(port):
                with self.subTest(port=port.__name__, method=name):
                    source = inspect.cleandoc(
                        inspect.getsource(getattr(port, name))
                    )
                    func = ast.parse(source).body[0]
                    body = list(func.body)
                    if body and isinstance(body[0], ast.Expr) and isinstance(
                        body[0].value, ast.Constant
                    ) and isinstance(body[0].value.value, str):
                        body = body[1:]  # the docstring
                    for statement in body:
                        self.assertTrue(
                            isinstance(statement, ast.Pass)
                            or (
                                isinstance(statement, ast.Expr)
                                and isinstance(statement.value, ast.Constant)
                                and statement.value.value is Ellipsis
                            ),
                            f"{port.__name__}.{name} contains logic: "
                            f"{ast.dump(statement)[:80]}",
                        )


class TestEveryPortHasAnImplementation(unittest.TestCase):
    """The ports package must not accumulate interfaces nothing satisfies."""

    def test_every_exported_port_is_covered_by_this_module(self):
        import trading.ports as ports

        exported = {
            name
            for name in ports.__all__
            if isinstance(getattr(ports, name), type)
            and abstract_methods(getattr(ports, name))
        }
        covered = {port.__name__ for port, _impl in PORT_IMPLEMENTATIONS}
        self.assertEqual(
            exported - covered,
            set(),
            "a port is exported that no Stage 1 class implements",
        )


class TestConformance(unittest.TestCase):
    """Each implementation satisfies its port, structurally and nominally."""

    def test_each_implementation_is_a_subclass_of_its_port(self):
        for port, impl in PORT_IMPLEMENTATIONS:
            with self.subTest(port=port.__name__):
                self.assertTrue(
                    issubclass(impl, port),
                    f"{impl.__name__} does not declare {port.__name__}; "
                    "inherit from it, or register() it from the ports side",
                )

    def test_each_implementation_defines_every_abstract_method(self):
        for port, impl in PORT_IMPLEMENTATIONS:
            for name in abstract_methods(port):
                with self.subTest(port=port.__name__, method=name):
                    attr = getattr(impl, name, None)
                    self.assertIsNotNone(
                        attr, f"{impl.__name__} is missing {name}"
                    )
                    self.assertTrue(callable(attr))
                    self.assertFalse(
                        getattr(attr, "__isabstractmethod__", False),
                        f"{impl.__name__}.{name} is still abstract",
                    )

    def test_no_implementation_narrows_a_port_signature(self):
        """The parameters a caller may pass through the port must all work.

        A registered class is not signature-checked by Python at all, so this is
        the only thing standing between the port and an implementation that
        renamed a keyword argument.
        """
        for port, impl in PORT_IMPLEMENTATIONS:
            for name in abstract_methods(port):
                with self.subTest(port=port.__name__, method=name):
                    expected = inspect.signature(getattr(port, name))
                    actual = inspect.signature(getattr(impl, name))
                    for param in expected.parameters.values():
                        if param.name == "self":
                            continue
                        if param.kind in (
                            inspect.Parameter.VAR_POSITIONAL,
                            inspect.Parameter.VAR_KEYWORD,
                        ):
                            continue
                        got = actual.parameters.get(param.name)
                        self.assertIsNotNone(
                            got,
                            f"{impl.__name__}.{name} does not accept "
                            f"{param.name!r}, which {port.__name__} promises",
                        )
                        self.assertEqual(
                            got.kind,
                            param.kind,
                            f"{impl.__name__}.{name} takes {param.name!r} "
                            "positionally/by keyword differently to the port",
                        )
                        if param.default is not inspect.Parameter.empty:
                            self.assertEqual(
                                got.default,
                                param.default,
                                f"{impl.__name__}.{name} changes the default "
                                f"for {param.name!r}",
                            )


class TestInstancesSatisfyIsinstance(unittest.TestCase):
    """The runtime check the gateway relies on."""

    def test_a_broker_instance_passes_the_gateway_s_isinstance_check(self):
        broker = SimulatedBroker(clock=ManualClock())
        self.assertIsInstance(broker, BrokerPort)

    def test_a_store_instance_is_a_repository(self):
        self.assertIsInstance(OrderStore(), OrderRepositoryPort)

    def test_a_ledger_instance_is_a_position_repository(self):
        self.assertIsInstance(PositionLedger(), PositionRepositoryPort)

    def test_a_feed_instance_is_a_market_data_port(self):
        feed = StaticMarketData({"BTCUSD": Price("50000", USD)})
        self.assertIsInstance(feed, MarketDataPort)


class TestPortsHoldNoInfrastructure(unittest.TestCase):
    """The claim in the package docstring, checked.

    ``test_core_purity.py`` proves the kernel imports no third party. This is the
    narrower claim that the ports package holds no *state* and no I/O -- it is
    interfaces and value types only.
    """

    def test_no_port_module_opens_anything(self):
        import trading.ports.broker as broker_mod
        import trading.ports.market_data as md_mod
        import trading.ports.repository as repo_mod

        forbidden = {"socket", "requests", "httpx", "urllib", "http", "os", "sys"}
        for module in (broker_mod, md_mod, repo_mod):
            with self.subTest(module=module.__name__):
                for name in vars(module):
                    self.assertNotIn(name, forbidden)

    def test_the_repository_port_documents_the_write_before_send_rule(self):
        """INVARIANT 12 depends on it, and a Stage 2 adapter must be told.

        In-memory stores satisfy ordering trivially because there is no separate
        commit; a database adapter has to be deliberate about it, and the only
        place that requirement is recorded is the port.
        """
        import trading.ports.repository as repo_mod

        doc = (repo_mod.__doc__ or "").lower()
        self.assertIn("pending_new", doc)
        self.assertIn("before", doc)


class TestBrokerPortRequiresAToken(unittest.TestCase):
    """The structural half of INVARIANT 3, at the port rather than the gateway.

    A strategy that somehow obtains a broker reference still cannot use it: the
    port's own signature demands a capability only the gateway can mint.
    """

    def test_place_order_takes_a_token(self):
        params = inspect.signature(BrokerPort.place_order).parameters
        self.assertIn("token", params)

    def test_the_token_is_keyword_only(self):
        """So it cannot be supplied by accident from positional argument drift."""
        param = inspect.signature(BrokerPort.place_order).parameters["token"]
        self.assertEqual(param.kind, inspect.Parameter.KEYWORD_ONLY)

    def test_the_token_has_no_default(self):
        param = inspect.signature(BrokerPort.place_order).parameters["token"]
        self.assertIs(param.default, inspect.Parameter.empty)

    def test_the_simulated_broker_honours_that(self):
        params = inspect.signature(SimulatedBroker.place_order).parameters
        self.assertEqual(
            params["token"].kind, inspect.Parameter.KEYWORD_ONLY
        )
        self.assertIs(params["token"].default, inspect.Parameter.empty)


class TestMarketDataPortIsDecimalOnly(unittest.TestCase):
    """INVARIANT 8 reaches the boundary, not just the core."""

    def feed(self) -> StaticMarketData:
        return StaticMarketData({"BTCUSD": Price("50000", USD)})

    def test_a_quoted_price_is_backed_by_a_decimal(self):
        price = self.feed().mark_price("BTCUSD")
        self.assertIsInstance(price.amount, Decimal)

    def test_the_feed_refuses_a_float_price(self):
        with self.assertRaises((TypeError, ValueError)):
            StaticMarketData({"BTCUSD": 50000.0})

    def test_an_unknown_symbol_is_reported_as_none_not_guessed(self):
        """The risk engine treats absence as a refusal; silence would defeat it."""
        self.assertIsNone(self.feed().mark_price("ETHUSD"))

    def test_the_batch_default_omits_what_it_does_not_know(self):
        """``mark_prices`` is the port's one concrete method, so pin its contract.

        A gap must stay a gap all the way to the risk engine. If this filled in a
        placeholder, an unpriced symbol would pass an exposure check unmeasured.
        """
        prices = self.feed().mark_prices(["BTCUSD", "ETHUSD"])
        self.assertEqual(set(prices), {"BTCUSD"})


if __name__ == "__main__":
    unittest.main()
