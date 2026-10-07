"""The execution gateway: the one place an order can leave this system.

Everything else in the kernel is a component that answers a question. This is
the component that *acts*, and it is deliberately the only one. Nothing else
holds a :class:`~trading.ports.broker.BrokerPort`, and even something that
smuggles one in cannot use it, because
:meth:`~trading.ports.broker.BrokerPort.place_order` demands an
:class:`~trading.core.authz.ExecutionToken` that only this module can mint.

The chain
=========

:meth:`ExecutionGateway.submit` runs these gates in this order, and refuses at
the first failure:

===  ================================  ==============================
#    Gate                              Invariant
===  ================================  ==============================
1    Caller may propose                3
2    Kill switch not engaged           10
3    All circuit breakers closed       --
4    Mode allows execution             2, 11
5    LIVE additionally authorised      1, 2
6    Idempotency key claimed           12
7    No UNKNOWN order / mismatch       5, 6
8    Risk approval covering all limits 4, 7
9    Token minted and order persisted  3
10   Broker call, token consumed       3, 12
===  ================================  ==============================

The order is not arbitrary. Cheap absolute stops come before expensive
evaluation, so a halted system does no risk arithmetic. Idempotency is claimed
*before* the risk check so a duplicate is rejected without consuming rate-limit
budget. Risk approval is the last gate before the token exists, so a token can
never exist without a complete approval behind it -- that is what makes
INVARIANT 4 structural rather than a matter of statement ordering.

Ordering is asserted directly in ``tests/test_gateway.py``: with several gates
tripped at once, the reported failure identifies the earliest one.

Failure is never silent
=======================

Three outcomes, and only three:

* **Refused** -- a :class:`~trading.core.errors.SafetyViolation` subclass names
  the gate. Nothing was sent. The idempotency key is released, so a corrected
  order may reuse it.
* **Executed** -- the broker answered definitively. The order state reflects the
  answer and the reservation settles.
* **Unknown** -- the broker answered ``UNCERTAIN`` *or* raised. The order goes to
  ``UNKNOWN``, the reservation goes to ``UNKNOWN``, and the whole system stops
  accepting new orders until an operator reconciles (INVARIANTS 5, 12). The key
  is *not* released: we cannot prove the venue never saw it.

A retry after an unknown outcome is the single most dangerous thing a trading
system can do, so there is no retry anywhere in this module.
"""

from __future__ import annotations

import decimal
import threading
from typing import Mapping, NoReturn

from .audit import AuditCategory, AuditLog, AuditOutcome
from .authz import (
    Action,
    ExecutionToken,
    Principal,
    authorize,
    is_authorized,
    mint_execution_token,
)
from .breaker import BreakerRegistry
from .clock import Clock
from .config import TradingConfig
from .dedupe import IdempotencyRegistry, ReservationState
from .errors import SafetyViolation, UnauthorizedAction, UnknownOrderStateBlocked
from .killswitch import KillSwitch
from .modes import TradingModeMachine
from .money import FINANCIAL_CONTEXT, Price
from .orders import Order, OrderIntent, OrderState, OrderStore
from .portfolio import Portfolio
from .reconciliation import ReconciliationGate
from .risk import RiskApproval, RiskEngine
from ..ports.broker import AckOutcome, BrokerAck, BrokerPort

__all__ = ["ExecutionGate", "ExecutionOutcome", "ExecutionResult", "ExecutionGateway"]


class ExecutionGate:
    """Names for the chain's stages, used in audit records and refusals.

    A plain namespace rather than an enum: these are labels for humans reading
    an audit trail, and the chain's shape is asserted by tests, not by types.
    """

    AUTHORIZATION = "authorization"
    KILL_SWITCH = "kill_switch"
    CIRCUIT_BREAKERS = "circuit_breakers"
    TRADING_MODE = "trading_mode"
    LIVE_AUTHORIZATION = "live_authorization"
    DUPLICATE_ORDER = "duplicate_order"
    RECONCILIATION = "reconciliation"
    RISK = "risk"
    TOKEN = "token"
    EXECUTION = "execution"

    #: The chain in order. Tests use this to prove the sequence.
    ORDER: tuple[str, ...] = (
        AUTHORIZATION,
        KILL_SWITCH,
        CIRCUIT_BREAKERS,
        TRADING_MODE,
        LIVE_AUTHORIZATION,
        DUPLICATE_ORDER,
        RECONCILIATION,
        RISK,
        TOKEN,
        EXECUTION,
    )


class ExecutionOutcome:
    """What became of a submission."""

    EXECUTED = "executed"
    REFUSED = "refused"
    UNKNOWN = "unknown"


