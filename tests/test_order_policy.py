"""Protected marketable LIMIT construction with explicit Decimal rounding."""

from __future__ import annotations

import datetime as dt
import unittest

from trading.app import MarketableLimitPolicy, VenueMarketSpecification
from trading.core.clock import UTC
from trading.core.marketdata import Quote
from trading.core.money import Money, Price, Quantity, USD
from trading.core.orders import OrderSide


def quote(*, bid: str = "49", ask: str = "51") -> Quote:
    return Quote(
        symbol="BTCUSD",
        bid=Price(bid, USD),
        ask=Price(ask, USD),
        as_of=dt.datetime(2026, 1, 1, tzinfo=UTC),
        source="test",
    )


class MarketableLimitPolicyTest(unittest.TestCase):
    def specification(
        self, *, minimum: str = "0.05", maximum: str = "1.00"
    ) -> VenueMarketSpecification:
        return VenueMarketSpecification(
            price_decimals=2,
            quantity_step="0.0001",  # type: ignore[arg-type]
            min_notional=Money(minimum, USD),
            max_notional=Money(maximum, USD),
        )

    def test_buy_caps_above_ask_and_rounds_up_to_venue_precision(self) -> None:
        policy = MarketableLimitPolicy(protection_bps=10, price_decimals=2)
        price = policy.limit_price(OrderSide.BUY, quote())
        self.assertEqual(price, Price("51.06", USD))

    def test_sell_floors_below_bid_and_rounds_down_to_venue_precision(self) -> None:
        policy = MarketableLimitPolicy(protection_bps=10, price_decimals=2)
        price = policy.limit_price(OrderSide.SELL, quote())
        self.assertEqual(price, Price("48.95", USD))

    def test_zero_protection_stays_marketable_after_tick_rounding(self) -> None:
        policy = MarketableLimitPolicy(protection_bps=0, price_decimals=2)
        market = quote(bid="49.001", ask="51.001")
        self.assertEqual(
            policy.limit_price(OrderSide.BUY, market), Price("51.01", USD)
        )
        self.assertEqual(
            policy.limit_price(OrderSide.SELL, market), Price("49.00", USD)
        )

    def test_configuration_and_zero_rounding_are_refused(self) -> None:
        for kwargs in (
            {"protection_bps": -1},
            {"protection_bps": 1001},
            {"price_decimals": -1},
            {"price_decimals": 13},
        ):
            with self.subTest(kwargs=kwargs):
                with self.assertRaises(ValueError):
                    MarketableLimitPolicy(**kwargs)
        with self.assertRaises(ValueError):
            MarketableLimitPolicy(
                protection_bps=10, price_decimals=2
            ).limit_price(OrderSide.SELL, quote(bid="0.001", ask="0.002"))

    def test_quantity_is_floored_to_venue_step_without_increasing_exposure(self) -> None:
        policy = MarketableLimitPolicy(
            protection_bps=10, market=self.specification()
        )
        quantity, price = policy.prepare(
            OrderSide.BUY, quote(), Quantity("0.001234", "BTC")
        )
        self.assertEqual(quantity, Quantity("0.0012", "BTC"))
        self.assertEqual(price, Price("51.06", USD))

    def test_minimum_maximum_and_zero_step_results_fail_closed(self) -> None:
        minimum = MarketableLimitPolicy(market=self.specification())
        with self.assertRaisesRegex(ValueError, "below venue minimum"):
            minimum.prepare(
                OrderSide.BUY, quote(), Quantity("0.0001", "BTC")
            )
        maximum = MarketableLimitPolicy(
            market=self.specification(minimum="0.00", maximum="0.06")
        )
        with self.assertRaisesRegex(ValueError, "exceeds venue maximum"):
            maximum.prepare(
                OrderSide.BUY, quote(), Quantity("0.001234", "BTC")
            )
        with self.assertRaisesRegex(ValueError, "rounds to zero"):
            minimum.prepare(
                OrderSide.BUY, quote(), Quantity("0.00001", "BTC")
            )

    def test_market_specification_is_strict_and_currency_consistent(self) -> None:
        with self.assertRaises(ValueError):
            VenueMarketSpecification(
                price_decimals=2,
                quantity_step="0",  # type: ignore[arg-type]
                min_notional=Money("0.05", USD),
                max_notional=Money("1.00", USD),
            )
        with self.assertRaises(ValueError):
            self.specification(minimum="2.00", maximum="1.00")

    def test_expired_market_metadata_refuses_new_order(self) -> None:
        market = VenueMarketSpecification(
            price_decimals=2,
            quantity_step="0.0001",  # type: ignore[arg-type]
            min_notional=Money("0.00", USD),
            max_notional=Money("100.00", USD),
            valid_until=dt.datetime(2025, 12, 31, tzinfo=UTC),
        )
        policy = MarketableLimitPolicy(market=market)

        with self.assertRaisesRegex(ValueError, "metadata is stale"):
            policy.prepare(
                OrderSide.BUY, quote(), Quantity("0.001", "BTC")
            )


if __name__ == "__main__":
    unittest.main()
