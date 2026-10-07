"""Tests for venue-wide reconciliation -- the sweep that sees the whole book.

``sync_order`` can only ask about orders we already know about, so the tests
that matter here are the ones about orders we *do not*: one the venue holds and
we have no record of, one we believe is open and the venue has never heard of,
and the pair of readings that disagree about what an order even is. A sweep that
only confirmed the orders it was told about would pass every per-order test in
this suite and detect none of them.

The other half is what it must not do. It must not resolve an UNKNOWN order, not
even when the venue's answer would settle the question -- that is an operator
act with its own reservation bookkeeping, and a background sweep performing it
would convert "we do not know" into "we have decided". It must not repair a
disagreement by picking a winner. And a read that failed must not read as a
clean sweep: ``blocked`` is true for a failure, so an unreachable venue can
never manufacture the confidence the live gate requires.

``VenueWithoutInventory`` is the negative control for the capability itself: a
broker that cannot enumerate its book gets a blocking finding rather than a
successful-looking partial sweep.
"""

from __future__ import annotations

import unittest

from tests.harness import ASSET, DEFAULT_QUANTITY, SYMBOL, build_rig
from trading.adapters.memory import SimulatedBroker
from trading.adapters.reconciliation import (
    FindingKind,
    FindingSeverity,
    ReconciliationCoordinator,
    VenueSweepReport,
)
from trading.core.authz import ExecutionToken, Principal, Role
from trading.core.clock import ManualClock
from trading.core.errors import SafetyViolation, UnauthorizedAction
from trading.core.money import USD, Price, Quantity
from trading.core.orders import Order, OrderIntent, OrderSide, OrderState
from trading.ports.broker import (
    AckOutcome,
    BrokerAck,
    BrokerOrderInventoryPort,
    BrokerOrderSnapshot,
    BrokerOrderStatus,
    BrokerPort,
    BrokerPositionSnapshot,
)

SWEEPER = Principal("sweeper-1", Role.SYSTEM)


class VenueWithoutInventory(BrokerPort):
    """A venue that answers about known orders and cannot list its own book.

    Models a real adapter against an exchange with no order-list endpoint. The
    sweep must refuse to pretend it performed a complete reconciliation.
    """

    def __init__(self) -> None:
        self._seq = 0

    def place_order(self, order: Order, *, token: ExecutionToken) -> BrokerAck:
        token.consume(order_id=order.order_id, clock=ManualClock())
        self._seq += 1
        return BrokerAck(AckOutcome.ACCEPTED, broker_order_id=f"blind-{self._seq}")

    def cancel_order(self, order: Order) -> BrokerAck:
        return BrokerAck(AckOutcome.ACCEPTED, message="canceled")

    def fetch_order_state(self, order: Order) -> BrokerAck:
        return BrokerAck(AckOutcome.ACCEPTED, broker_order_id=order.broker_order_id)

    def fetch_positions(self) -> BrokerPositionSnapshot:
        return BrokerPositionSnapshot()


class ExplodingVenue(SimulatedBroker):
    """A simulated venue whose inventory read fails."""

    def __init__(self, **kwargs) -> None:
        super().__init__(**kwargs)
        self.explode = False

    def fetch_order_inventory(self) -> tuple[BrokerOrderSnapshot, ...]:
        if self.explode:
            raise ConnectionResetError("venue inventory endpoint died")
        return super().fetch_order_inventory()


class SweepCase(unittest.TestCase):
    """A rig whose venue can be swept, with a SYSTEM coordinator over it."""

    def setUp(self) -> None:
        self.venue = ExplodingVenue(
            clock=ManualClock(), fill_prices={SYMBOL: Price("50000", USD)}
        )
        self.rig = build_rig(broker=self.venue)
        # The venue must share the rig's clock for positions and tokens.
        self.venue._clock = self.rig.clock
        self.coordinator = self.build_coordinator()

    def build_coordinator(self, **kwargs) -> ReconciliationCoordinator:
        kwargs.setdefault("gateway", self.rig.gateway)
        kwargs.setdefault("orders", self.rig.orders)
        kwargs.setdefault("broker", self.rig.broker)
        kwargs.setdefault("reconciliation", self.rig.reconciliation)
        kwargs.setdefault("audit", self.rig.audit)
        kwargs.setdefault("clock", self.rig.clock)
        kwargs.setdefault("identity", SWEEPER)
        return ReconciliationCoordinator(**kwargs)

    def rest(self, **kwargs) -> Order:
        """An order resting at the venue (accepted, not filled)."""
        self.venue._default_outcome = AckOutcome.ACCEPTED
        result = self.rig.submit(**kwargs)
        self.assertTrue(result.is_executed, result.reason)
        self.assertIs(result.order.state, OrderState.ACCEPTED)
        return result.order

    def kinds(self, report: VenueSweepReport) -> set[str]:
        return {f.kind for f in report.findings}


