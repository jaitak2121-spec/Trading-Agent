"""End-to-end tests for the paper-trading session.

Two layers. A :class:`ScriptedSignals` strategy gives precise control over what a
strategy emits and when, so a test can assert exact behaviour: that a flat round
trip loses the spread, that an unsized signal never becomes an order, that the
session is deterministic. Then the reference ``MovingAverageCrossover`` is driven
over the shipped demo series, proving the real strategy runs the whole chain.

Everything here is deterministic -- a ``ManualClock`` (the composition default)
is advanced one bar at a time, and no test sleeps.
"""

from __future__ import annotations

import unittest
from decimal import Decimal
from typing import Mapping, Sequence

from trading.app import (
    MarketFrame,
    PaperTradingSession,
    SymbolBar,
    build_paper_stack,
)
from trading.core.config import RiskConfig
from trading.core.money import USD, Money, Price, Quantity
from trading.strategy import MovingAverageCrossover, Signal, SignalDirection, SignalStrategy

SYMBOL = "DEMO"
ASSET = "DEMO"


def generous_risk() -> RiskConfig:
    """Limits with headroom, so sizing is driven by the stop, not clamped."""
    return RiskConfig(
        base_currency=USD,
        max_order_notional=Money("50000.00", USD),
        max_position_notional=Money("100000.00", USD),
        max_gross_exposure=Money("200000.00", USD),
        max_daily_loss=Money("2000.00", USD),
        max_orders_per_minute=6,
        max_open_orders=5,
        risk_fraction_per_trade=Decimal("0.001"),
    )


def flat_frames(
    n: int,
    *,
    mid: str = "100",
    half_spread: str = "0.1",
    interval: int = 60,
) -> list[MarketFrame]:
    """``n`` identical bars at ``mid`` with a two-sided spread. ATR is zero here;
    only the scripted strategy (which ignores indicators) is driven over these."""
    m = Decimal(mid)
    hs = Decimal(half_spread)
    bar = SymbolBar(
        open=Price(m, USD),
        high=Price(m, USD),
        low=Price(m, USD),
        close=Price(m, USD),
        volume=Quantity("1", ASSET),
        bid=Price(m - hs, USD),
        ask=Price(m + hs, USD),
    )
    return [MarketFrame(interval_seconds=interval, bars={SYMBOL: bar}) for _ in range(n)]


class ScriptedSignals(SignalStrategy):
    """Emits exactly the signals a test tells it to, on the ticks it names.

    ``plan`` maps a zero-based tick index to a list of ``(direction, stop)``
    pairs; ``stop`` is a decimal string for an entry or ``None`` (required for an
    EXIT, optional for an entry). The reference price is the current mid.
    """

    name = "scripted"

    def __init__(
        self, plan: Mapping[int, Sequence[tuple[SignalDirection, str | None]]]
    ) -> None:
        self._plan = plan
        self._tick = 0

    def generate(self, context) -> list[Signal]:
        instructions = self._plan.get(self._tick, [])
        tick = self._tick
        self._tick += 1
        mid = context.price(SYMBOL)
        if mid is None:
            return []
        signals: list[Signal] = []
        for index, (direction, stop) in enumerate(instructions):
            kwargs: dict[str, Price] = {}
            if direction is not SignalDirection.EXIT and stop is not None:
                kwargs["stop_loss"] = Price(Decimal(stop), USD)
            signals.append(
                Signal(
                    strategy_name=self.name,
                    signal_id=f"{self.name}:{SYMBOL}:{tick}:{direction.value}:{index}",
                    symbol=SYMBOL,
                    direction=direction,
                    reference_price=mid,
                    as_of=context.as_of,
                    rationale="scripted test signal",
                    **kwargs,
                )
            )
        return signals


class RoundTripTest(unittest.TestCase):
    def _session(self, plan):
        stack = build_paper_stack(
            starting_cash=Money("100000.00", USD), risk=generous_risk(), slippage_bps=0
        )
        session = PaperTradingSession(
            stack, ScriptedSignals(plan), instruments={SYMBOL: ASSET}
        )
        return stack, session

    def test_a_flat_round_trip_loses_the_spread(self) -> None:
        # Enter long on tick 1, exit on tick 3, mid flat at 100 throughout. The
        # buy lifts the ask (100.1) and the sell hits the bid (99.9), so even a
        # flat market costs the spread -- realized P&L must be strictly negative.
        plan = {
            1: [(SignalDirection.LONG, "50")],
            3: [(SignalDirection.EXIT, None)],
        }
        stack, session = self._session(plan)
        report = session.run(flat_frames(5))

        self.assertEqual(report.executed, 2, report.render())
        self.assertEqual(report.refused, 0)
        self.assertLess(
            Decimal(report.realized_pnl),
            Decimal(0),
            "a flat round trip must still lose the spread",
        )
        self.assertEqual(stack.portfolio.open_positions(), [], "should end flat")

    def test_a_stopless_entry_is_not_sized_and_submits_nothing(self) -> None:
        # A LONG with no stop has no defined risk, so the sizer declines it. The
        # session must record that and submit nothing -- no order, no fill.
        plan = {1: [(SignalDirection.LONG, None)]}
        stack, session = self._session(plan)
        report = session.run(flat_frames(3))

        self.assertEqual(report.not_sized, 1)
        self.assertEqual(report.executed, 0)
        self.assertEqual(len(stack.orders.all_orders()), 0, "no order may have been created")
        self.assertEqual(stack.portfolio.cash, Money("100000.00", USD), "cash untouched")

    def test_an_entry_that_is_sized_books_a_position(self) -> None:
        plan = {1: [(SignalDirection.LONG, "50")]}
        stack, session = self._session(plan)
        report = session.run(flat_frames(3))

        self.assertEqual(report.executed, 1)
        position = stack.portfolio.position(SYMBOL, asset=ASSET)
        self.assertTrue(position.is_long)
        # The booked quantity is the risk-sized one: budget 100 / stop distance 50.
        self.assertEqual(position.quantity, Quantity("2", ASSET))

    def test_the_session_is_deterministic(self) -> None:
        plan = {
            1: [(SignalDirection.LONG, "50")],
            3: [(SignalDirection.EXIT, None)],
        }
        _, first = self._session(plan)
        _, second = self._session(plan)
        report_a = first.run(flat_frames(5))
        report_b = second.run(flat_frames(5))
        # render() is compared rather than as_details(): order ids are UUIDs
        # (orders.py), so they differ between runs by design. Everything that
        # describes *what happened* -- counts, cash, equity, realized P&L,
        # positions, and the deterministic signal ids -- is identical.
        self.assertEqual(report_a.render(), report_b.render())
        self.assertEqual(
            (report_a.executed, report_a.refused, report_a.unknown, report_a.not_sized),
            (report_b.executed, report_b.refused, report_b.unknown, report_b.not_sized),
        )
        self.assertEqual(report_a.realized_pnl, report_b.realized_pnl)


