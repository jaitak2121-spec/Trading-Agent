"""Exception hierarchy for the safety core.

Design rules:

* Every safety refusal raises a subclass of :class:`SafetyViolation`, never a
  bare ``Exception``, so that a caller can never accidentally swallow a safety
  refusal with a narrow ``except``.
* :class:`SafetyViolation` deliberately does NOT inherit from anything a
  generic retry/ignore wrapper would typically catch beyond ``Exception``.
* Exception messages must never contain secret material. Callers pass
  identifiers, not credentials.
"""

from __future__ import annotations

__all__ = [
    "TradingError",
    "ConfigurationError",
    "PrecisionError",
    "CurrencyMismatch",
    "SafetyViolation",
    "LiveTradingDisabled",
    "UnauthorizedAction",
    "KillSwitchEngaged",
    "CircuitBreakerOpen",
    "RiskLimitExceeded",
    "InvalidModeTransition",
    "InvalidOrderTransition",
    "DuplicateOrderRejected",
    "UnknownOrderStateBlocked",
    "ReconciliationRequired",
    "PositionMismatch",
    "StaleMarketData",
    "StrategyExecutionForbidden",
]


class TradingError(Exception):
    """Base class for every error raised by this system."""


class ConfigurationError(TradingError):
    """Configuration is missing, malformed, or internally inconsistent."""


class PrecisionError(TradingError):
    """A numeric value carries more precision than its type permits.

    Raised instead of silently rounding. Silent rounding of monetary values is
    treated as a defect, not a convenience.
    """


class CurrencyMismatch(TradingError):
    """An arithmetic operation mixed two different currencies or assets."""


class SafetyViolation(TradingError):
    """Base class for a refusal issued by a safety control.

    Catching this type means "a safety control said no". Production code must
    not catch it in order to continue; it may catch it only to report, log, or
    halt.
    """


class LiveTradingDisabled(SafetyViolation):
    """INVARIANT 1 / 2: live trading is off, so no live order may execute."""


class UnauthorizedAction(SafetyViolation):
    """INVARIANT 3: the actor is not permitted to perform this action."""


class StrategyExecutionForbidden(UnauthorizedAction):
    """INVARIANT 3: strategy code attempted to execute an order directly."""


class KillSwitchEngaged(SafetyViolation):
    """INVARIANT 10: the kill switch is engaged, so no new order is allowed."""


class CircuitBreakerOpen(SafetyViolation):
    """A circuit breaker is open; the guarded operation is refused."""


class RiskLimitExceeded(SafetyViolation):
    """INVARIANT 7: a loss, exposure, or rate limit would be breached."""

    def __init__(self, limit_name: str, message: str) -> None:
        super().__init__(f"{limit_name}: {message}")
        self.limit_name = limit_name


class InvalidModeTransition(SafetyViolation):
    """INVARIANT 11: the requested trading-mode transition is not allowed."""


class InvalidOrderTransition(SafetyViolation):
    """An order state transition is not permitted by the state machine."""


class DuplicateOrderRejected(SafetyViolation):
    """INVARIANT 12: an order with this idempotency key already exists."""


class UnknownOrderStateBlocked(SafetyViolation):
    """INVARIANT 5: an order is in UNKNOWN state; new orders are blocked."""


class ReconciliationRequired(SafetyViolation):
    """Manual or automated reconciliation must complete before trading."""


class PositionMismatch(SafetyViolation):
    """INVARIANT 6: local and broker positions disagree."""


class StaleMarketData(SafetyViolation):
    """A decision was asked for using market data too old to support it.

    A :class:`SafetyViolation` rather than a plain error because acting on a
    price of unknown age is a safety failure, not a data inconvenience.

    Raised by callers that have no gate to refuse on their behalf -- chiefly
    advisory code. On the execution path a stale quote is instead reported as an
    *absent* mark price by
    :class:`~trading.core.marketdata.FreshMarkPrices`, which the risk engine
    already refuses on. Both routes fail closed; only the loudness differs.
    """
