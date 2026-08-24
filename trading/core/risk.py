"""Risk limits.

Covers:

* INVARIANT 4 -- risk checks must happen before execution. Enforced by making
  a :class:`RiskApproval` the only thing that unlocks the gateway's execution
  path, and by making :class:`RiskEngine` the only thing that can produce one.
* INVARIANT 7 -- loss and exposure limits cannot be bypassed.

Design notes:

**Approvals are capabilities, not booleans.** A returned ``True`` invites the
caller to ignore it. A single-use :class:`RiskApproval`, bound to one
idempotency key with a short TTL, does not: there is no way to execute without
holding one, and no way to hold one without having passed the checks.

**Every check runs, every time.** :meth:`RiskEngine.approve` evaluates the full
set and reports all violations rather than short-circuiting, so an operator sees
the whole picture rather than fixing one limit and rediscovering the next.

**A missing mark price is a violation, not a skipped check.** Exposure cannot be
computed without prices, and "I could not check" must never read as "the check
passed".

**Reducing orders are not blocked by exposure limits.** Projected exposure is
computed with signs, so an order that closes a position is judged on the
exposure it leaves behind. An order that *strictly shrinks* a position without
flipping its sign is treated as de-risking: the position, gross-exposure, and
daily-loss limits are waived for it and the waiver is audited. A position can
sit above a ceiling because the ceiling was tightened or because it was adopted
from the broker during reconciliation, and refusing every exit would trap the
operator in exactly the position the limit exists to prevent. The per-order,
rate, and open-order limits still apply -- those are transient, so being
throttled for a minute is not a trap.
"""

from __future__ import annotations

import datetime as dt
import decimal
import threading
import uuid
from collections import deque
from dataclasses import dataclass
from decimal import Decimal
from enum import Enum
from typing import Final, Mapping, Sequence

from .audit import AuditCategory, AuditLog, AuditOutcome
from .authz import Action, Principal, authorize, is_authorized
from .clock import Clock
from .config import RiskConfig
from .errors import RiskLimitExceeded, SafetyViolation, UnauthorizedAction
from .money import FINANCIAL_CONTEXT, Money, Price, Quantity
from .orders import OrderIntent, OrderSide, OrderStore

__all__ = [
    "RiskLimit",
    "LimitBreach",
    "RiskApproval",
    "PnlLedger",
    "RiskEngine",
]

#: Gate for :class:`RiskApproval` construction; see :class:`RiskEngine.approve`.
_APPROVAL_MINT_KEY: Final = object()

#: Window for the order-rate limit.
_RATE_WINDOW_SECONDS: Final = 60.0


class RiskLimit(Enum):
    """Every limit that can refuse an order. Used as the audit key."""

    ORDER_NOTIONAL = "max_order_notional"
    POSITION_NOTIONAL = "max_position_notional"
    GROSS_EXPOSURE = "max_gross_exposure"
    DAILY_LOSS = "max_daily_loss"
    ORDER_RATE = "max_orders_per_minute"
    OPEN_ORDERS = "max_open_orders"
    MARK_PRICE_AVAILABLE = "mark_price_available"


#: The checks :class:`RiskEngine` runs, in evaluation order. An approval that
#: does not name all of these is not a valid approval -- see
#: :meth:`RiskApproval.covers_all_limits`.
REQUIRED_CHECKS: Final[tuple[RiskLimit, ...]] = tuple(RiskLimit)


@dataclass(frozen=True, slots=True)
class LimitBreach:
    limit: RiskLimit
    message: str
    observed: str
    allowed: str

    def as_details(self) -> dict[str, object]:
        return {
            "limit": self.limit.value,
            "message": self.message,
            "observed": self.observed,
            "allowed": self.allowed,
        }

    def __str__(self) -> str:
        return f"{self.limit.value}: {self.message}"