class RealStrategyTest(unittest.TestCase):
    def test_moving_average_crossover_runs_the_whole_chain(self) -> None:
        # The shipped demo series and strategy, exercised as a test rather than a
        # script. Proves the reference strategy produces at least one executed
        # order through the full gateway chain, with none refused.
        from trading.app.__main__ import _demo_series, _demo_risk, _ASSET, _SYMBOL

        stack = build_paper_stack(
            starting_cash=Money("100000.00", USD), risk=_demo_risk(), slippage_bps=0
        )
        strategy = MovingAverageCrossover(fast=3, slow=5, atr_period=3, symbols=[_SYMBOL])
        session = PaperTradingSession(stack, strategy, instruments={_SYMBOL: _ASSET})
        report = session.run(_demo_series())

        self.assertEqual(report.ticks, 19)
        self.assertGreaterEqual(report.executed, 1)
        self.assertEqual(report.refused, 0, report.render())
        self.assertEqual(report.unknown, 0)


class SessionBranchesTest(unittest.TestCase):
    def test_a_signal_for_an_unmapped_symbol_is_not_sized(self) -> None:
        # No base asset configured for the symbol -> the session cannot size it
        # and must say so, rather than guess an asset from the ticker.
        stack = build_paper_stack(risk=generous_risk())
        session = PaperTradingSession(
            stack,
            ScriptedSignals({1: [(SignalDirection.LONG, "50")]}),
            instruments={},  # DEMO is deliberately absent
        )
        report = session.run(flat_frames(3))
        self.assertEqual(report.not_sized, 1)
        self.assertEqual(report.executed, 0)
        self.assertEqual(len(stack.orders.all_orders()), 0)
        self.assertIn("base asset", report.outcomes[0].reason)

    def test_an_exit_while_flat_is_a_no_op(self) -> None:
        stack = build_paper_stack(risk=generous_risk())
        session = PaperTradingSession(
            stack,
            ScriptedSignals({1: [(SignalDirection.EXIT, None)]}),
            instruments={SYMBOL: ASSET},
        )
        report = session.run(flat_frames(3))
        self.assertEqual(report.executed, 0)
        self.assertEqual(report.not_sized, 0)
        self.assertEqual(len(report.outcomes), 0, "a flat exit is not an order")
        self.assertEqual(len(stack.orders.all_orders()), 0)

    def test_a_session_needs_an_advanceable_clock(self) -> None:
        # The session replays bar by bar and advances the clock itself; a
        # real-time SystemClock has no advance() and must be refused up front.
        from trading.core.clock import SystemClock

        stack = build_paper_stack(clock=SystemClock())
        with self.assertRaises(TypeError):
            PaperTradingSession(
                stack, ScriptedSignals({}), instruments={SYMBOL: ASSET}
            )


class ReportingTest(unittest.TestCase):
    def _held_report(self):
        stack = build_paper_stack(
            starting_cash=Money("100000.00", USD), risk=generous_risk(), slippage_bps=0
        )
        session = PaperTradingSession(
            stack,
            ScriptedSignals({1: [(SignalDirection.LONG, "50")]}),
            instruments={SYMBOL: ASSET},
        )
        return session.run(flat_frames(3))

    def test_render_includes_the_position_and_outcome_sections(self) -> None:
        text = self._held_report().render()
        self.assertIn("paper trading session report", text)
        self.assertIn("open positions", text)
        self.assertIn(SYMBOL, text)
        self.assertIn("order outcomes", text)
        self.assertIn("executed", text)

    def test_render_of_an_empty_session_omits_the_detail_sections(self) -> None:
        stack = build_paper_stack()
        session = PaperTradingSession(
            stack, ScriptedSignals({}), instruments={SYMBOL: ASSET}
        )
        text = session.run([]).render()  # no frames at all
        self.assertIn("ticks processed : 0", text)
        self.assertNotIn("open positions", text)
        self.assertNotIn("order outcomes", text)

    def test_as_details_is_json_safe_and_consistent(self) -> None:
        import json

        details = self._held_report().as_details()
        self.assertEqual(details["executed"], 1)
        self.assertEqual(details["currency"], "USD")
        self.assertEqual(len(details["positions"]), 1)
        self.assertEqual(len(details["outcomes"]), 1)
        # Everything in the report is a string/number/bool/list -- safe to log.
        json.dumps(details)


if __name__ == "__main__":
    unittest.main()
