"""Tests for the signal layer: what a strategy decides, and what it cannot do.

Four things are worth testing here, and they are all safety properties rather
than arithmetic:

* **A signal cannot be internally incoherent.** A long whose stop sits at or
  above its reference price would produce a zero or negative risk-per-unit, and
  a sizer dividing by that computes an unbounded position from a plausible-looking
  number. Construction refuses it, so the bad value never exists to be divided by.
* **A signal carries no quantity, and a signal strategy cannot acquire one.**
  ``SignalStrategy`` rejects sizing-shaped and execution-shaped attribute names at
  class-definition time (INVARIANT 3), so the mistake fails to import rather than
  failing in production.
* **``MarketContext`` filters what it hands over.** Stale quotes read as absent and
  in-progress bars are withheld, so a strategy that forgets to check gets nothing
  rather than something wrong.
* **The runner audits and cannot execute.** Every signal is recorded before
  anything downstream sees it, and the runner holds no gateway, broker, or token.

``MovingAverageCrossover`` is exercised end to end because an interface is only
proven by something real passing through it -- including that re-deciding on the
same closed bar yields the same ``signal_id``, which is what makes a restart
idempotent at the gateway (INVARIANT 12).
"""

from __future__ import annotations

import unittest
from datetime import datetime, timedelta
from decimal import Decimal

from trading.core.audit import AuditCategory, AuditLog, InMemoryAuditSink
from trading.core.authz import Action, Principal, Role
from trading.core.clock import UTC, ManualClock
from trading.core.errors import (
    SafetyViolation,
    StaleMarketData,
    UnauthorizedAction,
)
from trading.core.marketdata import (
    Candle,
    Freshness,
    MarketSnapshot,
    Quote,
    StalenessPolicy,
)
from trading.core.money import USD, USDT, Money, Price, Quantity
from trading.strategy import (
    MarketContext,
    MarketView,
    MovingAverageCrossover,
    Signal,
    SignalDirection,
    SignalRunner,
    SignalStrategy,
)
from trading.strategy.indicators import atr

SYMBOL = "BTCUSD"
OTHER = "ETHUSD"
T0 = datetime(2026, 1, 1, 0, 0, tzinfo=UTC)
INTERVAL = 60
EQUITY = Money("10000.00", USD)


def price(value: object, currency=USD) -> Price:
    return Price(Decimal(str(value)), currency)


def quote(
    *,
    symbol: str = SYMBOL,
    bid: object = "119.99",
    ask: object = "120.01",
    as_of: datetime | None = None,
    currency=USD,
) -> Quote:
    return Quote(
        symbol=symbol,
        bid=price(bid, currency),
        ask=price(ask, currency),
        as_of=as_of if as_of is not None else T0,
        source="test",
    )


def candle(
    index: int, close: object, *, symbol: str = SYMBOL, currency=USD
) -> Candle:
    """One bar at slot ``index``, spanning ten either side of ``close``."""
    value = Decimal(str(close))
    return Candle(
        symbol=symbol,
        open_time=T0 + timedelta(seconds=INTERVAL * index),
        interval_seconds=INTERVAL,
        open=price(value, currency),
        high=price(value + 10, currency),
        low=price(value - 10, currency),
        close=price(value, currency),
        volume=Quantity("1", "BTC"),
        source="test",
    )


def flat_candle(index: int, value: object, *, symbol: str = SYMBOL) -> Candle:
    """A bar that opened, closed, and traded at one price -- zero true range."""
    at = price(Decimal(str(value)))
    return Candle(
        symbol=symbol,
        open_time=T0 + timedelta(seconds=INTERVAL * index),
        interval_seconds=INTERVAL,
        open=at,
        high=at,
        low=at,
        close=at,
        volume=Quantity("1", "BTC"),
        source="test",
    )


def signal(**overrides: object) -> Signal:
    """A valid long signal, so a test can vary one field and see it refused."""
    fields: dict[str, object] = {
        "strategy_name": "test-strategy",
        "signal_id": "sig-1",
        "symbol": SYMBOL,
        "direction": SignalDirection.LONG,
        "reference_price": price("100"),
        "as_of": T0,
        "rationale": "the fast mean crossed above the slow one",
        "stop_loss": price("90"),
        "take_profit": price("120"),
    }
    fields.update(overrides)
    return Signal(**fields)  # type: ignore[arg-type]


