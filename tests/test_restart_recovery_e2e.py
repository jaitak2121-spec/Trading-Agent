"""End-to-end integration tests for restart recovery and persistence.

This module verifies the end-to-end crash/restart recovery lifecycle across
the complete trading platform stack:

1. Clean restart with settled orders (executed / refused).
2. PENDING_NEW crash & operator-initiated reconciliation.
3. UNKNOWN crash & reconciliation gate blocking across restart.
4. Position ledger & portfolio consistency across restart.
5. Multi-order mixed-state recovery scan & batch resolution.
6. Idempotency key persistence preventing duplicate execution across restart.

INVARIANT 5: An UNKNOWN order blocks new orders until reconciled.
This blocking must survive process restart.

INVARIANT 12: Duplicate order submission must be prevented across process
restarts via persistent idempotency reservations.

INVARIANT: A PENDING_NEW order after restart is treated as potentially
submitted and blocks new orders until the venue is queried to determine
the actual state.

A note on the simulated venue, because it governs what these tests may
assert. :meth:`SimulatedBroker.script` queues an ack for the next
*placement* only; ``fetch_order_state`` answers from the orders the venue
actually recorded. So a recovery query cannot be scripted -- it reports
what the venue holds, which is the whole point of reconciliation. These
tests therefore assert the venue's own answer:

* An order the venue never received reconciles to REJECTED ("venue has no
  record of this order"). The system must not invent a fill for it.
* An order that landed behind an UNCERTAIN ack is recorded at the venue as
  a live order, so it reconciles to ACCEPTED.
"""

from __future__ import annotations

import unittest
from typing import Mapping

from trading.adapters.memory import SimulatedBroker
from trading.adapters.persistence import (
    OrderStore,
    PositionLedger,
    ReservationRepository,
)
from trading.adapters.recovery import RestartRecoveryCoordinator
from trading.core.audit import AuditLog, InMemoryAuditSink
from trading.core.authz import Principal, Role
from trading.core.breaker import BreakerRegistry, CircuitBreaker
from trading.core.clock import ManualClock
from trading.core.config import RiskConfig, TradingConfig
from trading.core.dedupe import IdempotencyRegistry, ReservationState
from trading.core.gateway import ExecutionGateway
from trading.core.killswitch import KillSwitch
from trading.core.modes import TradingMode, TradingModeMachine
from trading.core.money import USD, Money, Price, Quantity
from trading.core.orders import (
    Order,
    OrderIntent,
    OrderSide,
    OrderState,
    OrderType,
)
from trading.core.portfolio import Portfolio
from trading.core.reconciliation import ReconciliationGate
from trading.core.risk import RiskEngine
from trading.ports.broker import AckOutcome, BrokerAck

from .harness import ASSET, DEFAULT_PRICE, DEFAULT_QUANTITY, SYMBOL


