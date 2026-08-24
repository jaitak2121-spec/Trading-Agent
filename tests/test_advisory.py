"""Tests for advisory mode: it explains, it sizes, and it cannot execute.

Three groups of properties are worth proving here, and they are different in
kind:

* **Structural.** Advisory mode holds no execution surface, runs under an
  identity that cannot execute, produces no ``OrderIntent``, and mints no risk
  approval. ``test_core_purity.py`` proves the import half mechanically; what is
  proven here is the runtime half -- that advising a batch of signals leaves the
  broker untouched and the risk log empty.
* **Judgement.** The advisor's whole reason to exist is comparing a signal
  against the market as it is now. The line between a *block* and a *warning* is
  the substance of that: a stop the market has already passed makes the stated
  risk a fiction, while a target the market has already passed only makes the
  trade worse. One refuses, the other annotates.
* **Honesty of the report.** A blocked advice must carry no size, and a sized
  advice must carry exactly the sizer's size. Both are enforced in
  ``Advice.__post_init__`` and both are tested against it directly, because the
  failure they prevent is a number an operator reads and acts on.

Sizing arithmetic is pinned by ``test_sizing.py`` and ``test_signal_sizing.py``;
what is checked here is that a refusal from there arrives intact and that the
quantity an operator sees is the one the limits were applied to.
"""

from __future__ import annotations

import datetime as dt
import unittest
from dataclasses import fields
from decimal import Decimal

from tests.harness import ASSET, SYMBOL, build_rig
from trading.advisory import Advice, Advisor, Block, BlockReason
from trading.advisory import advisor as advisor_module
from trading.core.audit import AuditCategory, AuditOutcome
from trading.core.authz import Action, PERMISSIONS, Role
from trading.core.config import RiskConfig
from trading.core.errors import SafetyViolation, UnauthorizedAction
from trading.core.marketdata import Freshness, MarketSnapshot, Quote, StalenessPolicy
from trading.core.money import INR, USD, Money, Price, Quantity
from trading.core.sizing import SizingConstraint
from trading.strategy.context import MarketContext
from trading.strategy.signals import Signal, SignalDirection
from trading.strategy.sizing import SignalSizer

T0 = dt.datetime(2026, 1, 1, tzinfo=dt.timezone.utc)

#: Widened so the risk fraction binds in the ordinary case rather than the
#: per-order notional cap -- the same rig ``test_signal_sizing.py`` uses, for the
#: same reason. 0.5% of 100 000 USD equity is a 500 USD budget, and a 1 000 USD
#: stop distance turns that into 0.5 BTC.
ROOMY = RiskConfig(
    max_order_notional=Money("100000.00", USD),
    max_position_notional=Money("100000.00", USD),
    max_gross_exposure=Money("500000.00", USD),
    max_daily_loss=Money("1000.00", USD),
)

EQUITY = Money("100000.00", USD)
REFERENCE = Price("50000", USD)
LONG_STOP = Price("49000", USD)
LONG_TARGET = Price("52000", USD)
EXPECTED_QUANTITY = Quantity("0.5", ASSET)


def signal(**overrides: object) -> Signal:
    """A coherent long on the harness symbol, unless overridden."""
    spec: dict[str, object] = dict(
        strategy_name="crossover",
        signal_id="sig-1",
        symbol=SYMBOL,
        direction=SignalDirection.LONG,
        reference_price=REFERENCE,
        as_of=T0,
        rationale="fast crossed slow",
        stop_loss=LONG_STOP,
        take_profit=LONG_TARGET,
    )
    spec.update(overrides)
    return Signal(**spec)  # type: ignore[arg-type]


def short(**overrides: object) -> Signal:
    """The mirror image: stop above, target below."""
    spec: dict[str, object] = dict(
        direction=SignalDirection.SHORT,
        stop_loss=Price("51000", USD),
        take_profit=Price("48000", USD),
    )
    spec.update(overrides)
    return signal(**spec)


def exit_signal(**overrides: object) -> Signal:
    """An exit carries no levels -- ``Signal`` refuses one that does."""
    spec: dict[str, object] = dict(
        direction=SignalDirection.EXIT,
        stop_loss=None,
        take_profit=None,
        rationale="regime changed",
    )
    spec.update(overrides)
    return signal(**spec)


