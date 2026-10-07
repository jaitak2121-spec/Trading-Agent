"""Tests for the persistence repository ports.

These tests verify that the repository port implementations satisfy the
contract defined in :mod:`trading.ports.repository` and
:mod:`trading.ports.reservation_repository`.
"""

from __future__ import annotations

import threading
import unittest
from decimal import Decimal

from trading.adapters.persistence import (
    OrderStore,
    OrderStoreAdapter,
    PositionLedger,
    ReservationRepository,
)
from trading.core.clock import ManualClock
from trading.core.dedupe import Reservation, ReservationState
from trading.core.orders import Order, OrderIntent, OrderSide, OrderState, OrderStore as CoreOrderStore
from trading.core.reconciliation import PositionLedger as CorePositionLedger
from trading.ports.repository import OrderRepositoryPort, PositionRepositoryPort
from trading.ports.reservation_repository import ReservationRepositoryPort

from .harness import SYMBOL, ASSET, DEFAULT_PRICE, DEFAULT_QUANTITY


class OrderStoreAdapterTest(unittest.TestCase):
    """Tests for OrderStoreAdapter."""

    def test_package_export_is_adapter_not_core_store(self) -> None:
        """The persistence package must expose its repository *adapter*.

        This pins the Stage 2H repair. The historical defect was that
        ``persistence.OrderStore`` resolved to the imported *core* class, so the
        diagnostic ``persistence.OrderStore is core.OrderStore`` printed ``True``
        and the adapter was unreachable. After the repair the same diagnostic
        prints ``False`` -- which is the *correct* result, not a sign of an
        incomplete fix: the adapter is a distinct subclass of the core store, so
        it is not identical to it (``is`` is False) yet still IS-A core store
        (``issubclass`` is True), which is what keeps all behaviour and port
        conformance intact.
        """
        # The exported name is the adapter.
        self.assertIs(OrderStore, OrderStoreAdapter)
        # The exported name is NOT the core class. This is the assertion whose
        # failure (printing identity True) was the original defect.
        self.assertIsNot(OrderStore, CoreOrderStore)
        # But the adapter is a subclass of the core store, so it inherits every
        # behaviour and satisfies the repository port. This is *why* `is` being
        # False is correct rather than broken.
        self.assertTrue(issubclass(OrderStoreAdapter, CoreOrderStore))
        self.assertIsInstance(OrderStore(), OrderRepositoryPort)

    def test_implements_order_repository_port(self) -> None:
        """OrderStoreAdapter must implement OrderRepositoryPort."""
        store = OrderStore()
        self.assertIsInstance(store, OrderRepositoryPort)

    def test_add_order(self) -> None:
        """Adding an order persists it."""
        store = OrderStore()
        clock = ManualClock()
        intent = OrderIntent(
            strategy_id="strat-1",
            signal_id="sig-1",
            symbol=SYMBOL,
            side=OrderSide.BUY,
            quantity=DEFAULT_QUANTITY,
        )
        order = Order(intent, clock=clock)
        result = store.add(order)
        self.assertIs(result, order)
        self.assertIs(store.get(order.order_id), order)

    def test_add_duplicate_raises(self) -> None:
        """Adding a duplicate order_id raises."""
        store = OrderStore()
        clock = ManualClock()
        intent = OrderIntent(
            strategy_id="strat-1",
            signal_id="sig-1",
            symbol=SYMBOL,
            side=OrderSide.BUY,
            quantity=DEFAULT_QUANTITY,
        )
        order = Order(intent, clock=clock)
        store.add(order)
        with self.assertRaises(Exception):
            store.add(order)

    def test_find_by_idempotency_key(self) -> None:
        """Finding by idempotency key works."""
        store = OrderStore()
        clock = ManualClock()
        intent = OrderIntent(
            strategy_id="strat-1",
            signal_id="sig-1",
            symbol=SYMBOL,
            side=OrderSide.BUY,
            quantity=DEFAULT_QUANTITY,
        )
        order = Order(intent, clock=clock)
        store.add(order)
        found = store.find_by_idempotency_key(intent.idempotency_key)
        self.assertIs(found, order)

    def test_find_by_idempotency_key_not_found(self) -> None:
        """Finding by non-existent key returns None."""
        store = OrderStore()
        found = store.find_by_idempotency_key("nonexistent")
        self.assertIsNone(found)

    def test_all_orders(self) -> None:
        """all_orders returns all orders."""
        store = OrderStore()
        clock = ManualClock()
        intent1 = OrderIntent(
            strategy_id="strat-1",
            signal_id="sig-1",
            symbol=SYMBOL,
            side=OrderSide.BUY,
            quantity=DEFAULT_QUANTITY,
        )
        intent2 = OrderIntent(
            strategy_id="strat-1",
            signal_id="sig-2",
            symbol=SYMBOL,
            side=OrderSide.SELL,
            quantity=DEFAULT_QUANTITY,
        )
        order1 = Order(intent1, clock=clock)
        order2 = Order(intent2, clock=clock)
        store.add(order1)
        store.add(order2)
        orders = store.all_orders()
        self.assertEqual(len(orders), 2)
        self.assertIn(order1, orders)
        self.assertIn(order2, orders)

    def test_unknown_orders(self) -> None:
        """unknown_orders returns orders in UNKNOWN state."""
        store = OrderStore()
        clock = ManualClock()
        intent1 = OrderIntent(
            strategy_id="strat-1",
            signal_id="sig-1",
            symbol=SYMBOL,
            side=OrderSide.BUY,
            quantity=DEFAULT_QUANTITY,
        )
        intent2 = OrderIntent(
            strategy_id="strat-1",
            signal_id="sig-2",
            symbol=SYMBOL,
            side=OrderSide.SELL,
            quantity=DEFAULT_QUANTITY,
        )
        order1 = Order(intent1, clock=clock)
        order2 = Order(intent2, clock=clock)
        store.add(order1)
        store.add(order2)
        # Neither is UNKNOWN yet
        self.assertEqual(store.unknown_orders(), [])
        # Transition order1 through PENDING_NEW -> ACCEPTED -> UNKNOWN
        order1.transition_to(OrderState.PENDING_NEW, reason="submitted", via_reconciliation=False)
        order1.transition_to(OrderState.ACCEPTED, reason="accepted", via_reconciliation=False)
        order1.transition_to(OrderState.UNKNOWN, reason="test", via_reconciliation=True)
        unknown = store.unknown_orders()
        self.assertEqual(len(unknown), 1)
        self.assertIs(unknown[0], order1)

    def test_open_orders(self) -> None:
        """open_orders returns orders that are open (not terminal, not UNKNOWN)."""
        store = OrderStore()
        clock = ManualClock()
        intent1 = OrderIntent(
            strategy_id="strat-1",
            signal_id="sig-1",
            symbol=SYMBOL,
            side=OrderSide.BUY,
            quantity=DEFAULT_QUANTITY,
        )
        intent2 = OrderIntent(
            strategy_id="strat-1",
            signal_id="sig-2",
            symbol=SYMBOL,
            side=OrderSide.SELL,
            quantity=DEFAULT_QUANTITY,
        )
        order1 = Order(intent1, clock=clock)
        order2 = Order(intent2, clock=clock)
        store.add(order1)
        store.add(order2)
        # DRAFT orders are NOT in open_orders - only PENDING_NEW, ACCEPTED, PARTIALLY_FILLED
        # Both orders are still DRAFT, so open_orders is empty
        open_orders = store.open_orders()
        self.assertEqual(len(open_orders), 0)
        # Mark order1 as PENDING_NEW (open)
        order1.transition_to(OrderState.PENDING_NEW, reason="submitted", via_reconciliation=False)
        open_orders = store.open_orders()
        self.assertEqual(len(open_orders), 1)
        self.assertIs(open_orders[0], order1)
        # Mark order1 as ACCEPTED (still open)
        order1.transition_to(OrderState.ACCEPTED, reason="accepted", via_reconciliation=False)
        open_orders = store.open_orders()
        self.assertEqual(len(open_orders), 1)
        self.assertIs(open_orders[0], order1)
        # Mark order2 as PENDING_NEW, then ACCEPTED, then FILLED (terminal)
        order2.transition_to(OrderState.PENDING_NEW, reason="submitted", via_reconciliation=False)
        order2.transition_to(OrderState.ACCEPTED, reason="accepted", via_reconciliation=False)
        order2.transition_to(OrderState.FILLED, reason="filled", via_reconciliation=False)
        open_orders = store.open_orders()
        self.assertEqual(len(open_orders), 1)
        self.assertIs(open_orders[0], order1)

    def test_has_unknown_orders(self) -> None:
        """has_unknown_orders returns True if any order is UNKNOWN."""
        store = OrderStore()
        clock = ManualClock()
        intent = OrderIntent(
            strategy_id="strat-1",
            signal_id="sig-1",
            symbol=SYMBOL,
            side=OrderSide.BUY,
            quantity=DEFAULT_QUANTITY,
        )
        order = Order(intent, clock=clock)
        store.add(order)
        # Not UNKNOWN yet
        self.assertFalse(store.has_unknown_orders())
        # Transition to PENDING_NEW first, then to ACCEPTED, then to UNKNOWN
        order.transition_to(OrderState.PENDING_NEW, reason="submitted", via_reconciliation=False)
        order.transition_to(OrderState.ACCEPTED, reason="accepted", via_reconciliation=False)
        order.transition_to(OrderState.UNKNOWN, reason="test", via_reconciliation=True)
        self.assertTrue(store.has_unknown_orders())

    def test_wrapped_store_shares_backing_state_and_lock(self) -> None:
        """Wrapping a core store shares both its state and synchronization."""
        core_store = CoreOrderStore()
        store = OrderStoreAdapter(store=core_store)
        clock = ManualClock()
        intent = OrderIntent(
            strategy_id="strat-1",
            signal_id="sig-1",
            symbol=SYMBOL,
            side=OrderSide.BUY,
            quantity=DEFAULT_QUANTITY,
        )
        order = Order(intent, clock=clock)

        store.add(order)

        self.assertIs(core_store.get(order.order_id), order)
        self.assertIs(store._lock, core_store._lock)
        self.assertIs(store._by_id, core_store._by_id)
        self.assertIs(store._by_key, core_store._by_key)

    def test_update_returns_persisted_order(self) -> None:
        """update confirms an existing order is persisted by reference."""
        store = OrderStore()
        clock = ManualClock()
        intent = OrderIntent(
            strategy_id="strat-1",
            signal_id="sig-1",
            symbol=SYMBOL,
            side=OrderSide.BUY,
            quantity=DEFAULT_QUANTITY,
        )
        order = Order(intent, clock=clock)
        store.add(order)
        order.transition_to(OrderState.PENDING_NEW, reason="submitted", via_reconciliation=False)

        result = store.update(order)

        self.assertIs(result, order)
        self.assertIs(store.get(order.order_id), order)
        self.assertEqual(store.get(order.order_id).state, OrderState.PENDING_NEW)

    def test_update_missing_order_raises(self) -> None:
        """update rejects an order that was never persisted."""
        store = OrderStore()
        clock = ManualClock()
        intent = OrderIntent(
            strategy_id="strat-1",
            signal_id="sig-1",
            symbol=SYMBOL,
            side=OrderSide.BUY,
            quantity=DEFAULT_QUANTITY,
        )

        with self.assertRaises(KeyError):
            store.update(Order(intent, clock=clock))