class RiskApproval:
    """Single-use proof that one specific order passed every risk check.

    Bound to an idempotency key so it cannot be replayed against a different
    order, and expiring so a stale approval cannot authorise an order placed
    much later against much-changed exposure.
    """

    __slots__ = (
        "_approval_id",
        "_idempotency_key",
        "_order_notional",
        "_checks",
        "_issued_at",
        "_issued_at_mono",
        "_ttl_seconds",
        "_approver",
        "_consumed",
        "_lock",
    )

    def __init__(
        self,
        *,
        idempotency_key: str,
        order_notional: Money,
        checks: Sequence[RiskLimit],
        issued_at: dt.datetime,
        issued_at_mono: float,
        ttl_seconds: int,
        approver: str,
        mint_key: object = None,
    ) -> None:
        if mint_key is not _APPROVAL_MINT_KEY:
            raise UnauthorizedAction(
                "RiskApproval cannot be constructed directly; it is minted only "
                "by RiskEngine.approve() after every risk check has passed "
                "(INVARIANT 4)"
            )
        self._approval_id = f"RA-{uuid.uuid4().hex[:16]}"
        self._idempotency_key = idempotency_key
        self._order_notional = order_notional
        self._checks = tuple(checks)
        self._issued_at = issued_at
        self._issued_at_mono = issued_at_mono
        self._ttl_seconds = ttl_seconds
        self._approver = approver
        self._consumed = False
        self._lock = threading.Lock()

    @property
    def approval_id(self) -> str:
        return self._approval_id

    @property
    def idempotency_key(self) -> str:
        return self._idempotency_key

    @property
    def order_notional(self) -> Money:
        return self._order_notional

    @property
    def checks(self) -> tuple[RiskLimit, ...]:
        return self._checks

    @property
    def approver(self) -> str:
        return self._approver

    @property
    def is_consumed(self) -> bool:
        with self._lock:
            return self._consumed

    def covers_all_limits(self) -> bool:
        """Whether this approval represents a complete evaluation."""
        return set(self._checks) == set(REQUIRED_CHECKS)

    def is_expired(self, clock: Clock) -> bool:
        elapsed = clock.monotonic_seconds() - self._issued_at_mono
        if elapsed < 0:  # time went backwards; do not trust the approval
            return True
        return elapsed > self._ttl_seconds

    def consume(self, *, idempotency_key: str, clock: Clock) -> None:
        """Spend the approval, or raise.

        Atomic and single-use: two orders cannot share one approval.
        """
        with self._lock:
            if self._consumed:
                raise UnauthorizedAction(
                    f"risk approval {self._approval_id} has already been used; "
                    "approvals are single-use (INVARIANT 4)"
                )
            if idempotency_key != self._idempotency_key:
                raise UnauthorizedAction(
                    f"risk approval {self._approval_id} was issued for a different "
                    "order; refusing to apply it here (INVARIANT 4)"
                )
            if self.is_expired(clock):
                raise UnauthorizedAction(
                    f"risk approval {self._approval_id} has expired after "
                    f"{self._ttl_seconds}s; exposure may have changed since it was "
                    "issued (INVARIANT 4)"
                )
            if not self.covers_all_limits():
                missing = sorted(
                    limit.value for limit in set(REQUIRED_CHECKS) - set(self._checks)
                )
                raise UnauthorizedAction(
                    f"risk approval {self._approval_id} is incomplete; it does not "
                    f"cover {missing} (INVARIANT 7)"
                )
            self._consumed = True

    def as_details(self) -> dict[str, object]:
        return {
            "approval_id": self._approval_id,
            "idempotency_key": self._idempotency_key,
            "order_notional": str(self._order_notional.amount),
            "currency": self._order_notional.currency.code,
            "checks": [c.value for c in self._checks],
            "approver": self._approver,
            "issued_at": self._issued_at.isoformat(),
            "ttl_seconds": self._ttl_seconds,
        }

    def __repr__(self) -> str:
        return (
            f"RiskApproval({self._approval_id}, notional={self._order_notional}, "
            f"consumed={self._consumed})"
        )