class AdvisoryCase(unittest.TestCase):
    """A rig, an advisor over it, and a context builder.

    ``max_signal_age`` is a class attribute so a subclass can set the one knob
    that changes the advisor's behaviour without rewiring anything else.
    """

    max_signal_age: float | None = None

    def setUp(self) -> None:
        self.rig = build_rig(risk=ROOMY)
        self.sizer = SignalSizer(self.rig.risk)
        self.advisor = Advisor(
            self.sizer,
            identity=self.rig.strategy_id,
            audit=self.rig.audit,
            max_signal_age_seconds=self.max_signal_age,
        )

    def context(
        self,
        *,
        bid: str = "50050",
        ask: str = "50150",
        quote_age: float = 0.0,
        as_of: dt.datetime = T0,
        positions: dict[str, Quantity] | None = None,
        quoted: bool = True,
        currency: object = USD,
        policy: StalenessPolicy | None = None,
    ) -> MarketContext:
        """A one-symbol market. Mid is 50 100 by default: 0.1x the stop distance
        above the reference price, so no drift warning fires unless asked for."""
        quotes = {}
        if quoted:
            quotes[SYMBOL] = Quote(
                symbol=SYMBOL,
                bid=Price(bid, currency),
                ask=Price(ask, currency),
                as_of=as_of - dt.timedelta(seconds=quote_age),
                source="test",
            )
        return MarketContext(
            as_of=as_of,
            equity=EQUITY,
            snapshot=MarketSnapshot(as_of=as_of, quotes=quotes),
            positions=positions or {},
            policy=policy or StalenessPolicy(),
        )

    def advise(self, sig: Signal | None = None, **kwargs: object) -> Advice:
        return self.advisor.advise_one(
            self.context(**kwargs), sig if sig is not None else signal(), asset=ASSET
        )

    # -- assertions used across the file -----------------------------------

    def assertBlockedBy(self, advice: Advice, reason: BlockReason) -> None:
        self.assertIn(reason, advice.block_reasons, advice.explain())
        self.assertFalse(advice.is_actionable)
        # The invariant every refusal shares: no number to read out of it.
        self.assertTrue(advice.quantity.is_zero)
        self.assertIsNone(advice.notional)

    def advice_records(self) -> list:
        return [
            r for r in self.rig.sink.records if r.category == AuditCategory.ADVICE.value
        ]


class TestAdvisoryModeCannotExecute(AdvisoryCase):
    """INVARIANT 3, at runtime. The import half is in ``test_core_purity.py``."""

    def test_advising_places_nothing_and_mints_no_approval(self) -> None:
        advice = self.advise()
        self.assertTrue(advice.is_actionable)
        self.assertEqual(0, self.rig.broker.placement_count)
        categories = {r.category for r in self.rig.sink.records}
        # No risk record means approve() was never called, so no approval exists
        # that an execution attempt did not create. No order record means
        # nothing was ever registered as submittable.
        self.assertNotIn(AuditCategory.RISK.value, categories)
        self.assertNotIn(AuditCategory.ORDER.value, categories)

    def test_an_actionable_advice_is_not_an_intent(self) -> None:
        # Nothing in an Advice has the shape the gateway accepts, so advice
        # cannot be forwarded into execution -- it has to be deliberately
        # rebuilt as an intent by code outside this layer.
        for field in fields(Advice):
            self.assertNotIn("OrderIntent", str(field.type))
            self.assertNotIn("ExecutionToken", str(field.type))
        self.assertFalse(hasattr(advisor_module, "OrderIntent"))
        self.assertFalse(hasattr(advisor_module, "ExecutionToken"))

    def test_an_advisor_holding_a_broker_is_refused_at_construction(self) -> None:
        class LeakyAdvisor(Advisor):
            def __init__(self, sizer: SignalSizer, broker: object, **kwargs: object):
                self._broker = broker
                super().__init__(sizer, **kwargs)  # type: ignore[arg-type]

        with self.assertRaises(SafetyViolation) as caught:
            LeakyAdvisor(
                self.sizer,
                self.rig.broker,
                identity=self.rig.strategy_id,
                audit=self.rig.audit,
            )
        self.assertIn("execution surface", str(caught.exception))

    def test_the_gateway_identity_cannot_run_advisory_mode(self) -> None:
        with self.assertRaises(UnauthorizedAction):
            Advisor(
                self.sizer, identity=self.rig.gateway_id, audit=self.rig.audit
            )

    def test_the_operator_identity_cannot_run_advisory_mode(self) -> None:
        # Advisory output is a proposal, and only a proposer may make one.
        with self.assertRaises(UnauthorizedAction):
            Advisor(
                self.sizer, identity=self.rig.operator_id, audit=self.rig.audit
            )

    def test_no_role_can_both_propose_and_execute(self) -> None:
        # The advisor also refuses an identity holding EXECUTE_ORDER. That branch
        # is unreachable while this property holds, which is the point of
        # asserting the property: it is what would have to change first, and the
        # tripwire is there to catch that change.
        both = [
            role
            for role, actions in PERMISSIONS.items()
            if {Action.PROPOSE_ORDER, Action.EXECUTE_ORDER} <= set(actions)
        ]
        self.assertEqual([], both)
        self.assertEqual(Role.STRATEGY, self.advisor.identity.role)


