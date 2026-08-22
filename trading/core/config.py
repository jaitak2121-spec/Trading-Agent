"""Configuration management.

INVARIANT 1: ``LIVE_TRADING`` defaults to FALSE.

The invariant is upheld by three separate properties:

1. The dataclass field default is ``False``. Constructing
   ``TradingConfig(...)`` with no live-trading argument yields a config that
   forbids live trading.
2. Absent, empty, or whitespace-only environment values resolve to ``False``.
   A *malformed* value raises :class:`ConfigurationError` rather than being
   coerced -- "unparseable" must never resolve to "enabled".
3. Enabling live trading requires TWO independent signals: the boolean flag
   *and* an exact confirmation phrase. A single stray ``TRADING_LIVE=true`` in
   a shell profile or CI variable is not sufficient.

Even a config with ``is_live_authorized`` true does not permit an order. It is
a *necessary* condition consumed by the trading-mode machine and the gateway,
never a sufficient one. See docs/SAFETY.md.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field, replace
from decimal import Decimal
from typing import Any, Final, Mapping

from .errors import ConfigurationError
from .money import USD, Currency, Money, to_decimal
from .secrets import Secret

__all__ = [
    "REQUIRED_LIVE_CONFIRMATION",
    "ENV_PREFIX",
    "RiskConfig",
    "TradingConfig",
    "KNOWN_CURRENCIES",
]

#: The exact phrase that must be supplied as the second live-trading factor.
#: Deliberately long, deliberately shouty, deliberately not something a person
#: types by reflex or copies from a tutorial.
REQUIRED_LIVE_CONFIRMATION: Final = "I_UNDERSTAND_THIS_TRADES_REAL_MONEY"

ENV_PREFIX: Final = "TRADING_"

KNOWN_CURRENCIES: Final[dict[str, Currency]] = {
    "USD": Currency("USD", 2),
    "INR": Currency("INR", 2),
    "USDT": Currency("USDT", 8),
    "BTC": Currency("BTC", 8),
}

_TRUE_TOKENS: Final = frozenset({"true", "yes", "on", "1"})
_FALSE_TOKENS: Final = frozenset({"false", "no", "off", "0", ""})


def _parse_bool(value: Any, *, name: str) -> bool:
    """Parse a permissive boolean. Malformed input raises; it never defaults."""
    if isinstance(value, bool):
        return value
    if value is None:
        return False
    if not isinstance(value, str):
        raise ConfigurationError(f"{name}: expected a boolean or string, got {type(value).__name__}")
    token = value.strip().lower()
    if token in _TRUE_TOKENS:
        return True
    if token in _FALSE_TOKENS:
        return False
    raise ConfigurationError(
        f"{name}: {value!r} is not a recognised boolean. Use 'true' or 'false'. "
        "Refusing to guess, because guessing wrong on a safety flag is unsafe."
    )


def _parse_strict_bool(value: Any, *, name: str) -> bool:
    """Parse a boolean that accepts ONLY 'true'/'false'.

    Used for the live-trading flag. ``1``/``yes``/``on`` are rejected so that
    the value in a deployment manifest is unambiguous to a human reviewer.
    """
    if isinstance(value, bool):
        return value
    if value is None:
        return False
    if not isinstance(value, str):
        raise ConfigurationError(f"{name}: expected a boolean or string, got {type(value).__name__}")
    token = value.strip().lower()
    if token == "true":
        return True
    if token in ("false", ""):
        return False
    raise ConfigurationError(
        f"{name}: must be exactly 'true' or 'false' (got {value!r}). "
        "Shorthands like '1' or 'yes' are rejected for the live-trading flag."
    )


def _parse_int(value: Any, *, name: str, minimum: int = 0) -> int:
    if isinstance(value, bool):
        raise ConfigurationError(f"{name}: expected an integer, got a bool")
    if isinstance(value, int):
        parsed = value
    elif isinstance(value, str):
        try:
            parsed = int(value.strip())
        except ValueError as exc:
            raise ConfigurationError(f"{name}: {value!r} is not an integer") from exc
    else:
        raise ConfigurationError(f"{name}: expected an integer, got {type(value).__name__}")
    if parsed < minimum:
        raise ConfigurationError(f"{name}: must be >= {minimum}, got {parsed}")
    return parsed


def _parse_money(value: Any, currency: Currency, *, name: str) -> Money:
    if isinstance(value, Money):
        if value.currency != currency:
            raise ConfigurationError(
                f"{name}: expected {currency.code}, got {value.currency.code}"
            )
        return value
    try:
        return Money.rounded(value, currency)
    except (TypeError, ValueError) as exc:
        raise ConfigurationError(f"{name}: {exc}") from exc


def _parse_currency(value: Any, *, name: str) -> Currency:
    if isinstance(value, Currency):
        return value
    if not isinstance(value, str):
        raise ConfigurationError(f"{name}: expected a currency code string")
    code = value.strip().upper()
    if code not in KNOWN_CURRENCIES:
        raise ConfigurationError(
            f"{name}: unknown currency {code!r}; known: {sorted(KNOWN_CURRENCIES)}"
        )
    return KNOWN_CURRENCIES[code]


@dataclass(frozen=True, slots=True)
class RiskConfig:
    """Hard numeric limits. Every value is a ceiling, never a target.

    Defaults are deliberately tiny. A configuration mistake should produce a
    system that trades too little, not one that trades too much.
    """

    base_currency: Currency = USD
    max_order_notional: Money = field(default=Money("100.00", USD))
    max_position_notional: Money = field(default=Money("250.00", USD))
    max_gross_exposure: Money = field(default=Money("500.00", USD))
    max_daily_loss: Money = field(default=Money("50.00", USD))
    max_orders_per_minute: int = 6
    max_open_orders: int = 5
    risk_fraction_per_trade: Decimal = field(default=Decimal("0.005"))

    def __post_init__(self) -> None:
        for name in (
            "max_order_notional",
            "max_position_notional",
            "max_gross_exposure",
            "max_daily_loss",
        ):
            value: Money = getattr(self, name)
            if not isinstance(value, Money):
                raise ConfigurationError(f"{name}: expected Money, got {type(value).__name__}")
            if value.currency != self.base_currency:
                raise ConfigurationError(
                    f"{name}: must be denominated in {self.base_currency.code}, "
                    f"got {value.currency.code}"
                )
            if not value.is_positive:
                raise ConfigurationError(f"{name}: must be strictly positive, got {value}")

        if self.max_order_notional > self.max_position_notional:
            raise ConfigurationError(
                "max_order_notional cannot exceed max_position_notional: a single "
                "order would be allowed to create a position that is itself forbidden"
            )
        if self.max_position_notional > self.max_gross_exposure:
            raise ConfigurationError(
                "max_position_notional cannot exceed max_gross_exposure"
            )
        if self.max_orders_per_minute < 1:
            raise ConfigurationError("max_orders_per_minute must be >= 1")
        if self.max_open_orders < 1:
            raise ConfigurationError("max_open_orders must be >= 1")

        fraction = self.risk_fraction_per_trade
        if isinstance(fraction, float):
            raise ConfigurationError(
                "risk_fraction_per_trade must not be a float (INVARIANT 8); "
                "use Decimal('0.005')"
            )
        if not isinstance(fraction, Decimal):
            raise ConfigurationError("risk_fraction_per_trade must be a Decimal")
        if not (Decimal(0) < fraction <= Decimal("0.1")):
            raise ConfigurationError(
                f"risk_fraction_per_trade must be in (0, 0.1], got {fraction}. "
                "Risking more than 10% of equity on one trade is rejected outright."
            )


@dataclass(frozen=True, slots=True)
class TradingConfig:
    """Top-level configuration.

    ``repr`` is safe to log: secret fields are :class:`Secret` instances whose
    own ``repr`` is redacted.
    """

    live_trading: bool = False
    live_confirmation: str = ""
    risk: RiskConfig = field(default_factory=RiskConfig)
    api_key: Secret | None = None
    api_secret: Secret | None = None
    kill_switch_path: str | None = None
    audit_log_path: str | None = None
    environment: str = "development"

    def __post_init__(self) -> None:
        if not isinstance(self.live_trading, bool):
            raise ConfigurationError("live_trading must be a bool")
        if not isinstance(self.risk, RiskConfig):
            raise ConfigurationError("risk must be a RiskConfig")
        for name in ("api_key", "api_secret"):
            value = getattr(self, name)
            if value is not None and not isinstance(value, Secret):
                raise ConfigurationError(
                    f"{name} must be wrapped in Secret, never a bare str "
                    "(INVARIANT 9)"
                )
        if self.live_trading and self.live_confirmation != REQUIRED_LIVE_CONFIRMATION:
            # Fail loudly at construction: a half-armed config is a trap that
            # would otherwise be discovered only when an order is refused.
            raise ConfigurationError(
                "live_trading=true requires live_confirmation to equal the exact "
                "phrase REQUIRED_LIVE_CONFIRMATION. Live trading remains disabled."
            )

    @property
    def is_live_authorized(self) -> bool:
        """True only if BOTH live-trading factors are present.

        NECESSARY, NOT SUFFICIENT. The trading-mode machine and the gateway
        impose further conditions before any order can execute.
        """
        return bool(self.live_trading) and self.live_confirmation == REQUIRED_LIVE_CONFIRMATION

    @property
    def base_currency(self) -> Currency:
        return self.risk.base_currency

    def with_live_trading_disabled(self) -> "TradingConfig":
        """Return a copy with live trading forced off (used by the kill path)."""
        return replace(self, live_trading=False, live_confirmation="")

    def redacted_summary(self) -> dict[str, Any]:
        """A dict safe to write to an audit log or a status endpoint."""
        return {
            "environment": self.environment,
            "live_trading": self.live_trading,
            "is_live_authorized": self.is_live_authorized,
            "base_currency": self.risk.base_currency.code,
            "max_order_notional": str(self.risk.max_order_notional),
            "max_position_notional": str(self.risk.max_position_notional),
            "max_gross_exposure": str(self.risk.max_gross_exposure),
            "max_daily_loss": str(self.risk.max_daily_loss),
            "max_orders_per_minute": self.risk.max_orders_per_minute,
            "max_open_orders": self.risk.max_open_orders,
            "risk_fraction_per_trade": str(self.risk.risk_fraction_per_trade),
            "api_key_fingerprint": self.api_key.fingerprint() if self.api_key else None,
            "api_secret_present": self.api_secret is not None,
            "kill_switch_path": self.kill_switch_path,
        }

    # -- loaders -----------------------------------------------------------
    @classmethod
    def from_mapping(cls, data: Mapping[str, Any]) -> "TradingConfig":
        """Build from a plain mapping of un-prefixed lowercase keys."""
        unknown = set(data) - _ALLOWED_KEYS
        if unknown:
            # Silently ignoring an unknown key means a typo'd limit
            # (max_order_notinal) leaves the tiny default in place while the
            # operator believes they raised it.
            raise ConfigurationError(
                f"unknown configuration keys: {sorted(unknown)}; "
                f"allowed: {sorted(_ALLOWED_KEYS)}"
            )

        currency = _parse_currency(data.get("base_currency", USD), name="base_currency")
        defaults = RiskConfig()

        def money_or_default(key: str, fallback: Money) -> Money:
            if key not in data or data[key] is None:
                if fallback.currency != currency:
                    # Rescale the default into the configured base currency.
                    return Money.rounded(fallback.amount, currency)
                return fallback
            return _parse_money(data[key], currency, name=key)

        risk = RiskConfig(
            base_currency=currency,
            max_order_notional=money_or_default("max_order_notional", defaults.max_order_notional),
            max_position_notional=money_or_default(
                "max_position_notional", defaults.max_position_notional
            ),
            max_gross_exposure=money_or_default(
                "max_gross_exposure", defaults.max_gross_exposure
            ),
            max_daily_loss=money_or_default("max_daily_loss", defaults.max_daily_loss),
            max_orders_per_minute=_parse_int(
                data.get("max_orders_per_minute", defaults.max_orders_per_minute),
                name="max_orders_per_minute",
                minimum=1,
            ),
            max_open_orders=_parse_int(
                data.get("max_open_orders", defaults.max_open_orders),
                name="max_open_orders",
                minimum=1,
            ),
            risk_fraction_per_trade=_parse_risk_fraction(
                data.get("risk_fraction_per_trade", defaults.risk_fraction_per_trade)
            ),
        )

        api_key = data.get("api_key")
        api_secret = data.get("api_secret")

        return cls(
            live_trading=_parse_strict_bool(data.get("live_trading"), name="live_trading"),
            live_confirmation=str(data.get("live_confirmation") or ""),
            risk=risk,
            api_key=_wrap_secret(api_key, "api_key"),
            api_secret=_wrap_secret(api_secret, "api_secret"),
            kill_switch_path=_opt_str(data.get("kill_switch_path")),
            audit_log_path=_opt_str(data.get("audit_log_path")),
            environment=str(data.get("environment") or "development"),
        )

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> "TradingConfig":
        """Build from ``TRADING_``-prefixed environment variables.

        ``env`` is injectable so tests never touch the real process
        environment.
        """
        source = os.environ if env is None else env
        data: dict[str, Any] = {}
        for raw_key, raw_value in source.items():
            if not raw_key.startswith(ENV_PREFIX):
                continue
            key = raw_key[len(ENV_PREFIX) :].lower()
            if key in _ENV_ALIASES:
                key = _ENV_ALIASES[key]
            if key not in _ALLOWED_KEYS:
                raise ConfigurationError(
                    f"unknown environment variable {raw_key}; "
                    f"allowed suffixes: {sorted(k.upper() for k in _ALLOWED_KEYS)}"
                )
            data[key] = raw_value
        return cls.from_mapping(data)

    @classmethod
    def from_toml_file(cls, path: str) -> "TradingConfig":
        """Build from a TOML file (``tomllib``, standard library since 3.11).

        Secrets should NOT live in this file; supply them by environment. The
        loader accepts them so that a single-source local config is possible,
        but ``.gitignore`` is written to keep such files out of the repository.
        """
        import tomllib

        try:
            with open(path, "rb") as handle:
                parsed = tomllib.load(handle)
        except FileNotFoundError as exc:
            raise ConfigurationError(f"config file not found: {path}") from exc
        except tomllib.TOMLDecodeError as exc:
            raise ConfigurationError(f"malformed TOML in {path}: {exc}") from exc
        section = parsed.get("trading", parsed)
        if not isinstance(section, dict):
            raise ConfigurationError(f"{path}: [trading] must be a table")
        # TOML has a native float type, and a float in a financial field would
        # violate INVARIANT 8. Reject rather than coerce, so the operator fixes
        # the source of truth instead of trusting a lossy conversion.
        float_keys = sorted(k for k, v in section.items() if isinstance(v, float))
        if float_keys:
            raise ConfigurationError(
                f"{path}: TOML float values are rejected for financial fields "
                f"(INVARIANT 8): {float_keys}. Quote them as strings, e.g. "
                'max_daily_loss = "50.00".'
            )
        return cls.from_mapping(section)


def _parse_risk_fraction(value: Any) -> Decimal:
    if isinstance(value, Decimal):
        return value
    if isinstance(value, float):
        raise ConfigurationError(
            "risk_fraction_per_trade must not be a float (INVARIANT 8); "
            'pass a string such as "0.005"'
        )
    try:
        return to_decimal(value, field="risk_fraction_per_trade")
    except TypeError as exc:
        raise ConfigurationError(str(exc)) from exc


def _wrap_secret(value: Any, label: str) -> Secret | None:
    if value is None or value == "":
        return None
    if isinstance(value, Secret):
        return value
    if not isinstance(value, str):
        raise ConfigurationError(f"{label} must be a str or Secret")
    return Secret(value, label=label)


def _opt_str(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


_ALLOWED_KEYS: Final = frozenset(
    {
        "live_trading",
        "live_confirmation",
        "base_currency",
        "max_order_notional",
        "max_position_notional",
        "max_gross_exposure",
        "max_daily_loss",
        "max_orders_per_minute",
        "max_open_orders",
        "risk_fraction_per_trade",
        "api_key",
        "api_secret",
        "kill_switch_path",
        "audit_log_path",
        "environment",
    }
)

#: Friendlier environment names mapped onto canonical keys.
_ENV_ALIASES: Final = {
    "live_enabled": "live_trading",
    "live": "live_trading",
}