class PositionLedgerAdapterTest(unittest.TestCase):
    """Tests for PositionLedgerAdapter."""

    def test_implements_position_repository_port(self) -> None:
        """PositionLedgerAdapter must implement PositionRepositoryPort."""
        ledger = PositionLedger()
        self.assertIsInstance(ledger, PositionRepositoryPort)

    def test_position(self) -> None:
        """position returns the position for a symbol."""
        ledger = PositionLedger()
        qty = ledger.position(SYMBOL)
        self.assertEqual(qty.amount, Decimal("0"))

    def test_set_position(self) -> None:
        """set_position sets the position for a symbol."""
        ledger = PositionLedger()
        qty = DEFAULT_QUANTITY
        ledger.set_position(SYMBOL, qty)
        result = ledger.position(SYMBOL)
        self.assertEqual(result.amount, qty.amount)
        self.assertEqual(result.asset, qty.asset)

    def test_snapshot(self) -> None:
        """snapshot returns all positions."""
        ledger = PositionLedger()
        ledger.set_position(SYMBOL, DEFAULT_QUANTITY)
        snapshot = ledger.snapshot()
        self.assertEqual(len(snapshot), 1)
        self.assertIn(SYMBOL, snapshot)
        self.assertEqual(snapshot[SYMBOL].amount, DEFAULT_QUANTITY.amount)

    def test_symbols(self) -> None:
        """symbols returns all symbol names with positions."""
        ledger = PositionLedger()
        ledger.set_position(SYMBOL, DEFAULT_QUANTITY)
        symbols = ledger.symbols()
        self.assertEqual(symbols, [SYMBOL])


