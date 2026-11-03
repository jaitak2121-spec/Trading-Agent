"""End-to-end tests for the ingestion-driven real-time paper service."""

from __future__ import annotations

import datetime as dt
import json
import unittest
from dataclasses import replace
from typing import Sequence

from trading.adapters.marketdata import (
    CandleIngestor,
    CandleNormalizer,
    FeedDisconnected,
    InMemoryCandleStore,
    MarketDataIngestor,
    MarketDataSource,
    QuoteNormalizer,
    RawMessage,
)
from trading.app import (
    MarketableLimitPolicy,
    RealtimePaperService,
    VenueMarketSpecification,
    build_paper_stack,
)
from trading.adapters.reconciliation import (
    FindingKind,
    FindingSeverity,
    ReconciliationFinding,
    VenueSweepReport,
)
from trading.core.clock import UTC, ManualClock
from trading.core.money import Money, Price, Quantity, USD
from trading.core.orders import OrderType
from trading.strategy import MarketContext, Signal, SignalDirection, SignalStrategy

SYMBOL = "BTCUSD"
BASE = dt.datetime(2026, 1, 1, tzinfo=UTC)


class ScriptedSource(MarketDataSource):
    def __init__(self, script: Sequence[object]) -> None:
        self._script = list(script)
        self._index = 0

    def poll(self) -> Sequence[RawMessage]:
        if self._index >= len(self._script):
            return []
        item = self._script[self._index]
        self._index += 1
        if isinstance(item, Exception):
            raise item
        return item  # type: ignore[return-value]


class FixedReconciliation:
    def __init__(self, report: VenueSweepReport) -> None:
        self.report = report

    def sweep(self) -> VenueSweepReport:
        return self.report


class LongOnClosedBar(SignalStrategy):
    name = "integration-strategy"

    def generate(self, context: MarketContext) -> Sequence[Signal]:
        if not context.has_history(SYMBOL, at_least=1):
            return []
        return [
            Signal(
                strategy_name=self.name,
                signal_id=f"long-{context.as_of.isoformat()}",
                symbol=SYMBOL,
                direction=SignalDirection.LONG,
                reference_price=Price("50", USD),
                stop_loss=Price("45", USD),
                take_profit=Price("60", USD),
                as_of=context.as_of,
                rationale="exercise the complete ingestion-to-execution path",
            )
        ]


class StoplessEntry(SignalStrategy):
    name = "stopless"

    def generate(self, context: MarketContext) -> Sequence[Signal]:
        return [
            Signal(
                strategy_name=self.name,
                signal_id="stopless-entry",
                symbol=SYMBOL,
                direction=SignalDirection.LONG,
                reference_price=Price("50", USD),
                as_of=context.as_of,
                rationale="prove that the service preserves the sizer's refusal",
            )
        ]


class ExitWhileFlat(SignalStrategy):
    name = "flat-exit"

    def generate(self, context: MarketContext) -> Sequence[Signal]:
        return [
            Signal(
                strategy_name=self.name,
                signal_id="flat-exit",
                symbol=SYMBOL,
                direction=SignalDirection.EXIT,
                reference_price=Price("50", USD),
                as_of=context.as_of,
                rationale="exercise the safe flat-exit path",
            )
        ]


class EnterThenExit(SignalStrategy):
    name = "round-trip"

    def generate(self, context: MarketContext) -> Sequence[Signal]:
        direction = (
            SignalDirection.LONG if context.is_flat(SYMBOL) else SignalDirection.EXIT
        )
        return [
            Signal(
                strategy_name=self.name,
                signal_id=f"{direction.value}-{context.as_of.isoformat()}",
                symbol=SYMBOL,
                direction=direction,
                reference_price=Price("50", USD),
                stop_loss=(
                    Price("45", USD)
                    if direction is SignalDirection.LONG
                    else None
                ),
                as_of=context.as_of,
                rationale="open once, then close the position actually held",
            )
        ]


def quote(sequence: int = 1) -> dict[str, object]:
    return {"symbol": SYMBOL, "bid": "49", "ask": "51", "sequence": sequence}


def candle(index: int = 0) -> dict[str, object]:
    return {
        "symbol": SYMBOL,
        "open_time": (BASE + dt.timedelta(seconds=60 * index)).isoformat(),
        "open": "50",
        "high": "52",
        "low": "48",
        "close": "50",
        "volume": "10",
    }


