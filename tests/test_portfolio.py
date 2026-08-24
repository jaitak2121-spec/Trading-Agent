"""Tests for portfolio state: cost basis, realized profit, and equity.

The arithmetic here is simple enough that testing it line by line would prove
little. What is worth proving is the behaviour that decides whether a number can
be trusted by a position sizer or a loss limit:

* A basis we do not know is reported as unknown, not as zero. Zero would report
  an adopted long as pure profit.
* The basis stops being trusted the moment the ledger moves without going through
  the portfolio, because at that point it describes a quantity we no longer hold.
* Rounding never reports more equity or more profit than the exact figure.
* Equity refuses to answer at all when a held symbol has no mark price, rather
  than quietly omitting it from the total.

Expected values are computed by hand in each test rather than by re-deriving them
the way the implementation does, so a failure means the answer changed.
"""

from __future__ import annotations

import unittest
from decimal import Decimal

from tests.harness import ASSET, DEFAULT_PRICE, DEFAULT_QUANTITY, SYMBOL, build_rig
from trading.core.errors import CurrencyMismatch, SafetyViolation
from trading.core.money import USD, USDT, Money, Price, Quantity
from trading.core.orders import OrderSide
from trading.core.portfolio import FillEffect, Portfolio, Position
from trading.core.reconciliation import PositionLedger
from trading.ports.broker import AckOutcome, BrokerAck, BrokerPositionSnapshot

BUY, SELL = OrderSide.BUY, OrderSide.SELL
OTHER = "ETHUSD"


def q(amount: object, asset: str = ASSET) -> Quantity:
    return Quantity(amount, asset)


def p(amount: object) -> Price:
    return Price(amount, USD)


def usd(amount: object) -> Money:
    return Money(amount, USD)


def portfolio(cash: str = "10000.00", **kwargs: object) -> Portfolio:
    return Portfolio(usd(cash), **kwargs)  # type: ignore[arg-type]