class ReservationRepositoryTest(unittest.TestCase):
    """Tests for ReservationRepository."""

    def test_implements_reservation_repository_port(self) -> None:
        """ReservationRepository must implement ReservationRepositoryPort."""
        repo = ReservationRepository()
        self.assertIsInstance(repo, ReservationRepositoryPort)

    def test_add_reservation(self) -> None:
        """Adding a reservation persists it."""
        repo = ReservationRepository()
        reservation = Reservation(
            key="key-1",
            order_id="ord-1",
            state=ReservationState.RESERVED,
            reserved_at="2024-01-01T00:00:00Z",
            updated_at="2024-01-01T00:00:00Z",
        )
        result = repo.add(reservation)
        self.assertIs(result, reservation)
        self.assertIs(repo.get("key-1"), reservation)

    def test_add_duplicate_raises(self) -> None:
        """Adding a duplicate key raises."""
        repo = ReservationRepository()
        reservation = Reservation(
            key="key-1",
            order_id="ord-1",
            state=ReservationState.RESERVED,
            reserved_at="2024-01-01T00:00:00Z",
            updated_at="2024-01-01T00:00:00Z",
        )
        repo.add(reservation)
        with self.assertRaises(ValueError):
            repo.add(reservation)

    def test_get_reservation(self) -> None:
        """get returns a reservation by key."""
        repo = ReservationRepository()
        reservation = Reservation(
            key="key-1",
            order_id="ord-1",
            state=ReservationState.RESERVED,
            reserved_at="2024-01-01T00:00:00Z",
            updated_at="2024-01-01T00:00:00Z",
        )
        repo.add(reservation)
        found = repo.get("key-1")
        self.assertIs(found, reservation)

    def test_get_not_found(self) -> None:
        """get returns None for non-existent key."""
        repo = ReservationRepository()
        found = repo.get("nonexistent")
        self.assertIsNone(found)

    def test_update_reservation(self) -> None:
        """update persists reservation state changes."""
        repo = ReservationRepository()
        reservation = Reservation(
            key="key-1",
            order_id="ord-1",
            state=ReservationState.RESERVED,
            reserved_at="2024-01-01T00:00:00Z",
            updated_at="2024-01-01T00:00:00Z",
        )
        repo.add(reservation)
        # Update state to SUBMITTED by creating new reservation
        updated = Reservation(
            key="key-1",
            order_id="ord-1",
            state=ReservationState.SUBMITTED,
            reserved_at="2024-01-01T00:00:00Z",
            updated_at="2024-01-01T00:01:00Z",
        )
        result = repo.update(updated)
        self.assertIs(result, updated)
        # Verify the update was persisted
        found = repo.get("key-1")
        self.assertEqual(found.state, ReservationState.SUBMITTED)

    def test_update_not_found_raises(self) -> None:
        """update raises for non-existent key."""
        repo = ReservationRepository()
        reservation = Reservation(
            key="key-1",
            order_id="ord-1",
            state=ReservationState.RESERVED,
            reserved_at="2024-01-01T00:00:00Z",
            updated_at="2024-01-01T00:00:00Z",
        )
        with self.assertRaises(KeyError):
            repo.update(reservation)

    def test_all_reservations(self) -> None:
        """all_reservations returns all reservations."""
        repo = ReservationRepository()
        res1 = Reservation(
            key="key-1",
            order_id="ord-1",
            state=ReservationState.RESERVED,
            reserved_at="2024-01-01T00:00:00Z",
            updated_at="2024-01-01T00:00:00Z",
        )
        res2 = Reservation(
            key="key-2",
            order_id="ord-2",
            state=ReservationState.SUBMITTED,
            reserved_at="2024-01-01T00:00:00Z",
            updated_at="2024-01-01T00:00:00Z",
        )
        repo.add(res1)
        repo.add(res2)
        all_res = repo.all_reservations()
        self.assertEqual(len(all_res), 2)
        self.assertIn(res1, all_res)
        self.assertIn(res2, all_res)

    def test_unknown_reservations(self) -> None:
        """unknown_reservations returns reservations in UNKNOWN state."""
        repo = ReservationRepository()
        res1 = Reservation(
            key="key-1",
            order_id="ord-1",
            state=ReservationState.RESERVED,
            reserved_at="2024-01-01T00:00:00Z",
            updated_at="2024-01-01T00:00:00Z",
        )
        res2 = Reservation(
            key="key-2",
            order_id="ord-2",
            state=ReservationState.UNKNOWN,
            reserved_at="2024-01-01T00:00:00Z",
            updated_at="2024-01-01T00:00:00Z",
        )
        res3 = Reservation(
            key="key-3",
            order_id="ord-3",
            state=ReservationState.SETTLED,
            reserved_at="2024-01-01T00:00:00Z",
            updated_at="2024-01-01T00:00:00Z",
        )
        repo.add(res1)
        repo.add(res2)
        repo.add(res3)
        unknown = repo.unknown_reservations()
        self.assertEqual(len(unknown), 1)
        self.assertIs(unknown[0], res2)

    def test_has_unknown(self) -> None:
        """has_unknown returns True if any reservation is UNKNOWN."""
        repo = ReservationRepository()
        res1 = Reservation(
            key="key-1",
            order_id="ord-1",
            state=ReservationState.RESERVED,
            reserved_at="2024-01-01T00:00:00Z",
            updated_at="2024-01-01T00:00:00Z",
        )
        repo.add(res1)
        # Not UNKNOWN yet
        self.assertFalse(repo.has_unknown())
        # Add UNKNOWN reservation
        res2 = Reservation(
            key="key-2",
            order_id="ord-2",
            state=ReservationState.UNKNOWN,
            reserved_at="2024-01-01T00:00:00Z",
            updated_at="2024-01-01T00:00:00Z",
        )
        repo.add(res2)
        self.assertTrue(repo.has_unknown())

    def test_delete_reservation(self) -> None:
        """delete removes a reservation."""
        repo = ReservationRepository()
        reservation = Reservation(
            key="key-1",
            order_id="ord-1",
            state=ReservationState.RESERVED,
            reserved_at="2024-01-01T00:00:00Z",
            updated_at="2024-01-01T00:00:00Z",
        )
        repo.add(reservation)
        repo.delete("key-1")
        self.assertIsNone(repo.get("key-1"))

    def test_concurrent_access(self) -> None:
        """Concurrent access is thread-safe."""
        repo = ReservationRepository()
        errors: list[BaseException] = []

        def add_reservation(i: int) -> None:
            try:
                reservation = Reservation(
                    key=f"key-{i}",
                    order_id=f"ord-{i}",
                    state=ReservationState.RESERVED,
                    reserved_at="2024-01-01T00:00:00Z",
                    updated_at="2024-01-01T00:00:00Z",
                )
                repo.add(reservation)
            except BaseException as e:
                errors.append(e)

        threads = [threading.Thread(target=add_reservation, args=(i,)) for i in range(10)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(errors, [])
        self.assertEqual(len(repo.all_reservations()), 10)


if __name__ == "__main__":
    unittest.main()