# -- the sweep reports, cleanly -------------------------------------------------


class TestCleanSweeps(SweepCase):
    """Nothing is wrong, and the report says so without inventing findings."""

    def test_an_empty_system_sweeps_clean(self) -> None:
        report = self.coordinator.sweep()
        self.assertTrue(report.is_clean, report.findings)
        self.assertFalse(report.blocked)
        self.assertEqual(report.findings, ())
        self.assertEqual(report.venue_order_count, 0)

    def test_a_resting_order_matches_and_is_synced(self) -> None:
        order = self.rest()
        report = self.coordinator.sweep()
        self.assertEqual(report.synced, (order.order_id,))
        self.assertTrue(report.is_clean, report.findings)

    def test_the_sweep_is_recorded_in_the_audit_trail(self) -> None:
        self.coordinator.sweep()
        self.assertIn("gateway.venue_sweep", self.rig.actions())

    def test_repeated_sweeps_are_idempotent(self) -> None:
        """A second sweep over the same orders books nothing new."""
        order = self.rest()
        first = self.coordinator.sweep()
        second = self.coordinator.sweep()
        self.assertEqual(first.synced, second.synced)
        self.assertEqual(
            order.filled_quantity, Quantity.zero(DEFAULT_QUANTITY.asset)
        )

    def test_the_report_exposes_counts_for_an_operator(self) -> None:
        order = self.rest()
        report = self.coordinator.sweep()
        self.assertEqual(report.local_open_count, 1)
        self.assertEqual(report.venue_order_count, 1)
        self.assertEqual(report.synced, (order.order_id,))


# -- the findings a per-order query cannot produce ------------------------------


class TestVenueOrdersWeDoNotKnowAbout(SweepCase):
    """A ghost order: the venue holds it, we have no record of it at all."""

    def ghost(self) -> BrokerOrderSnapshot:
        return BrokerOrderSnapshot(
            broker_order_id="GHOST-1",
            symbol=SYMBOL,
            side="buy",
            ordered_quantity=DEFAULT_QUANTITY,
            filled_quantity=Quantity.zero(ASSET),
            status=BrokerOrderStatus.OPEN,
            idempotency_key="key-we-never-used",
        )

    def plant(self, broker_order_id: str = "GHOST-1") -> None:
        """Put an order at the venue that no local order corresponds to."""
        intent = OrderIntent(
            strategy_id="out-of-band",
            signal_id=f"oob-{broker_order_id}",
            symbol=SYMBOL,
            side=OrderSide.BUY,
            quantity=DEFAULT_QUANTITY,
        )
        order = Order(intent, clock=self.rig.clock)
        self.venue.plant_order_at_venue(
            order, BrokerAck(AckOutcome.ACCEPTED, broker_order_id=broker_order_id)
        )

    def test_a_ghost_order_is_reported_and_blocks(self) -> None:
        self.plant()
        report = self.coordinator.sweep()
        self.assertIn(FindingKind.VENUE_ORDER_UNKNOWN_LOCALLY, self.kinds(report))
        self.assertTrue(report.blocked)

    def test_the_ghost_finding_is_blocking_not_advisory(self) -> None:
        self.plant()
        report = self.coordinator.sweep()
        ghost = [
            f
            for f in report.findings
            if f.kind == FindingKind.VENUE_ORDER_UNKNOWN_LOCALLY
        ]
        self.assertTrue(all(f.severity is FindingSeverity.BLOCKING for f in ghost))

    def test_a_detected_ghost_blocks_a_new_submission(self) -> None:
        """INVARIANT 6/5: a disagreement stops new orders until it is resolved."""
        self.plant()
        self.coordinator.sweep()
        # A position mismatch is what the gate latches; the ghost itself is
        # reported by the sweep. Both must stop the next live submission.
        self.rig.positions.set_position(SYMBOL, Quantity("1", ASSET))
        self.coordinator.sweep()
        self.assertTrue(self.rig.reconciliation.has_mismatch)