def build_service(
    quote_script,
    candle_script,
    *,
    strategy: SignalStrategy | None = None,
    instruments: dict[str, str] | None = None,
    now_seconds: int = 120,
    order_policy: MarketableLimitPolicy | None = None,
):
    # The first bar is closed at this instant, while the quote is fresh.
    clock = ManualClock(BASE + dt.timedelta(seconds=now_seconds))
    stack = build_paper_stack(clock=clock)
    store = InMemoryCandleStore()
    quote_ingestor = MarketDataIngestor(
        source=ScriptedSource(quote_script),
        normalizer=QuoteNormalizer(clock=clock),
        feed=stack.feed,
        clock=clock,
        symbols=[SYMBOL],
    )
    candle_ingestor = CandleIngestor(
        source=ScriptedSource(candle_script),
        normalizer=CandleNormalizer(
            clock=clock, volume_asset="BTC", default_interval_seconds=60
        ),
        store=store,
        clock=clock,
    )
    service = RealtimePaperService(
        stack,
        strategy or LongOnClosedBar(),
        quote_ingestor=quote_ingestor,
        candle_ingestor=candle_ingestor,
        candles=store,
        instruments={SYMBOL: "BTC"} if instruments is None else instruments,
        order_policy=order_policy,
    )
    return service, stack