class PnlLedger:
    """Realized profit and loss for the current trading day.

    The day boundary is UTC and rollover is automatic, but rollover *resets the
    budget*, so it is deliberately driven by the injected clock rather than
    ``datetime.now()`` -- a test must be able to prove the reset happens exactly
    once, at the boundary, and not because a timer drifted.

    The ledger also counts fills whose realized amount could not be computed --
    a close against a position whose cost basis we never saw. Such a fill is not
    a zero; it is a hole in today's total, and :attr:`is_complete` reports it so
    the daily-loss limit can refuse rather than trust an understated figure.
    """

    def __init__(self, base_currency, *, clock: Clock) -> None:
        self._currency = base_currency
        self._clock = clock
        self._day = clock.now().date()
        self._realized = Money.zero(base_currency)
        self._unattributed = 0
        self._lock = threading.RLock()

    @property
    def day(self) -> dt.date:
        with self._lock:
            self._maybe_roll_over()
            return self._day

    def _maybe_roll_over(self) -> None:
        """Caller holds the lock."""
        today = self._clock.now().date()
        if today != self._day:
            self._day = today
            self._realized = Money.zero(self._currency)
            self._unattributed = 0

    def record(self, pnl: Money, *, attributed: bool = True) -> Money:
        """Add a realized result. Negative values are losses.

        ``attributed=False`` says this fill realized an amount we could not
        compute, so ``pnl`` is a placeholder rather than the answer. It is still
        added -- it is zero in that case -- but the fill is counted against
        :attr:`is_complete`, because the alternative is a silent zero making the
        loss budget look intact when it may not be.
        """
        if not isinstance(pnl, Money):
            raise TypeError("realized pnl must be a Money")
        if pnl.currency != self._currency:
            raise ValueError(
                f"realized pnl must be in {self._currency.code}, got {pnl.currency.code}"
            )
        with self._lock:
            self._maybe_roll_over()
            self._realized = self._realized + pnl
            if not attributed:
                self._unattributed += 1
            return self._realized

    @property
    def realized(self) -> Money:
        with self._lock:
            self._maybe_roll_over()
            return self._realized

    @property
    def unattributed_fills(self) -> int:
        """Fills today whose realized amount could not be computed."""
        with self._lock:
            self._maybe_roll_over()
            return self._unattributed

    @property
    def is_complete(self) -> bool:
        """False when today's realized total is known to be missing something."""
        return self.unattributed_fills == 0

    @property
    def realized_loss(self) -> Money:
        """Today's loss as a positive amount; zero if today is profitable.

        Read together with :attr:`is_complete`: an incomplete total may
        understate the loss, and understating it is the direction that matters.
        """
        current = self.realized
        if current.amount >= 0:
            return Money.zero(self._currency)
        return -current

    def remaining_budget(self, limit: Money) -> Money | None:
        """Unspent loss allowance against ``limit``, or ``None`` if unknowable.

        ``None`` means today's total is incomplete: some fill closed against a
        cost basis we never saw, so :attr:`realized_loss` is a lower bound and a
        remainder computed from it would be an *upper* bound. That is the
        direction that sizes a position too large, so the absence is reported
        rather than papered over -- a caller must refuse, not read it as room.

        Computed under the ledger's own lock, because completeness and the total
        have to be read as one fact. Sampled separately, a fill landing between
        the two reads would produce a budget derived from a fresher total than
        the completeness check that vouched for it.
        """
        if not isinstance(limit, Money):
            raise TypeError("limit must be a Money")
        if limit.currency != self._currency:
            raise ValueError(
                f"limit must be in {self._currency.code}, got {limit.currency.code}"
            )
        with self._lock:
            self._maybe_roll_over()
            if self._unattributed:
                return None
            spent = self.realized_loss
            if spent >= limit:
                return Money.zero(self._currency)
            return limit - spent


