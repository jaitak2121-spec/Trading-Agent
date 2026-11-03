"""A read-only picture of what a paper session did.

Nothing here executes, books, or decides anything. A :class:`SessionReport` is
built from state the kernel already owns -- the portfolio, the position ledger,
the operational monitor's status, and the list of submission outcomes the
session collected -- and formatted for a human. Money is rendered as exact
decimal strings, matching the audit log's convention, so a report is safe to
print or log.

The outcome vocabulary is the gateway's own, with one addition:

* ``executed`` / ``refused`` / ``unknown`` come straight from
  :class:`~trading.core.gateway.ExecutionResult.outcome`.
* ``not_sized`` is this layer's label for a signal that never became an order
  because the risk sizer declined it (no stop, or no stateable loss budget). It
  is not a gateway outcome -- the order was never submitted -- and it is kept
  distinct so "the risk engine refused" is never confused with "the strategy had
  nothing to size".
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Mapping, Sequence

from ..adapters.operations import OperationalStatus
from ..core.gateway import ExecutionResult
from ..core.money import Money, Price, Quantity
from ..core.portfolio import Portfolio

__all__ = ["OrderOutcome", "PositionLine", "SessionReport"]


@dataclass(frozen=True)
class OrderOutcome:
    """One signal's journey to the gateway, flattened for reporting."""

    signal_id: str
    symbol: str
    side: str
    quantity: str
    outcome: str  # executed / refused / unknown / not_sized
    gate: str | None = None
    reason: str = ""
    order_id: str | None = None

    @classmethod
    def from_result(
        cls,
        *,
        signal_id: str,
        symbol: str,
        side: str,
        quantity: Quantity,
        result: ExecutionResult,
    ) -> "OrderOutcome":
        order = result.order
        return cls(
            signal_id=signal_id,
            symbol=symbol,
            side=side,
            quantity=str(quantity.amount),
            outcome=result.outcome,
            gate=result.gate,
            reason=result.reason,
            order_id=order.order_id if order is not None else None,
        )

    @classmethod
    def not_sized(
        cls, *, signal_id: str, symbol: str, side: str, reason: str
    ) -> "OrderOutcome":
        return cls(
            signal_id=signal_id,
            symbol=symbol,
            side=side,
            quantity="0",
            outcome="not_sized",
            gate=None,
            reason=reason,
            order_id=None,
        )

    def as_details(self) -> dict[str, object]:
        return {
            "signal_id": self.signal_id,
            "symbol": self.symbol,
            "side": self.side,
            "quantity": self.quantity,
            "outcome": self.outcome,
            "gate": self.gate,
            "reason": self.reason,
            "order_id": self.order_id,
        }


@dataclass(frozen=True)
class PositionLine:
    """One open position at the session's final marks."""

    symbol: str
    quantity: str
    asset: str
    average_entry_price: str | None
    market_value: str | None
    unrealized_pnl: str | None

    def as_details(self) -> dict[str, object]:
        return {
            "symbol": self.symbol,
            "quantity": self.quantity,
            "asset": self.asset,
            "average_entry_price": self.average_entry_price,
            "market_value": self.market_value,
            "unrealized_pnl": self.unrealized_pnl,
        }


