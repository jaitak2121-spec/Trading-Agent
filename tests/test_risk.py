"""Tests for risk limits.

Covers INVARIANT 4 (risk checks happen before execution) and INVARIANT 7
(loss/exposure limits cannot be bypassed).
"""

from __future__ import annotations

import datetime as dt
import threading
import unittest
from decimal import Decimal

from trading.core.audit import AuditLog, InMemoryAuditSink
from trading.core.authz import PERMISSIONS, Action, Principal, Role
from trading.core.clock import Clock, ManualClock
from trading.core.config import RiskConfig
from trading.core.errors import (
    ConfigurationError,
    RiskLimitExceeded,
    UnauthorizedAction,
)
from trading.core.money import INR, USD, Money, Price, Quantity
from trading.core.orders import (
    Order,
    OrderIntent,
    OrderSide,
    OrderState,
    OrderStore,
    OrderType,
)
from trading.core.risk import (
    REQUIRED_CHECKS,
    LimitBreach,
    PnlLedger,
    RiskApproval,
    RiskEngine,
    RiskLimit,
)

BTC_PRICE = Price("50000", USD)


class SkewedClock(Clock):
    """Wraps a clock and skews only its monotonic reading.

    ``ManualClock.advance`` refuses negative seconds, so this is the only way to
    present a safety timer with time that has gone backwards.
    """

    def __init__(self, base: Clock, *, monotonic_offset: float) -> None:
        self._base = base
        self._offset = monotonic_offset

    def now(self):
        return self._base.now()

    def monotonic_seconds(self) -> float:
        return self._base.monotonic_seconds() + self._offset


def make_intent(
    *,
    signal_id: str = "sig-1",
    symbol: str = "BTCUSD",
    side: OrderSide = OrderSide.BUY,
    quantity: str = "0.001",
    asset: str | None = None,
) -> OrderIntent:
    return OrderIntent(
        strategy_id="strat-1",
        signal_id=signal_id,
        symbol=symbol,
        side=side,
        # Symbols here are <asset><quote>, so strip the quote to get the asset.
        quantity=Quantity(quantity, asset or symbol.removesuffix("USD")),
    )


class RiskFixture(unittest.TestCase):
    """Shared wiring. Default config: order<=100, position<=250, gross<=500."""

    def setUp(self) -> None:
        self.clock = ManualClock()
        self.sink = InMemoryAuditSink()
        self.audit = AuditLog(self.sink, clock=self.clock)
        self.store = OrderStore()
        self.config = RiskConfig()
        self.engine = self.new_engine()

    def new_engine(self, config: RiskConfig | None = None, **kwargs) -> RiskEngine:
        return RiskEngine(
            config or self.config,
            identity=Principal("risk-1", Role.RISK_MANAGER),
            order_store=self.store,
            audit=self.audit,
            clock=self.clock,
            **kwargs,
        )

    def approve(self, intent=None, *, positions=None, prices=None, engine=None):
        engine = engine or self.engine
        return engine.approve(
            intent or make_intent(),
            positions=positions if positions is not None else {},
            mark_prices=prices if prices is not None else {"BTCUSD": BTC_PRICE},
        )

    def open_order(self, signal_id: str) -> Order:
        order = Order(make_intent(signal_id=signal_id), clock=self.clock)
        order.transition_to(OrderState.PENDING_NEW, reason="test fixture")
        return self.store.add(order)


