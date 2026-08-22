"""Authorization rules and execution capability tokens.

INVARIANT 3: strategy code cannot directly execute an order.

Two layers, because a permission matrix alone is only as good as the discipline
of the code that consults it:

1. **Role/action matrix** -- :func:`authorize` answers "may this principal do
   this?". ``Role.STRATEGY`` is never granted ``Action.EXECUTE_ORDER``, and
   :func:`assert_matrix_invariants` re-checks that at import time so a careless
   edit to the table fails immediately rather than at 3am.

2. **Capability token** -- :class:`ExecutionToken` is the thing a broker
   adapter actually demands. Tokens are minted only by a principal holding
   ``Role.EXECUTION_GATEWAY``, are bound to one order, carry an expiry, and are
   single-use. Strategy code holds a ``STRATEGY`` principal, so it cannot mint
   one; and because tokens are single-use, a leaked token cannot be replayed.

**Honest limitation.** Python provides no capability isolation. Code running
in this process can reach ``trading.core.authz._MINT_KEY`` or construct a
``Principal(role=Role.EXECUTION_GATEWAY)``. These controls make the bypass
*deliberate, visible, and greppable* -- they do not make it impossible. Real
isolation requires a process or network boundary, which arrives with the
service split in a later stage. See docs/SAFETY.md.
"""

from __future__ import annotations

import datetime as _dt
import threading
import uuid
from dataclasses import dataclass
from enum import Enum
from typing import Final, Mapping

from .clock import Clock
from .errors import StrategyExecutionForbidden, UnauthorizedAction

__all__ = [
    "Role",
    "Action",
    "Principal",
    "PERMISSIONS",
    "authorize",
    "is_authorized",
    "ExecutionToken",
    "mint_execution_token",
    "DEFAULT_TOKEN_TTL_SECONDS",
    "assert_matrix_invariants",
]


class Role(Enum):
    """Who is acting."""

    #: Signal generation. May propose, may never execute.
    STRATEGY = "strategy"
    #: Pre-trade risk evaluation.
    RISK_MANAGER = "risk_manager"
    #: The single component permitted to execute. Mints execution tokens.
    EXECUTION_GATEWAY = "execution_gateway"
    #: A human operator.
    OPERATOR = "operator"
    #: Read-only access to the audit trail.
    AUDITOR = "auditor"
    #: Internal machinery (reconciliation, schedulers).
    SYSTEM = "system"


class Action(Enum):
    """What is being attempted."""

    PROPOSE_ORDER = "propose_order"
    APPROVE_ORDER = "approve_order"
    EXECUTE_ORDER = "execute_order"
    CANCEL_ORDER = "cancel_order"
    CHANGE_MODE = "change_mode"
    ENGAGE_KILL_SWITCH = "engage_kill_switch"
    RELEASE_KILL_SWITCH = "release_kill_switch"
    RESET_BREAKER = "reset_breaker"
    READ_AUDIT = "read_audit"
    RECONCILE = "reconcile"


#: The permission matrix. Deny by default: an action absent from a role's set
#: is refused.
PERMISSIONS: Final[Mapping[Role, frozenset[Action]]] = {
    Role.STRATEGY: frozenset(
        {
            Action.PROPOSE_ORDER,
            # Anyone may stop the system. Nobody stops a stop.
            Action.ENGAGE_KILL_SWITCH,
        }
    ),
    Role.RISK_MANAGER: frozenset(
        {
            Action.APPROVE_ORDER,
            Action.ENGAGE_KILL_SWITCH,
            Action.READ_AUDIT,
        }
    ),
    Role.EXECUTION_GATEWAY: frozenset(
        {
            Action.EXECUTE_ORDER,
            Action.CANCEL_ORDER,
            Action.ENGAGE_KILL_SWITCH,
            Action.READ_AUDIT,
        }
    ),
    Role.OPERATOR: frozenset(
        {
            Action.CANCEL_ORDER,
            Action.CHANGE_MODE,
            Action.ENGAGE_KILL_SWITCH,
            Action.RELEASE_KILL_SWITCH,
            Action.RESET_BREAKER,
            Action.READ_AUDIT,
            Action.RECONCILE,
        }
    ),
    Role.AUDITOR: frozenset({Action.READ_AUDIT}),
    Role.SYSTEM: frozenset(
        {
            Action.ENGAGE_KILL_SWITCH,
            Action.READ_AUDIT,
            Action.RECONCILE,
        }
    ),
}