class TestMarketDataIsReportedNotHidden(AdvisoryCase):
    """Execution declines on staleness; advisory mode has to say so."""

    def test_a_stale_quote_blocks_and_still_reports_the_freshness(self) -> None:
        advice = self.advise(quote_age=60.0)
        self.assertBlockedBy(advice, BlockReason.STALE_MARKET_DATA)
        self.assertEqual(Freshness.STALE, advice.freshness)
        self.assertIsNone(advice.live_price)
        self.assertIn("stale", advice.explain())

    def test_a_missing_quote_blocks_and_says_which_symbol(self) -> None:
        advice = self.advise(quoted=False)
        self.assertBlockedBy(advice, BlockReason.MISSING_MARKET_DATA)
        self.assertEqual(Freshness.MISSING, advice.freshness)
        self.assertIn(SYMBOL, str(advice.blocks[0]))
        self.assertIn("no usable quote", advice.explain())

    def test_a_fresh_quote_is_carried_as_the_live_mid(self) -> None:
        advice = self.advise()
        self.assertEqual(Freshness.FRESH, advice.freshness)
        self.assertEqual(Price("50100", USD), advice.live_price)

    def test_no_sizing_is_attempted_once_the_data_is_unusable(self) -> None:
        # Everything past the data check compares against the live price. A
        # sizing result here would be a number computed from nothing.
        advice = self.advise(quoted=False)
        self.assertIsNone(advice.sizing)

    def test_a_signal_and_a_feed_that_disagree_on_currency_raise(self) -> None:
        # Not a block: a block is a market judgement, and this is a wiring
        # error. Price comparison would raise CurrencyMismatch anyway, so the
        # guard exists to say something useful before it does.
        inr = signal(
            reference_price=Price("50000", INR),
            stop_loss=Price("49000", INR),
            take_profit=Price("52000", INR),
        )
        with self.assertRaises(ValueError) as caught:
            self.advise(inr)
        self.assertIn("disagree about what is being priced", str(caught.exception))


class TestSignalAge(AdvisoryCase):
    """A signal has to describe the same market the data does."""

    def test_the_age_is_reported_on_every_advice(self) -> None:
        advice = self.advise(as_of=T0 + dt.timedelta(seconds=3))
        self.assertAlmostEqual(3.0, advice.signal_age_seconds)
        self.assertIn("3.0s old", advice.explain())

    def test_an_old_signal_is_not_blocked_when_no_limit_is_set(self) -> None:
        # No defensible universal figure exists, so the default reports without
        # blocking rather than looking like a control that is not one.
        self.assertIsNone(self.advisor.max_signal_age_seconds)
        advice = self.advise(as_of=T0 + dt.timedelta(hours=9))
        self.assertTrue(advice.is_actionable)
        self.assertAlmostEqual(32400.0, advice.signal_age_seconds)

    def test_a_signal_from_the_future_is_blocked_regardless(self) -> None:
        advice = self.advise(as_of=T0 - dt.timedelta(seconds=30))
        self.assertBlockedBy(advice, BlockReason.SIGNAL_FROM_THE_FUTURE)
        self.assertLess(advice.signal_age_seconds, 0)

    def test_clock_skew_inside_the_context_tolerance_is_allowed(self) -> None:
        # The tolerance is the context's own, so there is one configured
        # allowance for skew rather than a second one here to disagree with it.
        policy = StalenessPolicy(max_age_seconds=5.0, future_tolerance_seconds=2.0)
        advice = self.advise(as_of=T0 - dt.timedelta(seconds=1), policy=policy)
        self.assertTrue(advice.is_actionable)