class TestApprovalIsACapability(RiskFixture):
    """INVARIANT 4: there is no way to execute without a real approval."""

    def test_approval_cannot_be_constructed_directly(self):
        with self.assertRaises(UnauthorizedAction) as ctx:
            RiskApproval(
                idempotency_key="forged",
                order_notional=Money("1.00", USD),
                checks=list(REQUIRED_CHECKS),
                issued_at=self.clock.now(),
                issued_at_mono=self.clock.monotonic_seconds(),
                ttl_seconds=30,
                approver="attacker",
            )
        self.assertIn("minted only by RiskEngine.approve", str(ctx.exception))

    def test_approval_cannot_be_constructed_with_a_guessed_key(self):
        with self.assertRaises(UnauthorizedAction):
            RiskApproval(
                idempotency_key="forged",
                order_notional=Money("1.00", USD),
                checks=list(REQUIRED_CHECKS),
                issued_at=self.clock.now(),
                issued_at_mono=self.clock.monotonic_seconds(),
                ttl_seconds=30,
                approver="attacker",
                mint_key=object(),
            )

    def test_approval_is_single_use(self):
        intent = make_intent()
        approval = self.approve(intent)
        approval.consume(idempotency_key=intent.idempotency_key, clock=self.clock)
        with self.assertRaises(UnauthorizedAction) as ctx:
            approval.consume(idempotency_key=intent.idempotency_key, clock=self.clock)
        self.assertIn("already been used", str(ctx.exception))

    def test_approval_is_bound_to_one_order(self):
        first = make_intent(signal_id="a")
        second = make_intent(signal_id="b")
        self.assertNotEqual(first.idempotency_key, second.idempotency_key)
        approval = self.approve(first)
        with self.assertRaises(UnauthorizedAction) as ctx:
            approval.consume(idempotency_key=second.idempotency_key, clock=self.clock)
        self.assertIn("different order", str(ctx.exception))
        self.assertFalse(approval.is_consumed)

    def test_a_failed_consume_does_not_spend_the_approval(self):
        intent = make_intent()
        approval = self.approve(intent)
        with self.assertRaises(UnauthorizedAction):
            approval.consume(idempotency_key="wrong", clock=self.clock)
        approval.consume(idempotency_key=intent.idempotency_key, clock=self.clock)
        self.assertTrue(approval.is_consumed)

    def test_approval_expires(self):
        intent = make_intent()
        approval = self.approve(intent, engine=self.new_engine(approval_ttl_seconds=30))
        self.clock.advance(31)
        with self.assertRaises(UnauthorizedAction) as ctx:
            approval.consume(idempotency_key=intent.idempotency_key, clock=self.clock)
        self.assertIn("expired", str(ctx.exception))

    def test_expiry_uses_the_monotonic_clock_not_the_wall_clock(self):
        intent = make_intent()
        approval = self.approve(intent)
        # Wall clock leaps a year; the monotonic clock has not moved.
        self.clock.set_wall_clock(dt.datetime(2027, 1, 1, tzinfo=dt.timezone.utc))
        approval.consume(idempotency_key=intent.idempotency_key, clock=self.clock)
        self.assertTrue(approval.is_consumed)

    def test_time_going_backwards_expires_the_approval(self):
        intent = make_intent()
        approval = self.approve(intent)
        skewed = SkewedClock(self.clock, monotonic_offset=-5)
        with self.assertRaises(UnauthorizedAction) as ctx:
            approval.consume(idempotency_key=intent.idempotency_key, clock=skewed)
        self.assertIn("expired", str(ctx.exception))

    def test_approval_records_every_check(self):
        approval = self.approve()
        self.assertEqual(set(approval.checks), set(REQUIRED_CHECKS))
        self.assertTrue(approval.covers_all_limits())

    def test_incomplete_approval_is_refused_at_consume(self):
        """A partial evaluation must not be spendable (INVARIANT 7)."""
        intent = make_intent()
        approval = self.approve(intent)
        # Simulate a tampered/partial approval by removing a check.
        object.__setattr__(
            approval, "_checks", (RiskLimit.ORDER_NOTIONAL,)
        )
        self.assertFalse(approval.covers_all_limits())
        with self.assertRaises(UnauthorizedAction) as ctx:
            approval.consume(idempotency_key=intent.idempotency_key, clock=self.clock)
        self.assertIn("incomplete", str(ctx.exception))
        self.assertIn("max_daily_loss", str(ctx.exception))

    def test_approval_ids_are_unique(self):
        ids = {
            self.approve(make_intent(signal_id=f"s{i}")).approval_id for i in range(20)
        }
        self.assertEqual(len(ids), 20)

    def test_approval_reports_the_order_notional(self):
        approval = self.approve(make_intent(quantity="0.001"))
        self.assertEqual(approval.order_notional, Money("50.00", USD))