@dataclass(frozen=True, slots=True)
class Principal:
    """An identified actor.

    In Stage 1 a principal is constructed locally. From the stage that adds an
    HTTP boundary onward it must be derived from an authenticated session --
    never from a request body.
    """

    principal_id: str
    role: Role

    def __post_init__(self) -> None:
        if not isinstance(self.principal_id, str) or not self.principal_id.strip():
            raise ValueError("principal_id must be a non-empty string")
        if not isinstance(self.role, Role):
            raise TypeError("role must be a Role")

    def __str__(self) -> str:
        return f"{self.principal_id}[{self.role.value}]"


def is_authorized(principal: Principal, action: Action) -> bool:
    if not isinstance(principal, Principal):
        raise TypeError("principal must be a Principal")
    if not isinstance(action, Action):
        raise TypeError("action must be an Action")
    return action in PERMISSIONS.get(principal.role, frozenset())


def authorize(principal: Principal, action: Action) -> None:
    """Raise unless ``principal`` may perform ``action``.

    Strategy attempts to execute get a dedicated exception type so that the
    single most dangerous violation is unmistakable in logs.
    """
    if is_authorized(principal, action):
        return
    if principal.role is Role.STRATEGY and action is Action.EXECUTE_ORDER:
        raise StrategyExecutionForbidden(
            f"{principal} attempted to execute an order directly. Strategy code "
            "may only propose intents; execution belongs to the gateway "
            "(INVARIANT 3)."
        )
    raise UnauthorizedAction(f"{principal} is not permitted to {action.value}")


def assert_matrix_invariants() -> None:
    """Fail loudly if the permission matrix violates a structural rule.

    Called at import time. A table this important should not depend on someone
    remembering to run the right test.
    """
    strategy = PERMISSIONS[Role.STRATEGY]
    if Action.EXECUTE_ORDER in strategy:
        raise AssertionError(
            "PERMISSIONS grants EXECUTE_ORDER to STRATEGY, violating INVARIANT 3"
        )
    if Action.APPROVE_ORDER in strategy:
        raise AssertionError(
            "PERMISSIONS grants APPROVE_ORDER to STRATEGY: a strategy must not "
            "approve its own risk checks (INVARIANT 4)"
        )
    executors = {role for role, actions in PERMISSIONS.items() if Action.EXECUTE_ORDER in actions}
    if executors != {Role.EXECUTION_GATEWAY}:
        raise AssertionError(
            f"EXECUTE_ORDER must be held by EXECUTION_GATEWAY alone, found {executors}"
        )
    releasers = {
        role for role, actions in PERMISSIONS.items() if Action.RELEASE_KILL_SWITCH in actions
    }
    if releasers != {Role.OPERATOR}:
        raise AssertionError(
            f"RELEASE_KILL_SWITCH must be held by OPERATOR alone, found {releasers}"
        )
    for role in Role:
        if role not in PERMISSIONS:
            raise AssertionError(f"PERMISSIONS has no entry for role {role}")


# --------------------------------------------------------------------------
# Execution capability tokens
# --------------------------------------------------------------------------

#: Module-private minting key. Reachable by determined in-process code; see the
#: "Honest limitation" note in this module's docstring.
_MINT_KEY: Final = object()

DEFAULT_TOKEN_TTL_SECONDS: Final = 30