class RestartRecoveryE2EFixture(unittest.TestCase):
    """Fixture providing repository persistence and rig recreation helpers."""

    def setUp(self) -> None:
        self.clock = ManualClock()

        # Persistent repositories that survive simulated restarts. Everything
        # else in the stack is rebuilt from scratch by _build_stack, so an
        # assertion that survives simulate_restart() is an assertion about
        # these three objects and nothing else.
        self.order_repository = OrderStore()
        self.reservation_repository = ReservationRepository()
        self.position_ledger = PositionLedger()

        # Shared principals
        self.strategy_id = Principal("strategy-1", Role.STRATEGY)
        self.risk_id = Principal("risk-1", Role.RISK_MANAGER)
        self.gateway_id = Principal("gateway-1", Role.EXECUTION_GATEWAY)
        self.operator_id = Principal("operator-1", Role.OPERATOR)

        # Build initial live environment
        self._build_stack()

    def _build_stack(
        self,
        *,
        broker: SimulatedBroker | None = None,
        clock: ManualClock | None = None,
    ) -> None:
        """Construct or reconstruct ephemeral components around persistent repos."""
        if clock is not None:
            self.clock = clock
        self.sink = InMemoryAuditSink()
        self.audit = AuditLog(self.sink, clock=self.clock)

        self.positions = self.position_ledger
        self.portfolio = Portfolio(Money("1000000.00", USD), ledger=self.positions)
        self.reconciliation = ReconciliationGate(
            self.positions,
            self.order_repository,
            self.audit,
            clock=self.clock,
            max_staleness_seconds=300.0,
        )
        self.dedupe = IdempotencyRegistry(
            self.audit, clock=self.clock, repository=self.reservation_repository
        )

        self.config = TradingConfig(
            live_trading=False,
            live_confirmation="",
            risk=RiskConfig(),
        )

        self.risk_engine = RiskEngine(
            self.config.risk,
            identity=self.risk_id,
            order_store=self.order_repository,
            audit=self.audit,
            clock=self.clock,
        )
        self.kill_switch = KillSwitch(
            self.audit, clock=self.clock, presence_probe=lambda _path: False
        )
        self.breakers = BreakerRegistry()
        self.breaker = self.breakers.add(
            CircuitBreaker(
                "broker", clock=self.clock, audit=self.audit, failure_threshold=3
            )
        )

        self.modes = TradingModeMachine(self.config, self.audit)
        self.modes.transition_to(
            TradingMode.PAPER, actor=self.operator_id.principal_id, reason="test"
        )

        if broker is None:
            self.broker = SimulatedBroker(
                clock=self.clock,
                default_outcome=AckOutcome.FILLED,
                fill_prices={SYMBOL: DEFAULT_PRICE},
            )
        else:
            self.broker = broker

        self.gateway = ExecutionGateway(
            identity=self.gateway_id,
            broker=self.broker,
            orders=self.order_repository,
            positions=self.portfolio,
            reconciliation=self.reconciliation,
            risk=self.risk_engine,
            dedupe=self.dedupe,
            kill_switch=self.kill_switch,
            breakers=self.breakers,
            modes=self.modes,
            config=self.config,
            audit=self.audit,
            clock=self.clock,
            token_ttl_seconds=30,
        )

        self.recovery = RestartRecoveryCoordinator(
            gateway=self.gateway,
            orders=self.order_repository,
            reservations=self.reservation_repository,
        )

    def intent(
        self,
        *,
        signal_id: str = "sig-1",
        symbol: str = SYMBOL,
        side: OrderSide = OrderSide.BUY,
        quantity: Quantity = DEFAULT_QUANTITY,
        strategy_id: str = "strat-1",
    ) -> OrderIntent:
        return OrderIntent(
            strategy_id=strategy_id,
            signal_id=signal_id,
            symbol=symbol,
            side=side,
            order_type=OrderType.MARKET,
            quantity=quantity,
        )

    def prices(self, **overrides: Price) -> Mapping[str, Price]:
        result: dict[str, Price] = {SYMBOL: DEFAULT_PRICE}
        result.update(overrides)
        return result

    def submit(self, intent: OrderIntent | None = None, **kwargs):
        if intent is None:
            intent = self.intent(**kwargs)
        return self.gateway.submit(
            intent, proposer=self.strategy_id, mark_prices=self.prices()
        )

    def crashed_pending_new(self, *, order_id: str, signal_id: str) -> Order:
        """An order that reached PENDING_NEW and a SUBMITTED reservation, then died.

        This is the state a process leaves behind when it is killed between
        handing the request to the venue and learning the answer. It is built
        by hand because there is no way to crash the gateway mid-submit, and
        the venue is deliberately *not* told about it: whether the request
        landed is exactly what recovery has to find out.
        """
        order = Order(
            self.intent(signal_id=signal_id),
            clock=self.clock,
            order_id=order_id,
        )
        order.transition_to(
            OrderState.PENDING_NEW, reason="handed to venue before crash"
        )
        self.order_repository.add(order)
        self.dedupe.reserve(order.idempotency_key, order.order_id)
        self.dedupe.mark_submitted(order.idempotency_key, note="sent to venue")
        return order

    def simulate_restart(self, *, broker: SimulatedBroker | None = None) -> None:
        """Simulate a full process crash and restart, preserving persistent repos."""
        # Advances clock to reflect downtime
        self.clock.advance(10.0)
        # Re-initialize all memory state around persistent repositories. The
        # venue survives too: it is an external system, and it does not forget
        # the orders it is holding because *our* process died. Handing
        # _build_stack a fresh SimulatedBroker here would quietly make every
        # recovery query answer "no record", which is the one answer a
        # reconciliation test must not get for free.
        self._build_stack(broker=broker if broker is not None else self.broker)


