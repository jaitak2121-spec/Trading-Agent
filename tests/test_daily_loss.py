"""Tests for realized profit and loss reaching the daily-loss limit.

Before this wiring, ``PnlLedger`` had no production caller: the limit read a
counter that only tests ever incremented, so `MAX_DAILY_LOSS` could not fire on a
real losing day. What is worth proving here is therefore not the arithmetic --
`test_portfolio.py` already pins that -- but that the number the portfolio
computes is the number the limit acts on, for every fill, and that "we could not
compute it" does not arrive at the limit disguised as zero.

The integration tests drive whole orders through the gateway rather than calling
``PnlLedger.record`` directly, because the thing that was broken was the wiring.
A test that records into the ledger by hand would have passed before this change.
"""

from __future__ import annotations

import datetime as dt
import unittest

from tests.harness import ASSET, DEFAULT_PRICE, SYMBOL, build_rig
from trading.core.clock import ManualClock
from trading.core.config import RiskConfig
from trading.core.errors import RiskLimitExceeded, SafetyViolation
from trading.core.gateway import ExecutionGate
from trading.core.money import INR, USD, Money, Price, Quantity
from trading.core.orders import OrderSide
from trading.core.risk import PnlLedger, RiskLimit

# A budget small enough that a plausible price move exhausts it. The notional
# ceilings are left at their defaults, so nothing else about the rig changes.
TIGHT = RiskConfig(max_daily_loss=Money("0.50", USD))


def usd(amount: str) -> Money:
    return Money(amount, USD)


class GatewayFills(unittest.TestCase):
    """One long round trip at a time, at prices the test chooses."""

    def setUp(self) -> None:
        self.rig = build_rig(risk=TIGHT)

    def buy(self, price: str = "50000"):
        self.rig.broker.set_fill_price(SYMBOL, Price(price, USD))
        return self.rig.submit(mark_prices={SYMBOL: Price(price, USD)})

    def sell(self, price: str):
        self.rig.broker.set_fill_price(SYMBOL, Price(price, USD))
        return self.rig.submit(
            side=OrderSide.SELL, mark_prices={SYMBOL: Price(price, USD)}
        )

    def assertRefusedForDailyLoss(self, result) -> None:
        self.assertTrue(result.is_refused, f"expected a refusal, got {result.outcome}")
        self.assertEqual(result.gate, ExecutionGate.RISK)
        self.assertIn(RiskLimit.DAILY_LOSS.value, result.reason)


class TestARealizedLossReachesTheLimit(GatewayFills):
    def test_a_losing_round_trip_spends_the_days_budget(self) -> None:
        self.assertEqual(self.rig.risk.pnl.realized, usd("0.00"))
        self.assertTrue(self.buy().is_executed)
        # Opening realizes nothing, so the budget is untouched by the buy alone.
        self.assertEqual(self.rig.risk.pnl.realized, usd("0.00"))
        self.assertTrue(self.sell("49000").is_executed)
        # 0.001 BTC bought at 50000 and sold at 49000 loses 1.00 USD.
        self.assertEqual(self.rig.risk.pnl.realized, usd("-1.00"))
        self.assertEqual(self.rig.risk.pnl.realized_loss, usd("1.00"))

    def test_the_allowance_shrinks_by_exactly_the_realized_loss(self) -> None:
        # A loss smaller than the budget leaves the difference available, and the
        # next opening order still goes through on it.
        self.buy()
        self.sell("49700")
        self.assertEqual(self.rig.risk.pnl.realized_loss, usd("0.30"))
        self.assertLess(
            self.rig.risk.pnl.realized_loss.amount, TIGHT.max_daily_loss.amount
        )
        self.assertTrue(self.buy().is_executed)

    def test_the_limit_refuses_a_new_order_once_the_budget_is_spent(self) -> None:
        self.buy()
        self.sell("49000")
        self.assertRefusedForDailyLoss(self.buy())

    def test_the_refusal_names_the_loss_and_the_budget(self) -> None:
        self.buy()
        self.sell("49000")
        reason = self.buy().reason
        self.assertIn("1.00", reason)
        self.assertIn("0.50", reason)

    def test_closing_is_still_allowed_once_the_budget_is_spent(self) -> None:
        """Spending the budget must not trap us in the position that spent it."""
        self.buy()
        self.sell("49000")
        self.assertEqual(self.rig.risk.pnl.realized_loss, usd("1.00"))
        self.buy()  # refused -- the budget is gone
        self.rig.positions.set_position(SYMBOL, Quantity("0.001", ASSET))
        self.assertTrue(self.sell("49000").is_executed)

    def test_a_loss_at_exactly_the_budget_refuses(self) -> None:
        # The check is >=, so the boundary is a breach rather than the last
        # permitted trade.
        self.buy()
        self.sell("49500")
        self.assertEqual(self.rig.risk.pnl.realized_loss, usd("0.50"))
        self.assertRefusedForDailyLoss(self.buy())

    def test_a_loss_one_cent_short_of_the_budget_still_trades(self) -> None:
        self.buy()
        self.sell("49510")
        self.assertEqual(self.rig.risk.pnl.realized_loss, usd("0.49"))
        self.assertTrue(self.buy().is_executed)


