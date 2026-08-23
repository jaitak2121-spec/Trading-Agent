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

import threading
from typing import Mapping

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
from .dedupe import IdempotencyRegistry
from .errors import SafetyViolation, UnauthorizedAction
from .killswitch import KillSwitch
from .modes import TradingModeMachine
from .money import Price
from .orders import Order, OrderIntent, OrderState, OrderStore
from .reconciliation import PositionLedger, ReconciliationGate
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
        positions: PositionLedger,
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
            self._positions.apply_fill(order.symbol, order.side, ack.filled_quantity)
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
        """Cancel an order. Needs no risk approval: it can only reduce exposure."""
        authorize(operator, Action.CANCEL_ORDER)
        ack = self._broker.cancel_order(order)
        self._audit.record(
            AuditCategory.ORDER,
            "gateway.cancel_requested",
            outcome=AuditOutcome.ALLOWED,
            actor=operator.principal_id,
            details={"order_id": order.order_id, "ack": ack.as_details()},
        )
        return ack

    def resolve_unknown(self, order: Order, *, operator: Principal) -> BrokerAck:
        """Ask the venue what happened to an UNKNOWN order and record the answer.

        This is the only way out of the UNKNOWN state, and it requires the venue
        to speak. There is no timeout after which an unknown order is assumed
        dead: assuming is how you end up with two positions.
        """
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

        if ack.outcome is AckOutcome.REJECTED:
            order.transition_to(
                OrderState.REJECTED,
                reason="venue has no record of this order",
                via_reconciliation=True,
            )
        elif ack.outcome is AckOutcome.FILLED:
            assert ack.filled_quantity is not None and ack.fill_price is not None
            order.apply_fill(
                ack.filled_quantity,
                ack.fill_price,
                reason="fill discovered during reconciliation",
                via_reconciliation=True,
            )
            self._positions.apply_fill(order.symbol, order.side, ack.filled_quantity)
        else:
            order.transition_to(
                OrderState.ACCEPTED,
                reason="order found resting at venue",
                via_reconciliation=True,
            )

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