class TestASignalCannotContradictItself(unittest.TestCase):
    """The coherence rules exist so risk-per-unit is always usable."""

    def test_a_long_stop_above_the_reference_is_refused(self) -> None:
        with self.assertRaises(ValueError) as caught:
            signal(stop_loss=price("110"))
        self.assertIn("below", str(caught.exception))

    def test_a_long_stop_at_the_reference_is_refused(self) -> None:
        # The dangerous boundary: a stop *at* entry has zero risk-per-unit, and a
        # sizer dividing by it would compute an unbounded position.
        with self.assertRaises(ValueError):
            signal(stop_loss=price("100"))

    def test_a_short_stop_below_the_reference_is_refused(self) -> None:
        with self.assertRaises(ValueError) as caught:
            signal(
                direction=SignalDirection.SHORT,
                stop_loss=price("90"),
                take_profit=price("80"),
            )
        self.assertIn("above", str(caught.exception))

    def test_a_short_stop_at_the_reference_is_refused(self) -> None:
        with self.assertRaises(ValueError):
            signal(
                direction=SignalDirection.SHORT,
                stop_loss=price("100"),
                take_profit=price("80"),
            )

    def test_a_long_target_below_the_reference_is_refused(self) -> None:
        with self.assertRaises(ValueError) as caught:
            signal(take_profit=price("95"))
        self.assertIn("above", str(caught.exception))

    def test_a_short_target_above_the_reference_is_refused(self) -> None:
        with self.assertRaises(ValueError):
            signal(
                direction=SignalDirection.SHORT,
                stop_loss=price("110"),
                take_profit=price("105"),
            )

    def test_a_coherent_short_is_accepted(self) -> None:
        held = signal(
            direction=SignalDirection.SHORT,
            stop_loss=price("110"),
            take_profit=price("80"),
        )
        self.assertEqual(held.risk_per_unit, Decimal("10"))
        self.assertEqual(held.reward_per_unit, Decimal("20"))

    def test_an_exit_may_not_carry_a_stop_or_target(self) -> None:
        # An exit closes a position; it does not open one with defined risk. A
        # stop attached to it would be a level nobody acts on.
        with self.assertRaises(ValueError):
            signal(direction=SignalDirection.EXIT, take_profit=None)
        with self.assertRaises(ValueError):
            signal(direction=SignalDirection.EXIT, stop_loss=None)

    def test_a_bare_exit_is_accepted(self) -> None:
        held = signal(direction=SignalDirection.EXIT, stop_loss=None, take_profit=None)
        self.assertIsNone(held.risk_per_unit)
        self.assertIsNone(held.risk_reward_ratio)


class TestASignalMustBeExplainable(unittest.TestCase):
    def test_an_empty_rationale_is_refused(self) -> None:
        # Advisory mode has to explain itself; an unexplained signal is one an
        # operator has to take on trust.
        for bad in ("", "   "):
            with self.assertRaises(ValueError) as caught:
                signal(rationale=bad)
            self.assertIn("rationale", str(caught.exception))

    def test_an_empty_signal_id_is_refused(self) -> None:
        with self.assertRaises(ValueError):
            signal(signal_id="")

    def test_evidence_must_be_strings(self) -> None:
        # It goes straight into the audit log, which does not serialise objects.
        with self.assertRaises(TypeError):
            signal(evidence={"atr": Decimal("3")})

    def test_warnings_must_be_a_tuple_of_strings(self) -> None:
        with self.assertRaises(TypeError):
            signal(warnings=["a list is mutable"])
        with self.assertRaises(TypeError):
            signal(warnings=(1,))

    def test_as_details_is_audit_safe(self) -> None:
        details = signal(evidence={"sma_10": "99.5"}).as_details()
        self.assertEqual(details["direction"], "long")
        self.assertEqual(details["risk_per_unit"], "10")
        self.assertEqual(details["evidence"], {"sma_10": "99.5"})
        for value in details.values():
            self.assertIsInstance(value, (str, dict, list, type(None)))