class TestCostBasisAccounting(unittest.TestCase):
    """The three things a fill can do: open or add, reduce, or flip."""

    def setUp(self) -> None:
        self.pf = portfolio()

    def test_opening_sets_the_basis_to_the_fill_price(self) -> None:
        effect = self.pf.apply_fill(SYMBOL, BUY, q("2"), p("100"))
        self.assertEqual(effect.position.average_entry_price, p("100"))
        self.assertEqual(effect.cash_delta, usd("-200.00"))
        self.assertEqual(self.pf.cash, usd("9800.00"))

    def test_adding_averages_by_volume_not_by_fill_count(self) -> None:
        # 1 @ 100 then 3 @ 200 is 175, not the 150 an unweighted mean would give.
        self.pf.apply_fill(SYMBOL, BUY, q("1"), p("100"))
        effect = self.pf.apply_fill(SYMBOL, BUY, q("3"), p("200"))
        self.assertEqual(effect.position.average_entry_price, p("175"))
        self.assertEqual(effect.realized_pnl, usd("0.00"))

    def test_reducing_realizes_the_closed_part_and_leaves_the_basis_alone(self) -> None:
        # A partial exit does not change what the remaining units cost.
        self.pf.apply_fill(SYMBOL, BUY, q("4"), p("150"))
        effect = self.pf.apply_fill(SYMBOL, SELL, q("1"), p("250"))
        self.assertEqual(effect.realized_pnl, usd("100.00"))
        self.assertEqual(effect.position.average_entry_price, p("150"))
        self.assertEqual(effect.position.quantity, q("3"))

    def test_closing_to_flat_leaves_no_basis(self) -> None:
        self.pf.apply_fill(SYMBOL, BUY, q("2"), p("100"))
        effect = self.pf.apply_fill(SYMBOL, SELL, q("2"), p("120"))
        self.assertEqual(effect.realized_pnl, usd("40.00"))
        self.assertTrue(effect.position.is_flat)
        self.assertIsNone(effect.position.average_entry_price)
        self.assertTrue(effect.position.basis_is_known)

    def test_a_flip_realizes_the_whole_old_side_and_reopens_at_the_fill_price(self) -> None:
        # Long 3 @ 150, then sell 5 @ 300: three units close for 450, and the two
        # remaining units are a new short whose basis is 300, not 150.
        self.pf.apply_fill(SYMBOL, BUY, q("3"), p("150"))
        effect = self.pf.apply_fill(SYMBOL, SELL, q("5"), p("300"))
        self.assertEqual(effect.realized_pnl, usd("450.00"))
        self.assertEqual(effect.position.quantity, q("-2"))
        self.assertEqual(effect.position.average_entry_price, p("300"))

    def test_a_short_profits_when_price_falls(self) -> None:
        self.pf.apply_fill(SYMBOL, SELL, q("2"), p("300"))
        effect = self.pf.apply_fill(SYMBOL, BUY, q("2"), p("250"))
        self.assertEqual(effect.realized_pnl, usd("100.00"))

    def test_a_long_that_falls_realizes_a_loss(self) -> None:
        self.pf.apply_fill(SYMBOL, BUY, q("2"), p("300"))
        effect = self.pf.apply_fill(SYMBOL, SELL, q("2"), p("250"))
        self.assertEqual(effect.realized_pnl, usd("-100.00"))
        self.assertTrue(effect.realized_pnl.is_negative)

    def test_realized_profit_accumulates_across_symbols_and_closed_positions(self) -> None:
        self.pf.apply_fill(SYMBOL, BUY, q("1"), p("100"))
        self.pf.apply_fill(SYMBOL, SELL, q("1"), p("150"))
        self.pf.apply_fill(OTHER, BUY, q("1", "ETH"), p("100"))
        self.pf.apply_fill(OTHER, SELL, q("1", "ETH"), p("80"))
        # The BTCUSD position is flat and gone from view, but its profit is not.
        self.assertEqual(self.pf.realized(), usd("30.00"))
        self.assertEqual(self.pf.position(SYMBOL).realized_pnl, usd("50.00"))
        self.assertEqual(self.pf.position(OTHER).realized_pnl, usd("-20.00"))
        self.assertEqual(self.pf.open_positions(), [])

    def test_positions_are_reported_independently_per_symbol(self) -> None:
        self.pf.apply_fill(SYMBOL, BUY, q("1"), p("100"))
        self.pf.apply_fill(OTHER, BUY, q("2", "ETH"), p("50"))
        # Sorted, not insertion-ordered: a report whose row order depends on
        # which fill arrived first is not diffable between two runs.
        self.assertEqual(
            [position.symbol for position in self.pf.open_positions()], [SYMBOL, OTHER]
        )
        self.assertEqual(self.pf.position(SYMBOL).average_entry_price, p("100"))
        self.assertEqual(self.pf.position(OTHER).average_entry_price, p("50"))

    def test_an_untouched_symbol_reads_flat_rather_than_raising(self) -> None:
        # A strategy asking about a symbol it has never traded is ordinary.
        position = self.pf.position("DOGEUSD", asset="DOGE")
        self.assertTrue(position.is_flat)
        self.assertEqual(position.realized_pnl, usd("0.00"))
        self.assertTrue(position.basis_is_known)


class TestTheLedgerRemainsTheSingleQuantityAuthority(unittest.TestCase):
    """The portfolio adds a basis layer; it does not keep a second position book.

    Two independently-maintained quantity views is the failure INVARIANT 6 exists
    to catch at the venue boundary, and there is no reason to create one locally.
    """

    def test_a_fill_moves_the_ledger_it_was_given(self) -> None:
        ledger = PositionLedger()
        pf = portfolio(ledger=ledger)
        pf.apply_fill(SYMBOL, BUY, q("2"), p("100"))
        self.assertEqual(ledger.position(SYMBOL, asset=ASSET), q("2"))
        self.assertEqual(pf.position(SYMBOL).quantity, ledger.position(SYMBOL))
        self.assertEqual(pf.snapshot(), ledger.snapshot())

    def test_a_portfolio_without_a_ledger_makes_its_own(self) -> None:
        pf = portfolio()
        pf.apply_fill(SYMBOL, BUY, q("2"), p("100"))
        self.assertEqual(pf.ledger.position(SYMBOL, asset=ASSET), q("2"))