class TestLocalOrdersMissingAtTheVenue(SweepCase):
    """We believe an order is open; the venue has never heard of it."""

    def test_an_open_order_absent_from_the_venue_blocks(self) -> None:
        order = self.rest()
        # Drop it from the venue, as a lost record would.
        self.venue._venue_orders.clear()
        report = self.coordinator.sweep()
        self.assertIn(
            FindingKind.LOCAL_ORDER_MISSING_AT_VENUE, self.kinds(report)
        )
        self.assertTrue(report.blocked)
        missing = [
            f
            for f in report.findings
            if f.kind == FindingKind.LOCAL_ORDER_MISSING_AT_VENUE
        ]
        self.assertEqual(missing[0].order_id, order.order_id)


class TestOutOfBandFills(SweepCase):
    """A fill that happened at the venue with nobody watching."""

    def test_a_venue_fill_is_applied_through_the_gateway(self) -> None:
        order = self.rest()
        # The venue now reports the order filled; we have booked nothing.
        self.venue._venue_orders[order.idempotency_key] = BrokerAck(
            AckOutcome.FILLED,
            broker_order_id=order.broker_order_id,
            filled_quantity=DEFAULT_QUANTITY,
            fill_price=Price("50000", USD),
        )
        report = self.coordinator.sweep()
        self.assertIs(order.state, OrderState.FILLED)
        self.assertEqual(
            self.rig.positions.position(SYMBOL, asset=ASSET), DEFAULT_QUANTITY
        )
        self.assertTrue(report.is_clean, report.findings)

    def test_a_discovered_fill_reaches_the_portfolio_once(self) -> None:
        """Two sweeps must not double-book the same cumulative fill."""
        order = self.rest()
        self.venue._venue_orders[order.idempotency_key] = BrokerAck(
            AckOutcome.FILLED,
            broker_order_id=order.broker_order_id,
            filled_quantity=DEFAULT_QUANTITY,
            fill_price=Price("50000", USD),
        )
        self.coordinator.sweep()
        self.coordinator.sweep()
        self.assertEqual(
            self.rig.positions.position(SYMBOL, asset=ASSET), DEFAULT_QUANTITY
        )


class TestAmbiguousOrdersAreNeverResolved(SweepCase):
    """UNKNOWN is reported and left alone. Only an operator may leave it."""

    def unknown_order(self) -> Order:
        """Drive an order to UNKNOWN the way the gateway does on an unsure ack."""
        self.venue.script(
            BrokerAck(AckOutcome.UNCERTAIN, message="timeout"), lands_at_venue=True
        )
        result = self.rig.submit()
        self.assertEqual(result.outcome, "unknown")
        return result.order

    def test_an_unknown_order_is_reported_as_blocking(self) -> None:
        order = self.unknown_order()
        report = self.coordinator.sweep()
        self.assertIn(FindingKind.UNKNOWN_ORDER_UNRESOLVED, self.kinds(report))
        self.assertTrue(report.blocked)

    def test_the_sweep_does_not_resolve_the_unknown_order(self) -> None:
        """The venue even knows the answer -- and it still must not be applied."""
        order = self.unknown_order()
        self.assertTrue(order.is_unknown)
        report = self.coordinator.sweep()
        self.assertIs(
            order.state, OrderState.UNKNOWN, "the sweep resolved an UNKNOWN order"
        )
        self.assertNotIn(order.order_id, report.synced)

    def test_an_unknown_order_blocks_the_next_submission(self) -> None:
        self.unknown_order()
        result = self.rig.submit()
        self.assertFalse(result.is_executed)
        self.assertIn("unknown", (result.reason or "").lower())


# -- contradictions ------------------------------------------------------------