class TestSeparationOfDuties(RiskFixture):
    """A component must not be able to approve its own executions."""

    def test_engine_requires_approve_order_permission(self):
        for role in (Role.STRATEGY, Role.AUDITOR, Role.EXECUTION_GATEWAY):
            with self.subTest(role=role):
                with self.assertRaises(UnauthorizedAction):
                    self.new_engine_as(role)

    def new_engine_as(self, role: Role) -> RiskEngine:
        return RiskEngine(
            self.config,
            identity=Principal("actor", role),
            order_store=self.store,
            audit=self.audit,
            clock=self.clock,
        )

    def test_no_role_can_both_approve_and_execute(self):
        """The matrix property the engine's second check defends."""
        for role, actions in PERMISSIONS.items():
            with self.subTest(role=role):
                self.assertFalse(
                    Action.APPROVE_ORDER in actions
                    and Action.EXECUTE_ORDER in actions,
                    f"{role} holds both APPROVE_ORDER and EXECUTE_ORDER",
                )

    def test_risk_manager_may_run_the_engine(self):
        engine = self.new_engine_as(Role.RISK_MANAGER)
        self.assertIsInstance(engine, RiskEngine)

    def test_ttl_must_be_a_sane_int(self):
        for bad in (0, -1, True, 1.5, "30"):
            with self.subTest(ttl=bad):
                with self.assertRaises((TypeError, ValueError)):
                    self.new_engine(approval_ttl_seconds=bad)


class TestMarkPriceIsFailClosed(RiskFixture):
    """"Could not check" must never read as "check passed"."""

    def test_missing_price_for_the_traded_symbol_refuses(self):
        with self.assertRaises(RiskLimitExceeded) as ctx:
            self.approve(prices={})
        self.assertEqual(ctx.exception.limit_name, RiskLimit.MARK_PRICE_AVAILABLE.value)
        self.assertIn("refused rather than", str(ctx.exception))

    def test_missing_price_for_an_unrelated_holding_refuses(self):
        with self.assertRaises(RiskLimitExceeded) as ctx:
            self.approve(
                positions={"ETHUSD": Quantity("10", "ETH")},
                prices={"BTCUSD": BTC_PRICE},
            )
        self.assertEqual(ctx.exception.limit_name, RiskLimit.MARK_PRICE_AVAILABLE.value)
        self.assertIn("ETHUSD", str(ctx.exception))

    def test_zero_position_without_a_price_is_not_a_problem(self):
        approval = self.approve(
            positions={"ETHUSD": Quantity.zero("ETH")},
            prices={"BTCUSD": BTC_PRICE},
        )
        self.assertTrue(approval.covers_all_limits())

    def test_limit_price_substitutes_for_a_missing_mark(self):
        """A LIMIT order carries its own price, so it can still be checked."""
        intent = OrderIntent(
            strategy_id="s",
            signal_id="sig",
            symbol="BTCUSD",
            side=OrderSide.BUY,
            quantity=Quantity("0.001", "BTC"),
            order_type=OrderType.LIMIT,
            limit_price=BTC_PRICE,
        )
        approval = self.approve(intent, prices={})
        self.assertEqual(approval.order_notional, Money("50.00", USD))

    def test_refusal_still_records_every_check_as_run(self):
        with self.assertRaises(RiskLimitExceeded):
            self.approve(prices={})
        record = self.sink.find("risk_refused")[-1]
        self.assertEqual(
            set(record.details["checks_run"]), {c.value for c in REQUIRED_CHECKS}
        )


class TestOrderNotionalLimit(RiskFixture):
    def test_order_at_the_ceiling_is_allowed(self):
        # 0.002 BTC * 50000 = 100.00 == max_order_notional
        approval = self.approve(make_intent(quantity="0.002"))
        self.assertEqual(approval.order_notional, Money("100.00", USD))

    def test_order_above_the_ceiling_is_refused(self):
        with self.assertRaises(RiskLimitExceeded) as ctx:
            self.approve(make_intent(quantity="0.00200001"))
        self.assertEqual(ctx.exception.limit_name, RiskLimit.ORDER_NOTIONAL.value)

    def test_notional_rounds_up_so_exposure_is_never_understated(self):
        engine = self.new_engine(
            RiskConfig(max_order_notional=Money("100.00", USD))
        )
        # 0.001 * 100000.001 = 100.000001 -> rounds UP to 100.01 > ceiling.
        with self.assertRaises(RiskLimitExceeded):
            self.approve(
                make_intent(quantity="0.001"),
                prices={"BTCUSD": Price("100000.001", USD)},
                engine=engine,
            )


