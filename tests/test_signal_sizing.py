"""Tests for sizing a signal: the join between a strategy view and a quantity.

Two properties here are the reason this module exists, and both are about a
*refusal* rather than a number:

* a signal with no stop has no defined loss, so no size follows from it -- and
  the sizer must say so rather than invent a stop;
* ``PositionSizer`` reads ``remaining_loss_budget=None`` as *uncapped*, while
  ``RiskEngine.remaining_loss_budget`` returns ``None`` for *unknowable*.
  Forwarding one into the other would produce the biggest position this system
  can compute from the least information it can have.

The arithmetic itself is pinned by ``test_sizing.py``; what is proven here is
that the right inputs reach it and that the wrong ones stop.
"""

from __future__ import annotations

import datetime as dt
import unittest
from decimal import Decimal

from tests.harness import ASSET, SYMBOL, build_rig
from trading.core.config import RiskConfig
from trading.core.money import INR, USD, Money, Price, Quantity
from trading.core.orders import OrderIntent, OrderSide, OrderType
from trading.core.sizing import SizingConstraint
from trading.strategy.signals import Signal, SignalDirection
from trading.strategy.sizing import SignalSizer

T0 = dt.datetime(2026, 1, 1, tzinfo=dt.timezone.utc)

#: 0.5% of equity per trade is the default; a 1 000 USD stop distance and
#: 100 000 USD equity therefore risk 500 USD and size 0.5 BTC -- before the
#: 100 USD per-order ceiling cuts it down. Widened here so the risk fraction,
#: not the notional cap, is what binds in the ordinary case.
ROOMY = RiskConfig(
    max_order_notional=Money("100000.00", USD),
    max_position_notional=Money("100000.00", USD),
    max_gross_exposure=Money("500000.00", USD),
    max_daily_loss=Money("1000.00", USD),
)


def usd(amount: str) -> Money:
    return Money(amount, USD)


def signal(**overrides: object) -> Signal:
    """A coherent long: reference 50 000, stop 49 000, so risk is 1 000 a unit."""
    fields: dict[str, object] = {
        "strategy_name": "test-strategy",
        "signal_id": "sig-1",
        "symbol": SYMBOL,
        "direction": SignalDirection.LONG,
        "reference_price": Price("50000", USD),
        "as_of": T0,
        "rationale": "the fast mean crossed above the slow one",
        "stop_loss": Price("49000", USD),
    }
    fields.update(overrides)
    return Signal(**fields)  # type: ignore[arg-type]


class SizerCase(unittest.TestCase):
    def setUp(self) -> None:
        self.rig = build_rig(risk=ROOMY)
        self.sizer = SignalSizer(self.rig.risk)

    def size(self, sig: Signal | None = None, *, equity: str = "100000.00"):
        return self.sizer.size(
            sig if sig is not None else signal(), equity=usd(equity), asset=ASSET
        )


class TestAStoplessSignalIsRefusedNotDefaulted(SizerCase):
    """The gap this module was written to close."""

    def test_a_signal_with_no_stop_produces_no_position(self) -> None:
        result = self.size(signal(stop_loss=None))
        self.assertFalse(result.is_tradeable)
        self.assertEqual(result.quantity, Quantity.zero(ASSET))
        self.assertEqual(result.binding_constraint, SizingConstraint.MISSING_STOP)

    def test_the_refusal_says_it_refused_to_invent_a_stop(self) -> None:
        result = self.size(signal(stop_loss=None))
        assert result.reason is not None
        self.assertIn("no stop", result.reason)
        self.assertIn("made up", result.reason)

    def test_the_same_signal_with_a_stop_is_sized(self) -> None:
        # The contrast is the point: nothing about the signal but the stop
        # changed, and only the stopped one gets a quantity.
        self.assertFalse(self.size(signal(stop_loss=None)).is_tradeable)
        self.assertTrue(self.size(signal()).is_tradeable)

    def test_a_refused_size_cannot_become_an_order(self) -> None:
        """The structural backstop, for a caller that ignores is_tradeable."""
        result = self.size(signal(stop_loss=None))
        with self.assertRaises(ValueError) as caught:
            OrderIntent(
                strategy_id="test-strategy",
                signal_id="sig-1",
                symbol=SYMBOL,
                side=self.rig.intent().side,
                order_type=OrderType.MARKET,
                quantity=result.quantity,
            )
        self.assertIn("strictly positive", str(caught.exception))

    def test_no_risk_is_reported_for_a_size_that_was_never_computed(self) -> None:
        result = self.size(signal(stop_loss=None))
        self.assertEqual(result.risk_per_unit, Decimal(0))
        self.assertEqual(result.max_loss_at_stop, usd("0.00"))
        self.assertEqual(result.notional, usd("0.00"))