class TestSignalValueRules(unittest.TestCase):
    def test_a_naive_timestamp_is_refused(self) -> None:
        with self.assertRaises(ValueError) as caught:
            signal(as_of=datetime(2026, 1, 1, 0, 0))
        self.assertIn("timezone-aware", str(caught.exception))

    def test_a_float_conviction_is_refused(self) -> None:
        # INVARIANT 8: floats are rejected wherever a number reaches arithmetic.
        with self.assertRaises(TypeError):
            signal(conviction=0.5)

    def test_conviction_outside_zero_to_one_is_refused(self) -> None:
        for bad in ("0", "-0.1", "1.5"):
            with self.assertRaises(ValueError):
                signal(conviction=Decimal(bad))

    def test_conviction_is_coerced_to_decimal(self) -> None:
        self.assertEqual(signal(conviction="0.25").conviction, Decimal("0.25"))

    def test_a_stop_in_another_currency_is_refused(self) -> None:
        with self.assertRaises(ValueError) as caught:
            signal(stop_loss=price("90", USDT))
        self.assertIn("USDT", str(caught.exception))

    def test_risk_reward_ratio_divides_reward_by_risk(self) -> None:
        self.assertEqual(signal().risk_reward_ratio, Decimal("2"))
        self.assertIsNone(signal(take_profit=None).risk_reward_ratio)

    def test_an_entry_side_is_intrinsic(self) -> None:
        self.assertEqual(signal().side_for(), "buy")
        self.assertEqual(
            signal(
                direction=SignalDirection.SHORT,
                stop_loss=price("110"),
                take_profit=price("80"),
            ).side_for(),
            "sell",
        )

    def test_an_exit_refuses_to_guess_its_side(self) -> None:
        # Guessing here turns an exit into a doubling.
        exit_signal = signal(
            direction=SignalDirection.EXIT, stop_loss=None, take_profit=None
        )
        with self.assertRaises(ValueError):
            exit_signal.side_for()
        self.assertEqual(exit_signal.side_for(position_is_long=True), "sell")
        self.assertEqual(exit_signal.side_for(position_is_long=False), "buy")

    def test_age_is_measured_from_the_decision_time(self) -> None:
        # Advisory mode uses this to decide a signal has gone too stale to act on.
        self.assertEqual(signal().age_seconds(T0 + timedelta(seconds=30)), 30.0)

    def test_is_entry_distinguishes_entries_from_exits(self) -> None:
        self.assertTrue(SignalDirection.LONG.is_entry)
        self.assertTrue(SignalDirection.SHORT.is_entry)
        self.assertFalse(SignalDirection.EXIT.is_entry)


class TestASignalStrategyCannotExecuteOrSize(unittest.TestCase):
    """INVARIANT 3, enforced at class-definition time."""

    def test_an_execution_shaped_method_fails_to_define(self) -> None:
        with self.assertRaises(SafetyViolation) as caught:

            class Rogue(SignalStrategy):
                def generate(self, context):  # pragma: no cover - never defined
                    return []

                def place_order(self):  # pragma: no cover - never defined
                    ...

        self.assertIn("place_order", str(caught.exception))

    def test_a_sizing_shaped_method_fails_to_define(self) -> None:
        # Sizing depends on equity and stop distance, neither of which a strategy
        # should be reasoning about.
        with self.assertRaises(SafetyViolation) as caught:

            class Sizer(SignalStrategy):
                def generate(self, context):  # pragma: no cover - never defined
                    return []

                def position_size(self):  # pragma: no cover - never defined
                    ...

        self.assertIn("position_size", str(caught.exception))

    def test_the_reference_strategy_defines_neither(self) -> None:
        self.assertTrue(issubclass(MovingAverageCrossover, SignalStrategy))

    def test_a_signal_has_no_quantity_field(self) -> None:
        # The structural half of the split: there is nowhere to put a size.
        for forbidden in ("quantity", "size", "notional"):
            self.assertFalse(hasattr(signal(), forbidden))