class RiskEngine:
    """Evaluates an intent against every configured limit.

    The engine carries its own :class:`~trading.core.authz.Principal`, which
    must hold :data:`~trading.core.authz.Action.APPROVE_ORDER`. A strategy
    cannot stand up an engine in its own name, so it cannot approve its own
    orders (separation of duties, supporting INVARIANT 3 and 4).
    """

    def __init__(
        self,
        config: RiskConfig,
        *,
        identity: Principal,
        order_store: OrderStore,
        audit: AuditLog,
        clock: Clock,
        approval_ttl_seconds: int = 30,
    ) -> None:
        authorize(identity, Action.APPROVE_ORDER)
        if is_authorized(identity, Action.EXECUTE_ORDER):
            raise UnauthorizedAction(
                f"{identity} may both approve and execute orders; the risk engine "
                "refuses to run under an identity that can approve its own "
                "executions (INVARIANT 4)"
            )
        if not isinstance(approval_ttl_seconds, int) or isinstance(
            approval_ttl_seconds, bool
        ):
            raise TypeError("approval_ttl_seconds must be an int")
        if approval_ttl_seconds < 1:
            raise ValueError("approval_ttl_seconds must be >= 1")

        self._config = config
        self._identity = identity
        self._orders = order_store
        self._audit = audit
        self._clock = clock
        self._ttl = approval_ttl_seconds
        self._pnl = PnlLedger(config.base_currency, clock=clock)
        self._submissions: deque[float] = deque()
        self._lock = threading.RLock()

    @property
    def config(self) -> RiskConfig:
        return self._config

    @property
    def pnl(self) -> PnlLedger:
        return self._pnl

    @property
    def remaining_loss_budget(self) -> Money | None:
        """Today's unspent daily-loss allowance, or ``None`` if it cannot be stated.

        The one definition of the day's remaining room, so a sizer proposing
        against it and :meth:`approve` enforcing it are reading the same number
        from the same ledger. ``None`` when the ledger is incomplete -- see
        :meth:`PnlLedger.remaining_budget`.
        """
        return self._pnl.remaining_budget(self._config.max_daily_loss)

    # -- rate accounting ---------------------------------------------------
    def record_submission(self) -> None:
        """Record that an order was submitted, for the rate limit.

        Called by the gateway *after* an order actually goes out, so a refused
        order does not consume rate budget.
        """
        with self._lock:
            now = self._clock.monotonic_seconds()
            self._submissions.append(now)
            self._trim_submissions(now)

    def _trim_submissions(self, now: float) -> None:
        """Caller holds the lock."""
        cutoff = now - _RATE_WINDOW_SECONDS
        while self._submissions and self._submissions[0] < cutoff:
            self._submissions.popleft()

    def submissions_in_window(self) -> int:
        with self._lock:
            now = self._clock.monotonic_seconds()
            self._trim_submissions(now)
            return len(self._submissions)

    # -- exposure ----------------------------------------------------------
    def _notional(self, quantity: Quantity, price: Price) -> Money:
        """Absolute cash value, rounded UP so exposure is never understated."""
        return price.notional(abs(quantity))

    def exposure_report(
        self,
        positions: Mapping[str, Quantity],
        mark_prices: Mapping[str, Price],
    ) -> tuple[Money, dict[str, Money], list[str]]:
        """Return (gross exposure, per-symbol exposure, symbols missing a price)."""
        gross = Money.zero(self._config.base_currency)
        per_symbol: dict[str, Money] = {}
        missing: list[str] = []
        for symbol, quantity in positions.items():
            if quantity.is_zero:
                continue
            price = mark_prices.get(symbol)
            if price is None:
                missing.append(symbol)
                continue
            value = self._notional(quantity, price)
            per_symbol[symbol] = value
            gross = gross + value
        return gross, per_symbol, sorted(missing)

    # -- the check ---------------------------------------------------------
    def approve(
        self,
        intent: OrderIntent,
        *,
        positions: Mapping[str, Quantity],
        mark_prices: Mapping[str, Price],
    ) -> RiskApproval:
        """Run every check and mint an approval, or raise :class:`RiskLimitExceeded`.

        ``positions`` is the current local position per symbol; ``mark_prices``
        must cover the intent's symbol and every symbol with a non-zero position.
        """
        if not isinstance(intent, OrderIntent):
            raise TypeError("intent must be an OrderIntent")

        with self._lock:
            breaches: list[LimitBreach] = []
            checked: list[RiskLimit] = []
            waived: list[RiskLimit] = []
            config = self._config
            symbol = intent.symbol

            # 1. Mark price availability. Without prices nothing else is
            #    computable, so a gap here is a breach, never a skipped check.
            checked.append(RiskLimit.MARK_PRICE_AVAILABLE)
            gross_before, _per_symbol, missing = self.exposure_report(
                positions, mark_prices
            )
            entry_price = mark_prices.get(symbol) or intent.limit_price
            if entry_price is None:
                missing = sorted(set(missing) | {symbol})
            if missing:
                breaches.append(
                    LimitBreach(
                        limit=RiskLimit.MARK_PRICE_AVAILABLE,
                        message=(
                            f"no mark price for {missing}; exposure cannot be "
                            "computed, so the order is refused rather than "
                            "approved unchecked"
                        ),
                        observed=f"missing={missing}",
                        allowed="a price for every symbol with exposure",
                    )
                )
                # Everything downstream needs prices. Record the remaining checks
                # as attempted-and-failed rather than silently passed.
                for limit in REQUIRED_CHECKS:
                    if limit not in checked:
                        checked.append(limit)
                self._refuse(intent, breaches, checked)

            assert entry_price is not None  # guarded above
            order_notional = self._notional(intent.quantity, entry_price)

            # 2. Per-order notional ceiling.
            checked.append(RiskLimit.ORDER_NOTIONAL)
            if order_notional > config.max_order_notional:
                breaches.append(
                    LimitBreach(
                        limit=RiskLimit.ORDER_NOTIONAL,
                        message=(
                            f"order notional {order_notional} exceeds the per-order "
                            f"ceiling {config.max_order_notional}"
                        ),
                        observed=str(order_notional),
                        allowed=str(config.max_order_notional),
                    )
                )

            # 3. Resulting position for this symbol. Signed, so a closing order
            #    is judged on what it leaves behind.
            checked.append(RiskLimit.POSITION_NOTIONAL)
            current = positions.get(symbol, Quantity.zero(intent.quantity.asset))
            # Incoherent inputs, not a risk condition: if the ledger thinks
            # BTCUSD is denominated in ETH, the position data cannot be trusted
            # and no arithmetic on it means anything. Refuse loudly rather than
            # letting a CurrencyMismatch escape as an unexpected error.
            if current.asset != intent.quantity.asset:
                raise SafetyViolation(
                    f"position for {symbol} is held in {current.asset} but the order "
                    f"is in {intent.quantity.asset}; refusing to evaluate risk "
                    "against inconsistent position data"
                )
            delta = (
                intent.quantity
                if intent.side is OrderSide.BUY
                else -intent.quantity
            )
            projected = current + delta
            projected_notional = self._notional(projected, entry_price)

            # An order that strictly shrinks an existing position, without
            # flipping its sign, is a de-risking order. Standing limits must not
            # block it: a position can be above a ceiling because the ceiling was
            # tightened or because it was adopted from the broker during
            # reconciliation, and refusing every exit would trap the operator in
            # exactly the position the limit exists to prevent.
            #
            # The no-sign-flip condition matters. Selling 0.017 of a 0.009 long
            # shrinks the magnitude but opens a fresh 0.008 short, which is a new
            # position and is judged on the ceiling like any other.
            reduces_position = (
                abs(projected.amount) < abs(current.amount)
                and (
                    projected.amount == 0
                    or (projected.amount > 0) == (current.amount > 0)
                )
            )

            if projected_notional > config.max_position_notional:
                if reduces_position:
                    waived.append(RiskLimit.POSITION_NOTIONAL)
                else:
                    breaches.append(
                        LimitBreach(
                            limit=RiskLimit.POSITION_NOTIONAL,
                            message=(
                                f"resulting {symbol} position {projected.amount} would "
                                f"be worth {projected_notional}, over the per-position "
                                f"ceiling {config.max_position_notional}"
                            ),
                            observed=str(projected_notional),
                            allowed=str(config.max_position_notional),
                        )
                    )

            # 4. Gross exposure across every symbol.
            checked.append(RiskLimit.GROSS_EXPOSURE)
            current_notional = self._notional(current, entry_price)
            projected_gross = gross_before - current_notional + projected_notional
            if projected_gross > config.max_gross_exposure:
                if reduces_position:
                    waived.append(RiskLimit.GROSS_EXPOSURE)
                else:
                    breaches.append(
                        LimitBreach(
                            limit=RiskLimit.GROSS_EXPOSURE,
                            message=(
                                f"resulting gross exposure {projected_gross} exceeds "
                                f"{config.max_gross_exposure}"
                            ),
                            observed=str(projected_gross),
                            allowed=str(config.max_gross_exposure),
                        )
                    )

            # 5. Daily loss budget. Also waived for de-risking orders -- having
            #    spent the day's loss budget must not mean being unable to close.
            checked.append(RiskLimit.DAILY_LOSS)
            loss = self._pnl.realized_loss
            complete = self._pnl.is_complete
            if loss >= config.max_daily_loss:
                if reduces_position:
                    waived.append(RiskLimit.DAILY_LOSS)
                else:
                    breaches.append(
                        LimitBreach(
                            limit=RiskLimit.DAILY_LOSS,
                            message=(
                                f"today's realized loss {loss} has reached the daily "
                                f"budget {config.max_daily_loss}; no further orders "
                                "today"
                            ),
                            observed=str(loss),
                            allowed=str(config.max_daily_loss),
                        )
                    )
            elif not complete:
                # Some fill today closed against a basis we never saw, so the
                # figure above is a floor, not the loss. A budget checked against
                # an understated loss is not a budget. De-risking is still
                # allowed, for the same reason it is when the budget is spent.
                if reduces_position:
                    waived.append(RiskLimit.DAILY_LOSS)
                else:
                    missing = self._pnl.unattributed_fills
                    breaches.append(
                        LimitBreach(
                            limit=RiskLimit.DAILY_LOSS,
                            message=(
                                f"{missing} fill(s) today closed a position with no "
                                f"known cost basis, so today's realized loss {loss} "
                                "is a lower bound rather than the figure; refusing "
                                "rather than checking the budget against an "
                                "understated loss"
                            ),
                            observed=f"{loss} (incomplete: {missing} fill(s))",
                            allowed=str(config.max_daily_loss),
                        )
                    )

            # 6. Submission rate.
            checked.append(RiskLimit.ORDER_RATE)
            recent = self.submissions_in_window()
            if recent >= config.max_orders_per_minute:
                breaches.append(
                    LimitBreach(
                        limit=RiskLimit.ORDER_RATE,
                        message=(
                            f"{recent} orders submitted in the last "
                            f"{int(_RATE_WINDOW_SECONDS)}s, at the limit of "
                            f"{config.max_orders_per_minute}"
                        ),
                        observed=str(recent),
                        allowed=str(config.max_orders_per_minute),
                    )
                )

            # 7. Concurrent open orders.
            checked.append(RiskLimit.OPEN_ORDERS)
            open_count = len(self._orders.open_orders())
            if open_count >= config.max_open_orders:
                breaches.append(
                    LimitBreach(
                        limit=RiskLimit.OPEN_ORDERS,
                        message=(
                            f"{open_count} orders are already open, at the limit of "
                            f"{config.max_open_orders}"
                        ),
                        observed=str(open_count),
                        allowed=str(config.max_open_orders),
                    )
                )

            if breaches:
                self._refuse(intent, breaches, checked)

            approval = RiskApproval(
                idempotency_key=intent.idempotency_key,
                order_notional=order_notional,
                checks=checked,
                issued_at=self._clock.now(),
                issued_at_mono=self._clock.monotonic_seconds(),
                ttl_seconds=self._ttl,
                approver=self._identity.principal_id,
                mint_key=_APPROVAL_MINT_KEY,
            )
            self._audit.record(
                AuditCategory.RISK,
                "risk_approved",
                outcome=AuditOutcome.ALLOWED,
                actor=self._identity.principal_id,
                details={
                    **intent.as_details(),
                    "approval_id": approval.approval_id,
                    "order_notional": str(order_notional.amount),
                    "projected_gross_exposure": str(projected_gross.amount),
                    "checks": [c.value for c in checked],
                    # Loud, because an over-limit order was permitted here.
                    "waived_as_de_risking": [w.value for w in waived],
                },
            )
            return approval

    def _refuse(
        self,
        intent: OrderIntent,
        breaches: Sequence[LimitBreach],
        checked: Sequence[RiskLimit],
    ) -> None:
        """Audit every breach and raise. Never returns."""
        self._audit.record(
            AuditCategory.RISK,
            "risk_refused",
            outcome=AuditOutcome.REFUSED,
            actor=self._identity.principal_id,
            details={
                **intent.as_details(),
                "breaches": [b.as_details() for b in breaches],
                "checks_run": [c.value for c in checked],
            },
        )
        summary = "; ".join(str(b) for b in breaches)
        raise RiskLimitExceeded(
            breaches[0].limit.value,
            f"order refused by {len(breaches)} risk limit(s): {summary} (INVARIANT 7)",
        )