class TestAnUnknownLossBudgetIsNotAnUnlimitedOne(SizerCase):
    """The sharper trap: two Nones that mean opposite things."""

    def spend_an_unknowable_loss(self) -> None:
        """Close an adopted position, whose cost basis was never seen."""
        self.rig.reconciliation.adopt_broker_positions(
            self.rig.operator_id,
            reason="test: venue is authoritative",
            broker_positions={SYMBOL: Quantity("0.002", ASSET)},
        )
        self.rig.broker.set_fill_price(SYMBOL, Price("50000", USD))
        result = self.rig.submit(
            side=OrderSide.SELL,
            quantity=Quantity("0.001", ASSET),
            mark_prices={SYMBOL: Price("50000", USD)},
        )
        self.assertTrue(result.is_executed)
        self.assertFalse(self.rig.risk.pnl.is_complete)

    def test_an_incomplete_day_refuses_rather_than_sizing(self) -> None:
        # Sized before, refused after: the only thing that changed is that the
        # day's loss stopped being knowable.
        self.assertTrue(self.size().is_tradeable)
        self.spend_an_unknowable_loss()
        result = self.size()
        self.assertFalse(result.is_tradeable)
        self.assertEqual(result.binding_constraint, SizingConstraint.LOSS_BUDGET_CAP)

    def test_the_refusal_explains_that_the_budget_cannot_be_stated(self) -> None:
        self.spend_an_unknowable_loss()
        reason = self.size().reason
        assert reason is not None
        self.assertIn("lower bound", reason)
        self.assertIn("unlimited", reason)

    def test_an_unknown_budget_does_not_size_as_though_uncapped(self) -> None:
        """The failure mode, stated directly.

        Were the engine's None forwarded into size_for_stop, it would mean 'no
        budget cap' and the result would be the full risk-fraction size -- the
        largest position available, on the least information.
        """
        uncapped = self.size()
        self.spend_an_unknowable_loss()
        refused = self.size()
        self.assertTrue(uncapped.quantity.is_positive)
        self.assertNotEqual(refused.quantity, uncapped.quantity)
        self.assertEqual(refused.quantity, Quantity.zero(ASSET))