class TestAnUnknownBasisIsNotZero(unittest.TestCase):
    """A position we did not see open has no basis, and must say so.

    Reporting zero would price an adopted long as pure profit, which is the most
    expensive possible lie for a loss limit to be told.
    """

    def setUp(self) -> None:
        # A ledger that already holds something: exactly what adopting a venue
        # snapshot leaves behind.
        self.ledger = PositionLedger()
        self.ledger.set_position(SYMBOL, q("5"))
        self.pf = portfolio(ledger=self.ledger)

    def test_a_preexisting_position_has_no_basis(self) -> None:
        position = self.pf.position(SYMBOL)
        self.assertEqual(position.quantity, q("5"))
        self.assertIsNone(position.average_entry_price)
        self.assertFalse(position.basis_is_known)

    def test_unrealized_profit_is_unknown_rather_than_zero(self) -> None:
        self.assertIsNone(self.pf.position(SYMBOL).unrealized_pnl(p("200")))
        self.assertIsNone(self.pf.position(SYMBOL).cost_basis())

    def test_adding_to_an_unknown_basis_stays_unknown(self) -> None:
        # Averaging a known price against an unknown one produces a number with
        # no meaning; an unknown basis is the honest answer.
        effect = self.pf.apply_fill(SYMBOL, BUY, q("5"), p("100"))
        self.assertIsNone(effect.position.average_entry_price)
        self.assertFalse(effect.basis_was_known)
        self.assertEqual(effect.position.quantity, q("10"))

    def test_reducing_an_unknown_basis_realizes_nothing_and_says_so(self) -> None:
        effect = self.pf.apply_fill(SYMBOL, SELL, q("2"), p("100"))
        self.assertEqual(effect.realized_pnl, usd("0.00"))
        self.assertFalse(effect.basis_was_known)
        self.assertEqual(self.pf.realized(), usd("0.00"))
        self.assertIsNone(self.pf.position(SYMBOL).average_entry_price)

    def test_flipping_through_zero_makes_the_basis_known_again(self) -> None:
        # Whatever the old five units cost, the new short side was opened here.
        effect = self.pf.apply_fill(SYMBOL, SELL, q("8"), p("100"))
        self.assertEqual(effect.position.quantity, q("-3"))
        self.assertEqual(effect.position.average_entry_price, p("100"))
        self.assertFalse(effect.basis_was_known)
        self.assertTrue(self.pf.position(SYMBOL).basis_is_known)

    def test_closing_an_unknown_basis_to_flat_realizes_nothing(self) -> None:
        effect = self.pf.apply_fill(SYMBOL, SELL, q("5"), p("100"))
        self.assertTrue(effect.position.is_flat)
        self.assertEqual(effect.realized_pnl, usd("0.00"))
        self.assertFalse(effect.basis_was_known)

    def test_equity_survives_an_unknown_basis_even_though_attribution_does_not(self) -> None:
        # Equity needs a mark, not a basis. So an adopted position costs us P&L
        # attribution without costing us the number the sizer actually reads.
        self.assertEqual(self.pf.equity({SYMBOL: p("200")}), usd("11000.00"))

    def test_a_flat_position_cannot_carry_an_entry_price(self) -> None:
        with self.assertRaises(ValueError) as caught:
            Position(SYMBOL, q("0"), p("100"), usd("0.00"))
        self.assertIn("flat position", str(caught.exception))


class TestTheBasisSelfInvalidatesWhenTheLedgerMoves(unittest.TestCase):
    """A write that bypasses the portfolio must not leave a stale basis behind.

    ``set_position``, ``adopt_broker_positions``, and any future persistence
    adapter all write the ledger directly. Requiring each of them to notify the
    portfolio would work until the next call site forgot; keying the basis to the
    quantity it describes cannot be forgotten.
    """

    def setUp(self) -> None:
        self.ledger = PositionLedger()
        self.pf = portfolio(ledger=self.ledger)
        self.pf.apply_fill(SYMBOL, BUY, q("2"), p("100"))

    def test_a_direct_ledger_write_invalidates_the_basis(self) -> None:
        self.assertEqual(self.pf.position(SYMBOL).average_entry_price, p("100"))
        self.ledger.set_position(SYMBOL, q("7"))
        position = self.pf.position(SYMBOL)
        self.assertIsNone(position.average_entry_price)
        self.assertFalse(position.basis_is_known)
        self.assertIsNone(position.unrealized_pnl(p("100")))

    def test_a_write_that_restores_the_same_quantity_keeps_the_basis(self) -> None:
        # The differential half: the check keys on "does this basis describe what
        # we now hold", not on "did anyone touch the ledger". Otherwise a
        # reconciliation that confirmed our position would destroy its basis.
        self.ledger.set_position(SYMBOL, q("2"))
        self.assertEqual(self.pf.position(SYMBOL).average_entry_price, p("100"))

    def test_adopting_broker_positions_invalidates_the_basis(self) -> None:
        # The real path, through the reconciliation gate rather than the ledger.
        rig = build_rig()
        rig.submit()
        held = rig.portfolio.position(SYMBOL, asset=ASSET).quantity
        self.assertEqual(rig.portfolio.position(SYMBOL).average_entry_price, DEFAULT_PRICE)

        venue = {SYMBOL: held + DEFAULT_QUANTITY}
        rig.reconciliation.reconcile(venue)
        rig.reconciliation.adopt_broker_positions(
            rig.operator_id, reason="venue is authoritative", broker_positions=venue
        )
        position = rig.portfolio.position(SYMBOL, asset=ASSET)
        self.assertEqual(position.quantity, venue[SYMBOL])
        self.assertFalse(position.basis_is_known)
        self.assertIsNone(position.unrealized_pnl(DEFAULT_PRICE))

    def test_a_fill_after_invalidation_reopens_a_known_basis_from_flat(self) -> None:
        self.ledger.set_position(SYMBOL, q("0"))
        effect = self.pf.apply_fill(SYMBOL, BUY, q("1"), p("500"))
        self.assertEqual(effect.position.average_entry_price, p("500"))
        self.assertTrue(effect.basis_was_known)