class TestRestartRecoveryEndToEnd(RestartRecoveryE2EFixture):
    """End-to-end crash and restart recovery tests."""

    def test_clean_restart_with_settled_orders(self) -> None:
        """Clean restart after normal settled orders allows immediate execution."""
        # 1. Submit multiple orders that settle normally (filled / rejected)
        res1 = self.submit(signal_id="sig-101")
        self.assertEqual(res1.outcome, "executed")

        self.broker.script(BrokerAck(outcome=AckOutcome.REJECTED), lands_at_venue=False)
        res2 = self.submit(signal_id="sig-102")
        # A venue rejection is a refusal at the execution stage, not a third
        # outcome: the submission chain has exactly executed / refused / unknown.
        self.assertEqual(res2.outcome, "refused")
        self.assertEqual(res2.gate, "execution")
        self.assertEqual(res2.order.state, OrderState.REJECTED)

        # 2. Crash and restart
        self.simulate_restart()

        # 3. Scan persistent state via recovery coordinator
        report = self.recovery.scan()
        self.assertEqual(report.total_orders, 2)
        self.assertEqual(len(report.ambiguous_orders), 0)
        self.assertEqual(len(report.unknown_orders), 0)
        self.assertEqual(len(report.pending_new_orders), 0)
        self.assertFalse(report.has_ambiguous_orders)
        self.assertFalse(report.has_unknown)

        # 4. New order submissions through restarted gateway succeed immediately
        res3 = self.submit(signal_id="sig-103")
        self.assertEqual(res3.outcome, "executed")
        self.assertEqual(len(self.order_repository.all_orders()), 3)

    def test_pending_new_crash_and_operator_reconciliation(self) -> None:
        """A PENDING_NEW order the venue never received reconciles to REJECTED."""
        # 1. An order that crashed in PENDING_NEW after reaching SUBMITTED
        order = self.crashed_pending_new(
            order_id="ord-crashed-201", signal_id="sig-201"
        )

        # 2. Crash and restart
        self.simulate_restart()

        # 3. Recovery scan detects PENDING_NEW as ambiguous
        report = self.recovery.scan()
        self.assertEqual(report.total_orders, 1)
        self.assertEqual(len(report.ambiguous_orders), 1)
        self.assertEqual(len(report.pending_new_orders), 1)
        self.assertEqual(report.ambiguous_orders[0].order.order_id, order.order_id)
        self.assertIn("PENDING_NEW", report.ambiguous_orders[0].reason)

        # 4. The operator asks the venue what happened. The venue holds no
        #    record of this order, so the honest answer is that it never
        #    existed -- the request died with the process before it landed.
        ack = self.recovery.sync_order(order, operator=self.operator_id)
        self.assertEqual(ack.outcome, AckOutcome.REJECTED)
        self.assertIn("no record", ack.message)

        # 5. Order is now settled in persistent storage. Nothing was filled:
        #    an ambiguous order must never resolve into an invented position.
        reconciled_order = self.order_repository.get(order.order_id)
        self.assertEqual(reconciled_order.state, OrderState.REJECTED)
        self.assertTrue(reconciled_order.filled_quantity.is_zero)
        self.assertEqual(
            self.position_ledger.position(SYMBOL, asset=ASSET),
            Quantity.zero(ASSET),
        )

        # 6. The reservation is now SETTLED. `sync_order` closes the key out
        #    when it drives the order to a terminal state, the same as `_settle`
        #    does on the submit path -- so a finished order no longer lingers in
        #    SUBMITTED / `in_flight()`. This does not weaken INVARIANT 12: a
        #    SETTLED key is as un-reusable as a SUBMITTED one (reserve() refuses
        #    a key in any state, and SETTLED has no successor), so a duplicate is
        #    still refused. See CLAUDE_HANDOFF.md §7 item 12.
        reservation = self.reservation_repository.get(order.idempotency_key)
        self.assertIsNotNone(reservation)
        self.assertEqual(reservation.state, ReservationState.SETTLED)
        self.assertTrue(self.dedupe.is_claimed(order.idempotency_key))

        # 7. Subsequent scan confirms clean state -- a REJECTED order is
        #    terminal and no longer ambiguous.
        clean_report = self.recovery.scan()
        self.assertFalse(clean_report.has_ambiguous_orders)
        self.assertFalse(clean_report.has_unknown)

        # 8. New submissions succeed
        res_new = self.submit(signal_id="sig-202")
        self.assertEqual(res_new.outcome, "executed")

    def test_sync_settles_reservation_without_permitting_a_duplicate(self) -> None:
        """Settling a recovered order's reservation must not open an INVARIANT 12 hole.

        A crashed PENDING_NEW order recovered via ``sync_order`` to a terminal
        state now has a SETTLED reservation rather than a lingering SUBMITTED
        one, and the finished order no longer shows in ``in_flight()``. The key
        stays permanently claimed, so re-submitting the identical intent is
        still refused at the duplicate gate: settling relabels the key, it does
        not free it.
        """
        order = self.crashed_pending_new(
            order_id="ord-crashed-dup", signal_id="sig-dup-guard"
        )
        self.simulate_restart()

        # Recover: the venue has no record, so it reconciles to REJECTED.
        ack = self.recovery.sync_order(order, operator=self.operator_id)
        self.assertEqual(ack.outcome, AckOutcome.REJECTED)
        self.assertEqual(order.state, OrderState.REJECTED)

        # The reservation is settled, and the finished order has left in_flight().
        reservation = self.reservation_repository.get(order.idempotency_key)
        self.assertEqual(reservation.state, ReservationState.SETTLED)
        in_flight_ids = {r.order_id for r in self.dedupe.in_flight()}
        self.assertNotIn(order.order_id, in_flight_ids)

        # INVARIANT 12 still holds: the same intent cannot become a second order.
        dup = self.submit(signal_id="sig-dup-guard")
        self.assertEqual(dup.outcome, "refused")
        self.assertEqual(dup.gate, "duplicate_order")
        self.assertEqual(self.broker.duplicate_keys, frozenset())

    def test_cancel_settles_reservation_without_permitting_a_duplicate(self) -> None:
        """The cancel path settles the reservation too, with the same guarantee.

        A crashed PENDING_NEW order has a SUBMITTED reservation and never ran the
        submit path, so only ``cancel`` can settle it here -- which makes this a
        direct test that the cancel-path settlement fires, not that submit
        already did it. The key stays claimed, so the intent cannot be
        resubmitted as a duplicate.
        """
        order = self.crashed_pending_new(
            order_id="ord-crashed-cancel", signal_id="sig-cancel-guard"
        )
        # Precondition: the reservation is SUBMITTED (crashed mid-submit), so a
        # settlement after cancel can only have come from the cancel path.
        before = self.reservation_repository.get(order.idempotency_key)
        self.assertEqual(before.state, ReservationState.SUBMITTED)

        self.gateway.cancel(order, operator=self.operator_id)
        self.assertEqual(order.state, OrderState.CANCELED)

        reservation = self.reservation_repository.get(order.idempotency_key)
        self.assertEqual(reservation.state, ReservationState.SETTLED)

        dup = self.submit(signal_id="sig-cancel-guard")
        self.assertEqual(dup.outcome, "refused")
        self.assertEqual(dup.gate, "duplicate_order")

    def test_unknown_order_blocks_across_restart_until_resolved(self) -> None:
        """UNKNOWN order blocks new submissions across restart until explicitly resolved."""
        # 1. Submit order that returns UNCERTAIN, having actually landed
        uncertain_ack = BrokerAck(outcome=AckOutcome.UNCERTAIN)
        self.broker.script(uncertain_ack, lands_at_venue=True)
        res = self.submit(signal_id="sig-301")
        self.assertEqual(res.outcome, "unknown")
        unknown_order_id = res.order.order_id

        # 2. Crash and restart
        self.simulate_restart()

        # 3. Recovery scan detects UNKNOWN order
        report = self.recovery.scan()
        self.assertTrue(report.has_unknown)
        self.assertTrue(report.has_ambiguous_orders)
        self.assertEqual(len(report.unknown_orders), 1)
        self.assertEqual(report.unknown_orders[0].order_id, unknown_order_id)

        # 4. Attempting new order submission is refused by reconciliation gate
        #    (INVARIANT 5). The block survived the restart.
        refused_res = self.submit(signal_id="sig-302")
        self.assertEqual(refused_res.outcome, "refused")
        self.assertEqual(refused_res.gate, "reconciliation")

        # 5. Operator resolves the UNKNOWN order. The venue recorded the order
        #    as live when it landed behind the uncertain ack, so it answers
        #    ACCEPTED -- the order exists and is still working.
        unknown_order = self.order_repository.get(unknown_order_id)
        ack = self.recovery.resolve_unknown(unknown_order, operator=self.operator_id)
        self.assertEqual(ack.outcome, AckOutcome.ACCEPTED)
        self.assertEqual(unknown_order.state, OrderState.ACCEPTED)

        # The reservation leaves UNKNOWN through reconciliation, not expiry.
        reservation = self.reservation_repository.get(unknown_order.idempotency_key)
        self.assertEqual(reservation.state, ReservationState.SETTLED)

        # 6. Recovery scan confirms UNKNOWN is cleared
        report_after = self.recovery.scan()
        self.assertFalse(report_after.has_unknown)
        self.assertFalse(report_after.has_ambiguous_orders)

        # 7. New order submission now passes and executes. The key from the
        #    refused attempt in step 4 was released, so the same signal is
        #    free to be submitted again.
        res_unblocked = self.submit(signal_id="sig-302")
        self.assertEqual(res_unblocked.outcome, "executed")

    def test_position_and_portfolio_consistency_across_restart(self) -> None:
        """Position ledger and portfolio quantities remain consistent across restart."""
        # 1. Fill a buy order. 0.002 BTC at the harness price is 100.00 USD,
        #    exactly the default max_order_notional ceiling -- the largest
        #    single order the risk engine will pass, which keeps this test
        #    about persistence rather than about sizing.
        bought = Quantity("0.002", ASSET)
        res_buy = self.submit(signal_id="sig-401", side=OrderSide.BUY, quantity=bought)
        self.assertEqual(res_buy.outcome, "executed")
        self.assertEqual(self.position_ledger.position(SYMBOL, asset=ASSET), bought)

        # 2. Crash and restart
        self.simulate_restart()

        # 3. The rebuilt portfolio reads the surviving ledger, not a fresh one.
        self.assertEqual(self.positions.position(SYMBOL, asset=ASSET), bought)
        self.assertEqual(
            self.portfolio.position(SYMBOL, asset=ASSET).quantity, bought
        )

        # The rebuilt reconciliation gate was handed that same ledger, so it
        # agrees with the venue across the restart boundary (INVARIANT 6).
        # Asserting through reconcile() rather than the gate's internals is
        # the point: a gate reading a fresh ledger would report a discrepancy.
        report = self.reconciliation.reconcile(self.broker.fetch_positions().positions)
        self.assertTrue(report.is_clean, report.as_details())
        self.assertFalse(self.reconciliation.has_mismatch)

        # 4. Fill a sell order on the reconstructed rig
        sold = Quantity("0.001", ASSET)
        res_sell = self.submit(signal_id="sig-402", side=OrderSide.SELL, quantity=sold)
        self.assertEqual(res_sell.outcome, "executed")

        # 5. The position decrements across the restart boundary, and the
        #    ledger and the portfolio still agree about what is held.
        remaining = Quantity("0.001", ASSET)
        self.assertEqual(
            self.position_ledger.position(SYMBOL, asset=ASSET), remaining
        )
        self.assertEqual(
            self.portfolio.position(SYMBOL, asset=ASSET).quantity, remaining
        )

    def test_mixed_state_multi_order_recovery_e2e(self) -> None:
        """Complex recovery with multiple orders in different states."""
        # Order 1: Filled
        self.submit(signal_id="sig-501")

        # Order 2: Rejected by the venue
        self.broker.script(BrokerAck(outcome=AckOutcome.REJECTED), lands_at_venue=False)
        self.submit(signal_id="sig-502")

        # Order 3: Unknown, and it really did land
        self.broker.script(BrokerAck(outcome=AckOutcome.UNCERTAIN), lands_at_venue=True)
        res3 = self.submit(signal_id="sig-503")

        # Order 4: Simulated crash in PENDING_NEW with SUBMITTED reservation
        order4 = self.crashed_pending_new(
            order_id="ord-crashed-504", signal_id="sig-504"
        )

        # Crash and restart
        self.simulate_restart()

        # Scan persistent state
        report = self.recovery.scan()
        self.assertEqual(report.total_orders, 4)
        self.assertEqual(len(report.unknown_orders), 1)
        self.assertEqual(report.unknown_orders[0].order_id, res3.order.order_id)
        self.assertEqual(len(report.pending_new_orders), 1)
        self.assertEqual(report.pending_new_orders[0].order_id, order4.order_id)
        self.assertEqual(len(report.ambiguous_orders), 2)
        self.assertTrue(report.has_unknown)
        self.assertTrue(report.has_ambiguous_orders)

        # Reconcile Order 3 (UNKNOWN). It landed, so the venue holds it.
        order3 = self.order_repository.get(res3.order.order_id)
        ack3 = self.recovery.resolve_unknown(order3, operator=self.operator_id)
        self.assertEqual(ack3.outcome, AckOutcome.ACCEPTED)

        # Reconcile Order 4 (PENDING_NEW). It never landed, so the venue has
        # no record and the order is rejected rather than assumed filled.
        ack4 = self.recovery.sync_order(order4, operator=self.operator_id)
        self.assertEqual(ack4.outcome, AckOutcome.REJECTED)
        self.assertEqual(order4.state, OrderState.REJECTED)

        # Re-scan confirms complete resolution
        report_after = self.recovery.scan()
        self.assertEqual(report_after.total_orders, 4)
        self.assertFalse(report_after.has_ambiguous_orders)
        self.assertFalse(report_after.has_unknown)

        # Only the two orders that genuinely filled moved the ledger: order 1
        # at submit time, and nothing from the two reconciled orders.
        self.assertEqual(
            self.position_ledger.position(SYMBOL, asset=ASSET), DEFAULT_QUANTITY
        )

        # Subsequent new order executes cleanly
        res5 = self.submit(signal_id="sig-505")
        self.assertEqual(res5.outcome, "executed")

    def test_idempotency_key_persistence_prevents_duplicate_after_restart(
        self,
    ) -> None:
        """INVARIANT 12: Idempotency keys survive restart and prevent duplicate orders."""
        # 1. Submit order with specific signal_id
        res1 = self.submit(signal_id="sig-duplicate-check")
        self.assertEqual(res1.outcome, "executed")
        original_key = res1.order.idempotency_key

        # 2. Crash and restart
        self.simulate_restart()

        # 3. Attempt to submit with identical strategy_id and signal_id
        dup_intent = self.intent(signal_id="sig-duplicate-check")
        self.assertEqual(dup_intent.idempotency_key, original_key)

        dup_res = self.gateway.submit(
            dup_intent, proposer=self.strategy_id, mark_prices=self.prices()
        )

        # 4. Refused at Gate 6 (duplicate_order)
        self.assertEqual(dup_res.outcome, "refused")
        self.assertEqual(dup_res.gate, "duplicate_order")
        self.assertIn("already claimed", dup_res.reason)

        # 5. The venue is the witness: it never saw the key twice.
        self.assertEqual(self.broker.duplicate_keys, frozenset())


if __name__ == "__main__":
    unittest.main()