class TestSignalAgeLimit(AdvisoryCase):
    max_signal_age = 60.0

    def test_a_signal_older_than_the_limit_is_blocked(self) -> None:
        advice = self.advise(as_of=T0 + dt.timedelta(seconds=61))
        self.assertBlockedBy(advice, BlockReason.SIGNAL_TOO_OLD)
        self.assertIn("60.0s", str(advice.blocks[0]))

    def test_a_signal_inside_the_limit_is_actionable(self) -> None:
        advice = self.advise(as_of=T0 + dt.timedelta(seconds=59))
        self.assertTrue(advice.is_actionable)

    def test_a_nonsense_limit_is_refused_at_construction(self) -> None:
        for bad in (0, -1.0):
            with self.subTest(limit=bad), self.assertRaises(ValueError):
                Advisor(
                    self.sizer,
                    identity=self.rig.strategy_id,
                    audit=self.rig.audit,
                    max_signal_age_seconds=bad,
                )
        for bad in (True, "60"):
            with self.subTest(limit=bad), self.assertRaises(TypeError):
                Advisor(
                    self.sizer,
                    identity=self.rig.strategy_id,
                    audit=self.rig.audit,
                    max_signal_age_seconds=bad,
                )


class TestAStopTheMarketHasAlreadyReached(AdvisoryCase):
    """The sharp case: sizing it yields a risk figure that is a fiction."""

    def test_a_long_below_its_stop_is_blocked(self) -> None:
        advice = self.advise(bid="48900", ask="48980")
        self.assertBlockedBy(advice, BlockReason.STOP_ALREADY_BREACHED)
        self.assertIn("already stopped out", str(advice.blocks[0]))

    def test_a_long_exactly_at_its_stop_is_blocked(self) -> None:
        # Equality counts. A stop sitting at the market is a stop that triggers,
        # not one with a hair of room left -- and Price offers no `<=` precisely
        # so this boundary has to be written down somewhere.
        advice = self.advise(bid="48950", ask="49050")
        self.assertEqual(Price("49000", USD), advice.live_price)
        self.assertBlockedBy(advice, BlockReason.STOP_ALREADY_BREACHED)

    def test_a_long_a_hair_above_its_stop_is_still_actionable(self) -> None:
        advice = self.advise(bid="49000", ask="49002")
        self.assertEqual(Price("49001", USD), advice.live_price)
        self.assertTrue(advice.is_actionable)

    def test_a_short_above_its_stop_is_blocked(self) -> None:
        advice = self.advise(short(), bid="51100", ask="51200")
        self.assertBlockedBy(advice, BlockReason.STOP_ALREADY_BREACHED)

    def test_a_short_exactly_at_its_stop_is_blocked(self) -> None:
        advice = self.advise(short(), bid="50950", ask="51050")
        self.assertEqual(Price("51000", USD), advice.live_price)
        self.assertBlockedBy(advice, BlockReason.STOP_ALREADY_BREACHED)

    def test_a_stopless_signal_reaches_the_sizer_rather_than_this_check(self) -> None:
        # There is no stop to have been breached; the refusal has to come from
        # the sizer, and it has to be the sizer's own words.
        advice = self.advise(signal(stop_loss=None))
        self.assertBlockedBy(advice, BlockReason.NO_SIZE)
        self.assertIn(SizingConstraint.MISSING_STOP.value, str(advice.blocks[0]))