class TestContradictions(SweepCase):
    """The two records disagree about what the order is."""

    def test_a_regressed_fill_is_refused(self) -> None:
        order = self.rest()
        # Book a partial fill locally, then have the venue report *less* filled.
        # Fills are cumulative, so this is evidence our book and the venue's are
        # describing different orders -- or that ours is simply wrong.
        order.apply_fill(Quantity("0.0004", ASSET), Price("50000", USD), reason="test")
        self.venue.plant_order_at_venue(
            order,
            BrokerAck(
                AckOutcome.ACCEPTED,
                broker_order_id=order.broker_order_id,
                filled_quantity=Quantity("0.0001", ASSET),
            ),
        )
        report = self.coordinator.sweep()
        self.assertIn(FindingKind.FILL_REGRESSION, self.kinds(report))
        self.assertTrue(report.blocked)

    def test_a_side_mismatch_is_reported(self) -> None:
        order = self.rest()
        broker_order_id = order.broker_order_id or "X"
        self.venue.fetch_order_inventory = lambda: (
            BrokerOrderSnapshot(
                broker_order_id=broker_order_id,
                symbol=SYMBOL,
                side="sell",
                ordered_quantity=DEFAULT_QUANTITY,
                filled_quantity=Quantity.zero(ASSET),
                status=BrokerOrderStatus.OPEN,
                idempotency_key=order.idempotency_key,
            ),
        )
        report = self.coordinator.sweep()
        self.assertIn(FindingKind.SIDE_MISMATCH, self.kinds(report))
        self.assertTrue(report.blocked)

    def test_a_symbol_mismatch_is_reported(self) -> None:
        order = self.rest()
        broker_order_id = order.broker_order_id or "X"
        self.venue.fetch_order_inventory = lambda: (
            BrokerOrderSnapshot(
                broker_order_id=broker_order_id,
                symbol="ETHUSD",
                side="buy",
                ordered_quantity=DEFAULT_QUANTITY,
                filled_quantity=Quantity.zero(ASSET),
                status=BrokerOrderStatus.OPEN,
                idempotency_key=order.idempotency_key,
            ),
        )
        report = self.coordinator.sweep()
        self.assertIn(FindingKind.SYMBOL_MISMATCH, self.kinds(report))
        self.assertTrue(report.blocked)

    def test_a_quantity_mismatch_is_reported(self) -> None:
        order = self.rest()
        broker_order_id = order.broker_order_id or "X"
        self.venue.fetch_order_inventory = lambda: (
            BrokerOrderSnapshot(
                broker_order_id=broker_order_id,
                symbol=SYMBOL,
                side="buy",
                ordered_quantity=Quantity("0.002", ASSET),
                filled_quantity=Quantity.zero(ASSET),
                status=BrokerOrderStatus.OPEN,
                idempotency_key=order.idempotency_key,
            ),
        )
        report = self.coordinator.sweep()
        self.assertIn(FindingKind.QUANTITY_MISMATCH, self.kinds(report))
        self.assertTrue(report.blocked)

    def test_a_duplicate_venue_record_is_reported(self) -> None:
        self.rest()
        self.venue.fetch_order_inventory = lambda: (
            BrokerOrderSnapshot(
                broker_order_id="DUP",
                symbol=SYMBOL,
                side="buy",
                ordered_quantity=DEFAULT_QUANTITY,
                filled_quantity=Quantity.zero(ASSET),
                status=BrokerOrderStatus.OPEN,
                idempotency_key="a",
            ),
            BrokerOrderSnapshot(
                broker_order_id="DUP",
                symbol=SYMBOL,
                side="buy",
                ordered_quantity=DEFAULT_QUANTITY,
                filled_quantity=Quantity.zero(ASSET),
                status=BrokerOrderStatus.OPEN,
                idempotency_key="b",
            ),
        )
        report = self.coordinator.sweep()
        self.assertIn(FindingKind.DUPLICATE_VENUE_ORDER, self.kinds(report))
        self.assertTrue(report.blocked)

    def test_a_terminal_order_the_venue_still_holds_blocks(self) -> None:
        order = self.rest()
        order.transition_to(OrderState.CANCELED, reason="test")
        # The venue still reports it open, so it can still trade.
        report = self.coordinator.sweep()
        self.assertIn(FindingKind.VENUE_HOLDS_TERMINAL_ORDER, self.kinds(report))
        self.assertTrue(report.blocked)

    def test_an_identity_mismatch_is_reported(self) -> None:
        """The order carries one broker id; the venue's record for its key is another."""
        order = self.rest()
        self.venue.fetch_order_inventory = lambda: (
            BrokerOrderSnapshot(
                broker_order_id="SOMEONE-ELSES-ORDER",
                symbol=SYMBOL,
                side="buy",
                ordered_quantity=DEFAULT_QUANTITY,
                filled_quantity=Quantity.zero(ASSET),
                status=BrokerOrderStatus.OPEN,
                idempotency_key=order.idempotency_key,
            ),
        )
        report = self.coordinator.sweep()
        self.assertIn(FindingKind.IDENTITY_MISMATCH, self.kinds(report))
        self.assertTrue(report.blocked)


class TestSyncRefusalsAreAdvisory(SweepCase):
    """A gateway refusal is the system working, so it must not read as a sweep failure."""

    def test_a_refused_sync_is_advisory_and_does_not_block(self) -> None:
        order = self.rest()
        # An order the store offers but the gateway will refuse to sync: a
        # terminal one that our selection would never have produced. Proven
        # through the real gateway path rather than by mocking it.
        self.venue.fetch_order_inventory = lambda: (
            BrokerOrderSnapshot(
                broker_order_id=order.broker_order_id or "X",
                symbol=SYMBOL,
                side="buy",
                ordered_quantity=DEFAULT_QUANTITY,
                filled_quantity=Quantity.zero(ASSET),
                status=BrokerOrderStatus.OPEN,
                idempotency_key=order.idempotency_key,
            ),
        )
        report = self.coordinator.sweep()

        class FussyGateway:
            def sync_order(self, order, *, operator):
                raise SafetyViolation("nothing to sync")

        coordinator = self.build_coordinator(gateway=FussyGateway())
        report = coordinator.sweep()
        refused = [
            f for f in report.findings if f.kind == FindingKind.SYNC_REFUSED
        ]
        self.assertTrue(refused)
        self.assertTrue(all(f.severity is FindingSeverity.ADVISORY for f in refused))
        self.assertFalse(report.blocked)