class TestRealizedGains(GatewayFills):
    def test_a_winning_round_trip_leaves_the_budget_intact(self) -> None:
        self.buy()
        self.assertTrue(self.sell("51000").is_executed)
        self.assertEqual(self.rig.risk.pnl.realized, usd("1.00"))
        # A profit is not a negative loss: the loss figure floors at zero.
        self.assertEqual(self.rig.risk.pnl.realized_loss, usd("0.00"))
        self.assertTrue(self.buy().is_executed)

    def test_a_gain_offsets_an_earlier_loss(self) -> None:
        # A winning round trip first, so the day stays green and the next buy is
        # allowed, then a losing one that nets the day back to flat.
        self.buy()
        self.assertTrue(self.sell("51000").is_executed)  # +1.00
        self.assertEqual(self.rig.risk.pnl.realized, usd("1.00"))
        self.assertTrue(self.buy().is_executed)  # allowed: no loss on a green day
        self.assertTrue(self.sell("49000").is_executed)  # -1.00
        # +1.00 then -1.00 nets to flat, and the day is still tradeable.
        self.assertEqual(self.rig.risk.pnl.realized, usd("0.00"))
        self.assertTrue(self.buy().is_executed)

    def test_a_gain_does_not_create_headroom_beyond_the_budget(self) -> None:
        self.buy()
        self.sell("51000")  # +1.00
        self.assertEqual(self.rig.risk.pnl.realized_loss, usd("0.00"))