class TestSizingRefusalsArriveIntact(AdvisoryCase):
    """The advisor re-derives no limit. It passes the sizer's answer through."""

    def test_a_stopless_signal_carries_the_sizers_reason_verbatim(self) -> None:
        sig = signal(stop_loss=None)
        expected = self.sizer.size(sig, equity=EQUITY, asset=ASSET)
        advice = self.advise(sig)
        self.assertBlockedBy(advice, BlockReason.NO_SIZE)
        self.assertIn(expected.reason, str(advice.blocks[0]))
        # Kept on the advice too, so the record shows what was computed and not
        # merely that something refused.
        self.assertIsNotNone(advice.sizing)
        self.assertFalse(advice.sizing.is_tradeable)

    def test_an_unknowable_loss_budget_blocks_rather_than_sizes(self) -> None:
        # A fill closed with no known cost basis makes today's realized loss a
        # lower bound. An unknown budget must not become an unlimited one.
        self.rig.risk.pnl.record(Money("0.00", USD), attributed=False)
        self.assertIsNone(self.rig.risk.remaining_loss_budget)
        advice = self.advise()
        self.assertBlockedBy(advice, BlockReason.NO_SIZE)
        self.assertIn(
            SizingConstraint.LOSS_BUDGET_CAP.value, str(advice.blocks[0])
        )

    def test_a_realized_loss_shrinks_the_advised_size(self) -> None:
        # Stage 2D wired realized P&L into the daily-loss limit; advisory mode
        # inherits it rather than modelling it again. 100 USD of budget left
        # over a 1 000 USD stop distance is 0.1 BTC.
        self.rig.risk.pnl.record(Money("-900.00", USD))
        advice = self.advise()
        self.assertTrue(advice.is_actionable)
        self.assertEqual(Quantity("0.1", ASSET), advice.quantity)
        self.assertEqual(
            SizingConstraint.LOSS_BUDGET_CAP, advice.sizing.binding_constraint
        )

    def test_the_ordinary_case_is_bound_by_the_risk_fraction(self) -> None:
        advice = self.advise()
        self.assertEqual(EXPECTED_QUANTITY, advice.quantity)
        self.assertEqual("buy", advice.side)
        # The sizer's notional, computed at the reference price -- not the live
        # one. Showing a live-price notional beside a reference-price size would
        # be the same mismatch Advice.__post_init__ exists to prevent: a figure
        # no limit was checked against. The live price is reported separately.
        self.assertEqual(Money("25000.00", USD), advice.notional)
        self.assertEqual(Money("500.00", USD), advice.sizing.max_loss_at_stop)
        self.assertEqual(
            SizingConstraint.RISK_FRACTION, advice.sizing.binding_constraint
        )

    def test_a_short_advises_a_sell(self) -> None:
        advice = self.advise(short())
        self.assertTrue(advice.is_actionable)
        self.assertEqual("sell", advice.side)
        self.assertEqual(EXPECTED_QUANTITY, advice.quantity)