class TestRoundingNeverFlattersTheAccount(unittest.TestCase):
    """Every rounding choice reports less equity and less profit, never more.

    These figures feed a loss limit and a position sizer. Being a cent
    pessimistic costs nothing; being a cent optimistic is a limit that lets one
    more order through than it should.
    """

    def test_a_purchase_rounds_its_cash_outflow_up(self) -> None:
        # 3 @ 0.005 = 0.015, which is not expressible in cents.
        pf = portfolio("100.00")
        effect = pf.apply_fill(SYMBOL, BUY, q("3"), p("0.005"))
        self.assertEqual(effect.cash_delta, usd("-0.02"))
        self.assertEqual(pf.cash, usd("99.98"))

    def test_a_sale_rounds_its_cash_inflow_down(self) -> None:
        pf = portfolio("100.00")
        effect = pf.apply_fill(SYMBOL, SELL, q("3"), p("0.005"))
        self.assertEqual(effect.cash_delta, usd("0.01"))
        self.assertEqual(pf.cash, usd("100.01"))

    def test_the_weighted_average_entry_rounds_up(self) -> None:
        # (100 + 101 + 101) / 3 = 100.666..., which as a basis is pessimistic
        # rounded up: it understates a long's profit.
        pf = portfolio()
        pf.apply_fill(SYMBOL, BUY, q("1"), p("100"))
        effect = pf.apply_fill(SYMBOL, BUY, q("2"), p("101"))
        entry = effect.position.average_entry_price
        assert entry is not None
        self.assertEqual(entry.amount, Decimal("100.666666666667"))

    def test_a_gain_rounds_toward_zero_and_a_loss_away_from_it(self) -> None:
        # The same fractional half-cent, once as profit and once as loss. Both
        # round to the worse outcome for us, which ROUND_DOWN alone would not do
        # because it is toward zero rather than toward negative.
        gain = portfolio()
        gain.apply_fill(SYMBOL, BUY, q("1"), p("1.000"))
        self.assertEqual(
            gain.apply_fill(SYMBOL, SELL, q("1"), p("1.005")).realized_pnl, usd("0.00")
        )
        loss = portfolio()
        loss.apply_fill(SYMBOL, BUY, q("1"), p("1.005"))
        self.assertEqual(
            loss.apply_fill(SYMBOL, SELL, q("1"), p("1.000")).realized_pnl, usd("-0.01")
        )

    def test_unrealized_profit_rounds_the_same_way(self) -> None:
        pf = portfolio()
        pf.apply_fill(SYMBOL, BUY, q("1"), p("1.000"))
        position = pf.position(SYMBOL)
        self.assertEqual(position.unrealized_pnl(p("1.005")), usd("0.00"))
        self.assertEqual(position.unrealized_pnl(p("0.995")), usd("-0.01"))

    def test_a_long_market_value_rounds_down_and_a_short_rounds_up(self) -> None:
        long_position = Position(SYMBOL, q("3"), p("1"), usd("0.00"))
        short_position = Position(SYMBOL, q("-3"), p("1"), usd("0.00"))
        # 3 * 0.005 = 0.015 either way; the sign decides which direction is
        # pessimistic, because a short's value is subtracted from equity.
        self.assertEqual(long_position.market_value(p("0.005")), usd("0.01"))
        self.assertEqual(short_position.market_value(p("0.005")), usd("-0.02"))