class TestTheBudgetActuallyCapsTheSize(SizerCase):
    def losing_round_trip(self, exit_price: str) -> None:
        self.rig.broker.set_fill_price(SYMBOL, Price("50000", USD))
        self.rig.submit(mark_prices={SYMBOL: Price("50000", USD)})
        self.rig.broker.set_fill_price(SYMBOL, Price(exit_price, USD))
        self.rig.submit(
            side=OrderSide.SELL, mark_prices={SYMBOL: Price(exit_price, USD)}
        )

    def test_a_realized_loss_shrinks_the_size(self) -> None:
        before = self.size()
        # 0.001 BTC from 50 000 to 40 000 loses 10 USD of a 1 000 USD budget,
        # which is still above the 500 USD risk fraction, so nothing changes yet.
        self.losing_round_trip("40000")
        self.assertEqual(self.rig.risk.pnl.realized_loss, usd("10.00"))
        self.assertEqual(self.size().quantity, before.quantity)

        # Spend down past the risk fraction and the budget becomes what binds.
        self.rig.risk.pnl.record(usd("-700.00"))
        self.assertEqual(self.rig.risk.remaining_loss_budget, usd("290.00"))
        after = self.size()
        self.assertLess(after.quantity.amount, before.quantity.amount)
        self.assertEqual(after.binding_constraint, SizingConstraint.LOSS_BUDGET_CAP)
        self.assertLessEqual(after.max_loss_at_stop, usd("290.00"))

    def test_an_exhausted_budget_produces_no_position(self) -> None:
        self.rig.risk.pnl.record(usd("-1000.00"))
        self.assertEqual(self.rig.risk.remaining_loss_budget, usd("0.00"))
        result = self.size()
        self.assertFalse(result.is_tradeable)
        self.assertEqual(result.binding_constraint, SizingConstraint.LOSS_BUDGET_CAP)

    def test_the_sized_loss_never_exceeds_the_remaining_budget(self) -> None:
        for spent in ("0.00", "100.00", "500.00", "900.00", "999.99"):
            with self.subTest(spent=spent):
                rig = build_rig(risk=ROOMY)
                rig.risk.pnl.record(-usd(spent))
                result = SignalSizer(rig.risk).size(
                    signal(), equity=usd("100000.00"), asset=ASSET
                )
                budget = rig.risk.remaining_loss_budget
                assert budget is not None
                self.assertLessEqual(result.max_loss_at_stop, budget)


class TestDirectionMapsToSide(SizerCase):
    def test_a_long_is_sized_as_a_buy(self) -> None:
        # A long's stop sits below entry; size_for_stop refuses the pairing
        # outright if the side is wrong, so a positive size proves the mapping.
        self.assertTrue(self.size(signal()).is_tradeable)

    def test_a_short_is_sized_as_a_sell(self) -> None:
        short = signal(
            direction=SignalDirection.SHORT, stop_loss=Price("51000", USD)
        )
        result = self.size(short)
        self.assertTrue(result.is_tradeable)
        # Same 1 000 stop distance, so the same size as the mirrored long.
        self.assertEqual(result.quantity, self.size(signal()).quantity)

    def test_an_exit_raises_rather_than_answering_zero(self) -> None:
        """A silent zero here would leave the position open."""
        exit_signal = signal(direction=SignalDirection.EXIT, stop_loss=None)
        with self.assertRaises(ValueError) as caught:
            self.size(exit_signal)
        message = str(caught.exception)
        self.assertIn("nothing to do", message)
        self.assertIn("position stayed open", message)


class TestWiringErrorsSurface(SizerCase):
    def test_a_signal_in_another_currency_is_refused(self) -> None:
        with self.assertRaises(ValueError) as caught:
            self.size(
                signal(
                    reference_price=Price("50000", INR),
                    stop_loss=Price("49000", INR),
                )
            )
        self.assertIn("INR", str(caught.exception))

    def test_a_currency_mismatch_is_reported_even_with_no_stop(self) -> None:
        # Checked before the stopless refusal: 'no trade' is the safe answer
        # either way, which is exactly why the misconfiguration would hide.
        with self.assertRaises(ValueError):
            self.size(signal(reference_price=Price("50000", INR), stop_loss=None))

    def test_the_sizer_uses_the_engines_own_config(self) -> None:
        # Not a caller-supplied PositionSizer: the size proposed and the limits
        # enforced have to come from one configuration.
        self.assertIs(self.sizer.sizer.config, self.rig.risk.config)

    def test_a_non_engine_is_refused(self) -> None:
        with self.assertRaises(TypeError):
            SignalSizer(ROOMY)  # type: ignore[arg-type]

    def test_a_non_signal_is_refused(self) -> None:
        with self.assertRaises(TypeError):
            self.sizer.size("buy some", equity=usd("1000.00"), asset=ASSET)  # type: ignore[arg-type]