class TestWarningsRatherThanRefusals(AdvisoryCase):
    """Where the line sits, and why it sits there."""

    def test_a_reached_target_warns_and_stays_actionable(self) -> None:
        # The reward is gone but the loss is still bounded by the stop and the
        # size is still real. Refusing would be a judgement about trade quality.
        advice = self.advise(bid="52100", ask="52200")
        self.assertTrue(advice.is_actionable)
        self.assertEqual(EXPECTED_QUANTITY, advice.quantity)
        self.assertTrue(
            any("target" in w and "already been reached" in w for w in advice.warnings),
            advice.warnings,
        )

    def test_price_drift_beyond_a_quarter_of_the_stop_distance_warns(self) -> None:
        advice = self.advise(bid="50300", ask="50400")
        self.assertTrue(advice.is_actionable)
        self.assertTrue(
            any("0.35x the stop distance" in w for w in advice.warnings),
            advice.warnings,
        )

    def test_drift_inside_the_threshold_is_silent(self) -> None:
        advice = self.advise()
        self.assertEqual((), advice.warnings)

    def test_the_drift_threshold_is_configurable(self) -> None:
        strict = Advisor(
            self.sizer,
            identity=self.rig.strategy_id,
            audit=self.rig.audit,
            drift_warning_fraction=Decimal("0.05"),
        )
        advice = strict.advise_one(self.context(), signal(), asset=ASSET)
        self.assertEqual(Decimal("0.05"), strict.drift_warning_fraction)
        self.assertTrue(advice.is_actionable)
        self.assertTrue(any("0.10x the stop distance" in w for w in advice.warnings))

    def test_a_float_threshold_is_refused(self) -> None:
        # INVARIANT 8 reaches the knobs too, not only the money.
        with self.assertRaises(TypeError):
            Advisor(
                self.sizer,
                identity=self.rig.strategy_id,
                audit=self.rig.audit,
                drift_warning_fraction=0.25,
            )

    def test_a_nonpositive_threshold_is_refused(self) -> None:
        with self.assertRaises(ValueError):
            Advisor(
                self.sizer,
                identity=self.rig.strategy_id,
                audit=self.rig.audit,
                drift_warning_fraction=Decimal("0"),
            )

    def test_the_signals_own_warnings_are_carried_verbatim(self) -> None:
        sig = signal(warnings=("thin book", "earnings in 2 days"))
        self.assertEqual(("thin book", "earnings in 2 days"), self.advise(sig).warnings)

    def test_the_signals_warnings_survive_a_refusal(self) -> None:
        # A blocked advice loses its size, not its context.
        sig = signal(warnings=("thin book",))
        advice = self.advise(sig, quoted=False)
        self.assertIn("thin book", advice.warnings)


class TestExitsAreSizedFromThePosition(AdvisoryCase):
    """An exit closes what is held. Guessing the side turns it into a doubling."""

    def test_a_long_position_closes_with_a_sell_of_the_whole_size(self) -> None:
        advice = self.advise(
            exit_signal(), positions={SYMBOL: Quantity("0.25", ASSET)}
        )
        self.assertTrue(advice.is_actionable)
        self.assertEqual("sell", advice.side)
        self.assertEqual(Quantity("0.25", ASSET), advice.quantity)
        self.assertEqual(Money("12525.00", USD), advice.notional)

    def test_a_short_position_closes_with_a_buy_of_the_absolute_size(self) -> None:
        advice = self.advise(
            exit_signal(), positions={SYMBOL: Quantity("-0.004", ASSET)}
        )
        self.assertEqual("buy", advice.side)
        self.assertEqual(Quantity("0.004", ASSET), advice.quantity)

    def test_an_exit_is_not_risk_sized(self) -> None:
        # SignalSizer raises on an EXIT precisely so this cannot happen by
        # accident; the absence of a SizingResult is what says it did not.
        advice = self.advise(
            exit_signal(), positions={SYMBOL: Quantity("0.25", ASSET)}
        )
        self.assertIsNone(advice.sizing)

    def test_an_exit_on_a_flat_book_is_blocked(self) -> None:
        advice = self.advise(exit_signal())
        self.assertBlockedBy(advice, BlockReason.NOTHING_TO_CLOSE)
        self.assertIn("opposite direction", str(advice.blocks[0]))

    def test_a_blocked_exit_names_no_side(self) -> None:
        # An exit blocked before the position was consulted has no knowable
        # closing side, and "none" beats a guess.
        advice = self.advise(exit_signal(), quoted=False)
        self.assertEqual("none", advice.side)