class TestEquityRefusesToGuess(unittest.TestCase):
    """A partial total must never be mistaken for equity.

    The same reasoning as ``RiskLimit.MARK_PRICE_AVAILABLE``: skipping an
    unpriceable position would hand a position sizer a number that looks like
    equity and is not.
    """

    def setUp(self) -> None:
        self.pf = portfolio()

    def test_an_empty_portfolio_is_worth_its_cash(self) -> None:
        self.assertEqual(self.pf.equity({}), usd("10000.00"))

    def test_equity_is_cash_plus_marked_value(self) -> None:
        self.pf.apply_fill(SYMBOL, BUY, q("2"), p("100"))  # cash 9800
        self.assertEqual(self.pf.equity({SYMBOL: p("150")}), usd("10100.00"))

    def test_a_short_reduces_equity_as_price_rises(self) -> None:
        self.pf.apply_fill(SYMBOL, SELL, q("2"), p("100"))  # cash 10200
        self.assertEqual(self.pf.equity({SYMBOL: p("150")}), usd("9900.00"))

    def test_a_missing_mark_for_a_held_symbol_refuses(self) -> None:
        self.pf.apply_fill(SYMBOL, BUY, q("2"), p("100"))
        with self.assertRaises(SafetyViolation) as caught:
            self.pf.equity({OTHER: p("100")})
        self.assertIn("no mark price for BTCUSD", str(caught.exception))

    def test_a_closed_position_does_not_need_a_mark(self) -> None:
        # A flat symbol stays in the ledger. Requiring a mark for it would mean a
        # closed position could block equity forever.
        self.pf.apply_fill(SYMBOL, BUY, q("2"), p("100"))
        self.pf.apply_fill(SYMBOL, SELL, q("2"), p("100"))
        self.assertEqual(self.pf.equity({}), usd("10000.00"))

    def test_a_mark_in_the_wrong_currency_refuses(self) -> None:
        self.pf.apply_fill(SYMBOL, BUY, q("2"), p("100"))
        with self.assertRaises(CurrencyMismatch):
            self.pf.equity({SYMBOL: Price("150", USDT)})

    def test_mark_prices_must_be_a_mapping(self) -> None:
        with self.assertRaises(TypeError):
            self.pf.equity([DEFAULT_PRICE])  # type: ignore[arg-type]


class TestFillInputIsValidated(unittest.TestCase):
    """Bad input raises rather than corrupting the books silently."""

    def setUp(self) -> None:
        self.pf = portfolio()

    def test_a_non_positive_fill_is_refused(self) -> None:
        for bad in ("0", "-1"):
            with self.subTest(quantity=bad), self.assertRaises(ValueError):
                self.pf.apply_fill(SYMBOL, BUY, q(bad), p("100"))

    def test_wrong_types_are_refused(self) -> None:
        with self.assertRaises(TypeError):
            self.pf.apply_fill(SYMBOL, "buy", q("1"), p("100"))  # type: ignore[arg-type]
        with self.assertRaises(TypeError):
            self.pf.apply_fill(SYMBOL, BUY, "1", p("100"))  # type: ignore[arg-type]
        with self.assertRaises(TypeError):
            self.pf.apply_fill(SYMBOL, BUY, q("1"), "100")  # type: ignore[arg-type]

    def test_a_fill_priced_in_another_currency_is_refused(self) -> None:
        with self.assertRaises(CurrencyMismatch):
            self.pf.apply_fill(SYMBOL, BUY, q("1"), Price("100", USDT))

    def test_a_fill_in_another_asset_is_refused(self) -> None:
        # BTCUSD holding BTC cannot absorb an ETH fill. Adding them would produce
        # a position denominated in nothing.
        self.pf.apply_fill(SYMBOL, BUY, q("1"), p("100"))
        with self.assertRaises(SafetyViolation) as caught:
            self.pf.apply_fill(SYMBOL, BUY, q("1", "ETH"), p("100"))
        self.assertIn("two different assets", str(caught.exception))

    def test_construction_is_validated(self) -> None:
        with self.assertRaises(TypeError):
            Portfolio("10000.00")  # type: ignore[arg-type]
        with self.assertRaises(TypeError):
            Portfolio(usd("1.00"), ledger=object())  # type: ignore[arg-type]
        with self.assertRaises(TypeError):
            Position(SYMBOL, "1", None, usd("0.00"))  # type: ignore[arg-type]
        with self.assertRaises(TypeError):
            Position(SYMBOL, q("1"), None, "0.00")  # type: ignore[arg-type]
        with self.assertRaises(TypeError):
            Position(SYMBOL, q("1"), "100", usd("0.00"))  # type: ignore[arg-type]

    def test_a_position_cannot_mix_its_price_and_pnl_currencies(self) -> None:
        with self.assertRaises(CurrencyMismatch):
            Position(SYMBOL, q("1"), Price("100", USDT), usd("0.00"))

    def test_a_mark_in_the_wrong_currency_is_refused_on_a_position(self) -> None:
        position = Position(SYMBOL, q("1"), p("100"), usd("0.00"))
        for method in (position.market_value, position.unrealized_pnl):
            with self.subTest(method=method.__name__):
                with self.assertRaises(CurrencyMismatch):
                    method(Price("100", USDT))
                with self.assertRaises(TypeError):
                    method("100")  # type: ignore[arg-type]