@dataclass(frozen=True)
class SessionReport:
    """What one paper session did, as a value. Immutable and audit-safe."""

    currency: str
    starting_cash: str
    cash: str
    equity: str
    realized_pnl: str
    positions: tuple[PositionLine, ...] = ()
    outcomes: tuple[OrderOutcome, ...] = ()
    ticks: int = 0
    executed: int = 0
    refused: int = 0
    unknown: int = 0
    not_sized: int = 0
    ready: bool = False
    blocked_reasons: tuple[str, ...] = ()

    @classmethod
    def build(
        cls,
        *,
        portfolio: Portfolio,
        marks: Mapping[str, Price],
        status: OperationalStatus,
        outcomes: Sequence[OrderOutcome],
        ticks: int,
        starting_cash: Money,
    ) -> "SessionReport":
        open_positions = portfolio.open_positions()

        # Equity needs a mark for every open symbol. Use the portfolio's own
        # method when every leg is marked; otherwise mark the legs we can and
        # leave the rest at cost, rather than raising mid-report.
        if open_positions and all(p.symbol in marks for p in open_positions):
            equity = portfolio.equity(marks)
        else:
            equity = portfolio.cash
            for pos in open_positions:
                mark = marks.get(pos.symbol)
                if mark is not None:
                    equity = equity + pos.market_value(mark)

        lines: list[PositionLine] = []
        for pos in open_positions:
            mark = marks.get(pos.symbol)
            value = pos.market_value(mark) if mark is not None else None
            unreal = pos.unrealized_pnl(mark) if mark is not None else None
            entry = pos.average_entry_price
            lines.append(
                PositionLine(
                    symbol=pos.symbol,
                    quantity=str(pos.quantity.amount),
                    asset=pos.quantity.asset,
                    average_entry_price=None if entry is None else str(entry.amount),
                    market_value=None if value is None else str(value.amount),
                    unrealized_pnl=None if unreal is None else str(unreal.amount),
                )
            )

        return cls(
            currency=portfolio.base_currency.code,
            starting_cash=str(starting_cash.amount),
            cash=str(portfolio.cash.amount),
            equity=str(equity.amount),
            realized_pnl=str(portfolio.realized().amount),
            positions=tuple(lines),
            outcomes=tuple(outcomes),
            ticks=ticks,
            executed=sum(1 for o in outcomes if o.outcome == "executed"),
            refused=sum(1 for o in outcomes if o.outcome == "refused"),
            unknown=sum(1 for o in outcomes if o.outcome == "unknown"),
            not_sized=sum(1 for o in outcomes if o.outcome == "not_sized"),
            ready=status.ready,
            blocked_reasons=tuple(status.blocked_reasons),
        )

    def as_details(self) -> dict[str, object]:
        return {
            "currency": self.currency,
            "starting_cash": self.starting_cash,
            "cash": self.cash,
            "equity": self.equity,
            "realized_pnl": self.realized_pnl,
            "ticks": self.ticks,
            "executed": self.executed,
            "refused": self.refused,
            "unknown": self.unknown,
            "not_sized": self.not_sized,
            "ready": self.ready,
            "blocked_reasons": list(self.blocked_reasons),
            "positions": [p.as_details() for p in self.positions],
            "outcomes": [o.as_details() for o in self.outcomes],
        }

    def render(self) -> str:
        """A compact human-readable block, for the CLI and for operator eyes."""
        cur = self.currency
        lines = [
            "paper trading session report",
            "============================",
            f"ticks processed : {self.ticks}",
            f"starting cash   : {self.starting_cash} {cur}",
            f"cash            : {self.cash} {cur}",
            f"equity (marked) : {self.equity} {cur}",
            f"realized P&L    : {self.realized_pnl} {cur}",
            f"orders          : {self.executed} executed, {self.refused} refused, "
            f"{self.unknown} unknown, {self.not_sized} not sized",
            f"ready to trade  : {self.ready}"
            + (
                ""
                if not self.blocked_reasons
                else f"  (blocked: {', '.join(self.blocked_reasons)})"
            ),
        ]

        if self.positions:
            lines.append("")
            lines.append("open positions")
            lines.append("--------------")
            for pos in self.positions:
                entry = "basis unknown" if pos.average_entry_price is None else pos.average_entry_price
                unreal = "n/a" if pos.unrealized_pnl is None else f"{pos.unrealized_pnl} {cur}"
                lines.append(
                    f"  {pos.symbol}: {pos.quantity} {pos.asset} @ {entry}  "
                    f"unrealized {unreal}"
                )

        if self.outcomes:
            lines.append("")
            lines.append("order outcomes")
            lines.append("--------------")
            for out in self.outcomes:
                detail = out.gate or ""
                if out.reason:
                    detail = f"{detail}: {out.reason}" if detail else out.reason
                suffix = f"  [{detail}]" if detail else ""
                lines.append(
                    f"  {out.outcome:<9} {out.side:<4} {out.quantity:>12} "
                    f"{out.symbol} ({out.signal_id}){suffix}"
                )

        return "\n".join(lines)