class TestABlockedAdviceCarriesNoSize(unittest.TestCase):
    """``Advice.__post_init__``, tested against directly.

    These constructions are what a future code path would have to do to produce
    a misleading report, so they are asserted at the type rather than through
    the advisor.
    """

    def setUp(self) -> None:
        self.rig = build_rig(risk=ROOMY)
        self.sizer = SignalSizer(self.rig.risk)
        self.signal = signal()
        self.sizing = self.sizer.size(self.signal, equity=EQUITY, asset=ASSET)

    def build(self, **overrides: object) -> Advice:
        spec: dict[str, object] = dict(
            signal=self.signal,
            asset=ASSET,
            freshness=Freshness.FRESH,
            live_price=Price("50100", USD),
            signal_age_seconds=0.0,
            quantity=EXPECTED_QUANTITY,
            notional=Money("25050.00", USD),
            side="buy",
            sizing=self.sizing,
        )
        spec.update(overrides)
        return Advice(**spec)  # type: ignore[arg-type]

    def test_the_reference_construction_is_valid(self) -> None:
        self.assertTrue(self.build().is_actionable)

    def test_a_block_with_a_size_is_a_safety_violation(self) -> None:
        with self.assertRaises(SafetyViolation) as caught:
            self.build(blocks=(Block(BlockReason.NO_SIZE, "no"),))
        self.assertIn("reads the size out of", str(caught.exception))

    def test_a_quantity_the_sizer_did_not_produce_is_a_safety_violation(self) -> None:
        with self.assertRaises(SafetyViolation) as caught:
            self.build(quantity=Quantity("0.4", ASSET))
        self.assertIn("limits were applied to", str(caught.exception))

    def test_a_negative_quantity_is_refused(self) -> None:
        with self.assertRaises(ValueError):
            self.build(quantity=Quantity("-0.5", ASSET), sizing=None)

    def test_a_zero_quantity_with_no_block_is_not_actionable(self) -> None:
        # Belt and braces: is_actionable requires both halves, so a bug that
        # produced this answers "do nothing" rather than "trade nothing".
        advice = self.build(quantity=Quantity.zero(ASSET), sizing=None, notional=None)
        self.assertFalse(advice.is_actionable)

    def test_blocks_must_contain_blocks(self) -> None:
        with self.assertRaises(TypeError):
            self.build(quantity=Quantity.zero(ASSET), sizing=None, blocks=("stale",))

    def test_the_signal_and_the_quantity_are_type_checked(self) -> None:
        with self.assertRaises(TypeError):
            self.build(signal="sig-1")
        with self.assertRaises(TypeError):
            self.build(quantity="0.5")

    def test_a_block_needs_a_reason_and_a_sentence(self) -> None:
        with self.assertRaises(TypeError):
            Block("stale", "detail")  # type: ignore[arg-type]
        with self.assertRaises(ValueError):
            Block(BlockReason.NO_SIZE, "   ")


class TestAdviceIsAuditedAsInformation(AdvisoryCase):
    """An operator who acted on advice has to be reconstructable from the log."""

    def test_an_actionable_advice_is_recorded_as_issued(self) -> None:
        self.advise()
        records = self.advice_records()
        self.assertEqual(1, len(records))
        self.assertEqual("advice.issued", records[0].action)
        # Never ALLOWED: that would read as an approval, and advice decides
        # nothing about permission.
        self.assertEqual(AuditOutcome.INFO.value, records[0].outcome)
        self.assertEqual(self.rig.strategy_id.principal_id, records[0].actor)

    def test_a_refusal_is_recorded_as_declined_with_its_reason(self) -> None:
        self.advise(quoted=False)
        record = self.advice_records()[0]
        self.assertEqual("advice.declined", record.action)
        # A refusal is INFO too: REFUSED is what a risk check says.
        self.assertEqual(AuditOutcome.INFO.value, record.outcome)
        self.assertEqual(
            [BlockReason.MISSING_MARKET_DATA.value],
            [b["reason"] for b in record.details["blocks"]],
        )

    def test_the_record_carries_the_size_and_the_signal(self) -> None:
        self.advise()
        details = self.advice_records()[0].details
        self.assertEqual("0.50000000", details["quantity"])
        self.assertTrue(details["actionable"])
        self.assertEqual("sig-1", details["signal"]["signal_id"])
        self.assertEqual("fast crossed slow", details["signal"]["rationale"])

    def test_a_batch_is_audited_once_per_signal_in_order(self) -> None:
        first, second = signal(), signal(signal_id="sig-2", stop_loss=None)
        advices = self.advisor.advise(
            self.context(), [first, second], assets={SYMBOL: ASSET}
        )
        self.assertEqual(2, len(advices))
        self.assertEqual(
            ["advice.issued", "advice.declined"],
            [r.action for r in self.advice_records()],
        )

    def test_a_malformed_batch_audits_nothing(self) -> None:
        # Half a report in the log looks like a whole one.
        with self.assertRaises(TypeError):
            self.advisor.advise(
                self.context(), [signal(), "nonsense"], assets={SYMBOL: ASSET}
            )
        self.assertEqual([], self.advice_records())

    def test_the_chain_still_verifies(self) -> None:
        self.advise()
        self.rig.audit.verify()