class TestPositionNotionalLimit(RiskFixture):
    def test_adding_to_a_position_is_judged_on_the_result(self):
        # Existing 0.004 BTC = 200 USD; adding 0.002 = 100 -> 300 > 250 ceiling.
        with self.assertRaises(RiskLimitExceeded) as ctx:
            self.approve(
                make_intent(quantity="0.002"),
                positions={"BTCUSD": Quantity("0.004", "BTC")},
            )
        self.assertEqual(ctx.exception.limit_name, RiskLimit.POSITION_NOTIONAL.value)

    def test_position_exactly_at_the_ceiling_is_allowed(self):
        # 0.003 + 0.002 = 0.005 BTC = 250.00 == max_position_notional
        approval = self.approve(
            make_intent(quantity="0.002"),
            positions={"BTCUSD": Quantity("0.003", "BTC")},
        )
        self.assertTrue(approval.covers_all_limits())

    def test_closing_order_is_not_blocked_by_an_oversized_position(self):
        """Blocking exits would be a safety hazard of its own."""
        approval = self.approve(
            make_intent(side=OrderSide.SELL, quantity="0.002"),
            positions={"BTCUSD": Quantity("0.009", "BTC")},
        )
        self.assertTrue(approval.covers_all_limits())

    def test_a_sell_that_opens_a_large_short_is_still_blocked(self):
        with self.assertRaises(RiskLimitExceeded) as ctx:
            self.approve(
                make_intent(side=OrderSide.SELL, quantity="0.002"),
                positions={"BTCUSD": Quantity("-0.004", "BTC")},
            )
        self.assertEqual(ctx.exception.limit_name, RiskLimit.POSITION_NOTIONAL.value)

    def test_short_exposure_is_measured_by_magnitude(self):
        # -0.005 BTC is 250 USD of exposure, not -250.
        with self.assertRaises(RiskLimitExceeded):
            self.approve(
                make_intent(side=OrderSide.SELL, quantity="0.002"),
                positions={"BTCUSD": Quantity("-0.0045", "BTC")},
            )


class TestGrossExposureLimit(RiskFixture):
    def setUp(self) -> None:
        super().setUp()
        self.prices = {
            "BTCUSD": BTC_PRICE,
            "ETHUSD": Price("2000", USD),
        }

    def test_exposure_sums_across_symbols(self):
        gross, per_symbol, missing = self.engine.exposure_report(
            {"BTCUSD": Quantity("0.002", "BTC"), "ETHUSD": Quantity("0.05", "ETH")},
            self.prices,
        )
        self.assertEqual(gross, Money("200.00", USD))
        self.assertEqual(per_symbol["BTCUSD"], Money("100.00", USD))
        self.assertEqual(per_symbol["ETHUSD"], Money("100.00", USD))
        self.assertEqual(missing, [])

    def test_gross_limit_refuses_when_other_symbols_fill_the_budget(self):
        # ETH 0.2 * 2000 = 400 gross; adding 100 of BTC -> 500 == limit, allowed.
        approval = self.approve(
            make_intent(quantity="0.002"),
            positions={"ETHUSD": Quantity("0.2", "ETH")},
            prices=self.prices,
        )
        self.assertTrue(approval.covers_all_limits())

        # One cent more of ETH exposure pushes the total over.
        with self.assertRaises(RiskLimitExceeded) as ctx:
            self.approve(
                make_intent(quantity="0.002"),
                positions={"ETHUSD": Quantity("0.20001", "ETH")},
                prices=self.prices,
            )
        self.assertEqual(ctx.exception.limit_name, RiskLimit.GROSS_EXPOSURE.value)

    def test_reducing_order_lowers_gross_exposure(self):
        approval = self.approve(
            make_intent(side=OrderSide.SELL, quantity="0.002"),
            positions={
                "BTCUSD": Quantity("0.004", "BTC"),
                "ETHUSD": Quantity("0.15", "ETH"),
            },
            prices=self.prices,
        )
        self.assertTrue(approval.covers_all_limits())

    def test_exposure_report_lists_missing_prices_sorted(self):
        _gross, _per, missing = self.engine.exposure_report(
            {
                "ZZZUSD": Quantity("1", "ZZZ"),
                "AAAUSD": Quantity("1", "AAA"),
                "BTCUSD": Quantity("1", "BTC"),
            },
            {"BTCUSD": BTC_PRICE},
        )
        self.assertEqual(missing, ["AAAUSD", "ZZZUSD"])


