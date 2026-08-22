"""Trading-mode state machine.

INVARIANT 11: invalid trading-mode transitions are rejected.
INVARIANT 2 (partly): no order can execute when live trading is disabled.

The mode is the coarsest safety control in the system: it decides whether
execution is possible at all. It is a state *machine* rather than a mutable
enum field so that reaching ``LIVE`` requires walking a path an operator can
audit, not flipping a variable.

The transition table encodes one deliberate asymmetry: getting *closer* to
live is hard, getting *away* from live is always easy. ``DISABLED -> LIVE``
is rejected -- a system must demonstrate it works in ``PAPER`` first -- while
every state can reach ``HALTED`` in one step.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass
from enum import Enum
from typing import Final, Mapping

from .audit import AuditCategory, AuditLog, AuditOutcome
from .config import TradingConfig
from .errors import InvalidModeTransition, LiveTradingDisabled

__all__ = ["TradingMode", "ModeSnapshot", "TradingModeMachine", "ALLOWED_TRANSITIONS"]


class TradingMode(Enum):
    """Operating mode of the trading system."""

    #: Default. Nothing executes. Strategies may run and produce intents.
    DISABLED = "disabled"
    #: Historical simulation. No broker contact of any kind.
    BACKTEST = "backtest"
    #: Live market data, simulated fills. No real money.
    PAPER = "paper"
    #: Real orders against a real venue. NOT IMPLEMENTED IN STAGE 1.
    LIVE = "live"
    #: Emergency stop. Reachable from anywhere in one step; exits only to
    #: DISABLED, and only by explicit operator action.
    HALTED = "halted"

    @property
    def allows_execution(self) -> bool:
        """Whether orders may be executed at all in this mode."""
        return self in (TradingMode.PAPER, TradingMode.LIVE)

    @property
    def is_live(self) -> bool:
        return self is TradingMode.LIVE


#: The complete transition table. Anything absent is forbidden.
ALLOWED_TRANSITIONS: Final[Mapping[TradingMode, frozenset[TradingMode]]] = {
    TradingMode.DISABLED: frozenset(
        {TradingMode.BACKTEST, TradingMode.PAPER, TradingMode.HALTED}
    ),
    TradingMode.BACKTEST: frozenset(
        {TradingMode.DISABLED, TradingMode.PAPER, TradingMode.HALTED}
    ),
    TradingMode.PAPER: frozenset(
        {
            TradingMode.DISABLED,
            TradingMode.BACKTEST,
            TradingMode.LIVE,
            TradingMode.HALTED,
        }
    ),
    TradingMode.LIVE: frozenset(
        {TradingMode.PAPER, TradingMode.DISABLED, TradingMode.HALTED}
    ),
    # Deliberately narrow: leaving HALTED requires a conscious step through
    # DISABLED, which forces re-arming rather than snapping back to LIVE.
    TradingMode.HALTED: frozenset({TradingMode.DISABLED}),
}


@dataclass(frozen=True, slots=True)
class ModeSnapshot:
    """Immutable view of the machine, safe to log or serialise."""

    mode: TradingMode
    live_authorized_by_config: bool
    transitions: int

    def as_details(self) -> dict[str, object]:
        return {
            "mode": self.mode.value,
            "live_authorized_by_config": self.live_authorized_by_config,
            "transitions": self.transitions,
        }


class TradingModeMachine:
    """Guards mode transitions and answers "may anything execute right now?".

    Thread-safe. Every attempt -- allowed or refused -- is audited before the
    state changes, so a refused transition leaves evidence too.
    """

    def __init__(
        self,
        config: TradingConfig,
        audit: AuditLog,
        *,
        initial: TradingMode = TradingMode.DISABLED,
    ) -> None:
        if not isinstance(config, TradingConfig):
            raise TypeError("config must be a TradingConfig")
        if not isinstance(initial, TradingMode):
            raise TypeError("initial must be a TradingMode")
        if initial is TradingMode.LIVE:
            # Constructing straight into LIVE would bypass the whole point of
            # the machine.
            raise InvalidModeTransition(
                "a TradingModeMachine cannot start in LIVE; reach it by "
                "transitioning through PAPER"
            )
        self._config = config
        self._audit = audit
        self._mode = initial
        self._transitions = 0
        self._lock = threading.RLock()

    # -- state -------------------------------------------------------------
    @property
    def mode(self) -> TradingMode:
        with self._lock:
            return self._mode

    def snapshot(self) -> ModeSnapshot:
        with self._lock:
            return ModeSnapshot(
                mode=self._mode,
                live_authorized_by_config=self._config.is_live_authorized,
                transitions=self._transitions,
            )

    # -- transitions -------------------------------------------------------
    def can_transition_to(self, target: TradingMode) -> bool:
        with self._lock:
            return target in ALLOWED_TRANSITIONS[self._mode]

    def transition_to(
        self,
        target: TradingMode,
        *,
        actor: str,
        reason: str = "",
    ) -> TradingMode:
        """Move to ``target`` or raise.

        Raises :class:`InvalidModeTransition` if the table forbids the edge,
        or :class:`LiveTradingDisabled` if the target is ``LIVE`` and the
        configuration has not authorised live trading.
        """
        if not isinstance(target, TradingMode):
            raise TypeError("target must be a TradingMode")

        with self._lock:
            current = self._mode
            details = {
                "from": current.value,
                "to": target.value,
                "reason": reason,
            }

            if target is current:
                # A self-transition is a no-op rather than an error: idempotent
                # "ensure we are in PAPER" calls are legitimate.
                self._audit.record(
                    AuditCategory.MODE,
                    "mode_transition_noop",
                    outcome=AuditOutcome.INFO,
                    actor=actor,
                    details=details,
                )
                return current

            if target not in ALLOWED_TRANSITIONS[current]:
                self._audit.record(
                    AuditCategory.MODE,
                    "mode_transition_refused",
                    outcome=AuditOutcome.REFUSED,
                    actor=actor,
                    details={**details, "cause": "transition_not_allowed"},
                )
                allowed = sorted(m.value for m in ALLOWED_TRANSITIONS[current])
                raise InvalidModeTransition(
                    f"{current.value} -> {target.value} is not an allowed transition; "
                    f"from {current.value} you may go to {allowed}"
                )

            if target is TradingMode.LIVE and not self._config.is_live_authorized:
                self._audit.record(
                    AuditCategory.MODE,
                    "mode_transition_refused",
                    outcome=AuditOutcome.REFUSED,
                    actor=actor,
                    details={**details, "cause": "live_not_authorized_by_config"},
                )
                raise LiveTradingDisabled(
                    "cannot enter LIVE: configuration does not authorise live "
                    "trading (INVARIANT 1). Both live_trading=true and the exact "
                    "live_confirmation phrase are required."
                )

            self._audit.record(
                AuditCategory.MODE,
                "mode_transition",
                outcome=AuditOutcome.ALLOWED,
                actor=actor,
                details=details,
            )
            self._mode = target
            self._transitions += 1
            return target

    def halt(self, *, actor: str, reason: str) -> TradingMode:
        """Emergency stop. Reachable from every state in one step."""
        return self.transition_to(TradingMode.HALTED, actor=actor, reason=reason)

    # -- gates -------------------------------------------------------------
    def require_execution_allowed(self) -> TradingMode:
        """Raise unless the current mode permits order execution."""
        with self._lock:
            mode = self._mode
        if not mode.allows_execution:
            raise LiveTradingDisabled(
                f"order execution is not permitted in mode {mode.value}; "
                "execution requires PAPER or LIVE"
            )
        return mode

    def require_live_allowed(self) -> None:
        """Raise unless live execution is permitted right now.

        Belt and braces: checks BOTH the mode and the configuration. Either one
        alone would be a single point of failure.
        """
        with self._lock:
            mode = self._mode
        if mode is not TradingMode.LIVE:
            raise LiveTradingDisabled(
                f"live execution requires mode LIVE, current mode is {mode.value}"
            )
        if not self._config.is_live_authorized:
            raise LiveTradingDisabled(
                "live execution requires configuration authorisation "
                "(INVARIANT 1); refusing even though mode is LIVE"
            )