class TestThePortfolioCannotExecute(unittest.TestCase):
    """INVARIANT 3: portfolio state is a reading, not a control surface."""

    def test_it_holds_nothing_that_could_place_an_order(self) -> None:
        pf = portfolio()
        for attribute in (
            "place_order",
            "submit",
            "submit_order",
            "execute",
            "broker",
            "gateway",
            "token",
        ):
            with self.subTest(attribute=attribute):
                self.assertFalse(hasattr(pf, attribute))


class TestReportingSurface(unittest.TestCase):
    """What goes into an audit record or an advisory report."""

    def test_position_details_distinguish_unknown_from_zero(self) -> None:
        known = Position(SYMBOL, q("2"), p("100"), usd("5.00")).as_details()
        self.assertEqual(known["average_entry_price"], "100")
        self.assertIs(known["basis_known"], True)
        self.assertEqual(known["asset"], ASSET)

        unknown = Position(SYMBOL, q("2"), None, usd("5.00")).as_details()
        self.assertIsNone(unknown["average_entry_price"])
        self.assertIs(unknown["basis_known"], False)

    def test_fill_details_are_all_strings_or_flags(self) -> None:
        pf = portfolio()
        effect = pf.apply_fill(SYMBOL, BUY, q("2"), p("100"))
        details = effect.as_details()
        self.assertEqual(details["side"], "buy")
        self.assertEqual(details["price"], "100")
        self.assertEqual(details["cash_delta"], "-200.00")
        self.assertIsInstance(details["position"], dict)

    def test_a_position_describes_its_own_direction(self) -> None:
        long_position = Position(SYMBOL, q("1"), p("100"), usd("0.00"))
        short_position = Position(SYMBOL, q("-1"), p("100"), usd("0.00"))
        flat = Position(SYMBOL, q("0"), None, usd("0.00"))
        self.assertEqual(
            [long_position.is_long, long_position.is_short, long_position.is_flat],
            [True, False, False],
        )
        self.assertEqual(
            [short_position.is_long, short_position.is_short, short_position.is_flat],
            [False, True, False],
        )
        self.assertEqual([flat.is_long, flat.is_short, flat.is_flat], [False, False, True])
        self.assertEqual(flat.unrealized_pnl(p("100")), usd("0.00"))
        self.assertEqual(flat.cost_basis(), usd("0.00"))
        self.assertEqual(flat.currency, USD)

    def test_a_position_and_a_portfolio_are_readable(self) -> None:
        pf = portfolio()
        pf.apply_fill(SYMBOL, BUY, q("2"), p("100"))
        self.assertIn("BTCUSD", str(pf.position(SYMBOL)))
        self.assertIn("basis unknown", str(Position(SYMBOL, q("1"), None, usd("0.00"))))
        self.assertIn("Portfolio(", repr(pf))
        self.assertEqual(pf.base_currency, USD)
        self.assertEqual(pf.symbols(), [SYMBOL])

    def test_cost_basis_is_what_the_open_quantity_was_paid_for(self) -> None:
        pf = portfolio()
        pf.apply_fill(SYMBOL, BUY, q("2"), p("100"))
        pf.apply_fill(SYMBOL, SELL, q("1"), p("500"))
        self.assertEqual(pf.position(SYMBOL).cost_basis(), usd("100.00"))