class TestDailyLossLimit(RiskFixture):
    def test_losses_accumulate(self):
        ledger = PnlLedger(USD, clock=self.clock)
        ledger.record(Money("-10.00", USD))
        ledger.record(Money("-5.00", USD))
        self.assertEqual(ledger.realized, Money("-15.00", USD))
        self.assertEqual(ledger.realized_loss, Money("15.00", USD))

    def test_profit_means_zero_loss_not_a_negative_loss(self):
        ledger = PnlLedger(USD, clock=self.clock)
        ledger.record(Money("25.00", USD))
        self.assertEqual(ledger.realized_loss, Money.zero(USD))

    def test_reaching_the_budget_refuses_further_orders(self):
        self.engine.pnl.record(Money("-50.00", USD))
        with self.assertRaises(RiskLimitExceeded) as ctx:
            self.approve()
        self.assertEqual(ctx.exception.limit_name, RiskLimit.DAILY_LOSS.value)

    def test_just_under_the_budget_still_trades(self):
        self.engine.pnl.record(Money("-49.99", USD))
        approval = self.approve()
        self.assertTrue(approval.covers_all_limits())

    def test_budget_resets_at_the_utc_day_boundary(self):
        self.engine.pnl.record(Money("-50.00", USD))
        with self.assertRaises(RiskLimitExceeded):
            self.approve()
        self.clock.set_wall_clock(dt.datetime(2026, 1, 2, tzinfo=dt.timezone.utc))
        self.assertEqual(self.engine.pnl.realized, Money.zero(USD))
        approval = self.approve()
        self.assertTrue(approval.covers_all_limits())

    def test_budget_does_not_reset_within_the_same_day(self):
        self.engine.pnl.record(Money("-50.00", USD))
        self.clock.set_wall_clock(
            dt.datetime(2026, 1, 1, 23, 59, 59, tzinfo=dt.timezone.utc)
        )
        with self.assertRaises(RiskLimitExceeded):
            self.approve()

    def test_ledger_rejects_wrong_currency_and_non_money(self):
        ledger = PnlLedger(USD, clock=self.clock)
        with self.assertRaises(TypeError):
            ledger.record("-10")
        with self.assertRaises(ValueError):
            ledger.record(Money("-10.00", INR))


class TestOrderRateLimit(RiskFixture):
    def test_rate_is_only_consumed_by_actual_submissions(self):
        for _ in range(10):
            self.approve(make_intent(signal_id="same"))
        self.assertEqual(self.engine.submissions_in_window(), 0)

    def test_reaching_the_rate_limit_refuses(self):
        for _ in range(self.config.max_orders_per_minute):
            self.engine.record_submission()
        with self.assertRaises(RiskLimitExceeded) as ctx:
            self.approve()
        self.assertEqual(ctx.exception.limit_name, RiskLimit.ORDER_RATE.value)

    def test_rate_window_slides(self):
        for _ in range(self.config.max_orders_per_minute):
            self.engine.record_submission()
        self.clock.advance(61)
        self.assertEqual(self.engine.submissions_in_window(), 0)
        approval = self.approve()
        self.assertTrue(approval.covers_all_limits())

    def test_window_uses_monotonic_time_not_the_wall_clock(self):
        for _ in range(self.config.max_orders_per_minute):
            self.engine.record_submission()
        self.clock.set_wall_clock(dt.datetime(2027, 1, 1, tzinfo=dt.timezone.utc))
        # Wall clock jumped a year; the rate window has not moved.
        self.assertEqual(
            self.engine.submissions_in_window(), self.config.max_orders_per_minute
        )

    def test_partial_window_expiry(self):
        self.engine.record_submission()
        self.clock.advance(30)
        self.engine.record_submission()
        self.assertEqual(self.engine.submissions_in_window(), 2)
        self.clock.advance(31)  # first one is now 61s old
        self.assertEqual(self.engine.submissions_in_window(), 1)