class TestRemainingLossBudget(unittest.TestCase):
    """The budget derivation itself, which the sizer must not re-derive."""

    def setUp(self) -> None:
        self.rig = build_rig(risk=ROOMY)

    def test_a_clean_day_has_the_whole_budget(self) -> None:
        self.assertEqual(self.rig.risk.remaining_loss_budget, usd("1000.00"))

    def test_a_loss_reduces_it_by_exactly_that_much(self) -> None:
        self.rig.risk.pnl.record(usd("-250.00"))
        self.assertEqual(self.rig.risk.remaining_loss_budget, usd("750.00"))

    def test_a_gain_does_not_increase_it_beyond_the_limit(self) -> None:
        self.rig.risk.pnl.record(usd("500.00"))
        self.assertEqual(self.rig.risk.remaining_loss_budget, usd("1000.00"))

    def test_an_exhausted_budget_is_zero_not_negative(self) -> None:
        self.rig.risk.pnl.record(usd("-1500.00"))
        self.assertEqual(self.rig.risk.remaining_loss_budget, usd("0.00"))

    def test_a_loss_at_exactly_the_limit_leaves_nothing(self) -> None:
        self.rig.risk.pnl.record(usd("-1000.00"))
        self.assertEqual(self.rig.risk.remaining_loss_budget, usd("0.00"))

    def test_an_incomplete_day_cannot_state_a_budget(self) -> None:
        self.rig.risk.pnl.record(usd("0.00"), attributed=False)
        self.assertIsNone(self.rig.risk.remaining_loss_budget)

    def test_incompleteness_outranks_a_healthy_looking_total(self) -> None:
        # The dangerous shape: the total says there is room, and the total is
        # the number we know to be wrong.
        self.rig.risk.pnl.record(usd("-1.00"))
        self.rig.risk.pnl.record(usd("0.00"), attributed=False)
        self.assertEqual(self.rig.risk.pnl.realized_loss, usd("1.00"))
        self.assertIsNone(self.rig.risk.remaining_loss_budget)

    def test_the_budget_returns_at_the_day_boundary(self) -> None:
        self.rig.risk.pnl.record(usd("-400.00"), attributed=False)
        self.assertIsNone(self.rig.risk.remaining_loss_budget)
        self.rig.clock.set_wall_clock(self.rig.clock.now() + dt.timedelta(days=1))
        self.assertEqual(self.rig.risk.remaining_loss_budget, usd("1000.00"))

    def test_the_limit_must_be_money_in_the_base_currency(self) -> None:
        with self.assertRaises(TypeError):
            self.rig.risk.pnl.remaining_budget("100")  # type: ignore[arg-type]
        with self.assertRaises(ValueError):
            self.rig.risk.pnl.remaining_budget(Money("100.00", INR))


class TestNothingElseRegressed(SizerCase):
    def test_the_per_order_ceiling_still_binds_when_it_is_tighter(self) -> None:
        rig = build_rig()  # default: 100 USD per order
        result = SignalSizer(rig.risk).size(
            signal(), equity=usd("100000.00"), asset=ASSET
        )
        self.assertEqual(
            result.binding_constraint, SizingConstraint.ORDER_NOTIONAL_CAP
        )
        self.assertLessEqual(result.notional, usd("100.00"))

    def test_zero_equity_produces_no_position(self) -> None:
        result = self.size(equity="0.00")
        self.assertFalse(result.is_tradeable)
        self.assertEqual(result.binding_constraint, SizingConstraint.EQUITY_CAP)

    def test_sizing_submits_nothing(self) -> None:
        """INVARIANT 3: proposing a size is not placing an order."""
        before = self.rig.broker.placement_count
        self.size()
        self.size(signal(stop_loss=None))
        self.assertEqual(self.rig.broker.placement_count, before)

    def test_the_result_is_auditable(self) -> None:
        details = self.size(signal(stop_loss=None)).as_details()
        self.assertEqual(details["binding_constraint"], "missing_stop")
        self.assertIs(details["tradeable"], False)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