class ExecutionResult:
    """The gateway's answer. Immutable and self-describing.

    Deliberately not a bare :class:`bool` or an :class:`Order`: a caller has to
    look at :attr:`outcome` to learn what happened, and ``UNKNOWN`` is
    impossible to mistake for success.
    """

    __slots__ = ("_outcome", "_order", "_ack", "_gate", "_reason")

    def __init__(
        self,
        *,
        outcome: str,
        order: Order | None = None,
        ack: BrokerAck | None = None,
        gate: str | None = None,
        reason: str = "",
    ) -> None:
        self._outcome = outcome
        self._order = order
        self._ack = ack
        self._gate = gate
        self._reason = reason

    @property
    def outcome(self) -> str:
        return self._outcome

    @property
    def order(self) -> Order | None:
        return self._order

    @property
    def ack(self) -> BrokerAck | None:
        return self._ack

    @property
    def gate(self) -> str | None:
        """Which gate refused, or ``None`` when nothing refused."""
        return self._gate

    @property
    def reason(self) -> str:
        return self._reason

    @property
    def is_executed(self) -> bool:
        return self._outcome == ExecutionOutcome.EXECUTED

    @property
    def is_refused(self) -> bool:
        return self._outcome == ExecutionOutcome.REFUSED

    @property
    def is_unknown(self) -> bool:
        return self._outcome == ExecutionOutcome.UNKNOWN

    def as_details(self) -> dict[str, object]:
        return {
            "outcome": self._outcome,
            "order_id": self._order.order_id if self._order else None,
            "gate": self._gate,
            "reason": self._reason,
            "ack": self._ack.as_details() if self._ack else None,
        }

    def __repr__(self) -> str:
        where = f" at {self._gate}" if self._gate else ""
        return f"<ExecutionResult {self._outcome}{where}: {self._reason}>"


