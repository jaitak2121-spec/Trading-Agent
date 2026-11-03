"""Crash-safe order persistence and the gateway's write-before-send rule."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from trading.adapters.memory import SimulatedBroker
from trading.adapters.persistence import (
    DurableOrderStore,
    DurableReservationRepository,
    PersistenceError,
)
from trading.core.clock import ManualClock
from trading.core.money import USD, Price, Quantity
from trading.core.orders import Order, OrderIntent, OrderSide, OrderState
from trading.ports.broker import AckOutcome, BrokerAck

from .harness import DEFAULT_PRICE, build_rig


class TestDurableOrderStore(unittest.TestCase):
    def setUp(self) -> None:
        self.path = Path(tempfile.mkdtemp()) / "orders.json"
        self.clock = ManualClock()

    def order(self, signal_id: str = "sig-1") -> Order:
        return Order(
            OrderIntent(
                strategy_id="strategy-1",
                signal_id=signal_id,
                symbol="BTCUSD",
                side=OrderSide.BUY,
                quantity=Quantity("0.001", "BTC"),
            ),
            clock=self.clock,
        )

    def reload(self) -> DurableOrderStore:
        return DurableOrderStore(self.path, clock=self.clock)

    def test_missing_file_is_an_empty_store(self) -> None:
        self.assertEqual(self.reload().all_orders(), [])

    def test_added_order_survives_process_reconstruction(self) -> None:
        store = self.reload()
        order = self.order()
        store.add(order)

        restored = self.reload().get(order.order_id)

        self.assertEqual(restored.state, OrderState.DRAFT)
        self.assertEqual(restored.intent, order.intent)
        self.assertEqual(restored.idempotency_key, order.idempotency_key)

    def test_update_persists_state_fill_and_venue_binding(self) -> None:
        store = self.reload()
        order = self.order()
        store.add(order)
        order.transition_to(OrderState.PENDING_NEW, reason="submitted")
        order.attach_broker_order_id("venue-17")
        order.apply_fill(Quantity("0.001", "BTC"), DEFAULT_PRICE)
        store.update(order)

        restored = self.reload().get(order.order_id)

        self.assertEqual(restored.state, OrderState.FILLED)
        self.assertEqual(restored.filled_quantity, Quantity("0.001", "BTC"))
        self.assertEqual(restored.average_fill_price, DEFAULT_PRICE)
        self.assertEqual(restored.broker_order_id, "venue-17")
        self.assertEqual(
            [(t.from_state, t.to_state) for t in restored.history],
            [(t.from_state, t.to_state) for t in order.history],
        )

    def test_unknown_order_still_blocks_store_queries_after_reload(self) -> None:
        store = self.reload()
        order = self.order()
        store.add(order)
        order.transition_to(OrderState.PENDING_NEW, reason="submitted")
        order.mark_unknown(reason="connection lost")
        store.update(order)

        restored = self.reload()

        self.assertTrue(restored.has_unknown_orders())
        self.assertEqual(restored.unknown_orders()[0].order_id, order.order_id)

    def test_update_of_an_unstored_order_is_refused(self) -> None:
        with self.assertRaises(KeyError):
            self.reload().update(self.order())

    def test_corrupt_json_fails_closed(self) -> None:
        self.path.write_text("{broken", encoding="utf-8")
        with self.assertRaises(PersistenceError):
            self.reload()

    def test_malformed_order_collection_fails_closed(self) -> None:
        self.path.write_text(
            json.dumps({"version": 1, "orders": {"not": "a list"}}),
            encoding="utf-8",
        )
        with self.assertRaises(PersistenceError):
            self.reload()

    def test_malformed_order_entry_fails_closed(self) -> None:
        self.path.write_text(
            json.dumps({"version": 1, "orders": ["not an order"]}),
            encoding="utf-8",
        )
        with self.assertRaises(PersistenceError):
            self.reload()

    def test_impossible_state_and_history_fail_closed(self) -> None:
        order = self.order()
        payload = order.to_dict()
        payload["state"] = "filled"
        self.path.write_text(
            json.dumps({"version": 1, "orders": [payload]}), encoding="utf-8"
        )
        with self.assertRaises(PersistenceError):
            self.reload()

    def test_atomic_write_leaves_no_temp_file(self) -> None:
        self.reload().add(self.order())
        self.assertEqual(list(self.path.parent.glob("*.tmp")), [])


class InspectingBroker(SimulatedBroker):
    """Reads the durable store at the exact moment the venue is called."""

    def __init__(self, *, path: Path, clock: ManualClock) -> None:
        super().__init__(
            clock=clock,
            default_outcome=AckOutcome.FILLED,
            fill_prices={"BTCUSD": Price("50000", USD)},
        )
        self.path = path
        self.observed_state: OrderState | None = None

    def place_order(self, order, *, token):
        restored = DurableOrderStore(self.path, clock=self._clock)
        self.observed_state = restored.get(order.order_id).state
        return super().place_order(order, token=token)


class TestGatewayDurableOrderCheckpoints(unittest.TestCase):
    def setUp(self) -> None:
        self.path = Path(tempfile.mkdtemp()) / "orders.json"
        self.clock = ManualClock()

    def test_pending_new_is_on_disk_before_the_broker_is_called(self) -> None:
        store = DurableOrderStore(self.path, clock=self.clock)
        broker = InspectingBroker(path=self.path, clock=self.clock)
        rig = build_rig(clock=self.clock, broker=broker, order_store=store)

        result = rig.submit()

        self.assertEqual(result.outcome, "executed")
        self.assertEqual(broker.observed_state, OrderState.PENDING_NEW)
        final = DurableOrderStore(self.path, clock=self.clock).get(result.order.order_id)
        self.assertEqual(final.state, OrderState.FILLED)
        self.assertEqual(final.filled_quantity, result.order.filled_quantity)

    def test_uncertain_outcome_is_restored_as_unknown(self) -> None:
        store = DurableOrderStore(self.path, clock=self.clock)
        broker = SimulatedBroker(clock=self.clock)
        broker.script(BrokerAck(AckOutcome.UNCERTAIN), lands_at_venue=True)
        rig = build_rig(clock=self.clock, broker=broker, order_store=store)

        result = rig.submit()
        restored = DurableOrderStore(self.path, clock=self.clock)

        self.assertEqual(result.outcome, "unknown")
        self.assertTrue(restored.has_unknown_orders())
        self.assertEqual(
            restored.get(result.order.order_id).state,
            OrderState.UNKNOWN,
        )

    def test_restart_keeps_unknown_blocking_until_gateway_reconciliation(self) -> None:
        reservation_path = self.path.with_name("reservations.json")
        orders = DurableOrderStore(self.path, clock=self.clock)
        reservations = DurableReservationRepository(reservation_path)
        broker = SimulatedBroker(clock=self.clock)
        broker.script(BrokerAck(AckOutcome.UNCERTAIN), lands_at_venue=True)
        first = build_rig(
            clock=self.clock,
            broker=broker,
            order_store=orders,
            reservation_repository=reservations,
        )
        unknown = first.submit()

        # New kernel objects, same durable files and external venue.
        restored_orders = DurableOrderStore(self.path, clock=self.clock)
        restored_reservations = DurableReservationRepository(reservation_path)
        restarted = build_rig(
            clock=self.clock,
            broker=broker,
            order_store=restored_orders,
            reservation_repository=restored_reservations,
        )

        blocked = restarted.submit(signal_id="unrelated-after-restart")
        self.assertEqual(blocked.outcome, "refused")
        self.assertEqual(blocked.gate, "reconciliation")

        restored_unknown = restored_orders.get(unknown.order.order_id)
        restarted.gateway.resolve_unknown(
            restored_unknown, operator=restarted.operator_id
        )

        self.assertFalse(restored_orders.has_unknown_orders())
        self.assertFalse(restored_reservations.has_unknown())
        persisted = DurableOrderStore(self.path, clock=self.clock)
        self.assertEqual(
            persisted.get(unknown.order.order_id).state,
            OrderState.ACCEPTED,
        )


if __name__ == "__main__":
    unittest.main()