class TestOpenOrderLimit(RiskFixture):
    def test_reaching_the_open_order_limit_refuses(self):
        for i in range(self.config.max_open_orders):
            self.open_order(f"open-{i}")
        with self.assertRaises(RiskLimitExceeded) as ctx:
            self.approve()
        self.assertEqual(ctx.exception.limit_name, RiskLimit.OPEN_ORDERS.value)

    def test_terminal_orders_do_not_count(self):
        for i in range(self.config.max_open_orders):
            order = self.open_order(f"open-{i}")
            order.transition_to(OrderState.CANCELED, reason="test fixture")
        approval = self.approve()
        self.assertTrue(approval.covers_all_limits())

    def test_one_below_the_limit_still_trades(self):
        for i in range(self.config.max_open_orders - 1):
            self.open_order(f"open-{i}")
        approval = self.approve()
        self.assertTrue(approval.covers_all_limits())


class TestEveryBreachIsReported(RiskFixture):
    def test_multiple_breaches_all_appear(self):
        for i in range(self.config.max_open_orders):
            self.open_order(f"open-{i}")
        self.engine.pnl.record(Money("-50.00", USD))
        with self.assertRaises(RiskLimitExceeded) as ctx:
            self.approve(make_intent(quantity="1"))
        message = str(ctx.exception)
        for limit in (
            RiskLimit.ORDER_NOTIONAL,
            RiskLimit.POSITION_NOTIONAL,
            RiskLimit.GROSS_EXPOSURE,
            RiskLimit.DAILY_LOSS,
            RiskLimit.OPEN_ORDERS,
        ):
            with self.subTest(limit=limit):
                self.assertIn(limit.value, message)

    def test_refusal_is_audited_with_all_breaches(self):
        self.engine.pnl.record(Money("-50.00", USD))
        with self.assertRaises(RiskLimitExceeded):
            self.approve(make_intent(quantity="1"))
        records = self.sink.find("risk_refused")
        self.assertEqual(len(records), 1)
        limits = {b["limit"] for b in records[0].details["breaches"]}
        self.assertIn(RiskLimit.DAILY_LOSS.value, limits)
        self.assertIn(RiskLimit.ORDER_NOTIONAL.value, limits)

    def test_approval_is_audited(self):
        approval = self.approve()
        records = self.sink.find("risk_approved")
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0].details["approval_id"], approval.approval_id)
        self.assertEqual(records[0].outcome, "allowed")

    def test_message_names_the_invariant(self):
        with self.assertRaises(RiskLimitExceeded) as ctx:
            self.approve(make_intent(quantity="1"))
        self.assertIn("INVARIANT 7", str(ctx.exception))

    def test_limit_breach_renders_readably(self):
        breach = LimitBreach(
            limit=RiskLimit.DAILY_LOSS, message="over budget", observed="60", allowed="50"
        )
        self.assertEqual(str(breach), "max_daily_loss: over budget")
        self.assertEqual(breach.as_details()["allowed"], "50")