class TestWiringErrorsSurface(AdvisoryCase):
    """Things the advisor refuses to guess at."""

    def test_a_symbol_with_no_asset_raises(self) -> None:
        with self.assertRaises(ValueError) as caught:
            self.advisor.advise(self.context(), [signal()], assets={})
        self.assertIn("will not guess", str(caught.exception))

    def test_a_blank_asset_raises(self) -> None:
        with self.assertRaises(ValueError):
            self.advisor.advise(self.context(), [signal()], assets={SYMBOL: "  "})

    def test_a_non_context_raises(self) -> None:
        with self.assertRaises(TypeError):
            self.advisor.advise(object(), [signal()], assets={SYMBOL: ASSET})

    def test_a_string_is_not_a_sequence_of_signals(self) -> None:
        with self.assertRaises(TypeError):
            self.advisor.advise(self.context(), "sig-1", assets={SYMBOL: ASSET})

    def test_a_non_sizer_raises(self) -> None:
        with self.assertRaises(TypeError):
            Advisor(
                object(),  # type: ignore[arg-type]
                identity=self.rig.strategy_id,
                audit=self.rig.audit,
            )

    def test_a_non_audit_log_raises(self) -> None:
        with self.assertRaises(TypeError):
            Advisor(self.sizer, identity=self.rig.strategy_id, audit=object())  # type: ignore[arg-type]

    def test_an_empty_batch_is_not_an_error(self) -> None:
        self.assertEqual((), self.advisor.advise(self.context(), [], assets={}))


class TestTheExplanation(AdvisoryCase):
    """The report is the product. It has to say the things that matter."""

    def test_an_actionable_advice_reads_as_an_instruction_with_caveats(self) -> None:
        sig = signal(evidence={"sma_fast": "50100", "sma_slow": "49800"})
        text = self.advise(sig).explain()
        self.assertIn("BTCUSD long -- buy 0.50000000 BTC (25000.00 USD)", text)
        self.assertIn("stop 49000 USD", text)
        self.assertIn("reward:risk 2.00", text)
        self.assertIn("risking 500.00 USD to the stop", text)
        self.assertIn("bound by risk_fraction_per_trade", text)
        self.assertIn("because: fast crossed slow", text)
        self.assertIn("sma_slow = 49800", text)

    def test_an_actionable_advice_says_it_is_not_an_order(self) -> None:
        text = self.advise().explain()
        self.assertIn(
            "advice only: no order exists, and every limit is checked again", text
        )

    def test_a_refusal_shows_no_quantity_and_names_the_block(self) -> None:
        text = self.advise(quote_age=60.0).explain()
        self.assertIn("NOT ACTIONABLE", text)
        self.assertNotIn("buy", text)
        self.assertIn("blocked -- stale_market_data:", text)
        # And carries no closing note: there is nothing to caveat.
        self.assertNotIn("advice only", text)

    def test_a_warning_is_shown_beside_an_actionable_advice(self) -> None:
        # Actionable and caveated at once: the operator gets both.
        sig = signal(warnings=("thin book",))
        text = self.advise(sig).explain()
        self.assertIn("warning -- thin book", text)
        self.assertIn("advice only:", text)

    def test_str_is_the_explanation(self) -> None:
        advice = self.advise()
        self.assertEqual(advice.explain(), str(advice))

    def test_as_details_is_json_shaped(self) -> None:
        details = self.advise().as_details()
        self.assertEqual(ASSET, details["asset"])
        self.assertEqual("fresh", details["freshness"])
        self.assertEqual("50100", details["live_price"])
        self.assertEqual("buy", details["side"])
        self.assertEqual([], details["blocks"])
        self.assertIsInstance(details["sizing"], dict)

    def test_the_symbol_is_the_signals(self) -> None:
        self.assertEqual(SYMBOL, self.advise().symbol)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