class TestMarketContextFiltersWhatItHandsOver(unittest.TestCase):
    def context(self, **overrides: object) -> MarketContext:
        as_of = overrides.pop("as_of", T0)
        quotes = overrides.pop("quotes", {SYMBOL: quote(as_of=T0)})
        fields: dict[str, object] = {
            "as_of": as_of,
            "equity": EQUITY,
            "snapshot": MarketSnapshot(as_of=as_of, quotes=quotes),
        }
        fields.update(overrides)
        return MarketContext(**fields)  # type: ignore[arg-type]

    def test_a_stale_quote_reads_as_absent(self) -> None:
        late = self.context(as_of=T0 + timedelta(seconds=60))
        self.assertIs(late.freshness(SYMBOL), Freshness.STALE)
        self.assertIsNone(late.quote(SYMBOL))
        self.assertIsNone(late.price(SYMBOL))
        self.assertEqual(late.tradable_symbols, [])
        self.assertEqual(late.stale_symbols, [SYMBOL])

    def test_require_quote_raises_rather_than_returning_none(self) -> None:
        late = self.context(as_of=T0 + timedelta(seconds=60))
        with self.assertRaises(StaleMarketData):
            late.require_quote(SYMBOL)

    def test_a_fresh_quote_is_handed_over(self) -> None:
        fresh = self.context()
        self.assertIs(fresh.freshness(SYMBOL), Freshness.FRESH)
        self.assertEqual(fresh.price(SYMBOL), price("120.00"))
        self.assertEqual(fresh.tradable_symbols, [SYMBOL])

    def test_an_unknown_symbol_is_missing_not_an_error(self) -> None:
        fresh = self.context()
        self.assertIs(fresh.freshness(OTHER), Freshness.MISSING)
        self.assertIsNone(fresh.quote(OTHER))

    def test_an_in_progress_bar_is_withheld(self) -> None:
        # Reading a partial bar as if it were complete is the classic reason a
        # strategy behaves differently live than in a backtest.
        bars = [candle(0, 100), candle(1, 101)]
        mid_bar = self.context(
            as_of=T0 + timedelta(seconds=90),
            quotes={SYMBOL: quote(as_of=T0 + timedelta(seconds=90))},
            history={SYMBOL: bars},
        )
        self.assertEqual(mid_bar.candles(SYMBOL), (bars[0],))
        self.assertTrue(mid_bar.has_history(SYMBOL, at_least=1))
        self.assertFalse(mid_bar.has_history(SYMBOL, at_least=2))

    def test_candles_can_be_limited_to_the_most_recent(self) -> None:
        bars = [candle(i, 100 + i) for i in range(3)]
        held = self.context(
            as_of=T0 + timedelta(seconds=180),
            quotes={SYMBOL: quote(as_of=T0 + timedelta(seconds=180))},
            history={SYMBOL: bars},
        )
        self.assertEqual(held.candles(SYMBOL, count=2), tuple(bars[1:]))
        self.assertEqual(held.candles(SYMBOL, count=0), ())
        with self.assertRaises(ValueError):
            held.candles(SYMBOL, count=-1)

    def test_out_of_order_history_is_refused(self) -> None:
        with self.assertRaises(ValueError) as caught:
            self.context(history={SYMBOL: [candle(1, 101), candle(0, 100)]})
        self.assertIn("chronological", str(caught.exception))

    def test_history_keyed_under_the_wrong_symbol_is_refused(self) -> None:
        with self.assertRaises(ValueError):
            self.context(history={OTHER: [candle(0, 100)]})

    def test_a_naive_as_of_is_refused(self) -> None:
        with self.assertRaises(ValueError):
            MarketContext(
                as_of=datetime(2026, 1, 1),
                equity=EQUITY,
                snapshot=MarketSnapshot(as_of=T0, quotes={}),
            )

    def test_positions_must_be_quantities(self) -> None:
        with self.assertRaises(TypeError):
            self.context(positions={SYMBOL: "0.5"})

    def test_position_direction_helpers(self) -> None:
        flat = self.context()
        self.assertTrue(flat.is_flat(SYMBOL))
        self.assertFalse(flat.is_long(SYMBOL))
        self.assertEqual(flat.position(SYMBOL, asset="BTC"), Quantity("0", "BTC"))

        long = self.context(positions={SYMBOL: Quantity("0.5", "BTC")})
        self.assertTrue(long.is_long(SYMBOL))
        self.assertFalse(long.is_flat(SYMBOL))
        self.assertFalse(long.is_short(SYMBOL))

        short = self.context(positions={SYMBOL: Quantity("-0.5", "BTC")})
        self.assertTrue(short.is_short(SYMBOL))
        self.assertFalse(short.is_long(SYMBOL))

    def test_a_caller_cannot_mutate_the_context_after_construction(self) -> None:
        positions = {SYMBOL: Quantity("0.5", "BTC")}
        held = self.context(positions=positions)
        positions[SYMBOL] = Quantity("99", "BTC")
        self.assertEqual(held.position(SYMBOL, asset="BTC"), Quantity("0.5", "BTC"))

    def test_the_stage_one_bridge_omits_stale_symbols(self) -> None:
        # A Stage 1 strategy has no way to ask a MarketView how old a price is,
        # so a stale price must not appear in one.
        mixed = self.context(
            quotes={
                SYMBOL: quote(as_of=T0),
                OTHER: quote(symbol=OTHER, as_of=T0 - timedelta(seconds=60)),
            },
            positions={SYMBOL: Quantity("0.5", "BTC")},
        )
        view = mixed.to_market_view()
        self.assertIsInstance(view, MarketView)
        self.assertEqual(sorted(view.prices), [SYMBOL])
        self.assertIsNone(view.price(OTHER))
        self.assertEqual(view.equity, EQUITY)
        self.assertFalse(view.is_flat(SYMBOL))

    def test_a_custom_policy_is_honoured(self) -> None:
        tolerant = self.context(
            as_of=T0 + timedelta(seconds=60),
            policy=StalenessPolicy(max_age_seconds=120),
        )
        self.assertIs(tolerant.freshness(SYMBOL), Freshness.FRESH)