class TestDeRiskingWaiver(RiskFixture):
    """A waiver is a hole by construction. These tests bound its shape.

    The rule: an order that strictly shrinks a position without flipping its
    sign may exceed the position, gross-exposure, and daily-loss limits. Nothing
    else may.
    """

    def test_reducing_an_over_limit_position_is_allowed(self):
        # 0.009 BTC = 450 USD, already over the 250 ceiling. Selling 0.002
        # leaves 350, still over -- but strictly less. Must be allowed, or the
        # operator is trapped in the position.
        approval = self.approve(
            make_intent(side=OrderSide.SELL, quantity="0.002"),
            positions={"BTCUSD": Quantity("0.009", "BTC")},
        )
        self.assertTrue(approval.covers_all_limits())

    def test_the_waiver_is_audited(self):
        self.approve(
            make_intent(side=OrderSide.SELL, quantity="0.002"),
            positions={"BTCUSD": Quantity("0.009", "BTC")},
        )
        record = self.sink.find("risk_approved")[-1]
        self.assertIn(
            RiskLimit.POSITION_NOTIONAL.value, record.details["waived_as_de_risking"]
        )

    def test_a_normal_order_waives_nothing(self):
        self.approve()
        record = self.sink.find("risk_approved")[-1]
        self.assertEqual(record.details["waived_as_de_risking"], [])

    def test_closing_is_allowed_after_the_daily_loss_budget_is_spent(self):
        """Hitting the loss limit must not mean being unable to exit."""
        self.engine.pnl.record(Money("-50.00", USD))
        approval = self.approve(
            make_intent(side=OrderSide.SELL, quantity="0.002"),
            positions={"BTCUSD": Quantity("0.009", "BTC")},
        )
        record = self.sink.find("risk_approved")[-1]
        self.assertIn(
            RiskLimit.DAILY_LOSS.value, record.details["waived_as_de_risking"]
        )
        self.assertTrue(approval.covers_all_limits())

    def test_opening_is_still_refused_after_the_loss_budget_is_spent(self):
        self.engine.pnl.record(Money("-50.00", USD))
        with self.assertRaises(RiskLimitExceeded) as ctx:
            self.approve(make_intent(side=OrderSide.BUY, quantity="0.001"))
        self.assertEqual(ctx.exception.limit_name, RiskLimit.DAILY_LOSS.value)

    def test_a_sign_flip_is_not_a_reduction_even_if_magnitude_shrinks(self):
        """The bypass this guards: shrink the number, open a fresh short.

        Long 0.009 (450 USD). Sell 0.017 -> short 0.008 (400 USD). The magnitude
        fell, but that is a brand-new 400 USD short position and the ceiling
        must apply to it.
        """
        with self.assertRaises(RiskLimitExceeded) as ctx:
            self.approve(
                make_intent(side=OrderSide.SELL, quantity="0.017"),
                positions={"BTCUSD": Quantity("0.009", "BTC")},
            )
        self.assertIn(RiskLimit.POSITION_NOTIONAL.value, str(ctx.exception))

    def test_a_sign_flip_from_short_to_long_is_also_not_a_reduction(self):
        with self.assertRaises(RiskLimitExceeded):
            self.approve(
                make_intent(side=OrderSide.BUY, quantity="0.017"),
                positions={"BTCUSD": Quantity("-0.009", "BTC")},
            )

    def test_growing_a_position_is_never_a_reduction(self):
        with self.assertRaises(RiskLimitExceeded) as ctx:
            self.approve(
                make_intent(side=OrderSide.BUY, quantity="0.001"),
                positions={"BTCUSD": Quantity("0.009", "BTC")},
            )
        self.assertEqual(ctx.exception.limit_name, RiskLimit.POSITION_NOTIONAL.value)

    def test_flat_to_new_position_is_never_a_reduction(self):
        with self.assertRaises(RiskLimitExceeded):
            self.approve(
                make_intent(quantity="0.002"),
                positions={"BTCUSD": Quantity.zero("BTC")},
                prices={"BTCUSD": Price("200000", USD)},
            )

    def test_closing_to_exactly_flat_is_a_reduction(self):
        """Exercises the projected == 0 branch of the reduction test."""
        approval = self.approve(
            make_intent(side=OrderSide.SELL, quantity="0.002"),
            positions={"BTCUSD": Quantity("0.002", "BTC")},
        )
        self.assertTrue(approval.covers_all_limits())

    def test_unwinding_an_over_limit_position_takes_several_orders(self):
        """The documented consequence of not waiving the per-order ceiling.

        A 450 USD position cannot be closed by one 450 USD order, because the
        per-order ceiling is 100. It is unwound in steps instead -- each step
        allowed by the de-risking waiver, none of them a single huge order.
        """
        over_limit = {"BTCUSD": Quantity("0.009", "BTC")}
        with self.assertRaises(RiskLimitExceeded) as ctx:
            self.approve(
                make_intent(side=OrderSide.SELL, quantity="0.009"),
                positions=over_limit,
            )
        self.assertEqual(ctx.exception.limit_name, RiskLimit.ORDER_NOTIONAL.value)

        approval = self.approve(
            make_intent(side=OrderSide.SELL, quantity="0.002"),
            positions=over_limit,
        )
        self.assertTrue(approval.covers_all_limits())

    def test_the_per_order_ceiling_still_applies_to_a_reduction(self):
        """Transient limits are not waived: they throttle, they do not trap."""
        with self.assertRaises(RiskLimitExceeded) as ctx:
            self.approve(
                make_intent(side=OrderSide.SELL, quantity="0.009"),
                positions={"BTCUSD": Quantity("0.02", "BTC")},
            )
        self.assertEqual(ctx.exception.limit_name, RiskLimit.ORDER_NOTIONAL.value)

    def test_the_rate_limit_still_applies_to_a_reduction(self):
        for _ in range(self.config.max_orders_per_minute):
            self.engine.record_submission()
        with self.assertRaises(RiskLimitExceeded) as ctx:
            self.approve(
                make_intent(side=OrderSide.SELL, quantity="0.002"),
                positions={"BTCUSD": Quantity("0.009", "BTC")},
            )
        self.assertEqual(ctx.exception.limit_name, RiskLimit.ORDER_RATE.value)

    def test_a_missing_mark_price_is_never_waived(self):
        with self.assertRaises(RiskLimitExceeded) as ctx:
            self.approve(
                make_intent(side=OrderSide.SELL, quantity="0.002"),
                positions={"BTCUSD": Quantity("0.009", "BTC")},
                prices={},
            )
        self.assertEqual(ctx.exception.limit_name, RiskLimit.MARK_PRICE_AVAILABLE.value)

    def test_reduction_on_one_symbol_does_not_waive_another_symbols_exposure(self):
        """The waiver is symbol-scoped; it must not license unrelated exposure."""
        with self.assertRaises(RiskLimitExceeded):
            self.approve(
                make_intent(symbol="ETHUSD", side=OrderSide.BUY, quantity="0.002"),
                positions={
                    "BTCUSD": Quantity("0.009", "BTC"),
                    "ETHUSD": Quantity("0.004", "ETH"),
                },
                prices={"BTCUSD": BTC_PRICE, "ETHUSD": Price("50000", USD)},
            )