class TestAcrossMultipleFills(GatewayFills):
    def test_losses_accumulate_across_round_trips(self) -> None:
        # Two 0.20 losses stay under a 0.50 budget; the third crosses it.
        for expected in ("-0.20", "-0.40"):
            self.assertTrue(self.buy().is_executed)
            self.assertTrue(self.sell("49800").is_executed)
            self.assertEqual(self.rig.risk.pnl.realized, usd(expected))
        self.assertTrue(self.buy().is_executed)
        self.assertTrue(self.sell("49800").is_executed)
        self.assertEqual(self.rig.risk.pnl.realized, usd("-0.60"))
        self.assertRefusedForDailyLoss(self.buy())

    def test_a_partial_close_realizes_only_what_it_closed(self) -> None:
        # Two lots at different prices, then close one. The average entry is
        # 49500, so selling one at 49500 realizes nothing.
        self.buy("50000")
        self.buy("49000")
        self.assertEqual(
            self.rig.portfolio.position(SYMBOL, asset=ASSET).average_entry_price,
            Price("49500", USD),
        )
        self.assertTrue(self.sell("49500").is_executed)
        self.assertEqual(self.rig.risk.pnl.realized, usd("0.00"))
        # The remaining lot still carries the same basis, so closing it below
        # that average is the loss that lands.
        self.assertTrue(self.sell("49000").is_executed)
        self.assertEqual(self.rig.risk.pnl.realized, usd("-0.50"))

    def test_the_days_total_survives_an_intervening_refusal(self) -> None:
        self.buy()
        self.sell("49800")
        self.assertEqual(self.rig.risk.pnl.realized, usd("-0.20"))
        # A refused order touches nothing: no fill, so no realized change.
        refused = self.rig.submit(quantity=Quantity("1", ASSET))
        self.assertTrue(refused.is_refused)
        self.assertEqual(self.rig.risk.pnl.realized, usd("-0.20"))

    def test_the_budget_resets_at_the_utc_day_boundary(self) -> None:
        self.buy()
        self.sell("49000")
        self.assertRefusedForDailyLoss(self.buy())
        tomorrow = self.rig.clock.now() + dt.timedelta(days=1)
        self.rig.clock.set_wall_clock(tomorrow)
        self.assertEqual(self.rig.risk.pnl.realized, usd("0.00"))
        self.assertTrue(self.buy().is_executed)


class TestFillsFoundDuringRecoveryAlsoCount(unittest.TestCase):
    """A fill discovered by reconciliation is as real as one we watched."""

    def test_a_loss_discovered_during_recovery_reaches_the_limit(self) -> None:
        from trading.ports.broker import AckOutcome, BrokerAck, BrokerPositionSnapshot

        rig = build_rig(risk=TIGHT)
        # Establish a long at 50000 the ordinary way.
        self.assertTrue(rig.submit().is_executed)
        self.assertEqual(rig.risk.pnl.realized, usd("0.00"))

        # Now a closing sell whose ack never comes back, which the venue in fact
        # filled at a loss.
        rig.broker.script(
            BrokerAck(AckOutcome.UNCERTAIN, message="timeout"), lands_at_venue=True
        )
        order = rig.submit(side=OrderSide.SELL).order
        self.assertTrue(order.is_unknown)
        filled = Quantity("0.001", ASSET)
        ack = BrokerAck(
            AckOutcome.FILLED,
            broker_order_id="venue-filled-1",
            filled_quantity=filled,
            fill_price=Price("49000", USD),
        )

        class VenueFilledIt:
            def fetch_order_state(self, _order):
                return ack

            def fetch_positions(self):
                return BrokerPositionSnapshot({})

        rig.gateway._broker = VenueFilledIt()
        rig.gateway.resolve_unknown(order, operator=rig.operator_id)
        # The loss was invisible until reconciliation; it counts from now on.
        self.assertEqual(rig.risk.pnl.realized, usd("-1.00"))
        self.assertEqual(rig.risk.pnl.realized_loss, usd("1.00"))