class TestFillsReachTheBasisThroughTheGateway(unittest.TestCase):
    """The wiring, end to end.

    The gateway already had the fill price in hand and was discarding it. These
    tests are what stops it going back to being discarded: a cost basis that is
    only ever set in tests is not a cost basis.
    """

    def test_an_executed_order_records_its_basis_and_cash(self) -> None:
        rig = build_rig()
        result = rig.submit()
        self.assertTrue(result.is_executed)
        position = rig.portfolio.position(SYMBOL, asset=ASSET)
        self.assertEqual(position.quantity, DEFAULT_QUANTITY)
        self.assertEqual(position.average_entry_price, DEFAULT_PRICE)
        # 0.001 BTC at 50000 is 50 USD, out of the rig's starting cash.
        self.assertEqual(rig.portfolio.cash, Money("999950.00", USD))

    def test_the_ledger_and_the_portfolio_do_not_diverge(self) -> None:
        rig = build_rig()
        rig.submit()
        rig.submit()
        self.assertEqual(
            rig.positions.position(SYMBOL, asset=ASSET),
            rig.portfolio.position(SYMBOL, asset=ASSET).quantity,
        )
        self.assertEqual(rig.reconciliation.reconcile(rig.portfolio.snapshot()).is_clean, True)

    def test_a_round_trip_through_the_gateway_realizes_profit(self) -> None:
        rig = build_rig()
        rig.submit()
        rig.broker.set_fill_price(SYMBOL, Price("60000", USD))
        rig.submit(side=OrderSide.SELL)
        # 0.001 BTC bought at 50000 and sold at 60000 is 10 USD.
        self.assertEqual(rig.portfolio.realized(), Money("10.00", USD))
        self.assertTrue(rig.portfolio.position(SYMBOL, asset=ASSET).is_flat)

    def test_a_fill_discovered_during_recovery_records_its_basis(self) -> None:
        # The second fill site in the gateway: a timed-out order that the venue
        # had actually filled. It has to record a basis too, or a position
        # recovered from an outage would be unpriced.
        rig = build_rig()
        rig.broker.script(
            BrokerAck(AckOutcome.UNCERTAIN, message="timeout"), lands_at_venue=True
        )
        order = rig.submit().order
        filled = Quantity("0.001", ASSET)
        ack = BrokerAck(
            AckOutcome.FILLED,
            broker_order_id="venue-filled-1",
            filled_quantity=filled,
            fill_price=Price("51000", USD),
        )

        class VenueFilledIt:
            def fetch_order_state(self, _order):
                return ack

            def fetch_positions(self):
                return BrokerPositionSnapshot({SYMBOL: filled})

        rig.gateway._broker = VenueFilledIt()
        rig.gateway.resolve_unknown(order, operator=rig.operator_id)
        position = rig.portfolio.position(SYMBOL, asset=ASSET)
        self.assertEqual(position.quantity, filled)
        self.assertEqual(position.average_entry_price, Price("51000", USD))
        self.assertTrue(position.basis_is_known)

    def test_wiring_a_gateway_to_a_bare_ledger_is_refused(self) -> None:
        # A PositionLedger would accept the quantity and silently drop the fill
        # price, so every position we filled ourselves would report no basis.
        # That has to fail at wiring time, not at the first fill.
        from trading.core.gateway import ExecutionGateway

        rig = build_rig()
        with self.assertRaises(TypeError) as caught:
            ExecutionGateway(
                identity=rig.gateway_id,
                broker=rig.broker,
                orders=rig.orders,
                positions=PositionLedger(),
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
        self.assertIn("Portfolio", str(caught.exception))


class TestFillEffectIsAValue(unittest.TestCase):
    def test_it_is_frozen(self) -> None:
        pf = portfolio()
        effect = pf.apply_fill(SYMBOL, BUY, q("1"), p("100"))
        self.assertIsInstance(effect, FillEffect)
        with self.assertRaises(Exception):
            effect.realized_pnl = usd("999.00")  # type: ignore[misc]

    def test_a_position_reading_does_not_change_when_the_portfolio_does(self) -> None:
        pf = portfolio()
        before = pf.position(SYMBOL, asset=ASSET)
        pf.apply_fill(SYMBOL, BUY, q("1"), p("100"))
        self.assertTrue(before.is_flat)
        self.assertFalse(pf.position(SYMBOL).is_flat)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