class TestSignalRunner(unittest.TestCase):
    def setUp(self) -> None:
        self.clock = ManualClock(T0)
        self.sink = InMemoryAuditSink()
        self.audit = AuditLog(sink=self.sink, clock=self.clock)
        self.identity = Principal("strategy-1", Role.STRATEGY)
        self.context = MarketContext(
            as_of=T0,
            equity=EQUITY,
            snapshot=MarketSnapshot(as_of=T0, quotes={SYMBOL: quote(as_of=T0)}),
        )

    def runner(self, strategy: SignalStrategy, **overrides: object) -> SignalRunner:
        return SignalRunner(
            strategy,
            identity=overrides.get("identity", self.identity),  # type: ignore[arg-type]
            audit=self.audit,
            clock=self.clock,
        )

    def actions(self) -> list[str]:
        return [record.action for record in self.sink.records]

    def test_it_records_every_signal_before_anything_downstream_sees_it(self) -> None:
        emitted = signal()

        class Fixed(SignalStrategy):
            name = "fixed"

            def generate(self, context):
                return [emitted]

        produced = self.runner(Fixed()).generate(self.context)
        self.assertEqual(produced, [emitted])
        self.assertEqual(self.actions(), ["strategy.signalled"])
        record = self.sink.records[0]
        self.assertEqual(record.category, AuditCategory.SIGNAL.value)
        self.assertEqual(record.actor, "strategy-1")
        self.assertEqual(record.details["signal_id"], "sig-1")

    def test_a_broken_strategy_is_audited_and_the_failure_propagates(self) -> None:
        class Broken(SignalStrategy):
            name = "broken"

            def generate(self, context):
                raise RuntimeError("indicator blew up")

        with self.assertRaises(RuntimeError):
            self.runner(Broken()).generate(self.context)
        self.assertEqual(self.actions(), ["strategy.failed"])
        self.assertIn("indicator blew up", self.sink.records[0].details["error"])

    def test_a_non_signal_return_is_a_safety_violation(self) -> None:
        class Wrong(SignalStrategy):
            name = "wrong"

            def generate(self, context):
                return ["buy some bitcoin"]

        with self.assertRaises(SafetyViolation):
            self.runner(Wrong()).generate(self.context)

    def test_nothing_is_audited_when_validation_rejects_the_batch(self) -> None:
        # Validation runs over the whole batch before any record is written, so a
        # partially-bad batch does not leave half its signals in the audit trail.
        good = signal()

        class Mixed(SignalStrategy):
            name = "mixed"

            def generate(self, context):
                return [good, object()]

        with self.assertRaises(SafetyViolation):
            self.runner(Mixed()).generate(self.context)
        self.assertEqual(self.actions(), [])

    def test_an_identity_that_may_not_propose_is_refused(self) -> None:
        class Quiet(SignalStrategy):
            name = "quiet"

            def generate(self, context):
                return []

        with self.assertRaises(UnauthorizedAction):
            self.runner(Quiet(), identity=Principal("auditor-1", Role.AUDITOR))

    def test_a_strategy_holding_an_execution_surface_is_refused(self) -> None:
        class Smuggler(SignalStrategy):
            name = "smuggler"

            def __init__(self) -> None:
                self.helper = _FakeBroker()

            def generate(self, context):  # pragma: no cover - never runs
                return []

        with self.assertRaises(SafetyViolation) as caught:
            self.runner(Smuggler())
        self.assertIn("place_order", str(caught.exception))

    def test_the_runner_holds_no_route_to_a_venue(self) -> None:
        # The structural guarantee: signal code and execution code never appear
        # in the same call stack, because the runner has nothing to call.
        class Quiet(SignalStrategy):
            name = "quiet"

            def generate(self, context):
                return []

        runner = self.runner(Quiet())
        for attribute in vars(runner):
            self.assertNotIn("gateway", attribute)
            self.assertNotIn("broker", attribute)
            self.assertNotIn("token", attribute)
        for forbidden in ("submit", "place_order", "execute"):
            self.assertFalse(hasattr(runner, forbidden))
        self.assertEqual(runner.strategy_name, "quiet")

    def test_it_refuses_a_non_context(self) -> None:
        class Quiet(SignalStrategy):
            name = "quiet"

            def generate(self, context):  # pragma: no cover - never runs
                return []

        with self.assertRaises(TypeError):
            self.runner(Quiet()).generate(self.context.to_market_view())

    def test_it_refuses_a_non_strategy(self) -> None:
        with self.assertRaises(TypeError):
            self.runner(object())  # type: ignore[arg-type]

    def test_proposing_is_the_only_permission_a_runner_needs(self) -> None:
        from trading.core.authz import is_authorized

        self.assertTrue(is_authorized(self.identity, Action.PROPOSE_ORDER))
        self.assertFalse(is_authorized(self.identity, Action.EXECUTE_ORDER))