class TestAnUncomputableRealizedAmountIsNotZero(unittest.TestCase):
    """The hole a naive wiring would leave.

    Closing a position adopted from the venue realizes an amount we cannot
    compute, because we never saw what was paid. Recording that as zero would
    leave the loss budget looking intact after a loss of unknown size.
    """

    def setUp(self) -> None:
        self.rig = build_rig(risk=TIGHT)
        # Adopt a long we have no basis for, the way an operator would after the
        # venue turned out to be right and we were wrong.
        self.rig.reconciliation.adopt_broker_positions(
            self.rig.operator_id,
            reason="test: venue is authoritative",
            broker_positions={SYMBOL: Quantity("0.002", ASSET)},
        )
        self.assertFalse(
            self.rig.portfolio.position(SYMBOL, asset=ASSET).basis_is_known
        )

    def sell(self, quantity: str, price: str = "50000"):
        self.rig.broker.set_fill_price(SYMBOL, Price(price, USD))
        return self.rig.submit(
            side=OrderSide.SELL,
            quantity=Quantity(quantity, ASSET),
            mark_prices={SYMBOL: Price(price, USD)},
        )

    def test_closing_an_adopted_position_marks_the_day_incomplete(self) -> None:
        self.assertTrue(self.rig.risk.pnl.is_complete)
        self.assertTrue(self.sell("0.001").is_executed)
        # Nothing computable was realized, so the total is unchanged -- and now
        # known to be missing something.
        self.assertEqual(self.rig.risk.pnl.realized, usd("0.00"))
        self.assertFalse(self.rig.risk.pnl.is_complete)
        self.assertEqual(self.rig.risk.pnl.unattributed_fills, 1)

    def test_an_incomplete_day_refuses_a_new_opening_order(self) -> None:
        self.sell("0.001")
        result = self.rig.submit()
        self.assertTrue(result.is_refused)
        self.assertEqual(result.gate, ExecutionGate.RISK)
        self.assertIn(RiskLimit.DAILY_LOSS.value, result.reason)
        self.assertIn("no known cost basis", result.reason)

    def test_an_incomplete_day_still_allows_de_risking(self) -> None:
        # Refusing to let an operator out of a position we cannot value would be
        # the opposite of safe.
        self.sell("0.001")
        self.assertFalse(self.rig.risk.pnl.is_complete)
        self.assertTrue(self.sell("0.001").is_executed)

    def test_adding_to_an_unknown_basis_does_not_mark_the_day_incomplete(self) -> None:
        # Buying more realizes nothing at all, so its zero is a real zero. The
        # basis stays unknown, but the day's total is not missing anything.
        self.assertTrue(self.rig.submit().is_executed)
        self.assertTrue(self.rig.risk.pnl.is_complete)
        self.assertEqual(self.rig.risk.pnl.unattributed_fills, 0)

    def test_a_flip_through_zero_restores_a_known_basis(self) -> None:
        # Selling through zero leaves a short opened at a price we do know, so
        # only the one uncomputable fill is counted. Re-adopted smaller so the
        # flipping order stays inside the per-order notional ceiling.
        self.rig.reconciliation.adopt_broker_positions(
            self.rig.operator_id,
            reason="test: smaller adopted long",
            broker_positions={SYMBOL: Quantity("0.001", ASSET)},
        )
        self.assertTrue(self.sell("0.0015").is_executed)
        self.assertEqual(self.rig.risk.pnl.unattributed_fills, 1)
        position = self.rig.portfolio.position(SYMBOL, asset=ASSET)
        self.assertTrue(position.is_short)
        self.assertTrue(position.basis_is_known)


class TestPnlLedgerRecordsWhatItCannotCompute(unittest.TestCase):
    """Unit-level: the ledger's own account of completeness."""

    def setUp(self) -> None:
        self.clock = ManualClock()
        self.ledger = PnlLedger(USD, clock=self.clock)

    def test_a_fresh_ledger_is_complete(self) -> None:
        self.assertTrue(self.ledger.is_complete)
        self.assertEqual(self.ledger.unattributed_fills, 0)

    def test_an_attributed_record_keeps_it_complete(self) -> None:
        self.ledger.record(usd("-10.00"))
        self.assertTrue(self.ledger.is_complete)
        self.assertEqual(self.ledger.realized, usd("-10.00"))

    def test_an_unattributed_record_makes_it_incomplete(self) -> None:
        self.ledger.record(usd("0.00"), attributed=False)
        self.assertFalse(self.ledger.is_complete)
        self.assertEqual(self.ledger.unattributed_fills, 1)
        # The total itself is untouched: the point is that it is a floor.
        self.assertEqual(self.ledger.realized, usd("0.00"))

    def test_unattributed_records_are_counted_not_latched(self) -> None:
        for _ in range(3):
            self.ledger.record(usd("0.00"), attributed=False)
        self.assertEqual(self.ledger.unattributed_fills, 3)

    def test_the_day_boundary_clears_incompleteness(self) -> None:
        self.ledger.record(usd("-5.00"), attributed=False)
        self.assertFalse(self.ledger.is_complete)
        self.clock.set_wall_clock(self.clock.now() + dt.timedelta(days=1))
        self.assertTrue(self.ledger.is_complete)
        self.assertEqual(self.ledger.unattributed_fills, 0)
        self.assertEqual(self.ledger.realized, usd("0.00"))

    def test_validation_is_unchanged(self) -> None:
        with self.assertRaises(TypeError):
            self.ledger.record("-10")  # type: ignore[arg-type]
        with self.assertRaises(ValueError):
            self.ledger.record(Money("-10.00", INR))