class ExecutionGateway:
    """The single execution chokepoint.

    Constructed with an identity that must be able to execute; a gateway whose
    identity cannot execute is refused at construction rather than at the first
    order, so a misconfiguration surfaces at startup.

    The gateway holds the only broker reference in a correctly wired system.
    """

    def __init__(
        self,
        *,
        identity: Principal,
        broker: BrokerPort,
        orders: OrderStore,
        positions: Portfolio,
        reconciliation: ReconciliationGate,
        risk: RiskEngine,
        dedupe: IdempotencyRegistry,
        kill_switch: KillSwitch,
        breakers: BreakerRegistry,
        modes: TradingModeMachine,
        config: TradingConfig,
        audit: AuditLog,
        clock: Clock,
        token_ttl_seconds: int = 30,
    ) -> None:
        # Fail at wiring time, not at the first order.
        authorize(identity, Action.EXECUTE_ORDER)
        if is_authorized(identity, Action.APPROVE_ORDER):
            raise UnauthorizedAction(
                f"{identity.principal_id} can both approve and execute orders; "
                "the gateway must not be able to approve its own orders "
                "(INVARIANT 4)"
            )
        if not isinstance(broker, BrokerPort):
            raise TypeError("broker must implement BrokerPort")
        if not isinstance(positions, Portfolio):
            # A bare PositionLedger would accept the quantity and drop the fill
            # price, leaving every position we filled ourselves with no cost
            # basis. Caught here rather than at the first fill.
            raise TypeError("positions must be a Portfolio")
        if positions.base_currency != risk.config.base_currency:
            # Otherwise recording a fill's realized result would raise *after*
            # the fill had already been applied, which is the worst possible
            # moment to discover a wiring error.
            raise SafetyViolation(
                f"the portfolio is denominated in {positions.base_currency.code} but "
                f"the risk config is in {risk.config.base_currency.code}; realized "
                "profit and loss could not reach the daily-loss limit"
            )

        self._identity = identity
        self._broker = broker
        self._orders = orders
        self._positions = positions
        self._reconciliation = reconciliation
        self._risk = risk
        self._dedupe = dedupe
        self._kill_switch = kill_switch
        self._breakers = breakers
        self._modes = modes
        self._config = config
        self._audit = audit
        self._clock = clock
        self._token_ttl_seconds = int(token_ttl_seconds)
        # One order at a time through the chain. The gates read shared state
        # (open-order counts, reservations, reconciliation status) and a
        # concurrent submission could otherwise observe a half-updated view.
        self._lock = threading.Lock()

    @property
    def identity(self) -> Principal:
        return self._identity

    # -- the chain --------------------------------------------------------

    def submit(
        self,
        intent: OrderIntent,
        *,
        proposer: Principal,
        mark_prices: Mapping[str, Price] | None = None,
    ) -> ExecutionResult:
        """Run the full safety chain for ``intent`` and, if every gate passes, execute.

        ``proposer`` is the identity that produced the intent -- checked
        separately from the gateway's own identity so a component that may not
        even propose cannot get an order in through a gateway that may execute.
        """
        if not isinstance(intent, OrderIntent):
            raise TypeError("submit() takes an OrderIntent")

        with self._lock:
            return self._submit_locked(intent, proposer, mark_prices or {})

    def _submit_locked(
        self,
        intent: OrderIntent,
        proposer: Principal,
        mark_prices: Mapping[str, Price],
    ) -> ExecutionResult:
        key = intent.idempotency_key

        # 1. The caller must be allowed to propose. A strategy passes here; an
        #    auditor does not.
        try:
            authorize(proposer, Action.PROPOSE_ORDER)
        except SafetyViolation as exc:
            return self._refuse(ExecutionGate.AUTHORIZATION, exc, intent)

        # 2. The kill switch. Absolute, and checked before anything expensive.
        try:
            self._kill_switch.require_not_engaged()
        except SafetyViolation as exc:
            return self._refuse(ExecutionGate.KILL_SWITCH, exc, intent)

        # 3. Circuit breakers.
        try:
            self._breakers.require_all_closed()
        except SafetyViolation as exc:
            return self._refuse(ExecutionGate.CIRCUIT_BREAKERS, exc, intent)

        # 4. Mode. DISABLED is the default, so this is the gate that makes
        #    INVARIANT 2 true out of the box.
        try:
            mode = self._modes.require_execution_allowed()
        except SafetyViolation as exc:
            return self._refuse(ExecutionGate.TRADING_MODE, exc, intent)

        # 5. LIVE needs the config's blessing too, not just the mode's.
        if mode.is_live:
            try:
                self._modes.require_live_allowed()
            except SafetyViolation as exc:
                return self._refuse(ExecutionGate.LIVE_AUTHORIZATION, exc, intent)

        # 6. Claim the key before doing anything else. A duplicate must not
        #    consume rate-limit budget or leave a half-built order behind.
        order = Order(intent, clock=self._clock)
        try:
            self._dedupe.reserve(key, order.order_id)
        except SafetyViolation as exc:
            return self._refuse(ExecutionGate.DUPLICATE_ORDER, exc, intent)

        # From here on the key is claimed, so every refusal path must release
        # it -- but only while we can still prove nothing was sent.
        try:
            # 7. Nothing may be in flight with an unresolved fate, and our
            #    positions must agree with the venue's.
            try:
                self._reconciliation.require_clean(live=mode.is_live)
                self._require_no_unknown_reservation()
            except SafetyViolation as exc:
                return self._refuse(
                    ExecutionGate.RECONCILIATION, exc, intent, release_key=True
                )

            # 8. Risk. The approval that comes back is a capability covering
            #    every limit; there is no way to reach step 9 without one.
            try:
                approval = self._risk.approve(
                    intent,
                    positions=self._positions.snapshot(),
                    mark_prices=self._resolve_prices(intent, mark_prices),
                )
            except SafetyViolation as exc:
                return self._refuse(
                    ExecutionGate.RISK, exc, intent, release_key=True
                )

            # 9. Mint the token and consume the approval. Both are single-use
            #    and order-bound, so neither can be replayed onto another order.
            try:
                token = self._mint(order, approval)
            except SafetyViolation as exc:
                return self._refuse(
                    ExecutionGate.TOKEN, exc, intent, release_key=True
                )

            # 10. Send it. Past this line the key is never released.
            return self._execute(order, token, mode_is_live=mode.is_live)
        except Exception:
            # An unexpected failure before submission still leaves the key
            # claimed, which is the safe direction: worst case an operator has
            # to clear a reservation. Never guess that nothing was sent.
            raise

    # -- steps 9 and 10 ---------------------------------------------------

    def _require_no_unknown_reservation(self) -> None:
        """Block new orders while any idempotency reservation is UNKNOWN.

        This is a fail-closed backstop to ``require_clean``, which sources the
        UNKNOWN block from the *order* store. In the default in-memory wiring the
        two always agree -- an order and its reservation are marked UNKNOWN
        together, and ``require_clean`` checks the order store first, so this
        never fires before it. It becomes load-bearing only when reservations
        are durable and orders are not: after a real restart the persisted
        UNKNOWN reservation survives while the in-memory order object does not,
        and this is what keeps INVARIANT 5 blocking across that boundary.

        It can only ever *add* a refusal, never remove one, so it weakens no
        gate. Raised as :class:`UnknownOrderStateBlocked`, which the submit chain
        already turns into a RECONCILIATION-gate refusal that releases the
        just-claimed (still-RESERVED) key -- nothing was sent, and the UNKNOWN
        reservation it is blocking on is a *different* key, left untouched.
        """
        if self._dedupe.has_unknown():
            raise UnknownOrderStateBlocked(
                "an idempotency reservation is in UNKNOWN state (its order may "
                "not have survived a restart); no new orders will be accepted "
                "until it is reconciled (INVARIANT 5)"
            )

    def _mint(self, order: Order, approval: RiskApproval) -> ExecutionToken:
        """Turn a risk approval into an execution token.

        The approval is consumed here, bound to this order's idempotency key.
        Consumption is atomic and single-use, so two threads holding the same
        approval cannot both mint.
        """
        # Belt and braces: approve() cannot return an incomplete approval, but
        # the token must not exist if it somehow did.
        if not approval.covers_all_limits():
            raise SafetyViolation(
                "risk approval does not cover every configured limit; "
                "refusing to mint an execution token (INVARIANT 4)"
            )
        approval.consume(idempotency_key=order.idempotency_key, clock=self._clock)
        return mint_execution_token(
            self._identity,
            order_id=order.order_id,
            idempotency_key=order.idempotency_key,
            clock=self._clock,
            ttl_seconds=self._token_ttl_seconds,
        )

    def _execute(
        self, order: Order, token: ExecutionToken, *, mode_is_live: bool
    ) -> ExecutionResult:
        """Persist, send, and interpret the answer."""
        # Persist in PENDING_NEW *before* sending: a crash after this point is
        # recoverable, a crash before it means nothing was sent.
        self._orders.add(order)
        order.transition_to(
            OrderState.PENDING_NEW, reason="submitted through execution gateway"
        )
        self._risk.record_submission()
        self._dedupe.mark_submitted(order.idempotency_key, note="sent to broker")

        try:
            ack = self._broker.place_order(order, token=token)
        except Exception as exc:
            # A raised exception says nothing about whether the venue got it.
            return self._to_unknown(
                order,
                reason=f"broker raised {type(exc).__name__}: {exc}",
                ack=None,
            )

        if not isinstance(ack, BrokerAck):
            return self._to_unknown(
                order,
                reason=(
                    f"broker returned {type(ack).__name__} instead of a BrokerAck; "
                    "treating the outcome as unknown"
                ),
                ack=None,
            )

        if ack.outcome is AckOutcome.UNCERTAIN:
            return self._to_unknown(order, reason=ack.message or "uncertain ack", ack=ack)

        return self._settle(order, ack, mode_is_live=mode_is_live)

    def _settle(
        self, order: Order, ack: BrokerAck, *, mode_is_live: bool
    ) -> ExecutionResult:
        """Apply a definitive ack to the order and the ledger."""
        if ack.broker_order_id:
            order.attach_broker_order_id(ack.broker_order_id)

        if ack.outcome is AckOutcome.REJECTED:
            order.transition_to(
                OrderState.REJECTED, reason=ack.message or "rejected by venue"
            )
            self._dedupe.mark_settled(order.idempotency_key, note="rejected by venue")
            self._audit_result(order, ExecutionOutcome.REFUSED, ack, AuditOutcome.REFUSED)
            return ExecutionResult(
                outcome=ExecutionOutcome.REFUSED,
                order=order,
                ack=ack,
                gate=ExecutionGate.EXECUTION,
                reason=ack.message or "rejected by venue",
            )

        if ack.outcome is AckOutcome.FILLED:
            assert ack.filled_quantity is not None and ack.fill_price is not None
            order.apply_fill(ack.filled_quantity, ack.fill_price, reason="venue fill")
            self._record_fill(order, ack)
        else:
            order.transition_to(
                OrderState.ACCEPTED, reason=ack.message or "accepted by venue"
            )

        self._dedupe.mark_settled(
            order.idempotency_key, note=f"venue {ack.outcome.value}"
        )
        self._audit_result(order, ExecutionOutcome.EXECUTED, ack, AuditOutcome.ALLOWED)
        return ExecutionResult(
            outcome=ExecutionOutcome.EXECUTED, order=order, ack=ack
        )

    def _record_fill(self, order: Order, ack: BrokerAck) -> None:
        """Book a fill into the portfolio and its realized result into the risk ledger.

        Both fill sites go through here so the daily-loss limit cannot see one
        kind of fill and miss the other -- a fill discovered during recovery is
        as real as one we watched happen.

        The realized figure is the portfolio's, not a second derivation: the
        cost basis lives there, and recomputing it here would be a second
        answer to the same question with its own way of being wrong.
        """
        assert ack.filled_quantity is not None and ack.fill_price is not None
        effect = self._positions.apply_fill(
            order.symbol, order.side, ack.filled_quantity, ack.fill_price
        )
        # A fill that closed against an unknown basis realized something we
        # cannot name. Recorded as such, so the limit refuses instead of
        # reading an understated loss (INVARIANT 7).
        self._risk.pnl.record(effect.realized_pnl, attributed=effect.realized_is_known)

    def _apply_fetched_state(
        self,
        order: Order,
        ack: BrokerAck,
        *,
        via_reconciliation: bool,
        after_cancel: bool = False,
    ) -> None:
        """Interpret cumulative venue state from fetch_order_state and update order.

        This method handles the three definitive outcomes from a broker's
        fetch_order_state query (REJECTED, ACCEPTED, FILLED) and applies the
        cumulative state to the local order using delta logic. It performs all
        defensive refusals for invalid or contradictory venue responses.

        When fills are applied, this method also updates the portfolio by recording
        only the delta fill, preventing double-booking.

        Args:
            order: Order to update
            ack: BrokerAck from fetch_order_state (cumulative snapshot)
            via_reconciliation: True when called from resolve_unknown
            after_cancel: True when the venue was just asked to cancel this
                order and acknowledged. It changes exactly one thing: a
                "no record" answer stops meaning REJECTED. See the REJECTED
                branch below.

        Raises:
            SafetyViolation: On broker_order_id mismatch, fill against terminal
                order, UNCERTAIN response, or any defensive refusal from
                apply_fill_delta. Every refusal is audited before it is raised
                (INVARIANT 13), and leaves the order untouched.
        """
        # -- validation ------------------------------------------------------
        # Every refusal below runs before anything mutates, so a contradictory
        # venue answer leaves the order exactly as it was. In particular the
        # broker_order_id is attached at the *end* of this method rather than
        # here: attaching first would leave a refused response half-applied.
        if (
            ack.broker_order_id
            and order.broker_order_id
            and order.broker_order_id != ack.broker_order_id
        ):
            self._refuse_venue_state(
                order,
                ack,
                f"broker_order_id mismatch on {order.order_id}: stored "
                f"{order.broker_order_id}, venue reports {ack.broker_order_id}",
            )

        if ack.outcome is AckOutcome.UNCERTAIN:
            # UNCERTAIN should not come from fetch_order_state in normal brokers,
            # but if it does, it is a sign something is wrong.
            self._refuse_venue_state(
                order,
                ack,
                f"fetch_order_state returned UNCERTAIN for {order.order_id}; "
                "this should not happen on a direct query",
            )

        if ack.outcome is AckOutcome.FILLED:
            # BrokerAck.__post_init__ already guarantees both are present on a
            # FILLED ack, so this is a type-checker aid, not a second check --
            # the same reasoning (and the same assert) as _settle.
            assert ack.filled_quantity is not None and ack.fill_price is not None
            # Refuse a fill against an order that already reached a terminal
            # state. UNKNOWN is deliberately *not* in TERMINAL_STATES, so an
            # order being resolved out of UNKNOWN passes this guard on its own
            # -- learning what actually happened is the whole point of that
            # path, and it needs no exemption here.
            if not ack.filled_quantity.is_zero and order.state.is_terminal:
                self._refuse_venue_state(
                    order,
                    ack,
                    f"venue reports fill against terminal order {order.order_id} "
                    f"in state {order.state.value}",
                )

        # -- application -----------------------------------------------------
        if ack.outcome is AckOutcome.REJECTED:
            # "The venue has no record" normally means the order never existed,
            # so REJECTED is the honest reading. Immediately after a cancel the
            # venue acknowledged, it means the opposite: the venue *did* have
            # the order and we withdrew it. Calling that REJECTED would claim
            # the venue never took the order, a stronger and different claim.
            # cancel() makes the CANCELED transition itself, once it has
            # confirmed nothing else got there first.
            if not after_cancel:
                self._transition_or_refuse(
                    order,
                    ack,
                    OrderState.REJECTED,
                    reason="venue has no record of this order",
                    via_reconciliation=via_reconciliation,
                )
        elif ack.outcome is AckOutcome.FILLED:
            # Compute cumulative notional from ack
            with decimal.localcontext(FINANCIAL_CONTEXT):
                cumulative_notional = ack.fill_price.amount * ack.filled_quantity.amount

            # Order's own defensive refusals -- regressed quantity, overfill,
            # asset and currency mismatch -- surface as SafetyViolation. They
            # refuse before mutating, so auditing here still precedes any
            # effect (INVARIANT 13). Order holds no audit log by design.
            try:
                _, delta_qty, delta_price = order.apply_fill_delta(
                    cumulative_quantity=ack.filled_quantity,
                    cumulative_notional=cumulative_notional,
                    currency=ack.fill_price.currency,
                    reason=(
                        "fill sync from venue"
                        if not via_reconciliation
                        else "fill discovered during reconciliation"
                    ),
                    via_reconciliation=via_reconciliation,
                )
            except SafetyViolation as exc:
                self._audit_venue_refusal(order, ack, str(exc))
                raise

            # Record only the delta fill in the portfolio (if any delta was applied)
            if not delta_qty.is_zero:
                delta_ack = BrokerAck(
                    outcome=AckOutcome.FILLED,
                    broker_order_id=ack.broker_order_id,
                    filled_quantity=delta_qty,
                    fill_price=delta_price,
                )
                self._record_fill(order, delta_ack)
        elif ack.outcome is AckOutcome.ACCEPTED:
            # The venue confirms the order is working. If we already believe
            # that, there is nothing to record: ACCEPTED -> ACCEPTED is absent
            # from the transition table because re-affirming a state is not a
            # change. Any *other* disagreement -- a venue calling an order
            # merely resting when we have booked fills against it -- is a real
            # contradiction, and refuses rather than quietly unwinding them.
            if order.state is not OrderState.ACCEPTED:
                self._transition_or_refuse(
                    order,
                    ack,
                    OrderState.ACCEPTED,
                    reason="order found resting at venue",
                    via_reconciliation=via_reconciliation,
                )

        # Bind the venue's identifier only now that its answer has been applied.
        if ack.broker_order_id:
            order.attach_broker_order_id(ack.broker_order_id)

    def _transition_or_refuse(
        self,
        order: Order,
        ack: BrokerAck,
        target: OrderState,
        *,
        reason: str,
        via_reconciliation: bool,
    ) -> None:
        """Move the order, auditing first if the state machine refuses.

        ``transition_to`` raises :class:`InvalidOrderTransition`, which is a
        :class:`SafetyViolation` like any other refusal here -- and like the
        others it must not disappear from the trail (INVARIANT 13). It refuses
        before mutating, so auditing on the way out still precedes any effect.
        """
        try:
            order.transition_to(
                target, reason=reason, via_reconciliation=via_reconciliation
            )
        except SafetyViolation as exc:
            self._audit_venue_refusal(order, ack, str(exc))
            raise

    def _audit_venue_refusal(self, order: Order, ack: BrokerAck, reason: str) -> None:
        """Record a refusal to apply a venue's answer. Nothing has changed yet."""
        self._audit.record(
            AuditCategory.RECONCILIATION,
            "gateway.venue_state_refused",
            outcome=AuditOutcome.REFUSED,
            actor=self._identity.principal_id,
            details={
                "order_id": order.order_id,
                "state": order.state.value,
                "idempotency_key": order.idempotency_key,
                "reason": reason,
                "ack": ack.as_details(),
            },
        )

    def _refuse_venue_state(self, order: Order, ack: BrokerAck, reason: str) -> NoReturn:
        """Audit a contradictory venue answer, then refuse it (INVARIANT 13)."""
        self._audit_venue_refusal(order, ack, reason)
        raise SafetyViolation(reason)

    def _to_unknown(
        self, order: Order, *, reason: str, ack: BrokerAck | None
    ) -> ExecutionResult:
        """The dangerous path: we do not know what happened.

        Both the order and the reservation move to UNKNOWN, which blocks every
        subsequent submission until an operator reconciles (INVARIANTS 5, 12).
        The key stays claimed, so a retry cannot smuggle a second copy through.
        """
        order.mark_unknown(reason=reason)
        self._dedupe.mark_unknown(order.idempotency_key, note=reason)
        self._audit_result(order, ExecutionOutcome.UNKNOWN, ack, AuditOutcome.ERROR)
        return ExecutionResult(
            outcome=ExecutionOutcome.UNKNOWN,
            order=order,
            ack=ack,
            gate=ExecutionGate.EXECUTION,
            reason=reason,
        )

    # -- refusal and bookkeeping -----------------------------------------

    def _refuse(
        self,
        gate: str,
        exc: Exception,
        intent: OrderIntent,
        *,
        release_key: bool = False,
    ) -> ExecutionResult:
        """Record a refusal and return it. Nothing was sent."""
        if release_key:
            # Safe: this is only reached from gates that run before the broker
            # call, so we can prove the venue never saw this key.
            self._dedupe.release_unsent(
                intent.idempotency_key, reason=f"refused at {gate}"
            )
        self._audit.record(
            AuditCategory.ORDER,
            "gateway.refused",
            outcome=AuditOutcome.REFUSED,
            actor=self._identity.principal_id,
            details={
                "gate": gate,
                "error": type(exc).__name__,
                "reason": str(exc),
                "symbol": intent.symbol,
                "strategy_id": intent.strategy_id,
                "idempotency_key": intent.idempotency_key,
            },
        )
        return ExecutionResult(
            outcome=ExecutionOutcome.REFUSED,
            gate=gate,
            reason=str(exc),
        )

    def _audit_result(
        self,
        order: Order,
        outcome: str,
        ack: BrokerAck | None,
        audit_outcome: AuditOutcome,
    ) -> None:
        self._audit.record(
            AuditCategory.ORDER,
            f"gateway.{outcome}",
            outcome=audit_outcome,
            actor=self._identity.principal_id,
            details={
                "order_id": order.order_id,
                "state": order.state.value,
                "symbol": order.symbol,
                "idempotency_key": order.idempotency_key,
                "ack": ack.as_details() if ack else None,
            },
        )

    def _resolve_prices(
        self, intent: OrderIntent, supplied: Mapping[str, Price]
    ) -> Mapping[str, Price]:
        """Prices for the risk engine, unchanged.

        The gateway does not fetch, default, or interpolate a price. A gap stays
        a gap so the risk engine can refuse -- inventing a price here would turn
        a fail-closed check into a fail-open one.
        """
        for symbol, price in supplied.items():
            if not isinstance(price, Price):
                raise TypeError(
                    f"mark price for {symbol} must be a Price, "
                    f"got {type(price).__name__} (INVARIANT 8)"
                )
        return dict(supplied)

    # -- operator actions -------------------------------------------------

    def cancel(self, order: Order, *, operator: Principal) -> BrokerAck:
        """Cancel an order, then reconcile what the venue actually did.

        Needs no risk approval: cancelling can only reduce exposure. It does
        need the gateway lock, because it can book a fill.

        A cancel acknowledgement is not an outcome. The venue may have filled
        the order while the request was in flight, so this asks the venue for
        its authoritative state afterwards and applies that. A fill that won
        the race is booked exactly once and the order reaches FILLED, instead
        of being quietly retired as CANCELED with a position nobody recorded.

        Returns the acknowledgement to the *cancel request*, because that is
        the question the caller asked -- whether the cancel went through. The
        authoritative outcome is ``order.state``, which this has just updated.

        Raises:
            UnauthorizedAction: ``operator`` may not cancel orders.
            SafetyViolation: the order is UNKNOWN (resolve it first) or already
                terminal -- refused before anything is sent -- or the venue's
                answer to the follow-up query was contradictory, in which case
                the cancel was sent but the order is left as it was. Every
                refusal is audited before it is raised (INVARIANT 13).
        """
        with self._lock:
            return self._cancel_locked(order, operator)

    def _cancel_locked(self, order: Order, operator: Principal) -> BrokerAck:
        authorize(operator, Action.CANCEL_ORDER)

        # An UNKNOWN order must not be cancelled. We do not know whether the
        # venue holds it, so "cancel" would be a guess, and the answer to the
        # follow-up query would have nothing to reconcile against.
        # resolve_unknown() is the only way out of that state (INVARIANT 5).
        if order.is_unknown:
            self._refuse_cancel(
                order,
                operator,
                f"order {order.order_id} is UNKNOWN; "
                "resolve it before trying to cancel it",
            )

        state = order.state
        if state.is_terminal:
            # Also what makes a second cancel refuse cleanly rather than send
            # a pointless request: the first one left the order CANCELED.
            self._refuse_cancel(
                order,
                operator,
                f"order {order.order_id} is already {state.value}; nothing to cancel",
            )

        # INVARIANT 13: the intent is recorded before the request leaves, so a
        # crash in flight still leaves evidence that we asked.
        self._audit.record(
            AuditCategory.ORDER,
            "gateway.cancel_requested",
            outcome=AuditOutcome.ALLOWED,
            actor=operator.principal_id,
            details={
                "order_id": order.order_id,
                "state": state.value,
                "idempotency_key": order.idempotency_key,
            },
        )

        cancel_ack = self._broker.cancel_order(order)
        # Only a definitive acknowledgement proves the request landed. UNCERTAIN
        # does not, and CANCELED will not be declared on a guess.
        cancel_succeeded = cancel_ack.outcome is AckOutcome.ACCEPTED

        # The venue decides what happened, not its acknowledgement. Any fill
        # that beat the cancel is booked here, exactly once: _apply_fetched_state
        # applies the delta against what is already on the books, so a fill we
        # had already recorded produces no second position and no second P&L.
        fetched = self._broker.fetch_order_state(order)
        self._apply_fetched_state(
            order,
            fetched,
            via_reconciliation=False,
            after_cancel=cancel_succeeded,
        )

        # CANCELED only when nothing else got there first. A fetched ACCEPTED
        # means the order is still working at the venue whatever the cancel
        # acknowledgement claimed, and retiring it here would hide an order
        # that can still fill. A fetched FILLED has already left the order
        # terminal, so is_open closes that case too.
        if (
            cancel_succeeded
            and fetched.outcome is not AckOutcome.ACCEPTED
            and order.is_open
        ):
            order.transition_to(
                OrderState.CANCELED, reason="canceled at operator request"
            )

        self._audit.record(
            AuditCategory.ORDER,
            "gateway.cancel_completed",
            outcome=AuditOutcome.ALLOWED if cancel_succeeded else AuditOutcome.REFUSED,
            actor=operator.principal_id,
            details={
                "order_id": order.order_id,
                "state": order.state.value,
                "idempotency_key": order.idempotency_key,
                "ack": cancel_ack.as_details(),
                "venue_state": fetched.as_details(),
            },
        )
        # A cancel that retired the order (or found it already filled) closes
        # its reservation out, the same as the sync path. See
        # _settle_reservation_if_terminal for why this preserves INVARIANT 12.
        self._settle_reservation_if_terminal(
            order, note="settled via cancel"
        )
        return cancel_ack

    def sync_order(self, order: Order, *, operator: Principal) -> BrokerAck:
        """Ask the venue what an open order has become, and apply the answer.

        The route a fill takes when nobody was watching. ``place_order`` reports
        what happened at the moment of placement; an order that rests at the
        venue and fills ten minutes later has no other way into the portfolio.
        Without this a resting order stays ACCEPTED forever and its fill is
        invisible to the position ledger, to the cost basis, and therefore to
        the daily-loss limit.

        The venue's answer is a *cumulative* snapshot, and only the delta
        against what is already booked reaches the portfolio. Syncing the same
        order twice books nothing the second time, so a poller may run as often
        as it likes without inventing a position.

        Authorised by :data:`Action.RECONCILE` rather than ``CANCEL_ORDER``:
        this asks the venue what is true instead of telling it to do anything.
        That is also why ``SYSTEM`` holds it -- the periodic poller a later
        stage adds runs unattended, and an operator will not be awake for it.

        Takes the gateway lock, because applying the answer can move the
        portfolio and the risk ledger, and a concurrent ``submit`` must not
        weigh its limits against a position changing underneath it.

        Returns the venue's acknowledgement. The authoritative outcome is
        ``order.state``, which this has just updated.

        Raises:
            UnauthorizedAction: ``operator`` may not reconcile.
            SafetyViolation: the order is UNKNOWN or already terminal -- both
                refused before the venue is asked -- or the venue's answer was
                contradictory, in which case the order is left exactly as it
                was. Every refusal is audited before it is raised
                (INVARIANT 13).
        """
        with self._lock:
            return self._sync_locked(order, operator)

    def _sync_locked(self, order: Order, operator: Principal) -> BrokerAck:
        authorize(operator, Action.RECONCILE)

        # An UNKNOWN order is not syncable, though the mechanics would work.
        # Leaving UNKNOWN is a deliberate operator act with its own audit trail
        # and its own reservation bookkeeping (INVARIANT 5); resolve_unknown is
        # that act, and a routine poll must not perform it silently.
        if order.is_unknown:
            self._refuse_sync(
                order,
                operator,
                f"order {order.order_id} is UNKNOWN; resolve it rather than syncing it",
            )

        state = order.state
        if state.is_terminal:
            # Nothing a venue says about a finished order is news, and
            # _apply_fetched_state refuses a fill against a terminal order
            # anyway -- including a re-report of the very fill that finished it.
            # Refusing here stops an ordinary poll from looking like a
            # contradiction the venue never intended.
            self._refuse_sync(
                order,
                operator,
                f"order {order.order_id} is already {state.value}; nothing to sync",
            )

        # Reading changes nothing at the venue, so INVARIANT 13 does not demand
        # this record. It is here so the trail can tell a poll that never
        # returned from a poll that never happened.
        self._audit.record(
            AuditCategory.RECONCILIATION,
            "gateway.sync_requested",
            outcome=AuditOutcome.ALLOWED,
            actor=operator.principal_id,
            details={
                "order_id": order.order_id,
                "state": state.value,
                "idempotency_key": order.idempotency_key,
            },
        )

        ack = self._broker.fetch_order_state(order)
        # via_reconciliation stays False: that flag exists to let an order leave
        # UNKNOWN, and this path refused every UNKNOWN order above. An UNCERTAIN
        # answer is refused in there rather than marking the order UNKNOWN -- a
        # read that failed is not the same as an order we cannot account for,
        # and latching the system on every unanswered poll would make a network
        # blip indistinguishable from a lost order.
        self._apply_fetched_state(order, ack, via_reconciliation=False)

        self._audit.record(
            AuditCategory.RECONCILIATION,
            "gateway.sync_completed",
            outcome=AuditOutcome.ALLOWED,
            actor=operator.principal_id,
            details={
                "order_id": order.order_id,
                "state": order.state.value,
                "previous_state": state.value,
                "idempotency_key": order.idempotency_key,
                "ack": ack.as_details(),
            },
        )
        # A sync that learned the order is finished must close its reservation
        # too, exactly as _settle does on the submit path. Safe: it only moves a
        # RESERVED/SUBMITTED key to SETTLED, which frees nothing (INVARIANT 12).
        self._settle_reservation_if_terminal(
            order, note=f"settled via sync: venue {ack.outcome.value}"
        )
        return ack

    def _settle_reservation_if_terminal(self, order: Order, *, note: str) -> None:
        """Settle the order's reservation once the order itself is terminal.

        ``sync_order`` and ``cancel`` can drive an order to a terminal state
        (FILLED/REJECTED/CANCELED/EXPIRED) by learning what the venue actually
        did. When they do, the reservation must close out too, exactly as
        :meth:`_settle` does on the submit path -- otherwise a finished order's
        key is left in SUBMITTED and :meth:`IdempotencyRegistry.in_flight`
        reports it as live forever.

        **Why this does not weaken INVARIANT 12.** ``SETTLED`` is as un-reusable
        as ``SUBMITTED``: :meth:`IdempotencyRegistry.reserve` refuses any key
        that already has a reservation in *any* state, and ``SETTLED`` has no
        successor in the transition table, so the key can never be freed for a
        duplicate. Moving SUBMITTED -> SETTLED is a truthful relabelling of a key
        that is already permanently claimed; it frees nothing and permits no
        retry.

        **Why it is safe against UNKNOWN.** It never touches an UNKNOWN
        reservation -- leaving UNKNOWN is :meth:`resolve_unknown`'s job (the
        transition table forbids UNKNOWN -> SETTLED, so skipping UNKNOWN here is
        also what keeps the state machine legal). And an UNKNOWN *order* is not
        terminal, so for an UNKNOWN order this returns at the first guard. The
        reservation lookup is a no-op when the key is already SETTLED, so this is
        safe to call unconditionally.
        """
        if not order.state.is_terminal:
            return
        reservation = self._dedupe.get(order.idempotency_key)
        if reservation is None:
            return
        if reservation.state in (
            ReservationState.RESERVED,
            ReservationState.SUBMITTED,
        ):
            self._dedupe.mark_settled(order.idempotency_key, note=note)

    def _refuse_cancel(
        self, order: Order, operator: Principal, reason: str
    ) -> NoReturn:
        """Audit a refused cancel, then refuse it. Nothing was sent (INVARIANT 13)."""
        self._refuse_order_action(
            order,
            operator,
            reason,
            category=AuditCategory.ORDER,
            action="gateway.cancel_refused",
        )

    def _refuse_sync(
        self, order: Order, operator: Principal, reason: str
    ) -> NoReturn:
        """Audit a refused sync, then refuse it. The venue was never asked."""
        self._refuse_order_action(
            order,
            operator,
            reason,
            category=AuditCategory.RECONCILIATION,
            action="gateway.sync_refused",
        )

    def _refuse_order_action(
        self,
        order: Order,
        operator: Principal,
        reason: str,
        *,
        category: AuditCategory,
        action: str,
    ) -> NoReturn:
        """Record an operator's request that was refused before it was attempted.

        These refusals all happen before anything leaves the process, so the
        record is the whole story: the order is exactly as it was, and the venue
        never heard about it.
        """
        self._audit.record(
            category,
            action,
            outcome=AuditOutcome.REFUSED,
            actor=operator.principal_id,
            details={
                "order_id": order.order_id,
                "state": order.state.value,
                "idempotency_key": order.idempotency_key,
                "reason": reason,
            },
        )
        raise SafetyViolation(reason)

    def resolve_unknown(self, order: Order, *, operator: Principal) -> BrokerAck:
        """Ask the venue what happened to an UNKNOWN order and record the answer.

        This is the only way out of the UNKNOWN state, and it requires the venue
        to speak. There is no timeout after which an unknown order is assumed
        dead: assuming is how you end up with two positions.

        Takes the gateway lock for the same reason ``submit``, ``cancel`` and
        ``sync_order`` do: discovering what an unknown order did can book a
        fill, and every path that moves the portfolio has to be serialised
        against every other one. The individual ledgers guard their own writes,
        but a ``submit`` weighing its risk limits reads several of them in turn,
        and a fill landing between those reads would be judged against a
        position that no longer exists.
        """
        with self._lock:
            return self._resolve_unknown_locked(order, operator)

    def _resolve_unknown_locked(
        self, order: Order, operator: Principal
    ) -> BrokerAck:
        authorize(operator, Action.RECONCILE)
        if not order.is_unknown:
            raise SafetyViolation(
                f"order {order.order_id} is in state {order.state.value}, "
                "not UNKNOWN; nothing to resolve"
            )

        ack = self._broker.fetch_order_state(order)
        if ack.outcome is AckOutcome.UNCERTAIN:
            self._audit.record(
                AuditCategory.RECONCILIATION,
                "gateway.unknown_unresolved",
                outcome=AuditOutcome.ERROR,
                actor=operator.principal_id,
                details={"order_id": order.order_id, "ack": ack.as_details()},
            )
            return ack

        # Use _apply_fetched_state for consistent cumulative-to-delta handling.
        # This prevents double-booking when the order had prior fills before
        # becoming UNKNOWN. The method handles both order state update and
        # portfolio delta recording internally.
        self._apply_fetched_state(order, ack, via_reconciliation=True)

        # Clear the reservation's UNKNOWN state, but only if it's actually UNKNOWN.
        # The order might have been marked UNKNOWN while the reservation stayed
        # SUBMITTED or was already SETTLED.
        reservation = self._dedupe.get(order.idempotency_key)
        if reservation and reservation.state is ReservationState.UNKNOWN:
            self._dedupe.resolve_unknown(
                order.idempotency_key, resolution=f"venue reported {ack.outcome.value}"
            )

        self._audit.record(
            AuditCategory.RECONCILIATION,
            "gateway.unknown_resolved",
            outcome=AuditOutcome.ALLOWED,
            actor=operator.principal_id,
            details={
                "order_id": order.order_id,
                "resolved_state": order.state.value,
                "ack": ack.as_details(),
            },
        )
        return ack