class TestNoFloatsReachRisk(RiskFixture):
    def test_approve_rejects_a_non_intent(self):
        with self.assertRaises(TypeError):
            self.engine.approve("not an intent", positions={}, mark_prices={})

    def test_risk_fraction_must_be_decimal(self):
        with self.assertRaises(ConfigurationError) as ctx:
            RiskConfig(risk_fraction_per_trade=0.005)
        self.assertIn("INVARIANT 8", str(ctx.exception))

    def test_config_money_fields_reject_floats(self):
        with self.assertRaises(ConfigurationError):
            RiskConfig(max_order_notional=100.0)

    def test_approval_details_carry_strings_not_floats(self):
        details = self.approve().as_details()
        self.assertIsInstance(details["order_notional"], str)
        self.assertNotIn("e", details["order_notional"].lower())


class TestConcurrency(RiskFixture):
    def test_concurrent_approvals_all_see_a_consistent_limit(self):
        """16 threads race; the open-order limit must hold for all of them."""
        for i in range(self.config.max_open_orders):
            self.open_order(f"open-{i}")
        errors: list[BaseException] = []
        allowed: list[RiskApproval] = []
        barrier = threading.Barrier(16)

        def attempt(i: int) -> None:
            barrier.wait()
            try:
                allowed.append(self.approve(make_intent(signal_id=f"race-{i}")))
            except RiskLimitExceeded as exc:
                errors.append(exc)

        threads = [threading.Thread(target=attempt, args=(i,)) for i in range(16)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(allowed, [])
        self.assertEqual(len(errors), 16)

    def test_concurrent_consume_yields_exactly_one_winner(self):
        intent = make_intent()
        approval = self.approve(intent)
        winners: list[int] = []
        barrier = threading.Barrier(16)

        def attempt(i: int) -> None:
            barrier.wait()
            try:
                approval.consume(
                    idempotency_key=intent.idempotency_key, clock=self.clock
                )
                winners.append(i)
            except UnauthorizedAction:
                pass

        threads = [threading.Thread(target=attempt, args=(i,)) for i in range(16)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(len(winners), 1)

    def test_concurrent_submission_recording_is_exact(self):
        barrier = threading.Barrier(20)

        def submit() -> None:
            barrier.wait()
            self.engine.record_submission()

        threads = [threading.Thread(target=submit) for _ in range(20)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(self.engine.submissions_in_window(), 20)


if __name__ == "__main__":
    unittest.main()