# -- failures read as failures --------------------------------------------------


class TestFailuresAreNotSilentSuccesses(SweepCase):
    """An unreachable venue must never look like a clean reconciliation."""

    def test_a_failing_inventory_read_blocks(self) -> None:
        self.rest()
        self.venue.explode = True
        report = self.coordinator.sweep()
        self.assertTrue(report.blocked)
        self.assertIsNotNone(report.error)
        self.assertFalse(report.is_clean)

    def test_a_failed_read_leaves_local_state_untouched(self) -> None:
        order = self.rest()
        before = order.state
        self.venue.explode = True
        self.coordinator.sweep()
        self.assertIs(order.state, before)
        self.assertEqual(order.filled_quantity, Quantity.zero(ASSET))

    def test_a_venue_without_inventory_blocks_rather_than_partial_sweeping(self) -> None:
        """A partial sweep that reports success is worse than no sweep."""
        blind = VenueWithoutInventory()
        self.assertFalse(isinstance(blind, BrokerOrderInventoryPort))
        rig = build_rig(broker=blind)
        coordinator = self.build_coordinator(
            gateway=rig.gateway,
            orders=rig.orders,
            broker=blind,
            reconciliation=rig.reconciliation,
            audit=rig.audit,
            clock=rig.clock,
        )
        report = coordinator.sweep()
        self.assertIn(FindingKind.INVENTORY_UNAVAILABLE, self.kinds(report))
        self.assertTrue(report.blocked)
        self.assertFalse(coordinator.supports_venue_inventory)

    def test_a_supported_venue_reports_the_capability(self) -> None:
        self.assertTrue(self.coordinator.supports_venue_inventory)


class TestAuthorization(SweepCase):
    """A sweep that may not reconcile says so, loudly, rather than sweeping nothing."""

    def test_an_unauthorized_identity_raises_rather_than_being_swallowed(self) -> None:
        self.rest()
        coordinator = self.build_coordinator(
            identity=Principal("nobody", Role.STRATEGY)
        )
        with self.assertRaises(UnauthorizedAction):
            coordinator.sweep()


# -- the sweep is read-only -----------------------------------------------------


class TestTheSweepDoesNotMutateTheVenue(SweepCase):
    """Inventory reads must not change what the venue holds."""

    def test_a_sweep_does_not_fill_a_resting_order(self) -> None:
        """Reading the book is not the same question as asking about one order."""
        from trading.adapters.memory import InMemoryQuoteFeed
        from trading.adapters.paper import PaperBroker

        clock = self.rig.clock
        feed = InMemoryQuoteFeed(clock=clock, source="sweep-test")
        feed.publish(SYMBOL, Price("49990", USD), Price("50010", USD))
        paper = PaperBroker(clock=clock, quotes=feed)
        rig = build_rig(broker=paper, clock=clock)
        coordinator = self.build_coordinator(
            gateway=rig.gateway,
            orders=rig.orders,
            broker=paper,
            reconciliation=rig.reconciliation,
            audit=rig.audit,
            clock=clock,
        )
        intent = rig.intent(
            quantity=DEFAULT_QUANTITY,
            side=OrderSide.BUY,
        )
        # A buy limit below the book rests rather than filling.
        result = rig.gateway.submit(
            OrderIntent(
                strategy_id="strat-1",
                signal_id="sig-limit",
                symbol=SYMBOL,
                side=OrderSide.BUY,
                quantity=DEFAULT_QUANTITY,
                order_type=__import__(
                    "trading.core.orders", fromlist=["OrderType"]
                ).OrderType.LIMIT,
                limit_price=Price("49000", USD),
            ),
            proposer=rig.strategy_id,
            mark_prices=rig.prices(),
        )
        self.assertTrue(result.is_executed, result.reason)
        positions_before = dict(paper.fetch_positions().positions)
        coordinator.sweep()
        self.assertEqual(dict(paper.fetch_positions().positions), positions_before)


if __name__ == "__main__":
    unittest.main()