class TestNothingElseRegressed(unittest.TestCase):
    def test_the_order_notional_ceiling_still_bites(self) -> None:
        # Proves the daily-loss branch did not swallow the other limits: this
        # order is refused for its size, on a day with no realized loss at all.
        rig = build_rig(risk=TIGHT)
        result = rig.submit(quantity=Quantity("1", ASSET))
        self.assertTrue(result.is_refused)
        self.assertEqual(result.gate, ExecutionGate.RISK)
        self.assertIn(RiskLimit.ORDER_NOTIONAL.value, result.reason)

    def test_a_clean_day_still_reports_a_complete_evaluation(self) -> None:
        rig = build_rig(risk=TIGHT)
        self.assertTrue(rig.submit().is_executed)
        record = rig.sink.find("risk_approved")[-1]
        self.assertIn(RiskLimit.DAILY_LOSS.value, record.details["checks"])
        self.assertEqual(record.details["waived_as_de_risking"], [])

    def test_the_portfolio_and_the_ledger_still_agree_after_fills(self) -> None:
        rig = build_rig(risk=TIGHT)
        rig.submit()
        rig.broker.set_fill_price(SYMBOL, Price("49000", USD))
        rig.submit(side=OrderSide.SELL, mark_prices={SYMBOL: Price("49000", USD)})
        self.assertEqual(
            rig.positions.position(SYMBOL, asset=ASSET),
            rig.portfolio.position(SYMBOL, asset=ASSET).quantity,
        )
        self.assertTrue(rig.reconciliation.reconcile(rig.portfolio.snapshot()).is_clean)

    def test_cash_still_moves_with_the_fills(self) -> None:
        rig = build_rig(risk=TIGHT)
        rig.submit()
        # 0.001 BTC at 50000 is 50 USD out of 1,000,000.
        self.assertEqual(rig.portfolio.cash, Money("999950.00", USD))
        self.assertEqual(rig.risk.pnl.realized, usd("0.00"))

    def test_a_portfolio_in_the_wrong_currency_is_refused_at_wiring_time(self) -> None:
        # The realized figure has to be recordable, and discovering otherwise
        # after a fill has landed is the worst available moment.
        from trading.core.gateway import ExecutionGateway
        from trading.core.money import INR
        from trading.core.portfolio import Portfolio

        rig = build_rig(risk=TIGHT)
        with self.assertRaises(SafetyViolation) as caught:
            ExecutionGateway(
                identity=rig.gateway_id,
                broker=rig.broker,
                orders=rig.orders,
                positions=Portfolio(Money("1000.00", INR)),
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
        self.assertIn("INR", str(caught.exception))

    def test_the_engine_still_refuses_an_opening_order_directly(self) -> None:
        # The pre-existing unit-level contract, unchanged: a spent budget raises
        # RiskLimitExceeded out of approve() itself.
        rig = build_rig(risk=TIGHT)
        rig.risk.pnl.record(usd("-1.00"))
        with self.assertRaises(RiskLimitExceeded) as caught:
            rig.risk.approve(
                rig.intent(), positions={}, mark_prices={SYMBOL: DEFAULT_PRICE}
            )
        self.assertEqual(caught.exception.limit_name, RiskLimit.DAILY_LOSS.value)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