class RealtimePaperServiceTest(unittest.TestCase):
    def test_protected_limit_policy_creates_marketable_limit_order(self) -> None:
        service, stack = build_service(
            [[quote()]],
            [[candle()]],
            order_policy=MarketableLimitPolicy(
                protection_bps=10, price_decimals=2
            ),
        )

        report = service.poll_once()
        order = stack.orders.all_orders()[0]

        self.assertEqual(report.outcomes[0].outcome, "executed")
        self.assertIs(order.intent.order_type, OrderType.LIMIT)
        self.assertEqual(order.intent.limit_price, Price("51.06", USD))

    def test_expired_venue_metadata_blocks_the_whole_service_cycle(self) -> None:
        market = VenueMarketSpecification(
            price_decimals=2,
            quantity_step="0.0001",  # type: ignore[arg-type]
            min_notional=Money("0.00", USD),
            max_notional=Money("1000.00", USD),
            valid_until=BASE - dt.timedelta(seconds=1),
        )
        service, stack = build_service(
            [[quote()]],
            [[candle()]],
            order_policy=MarketableLimitPolicy(market=market),
        )

        report = service.poll_once()

        self.assertEqual(report.skipped_reason, "venue market metadata is stale")
        self.assertFalse(report.ready)
        self.assertEqual(stack.orders.all_orders(), [])

    def test_venue_quantity_step_reaches_gateway_without_increasing_size(self) -> None:
        service, stack = build_service(
            [[quote()]],
            [[candle()]],
            order_policy=MarketableLimitPolicy(
                protection_bps=10,
                market=VenueMarketSpecification(
                    price_decimals=2,
                    quantity_step="0.7",  # type: ignore[arg-type]
                    min_notional=Money("1.00", USD),
                    max_notional=Money("1000.00", USD),
                ),
            ),
        )

        report = service.poll_once()
        order = stack.orders.all_orders()[0]

        self.assertEqual(report.outcomes[0].outcome, "executed")
        self.assertEqual(order.intent.quantity, Quantity("1.4", "BTC"))
        self.assertEqual(report.outcomes[0].quantity, "1.4")

    def test_clean_cycle_connects_ingestion_strategy_sizing_and_gateway(self) -> None:
        service, stack = build_service([[quote()]], [[candle()]])

        report = service.poll_once()

        self.assertTrue(report.healthy, report.as_details())
        self.assertEqual(len(report.signals), 1)
        self.assertEqual(len(report.outcomes), 1)
        self.assertEqual(report.outcomes[0].outcome, "executed")
        self.assertGreater(stack.portfolio.snapshot()[SYMBOL].amount, 0)
        self.assertEqual(service.cycles, 1)
        self.assertIs(service.last_cycle, report)

    def test_quote_disconnect_fails_closed_before_strategy_execution(self) -> None:
        service, stack = build_service(
            [FeedDisconnected("wire dropped")], [[candle()]]
        )

        report = service.poll_once()

        self.assertEqual(report.skipped_reason, "quote feed is interrupted")
        self.assertFalse(report.healthy)
        self.assertEqual(report.signals, ())
        self.assertEqual(stack.orders.all_orders(), [])

    def test_candle_disconnect_fails_closed_before_strategy_execution(self) -> None:
        service, stack = build_service(
            [[quote()]], [FeedDisconnected("bar stream dropped")]
        )

        report = service.poll_once()

        self.assertEqual(report.skipped_reason, "candle feed is interrupted")
        self.assertEqual(stack.orders.all_orders(), [])

    def test_blocking_venue_reconciliation_prevents_new_exposure(self) -> None:
        service, stack = build_service([[quote()]], [[candle()]])
        blocked = VenueSweepReport(
            at=stack.clock.now().isoformat(),
            findings=(
                ReconciliationFinding(
                    kind=FindingKind.VENUE_ORDER_UNKNOWN_LOCALLY,
                    severity=FindingSeverity.BLOCKING,
                    detail="venue reported an order absent from the local book",
                ),
            ),
        )
        service._stack = replace(  # type: ignore[attr-defined]
            stack, reconciliation_coordinator=FixedReconciliation(blocked)
        )

        report = service.poll_once()

        self.assertEqual(
            report.skipped_reason,
            "venue-wide reconciliation reported a blocking finding",
        )
        self.assertTrue(report.reconciliation.blocked)
        self.assertEqual(stack.orders.all_orders(), [])

    def test_malformed_quote_blocks_even_when_a_valid_quote_also_arrives(self) -> None:
        service, stack = build_service(
            [[quote(), {"symbol": "BAD", "bid": 1.5, "ask": "2"}]],
            [[candle()]],
        )

        report = service.poll_once()

        self.assertIn("quote ingestion reported", report.skipped_reason or "")
        self.assertEqual(stack.orders.all_orders(), [])

    def test_candle_gap_is_applied_but_blocks_a_decision_for_that_cycle(self) -> None:
        service, stack = build_service(
            [[quote(1)], [quote(2)]], [[candle(0)], [candle(3)]]
        )
        service.poll_once()
        existing = len(stack.orders.all_orders())

        report = service.poll_once()

        self.assertIn("bar gap", report.skipped_reason or "")
        self.assertEqual(len(stack.orders.all_orders()), existing)
        self.assertEqual(report.signals, ())

    def test_no_new_closed_bar_means_no_repeated_decision(self) -> None:
        service, stack = build_service([[quote(1)], [quote(2)]], [[candle()], []])
        service.poll_once()
        existing = len(stack.orders.all_orders())

        report = service.poll_once()

        self.assertEqual(report.skipped_reason, "no new closed candle")
        self.assertEqual(len(stack.orders.all_orders()), existing)

    def test_an_in_progress_bar_does_not_retrigger_old_closed_history(self) -> None:
        service, stack = build_service(
            [[quote(1)], [quote(2)]],
            [[candle(0)], [candle(1)]],
            now_seconds=90,
        )
        service.poll_once()
        existing = len(stack.orders.all_orders())

        report = service.poll_once()

        self.assertEqual(report.candles.accepted, (SYMBOL,))
        self.assertEqual(report.skipped_reason, "no new closed candle")
        self.assertEqual(len(stack.orders.all_orders()), existing)

    def test_missing_instrument_mapping_is_reported_without_guessing(self) -> None:
        service, stack = build_service(
            [[quote()]], [[candle()]], instruments={}
        )

        report = service.poll_once()

        self.assertEqual(report.outcomes[0].outcome, "not_sized")
        self.assertIn("no base asset configured", report.outcomes[0].reason)
        self.assertEqual(stack.orders.all_orders(), [])

    def test_sizer_refusal_remains_distinct_from_a_gateway_refusal(self) -> None:
        service, stack = build_service(
            [[quote()]], [[candle()]], strategy=StoplessEntry()
        )

        report = service.poll_once()

        self.assertEqual(report.outcomes[0].outcome, "not_sized")
        self.assertIn("stop", report.outcomes[0].reason)
        self.assertEqual(stack.orders.all_orders(), [])

    def test_exit_while_flat_is_visible_and_submits_nothing(self) -> None:
        service, stack = build_service(
            [[quote()]], [[candle()]], strategy=ExitWhileFlat()
        )

        report = service.poll_once()

        self.assertEqual(report.outcomes[0].outcome, "not_sized")
        self.assertEqual(report.outcomes[0].reason, "exit signal arrived while flat")
        self.assertEqual(stack.orders.all_orders(), [])

    def test_exit_uses_the_held_quantity_and_closes_the_position(self) -> None:
        service, stack = build_service(
            [[quote(1)], [quote(2)]],
            [[candle(0)], [candle(1)]],
            strategy=EnterThenExit(),
        )
        entered = service.poll_once()
        held = stack.portfolio.snapshot()[SYMBOL]

        exited = service.poll_once()

        self.assertEqual(entered.outcomes[0].side, "buy")
        self.assertEqual(exited.outcomes[0].side, "sell")
        self.assertEqual(exited.outcomes[0].quantity, str(held.amount))
        self.assertEqual(exited.outcomes[0].outcome, "executed")
        self.assertTrue(stack.portfolio.snapshot()[SYMBOL].is_zero)

    def test_cycle_details_are_plain_json_and_include_lifecycle_health(self) -> None:
        service, _ = build_service([[quote()]], [[candle()]])

        details = service.poll_once().as_details()

        self.assertTrue(details["lifecycle"]["healthy"])
        json.dumps(details)


if __name__ == "__main__":
    unittest.main()