class _FakeBroker:
    """Execution-shaped by duck type, which is what the tripwire looks for."""

    def place_order(self, *args: object, **kwargs: object) -> None:  # pragma: no cover
        ...


class TestMovingAverageCrossoverEndToEnd(unittest.TestCase):
    """A real decision through the real interface.

    Windows are small (fast 2, slow 3, ATR 2) so the bar series stays readable
    and the crossover can be verified by hand rather than asserted on trust.
    """

    #: fast(2) below slow(3) on the prior bar, above it on the last -- a cross up.
    RISING = ["100", "100", "90", "85", "120"]
    #: The mirror image: a cross down.
    FALLING = ["90", "90", "100", "105", "80"]

    def strategy(self, **overrides: object) -> MovingAverageCrossover:
        fields: dict[str, object] = {
            "fast": 2,
            "slow": 3,
            "atr_period": 2,
            "symbols": [SYMBOL],
        }
        fields.update(overrides)
        return MovingAverageCrossover(**fields)  # type: ignore[arg-type]

    def context(
        self,
        closes: list[str],
        *,
        positions: dict | None = None,
        bid: str = "119.99",
        ask: str = "120.01",
    ) -> MarketContext:
        as_of = T0 + timedelta(seconds=INTERVAL * len(closes))
        return MarketContext(
            as_of=as_of,
            equity=EQUITY,
            snapshot=MarketSnapshot(
                as_of=as_of,
                quotes={SYMBOL: quote(bid=bid, ask=ask, as_of=as_of)},
            ),
            positions=positions or {},
            history={SYMBOL: [candle(i, c) for i, c in enumerate(closes)]},
        )

    def test_a_cross_up_while_flat_produces_a_long_with_defined_risk(self) -> None:
        produced = self.strategy().generate(self.context(self.RISING))
        self.assertEqual(len(produced), 1)
        held = produced[0]
        self.assertIs(held.direction, SignalDirection.LONG)
        self.assertEqual(held.symbol, SYMBOL)
        self.assertEqual(held.reference_price, price("120.00"))
        # ATR(2) over the last two bars: ranges 20 and 45 (the second gaps up
        # from a close of 85 to a high of 130), so 32.5. Stop is 2x below.
        self.assertEqual(held.risk_per_unit, Decimal("65.00"))
        self.assertEqual(held.stop_loss, price("55.00"))
        self.assertEqual(held.take_profit, price("250.00"))
        self.assertEqual(held.risk_reward_ratio, Decimal("2"))
        self.assertEqual(held.warnings, ())

    def test_the_signal_carries_the_evidence_that_justified_it(self) -> None:
        held = self.strategy().generate(self.context(self.RISING))[0]
        self.assertEqual(held.evidence["sma_2"], "102.5")
        self.assertEqual(held.evidence["sma_3"], "98.333333333333")
        self.assertEqual(held.evidence["atr_2"], "32.5")
        self.assertEqual(held.evidence["bars_available"], "5")
        self.assertIn("crossed above", held.rationale)

    def test_re_deciding_on_the_same_closed_bar_yields_the_same_id(self) -> None:
        # This is what makes a process restart idempotent at the gateway
        # (INVARIANT 12): a random or wall-clock id would defeat the duplicate
        # check before the registry was ever consulted.
        first = self.strategy().generate(self.context(self.RISING))[0]
        second = self.strategy().generate(self.context(self.RISING))[0]
        self.assertEqual(first.signal_id, second.signal_id)
        self.assertIn(SYMBOL, first.signal_id)
        self.assertIn("ma-crossover", first.signal_id)

    def test_a_new_bar_yields_a_different_id(self) -> None:
        rising = self.strategy().generate(self.context(self.RISING))[0]
        shifted = self.strategy().generate(self.context(self.RISING + ["130"]))
        self.assertNotEqual(
            rising.signal_id,
            shifted[0].signal_id if shifted else "no-signal",
        )

    def test_a_cross_up_while_already_long_produces_nothing(self) -> None:
        # Adding to a position is a different decision from opening one, and
        # this strategy does not make it.
        produced = self.strategy().generate(
            self.context(self.RISING, positions={SYMBOL: Quantity("0.5", "BTC")})
        )
        self.assertEqual(produced, [])

    def test_a_cross_down_while_long_produces_a_bare_exit(self) -> None:
        produced = self.strategy().generate(
            self.context(
                self.FALLING,
                positions={SYMBOL: Quantity("0.5", "BTC")},
                bid="79.99",
                ask="80.01",
            )
        )
        self.assertEqual(len(produced), 1)
        self.assertIs(produced[0].direction, SignalDirection.EXIT)
        self.assertIsNone(produced[0].stop_loss)
        self.assertEqual(produced[0].side_for(position_is_long=True), "sell")

    def test_a_cross_down_while_flat_produces_nothing(self) -> None:
        # There is nothing to exit, and this strategy does not short.
        produced = self.strategy().generate(
            self.context(self.FALLING, bid="79.99", ask="80.01")
        )
        self.assertEqual(produced, [])

    def test_too_little_history_produces_nothing_not_a_guess(self) -> None:
        strategy = self.strategy()
        self.assertEqual(strategy.required_bars, 4)
        produced = strategy.generate(self.context(self.RISING[:3]))
        self.assertEqual(produced, [])

    def test_a_stale_quote_produces_nothing(self) -> None:
        as_of = T0 + timedelta(seconds=INTERVAL * len(self.RISING))
        stale = MarketContext(
            as_of=as_of,
            equity=EQUITY,
            snapshot=MarketSnapshot(
                as_of=as_of,
                quotes={SYMBOL: quote(as_of=as_of - timedelta(seconds=60))},
            ),
            history={SYMBOL: [candle(i, c) for i, c in enumerate(self.RISING)]},
        )
        self.assertEqual(self.strategy().generate(stale), [])

    def zero_volatility_bars(self, *, flat_tail: bool) -> list[Candle]:
        """Bars that cross up, with a tail that either has range or has none.

        Reaching the zero-ATR guard takes care. A market flat enough for ATR to
        be zero normally has no crossover either -- both moving averages sit on
        the same flat price -- so the earlier no-crossover return fires first and
        the guard is never reached. The slow window has to reach back *past* the
        flat stretch for both conditions to hold at once, which is why this uses
        slow 4 over an ATR window of 2.

        With ``flat_tail`` the last two bars have zero range, so ATR is zero.
        Without it the closes are identical but the bars have range, so ATR is
        not. That is the only difference between the two, which is what makes the
        pair a proof that the guard is what declined rather than something else.
        """
        tail = flat_candle if flat_tail else candle
        return [
            candle(0, 130),
            candle(1, 90),
            candle(2, 100),
            tail(3, 100),
            tail(4, 100),
        ]

    def zero_volatility_context(self, bars: list[Candle]) -> MarketContext:
        as_of = T0 + timedelta(seconds=INTERVAL * len(bars))
        return MarketContext(
            as_of=as_of,
            equity=EQUITY,
            snapshot=MarketSnapshot(
                as_of=as_of,
                quotes={SYMBOL: quote(bid="99.99", ask="100.01", as_of=as_of)},
            ),
            history={SYMBOL: bars},
        )

    def test_zero_volatility_leaves_no_stop_to_place(self) -> None:
        # Zero range means no volatility to place a stop against, so there is no
        # risk-defined position to take. Inventing a stop would invent the
        # position size that follows from it.
        strategy = MovingAverageCrossover(fast=2, slow=4, atr_period=2, symbols=[SYMBOL])

        flat = self.zero_volatility_bars(flat_tail=True)
        self.assertEqual(atr(flat, 2), Decimal("0"))
        self.assertEqual(strategy.generate(self.zero_volatility_context(flat)), [])

        # Same closes, same crossover, but the tail has range -- so the decision
        # goes through. Without this half the test above would pass just as well
        # if the crossover had never been detected.
        with_range = self.zero_volatility_bars(flat_tail=False)
        self.assertEqual(atr(with_range, 2), Decimal("20"))
        produced = strategy.generate(self.zero_volatility_context(with_range))
        self.assertEqual(len(produced), 1)
        self.assertIs(produced[0].direction, SignalDirection.LONG)
        self.assertEqual(produced[0].stop_loss, price("60.00"))

    def test_a_stop_at_or_below_zero_is_declined(self) -> None:
        # Four times an ATR of 32.5 puts the stop below zero. Clamping it to
        # something positive would invent the risk figure the sizer divides by.
        strategy = self.strategy(stop_atr_multiple="4")
        self.assertEqual(strategy.generate(self.context(self.RISING)), [])

    def test_a_move_finer_than_a_price_can_express_is_declined(self) -> None:
        # A stop distance of 3.25e-14 rounds the target back onto the reference
        # price at twelve decimal places. A level equal to the entry is not a
        # level, so there is no risk-defined trade to propose.
        strategy = self.strategy(stop_atr_multiple="1E-15")
        self.assertEqual(strategy.generate(self.context(self.RISING)), [])

    def test_a_wide_spread_warns_without_refusing(self) -> None:
        # Thin books are normal in some markets, so this is information for the
        # operator rather than a block.
        produced = self.strategy().generate(
            self.context(self.RISING, bid="118.00", ask="122.00")
        )
        self.assertEqual(len(produced), 1)
        self.assertEqual(len(produced[0].warnings), 1)
        self.assertIn("spread", produced[0].warnings[0])

    def test_it_scans_the_tradable_symbols_when_none_are_named(self) -> None:
        strategy = MovingAverageCrossover(fast=2, slow=3, atr_period=2)
        produced = strategy.generate(self.context(self.RISING))
        self.assertEqual([s.symbol for s in produced], [SYMBOL])

    def test_it_produces_nothing_for_a_symbol_it_has_no_history_for(self) -> None:
        strategy = MovingAverageCrossover(
            fast=2, slow=3, atr_period=2, symbols=[SYMBOL, OTHER]
        )
        self.assertEqual(
            [s.symbol for s in strategy.generate(self.context(self.RISING))], [SYMBOL]
        )

    def test_its_configuration_is_validated(self) -> None:
        with self.assertRaises(ValueError) as caught:
            MovingAverageCrossover(fast=30, slow=10)
        self.assertIn("shorter", str(caught.exception))
        with self.assertRaises(ValueError):
            MovingAverageCrossover(fast=0)
        with self.assertRaises(TypeError):
            MovingAverageCrossover(fast=True)
        with self.assertRaises(ValueError):
            MovingAverageCrossover(stop_atr_multiple="0")
        with self.assertRaises(ValueError):
            MovingAverageCrossover(reward_multiple="-1")

    def test_it_runs_under_a_runner_and_is_audited(self) -> None:
        clock = ManualClock(T0)
        sink = InMemoryAuditSink()
        audit = AuditLog(sink=sink, clock=clock)
        runner = SignalRunner(
            self.strategy(),
            identity=Principal("strategy-1", Role.STRATEGY),
            audit=audit,
            clock=clock,
        )
        produced = runner.generate(self.context(self.RISING))
        self.assertEqual(len(produced), 1)
        self.assertEqual([r.action for r in sink.records], ["strategy.signalled"])
        self.assertEqual(sink.records[0].details["stop_loss"], "55.00")
        audit.verify()  # raises if the hash chain was broken by the write


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