class ExecutionToken:
    """A single-use capability to execute one specific order.

    Bound to an order id and idempotency key, so a token issued for one order
    cannot be used to execute a different one. Expires, so a token that leaks
    into a queue cannot be replayed an hour later.
    """

    __slots__ = (
        "_order_id",
        "_idempotency_key",
        "_issued_at",
        "_ttl_seconds",
        "_issuer",
        "_token_id",
        "_consumed",
        "_lock",
    )

    def __init__(
        self,
        *,
        order_id: str,
        idempotency_key: str,
        issued_at: _dt.datetime,
        ttl_seconds: int,
        issuer: str,
        mint_key: object,
    ) -> None:
        if mint_key is not _MINT_KEY:
            raise UnauthorizedAction(
                "ExecutionToken cannot be constructed directly; it must be "
                "minted by a principal holding Role.EXECUTION_GATEWAY via "
                "mint_execution_token() (INVARIANT 3)"
            )
        self._order_id = order_id
        self._idempotency_key = idempotency_key
        self._issued_at = issued_at
        self._ttl_seconds = ttl_seconds
        self._issuer = issuer
        self._token_id = uuid.uuid4().hex
        self._consumed = False
        self._lock = threading.Lock()

    @property
    def order_id(self) -> str:
        return self._order_id

    @property
    def idempotency_key(self) -> str:
        return self._idempotency_key

    @property
    def token_id(self) -> str:
        return self._token_id

    @property
    def issuer(self) -> str:
        return self._issuer

    @property
    def issued_at(self) -> _dt.datetime:
        return self._issued_at

    @property
    def is_consumed(self) -> bool:
        with self._lock:
            return self._consumed

    def is_expired(self, clock: Clock) -> bool:
        elapsed = (clock.now() - self._issued_at).total_seconds()
        return elapsed > self._ttl_seconds or elapsed < 0

    def consume(self, *, order_id: str, clock: Clock) -> None:
        """Spend the token for ``order_id``, or raise.

        Atomic: two threads racing to spend the same token produce exactly one
        success. This is one of the layers behind INVARIANT 12.
        """
        if order_id != self._order_id:
            raise UnauthorizedAction(
                f"execution token was issued for order {self._order_id}, "
                f"not {order_id}"
            )
        if self.is_expired(clock):
            raise UnauthorizedAction(
                f"execution token for order {self._order_id} has expired "
                f"(ttl {self._ttl_seconds}s)"
            )
        with self._lock:
            if self._consumed:
                raise UnauthorizedAction(
                    f"execution token for order {self._order_id} has already "
                    "been used; tokens are single-use to prevent replay"
                )
            self._consumed = True

    def __repr__(self) -> str:
        return (
            f"ExecutionToken(order_id={self._order_id!r}, "
            f"token_id={self._token_id[:8]}..., consumed={self._consumed})"
        )

    def __reduce__(self):
        raise TypeError(
            "ExecutionToken cannot be pickled: a serialised capability could be "
            "replayed from another process"
        )


def mint_execution_token(
    principal: Principal,
    *,
    order_id: str,
    idempotency_key: str,
    clock: Clock,
    ttl_seconds: int = DEFAULT_TOKEN_TTL_SECONDS,
) -> ExecutionToken:
    """Mint a token, or raise if ``principal`` may not execute orders."""
    authorize(principal, Action.EXECUTE_ORDER)
    if not order_id or not isinstance(order_id, str):
        raise ValueError("order_id must be a non-empty string")
    if not idempotency_key or not isinstance(idempotency_key, str):
        raise ValueError("idempotency_key must be a non-empty string")
    if not isinstance(ttl_seconds, int) or isinstance(ttl_seconds, bool) or ttl_seconds <= 0:
        raise ValueError("ttl_seconds must be a positive int")
    return ExecutionToken(
        order_id=order_id,
        idempotency_key=idempotency_key,
        issued_at=clock.now(),
        ttl_seconds=ttl_seconds,
        issuer=principal.principal_id,
        mint_key=_MINT_KEY,
    )


assert_matrix_invariants()
