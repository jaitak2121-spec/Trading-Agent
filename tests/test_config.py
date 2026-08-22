"""Tests for configuration management.

Covers INVARIANT 1: LIVE_TRADING defaults to FALSE.
"""

from __future__ import annotations

import os
import tempfile
import unittest
from decimal import Decimal

from trading.core.config import (
    REQUIRED_LIVE_CONFIRMATION,
    RiskConfig,
    TradingConfig,
)
from trading.core.errors import ConfigurationError
from trading.core.money import BTC, INR, USD, Money
from trading.core.secrets import Secret, global_redactor

SECRET_VALUE = "cs_key_11223344556677889900"


class TestLiveTradingDefaultsOff(unittest.TestCase):
    """INVARIANT 1, checked from every construction path."""

    def test_bare_constructor_defaults_to_disabled(self):
        cfg = TradingConfig()
        self.assertFalse(cfg.live_trading)
        self.assertFalse(cfg.is_live_authorized)

    def test_empty_env_defaults_to_disabled(self):
        cfg = TradingConfig.from_env({})
        self.assertFalse(cfg.live_trading)
        self.assertFalse(cfg.is_live_authorized)

    def test_empty_mapping_defaults_to_disabled(self):
        cfg = TradingConfig.from_mapping({})
        self.assertFalse(cfg.is_live_authorized)

    def test_unrelated_env_vars_do_not_enable_live(self):
        cfg = TradingConfig.from_env({"PATH": "/usr/bin", "HOME": "/root"})
        self.assertFalse(cfg.is_live_authorized)

    def test_explicit_none_defaults_to_disabled(self):
        cfg = TradingConfig.from_mapping({"live_trading": None})
        self.assertFalse(cfg.is_live_authorized)

    def test_empty_string_defaults_to_disabled(self):
        cfg = TradingConfig.from_mapping({"live_trading": ""})
        self.assertFalse(cfg.is_live_authorized)

    def test_whitespace_defaults_to_disabled(self):
        cfg = TradingConfig.from_mapping({"live_trading": "   "})
        self.assertFalse(cfg.is_live_authorized)


class TestLiveTradingRequiresTwoFactors(unittest.TestCase):
    def test_flag_alone_is_rejected_at_construction(self):
        with self.assertRaises(ConfigurationError) as ctx:
            TradingConfig(live_trading=True)
        self.assertIn("live_confirmation", str(ctx.exception))

    def test_flag_alone_via_env_is_rejected(self):
        with self.assertRaises(ConfigurationError):
            TradingConfig.from_env({"TRADING_LIVE_TRADING": "true"})

    def test_wrong_confirmation_phrase_is_rejected(self):
        with self.assertRaises(ConfigurationError):
            TradingConfig(live_trading=True, live_confirmation="yes please")

    def test_confirmation_alone_does_not_enable_live(self):
        cfg = TradingConfig(live_confirmation=REQUIRED_LIVE_CONFIRMATION)
        self.assertFalse(cfg.live_trading)
        self.assertFalse(cfg.is_live_authorized)

    def test_both_factors_authorize(self):
        cfg = TradingConfig(
            live_trading=True, live_confirmation=REQUIRED_LIVE_CONFIRMATION
        )
        self.assertTrue(cfg.is_live_authorized)

    def test_both_factors_via_env_authorize(self):
        cfg = TradingConfig.from_env(
            {
                "TRADING_LIVE_TRADING": "true",
                "TRADING_LIVE_CONFIRMATION": REQUIRED_LIVE_CONFIRMATION,
            }
        )
        self.assertTrue(cfg.is_live_authorized)

    def test_confirmation_is_case_sensitive(self):
        with self.assertRaises(ConfigurationError):
            TradingConfig(
                live_trading=True,
                live_confirmation=REQUIRED_LIVE_CONFIRMATION.lower(),
            )

    def test_disabling_helper_clears_both_factors(self):
        cfg = TradingConfig(
            live_trading=True, live_confirmation=REQUIRED_LIVE_CONFIRMATION
        )
        off = cfg.with_live_trading_disabled()
        self.assertFalse(off.is_live_authorized)
        self.assertEqual(off.live_confirmation, "")


class TestBooleanParsingIsFailClosed(unittest.TestCase):
    def test_malformed_value_raises_rather_than_defaulting(self):
        for value in ["maybe", "TRUE-ish", "2", "y", "enabled", "on!"]:
            with self.subTest(value=value):
                with self.assertRaises(ConfigurationError):
                    TradingConfig.from_mapping({"live_trading": value})

    def test_shorthand_truthy_tokens_rejected_for_live_flag(self):
        # '1'/'yes'/'on' are valid booleans elsewhere but rejected here so the
        # deployment manifest is unambiguous to a human reviewer.
        for value in ["1", "yes", "on"]:
            with self.subTest(value=value):
                with self.assertRaises(ConfigurationError):
                    TradingConfig.from_mapping({"live_trading": value})

    def test_explicit_false_accepted(self):
        for value in ["false", "FALSE", " False "]:
            with self.subTest(value=value):
                self.assertFalse(
                    TradingConfig.from_mapping({"live_trading": value}).live_trading
                )

    def test_true_is_accepted_case_insensitively(self):
        cfg = TradingConfig.from_mapping(
            {"live_trading": " TRUE ", "live_confirmation": REQUIRED_LIVE_CONFIRMATION}
        )
        self.assertTrue(cfg.live_trading)


class TestUnknownKeysRejected(unittest.TestCase):
    def test_typo_in_limit_name_is_rejected(self):
        # A silently ignored typo would leave the tiny default in force while
        # the operator believes the limit was raised.
        with self.assertRaises(ConfigurationError) as ctx:
            TradingConfig.from_mapping({"max_order_notinal": "1000"})
        self.assertIn("unknown configuration keys", str(ctx.exception))

    def test_unknown_prefixed_env_var_is_rejected(self):
        with self.assertRaises(ConfigurationError):
            TradingConfig.from_env({"TRADING_MAX_ORDER_NOTINAL": "1000"})

    def test_env_aliases_are_accepted(self):
        cfg = TradingConfig.from_env(
            {
                "TRADING_LIVE_ENABLED": "true",
                "TRADING_LIVE_CONFIRMATION": REQUIRED_LIVE_CONFIRMATION,
            }
        )
        self.assertTrue(cfg.is_live_authorized)


class TestRiskConfigValidation(unittest.TestCase):
    def test_defaults_are_small_and_coherent(self):
        risk = RiskConfig()
        self.assertEqual(risk.max_order_notional, Money("100.00", USD))
        self.assertLessEqual(risk.max_order_notional, risk.max_position_notional)
        self.assertLessEqual(risk.max_position_notional, risk.max_gross_exposure)

    def test_order_limit_above_position_limit_rejected(self):
        with self.assertRaises(ConfigurationError) as ctx:
            RiskConfig(
                max_order_notional=Money("1000.00", USD),
                max_position_notional=Money("500.00", USD),
            )
        self.assertIn("max_order_notional", str(ctx.exception))

    def test_position_limit_above_gross_exposure_rejected(self):
        with self.assertRaises(ConfigurationError):
            RiskConfig(
                max_order_notional=Money("10.00", USD),
                max_position_notional=Money("900.00", USD),
                max_gross_exposure=Money("500.00", USD),
            )

    def test_non_positive_limits_rejected(self):
        with self.assertRaises(ConfigurationError):
            RiskConfig(max_order_notional=Money("0.00", USD))
        with self.assertRaises(ConfigurationError):
            RiskConfig(max_daily_loss=Money("-1.00", USD))

    def test_currency_mismatch_rejected(self):
        with self.assertRaises(ConfigurationError):
            RiskConfig(base_currency=USD, max_daily_loss=Money("50.00", INR))

    def test_float_risk_fraction_rejected(self):
        with self.assertRaises(ConfigurationError) as ctx:
            RiskConfig(risk_fraction_per_trade=0.005)  # type: ignore[arg-type]
        self.assertIn("float", str(ctx.exception))

    def test_risk_fraction_bounds_enforced(self):
        with self.assertRaises(ConfigurationError):
            RiskConfig(risk_fraction_per_trade=Decimal("0"))
        with self.assertRaises(ConfigurationError):
            RiskConfig(risk_fraction_per_trade=Decimal("-0.01"))
        with self.assertRaises(ConfigurationError):
            RiskConfig(risk_fraction_per_trade=Decimal("0.5"))
        self.assertEqual(
            RiskConfig(risk_fraction_per_trade=Decimal("0.1")).risk_fraction_per_trade,
            Decimal("0.1"),
        )

    def test_rate_limits_must_be_at_least_one(self):
        with self.assertRaises(ConfigurationError):
            RiskConfig(max_orders_per_minute=0)
        with self.assertRaises(ConfigurationError):
            RiskConfig(max_open_orders=0)

    def test_float_money_rejected_through_mapping(self):
        with self.assertRaises(ConfigurationError):
            TradingConfig.from_mapping({"max_order_notional": 100.5})

    def test_float_risk_fraction_rejected_through_mapping(self):
        with self.assertRaises(ConfigurationError):
            TradingConfig.from_mapping({"risk_fraction_per_trade": 0.01})

    def test_limits_parse_from_strings(self):
        cfg = TradingConfig.from_mapping(
            {
                "max_order_notional": "20.00",
                "max_position_notional": "40.00",
                "max_gross_exposure": "80.00",
                "max_daily_loss": "10.00",
                "risk_fraction_per_trade": "0.01",
            }
        )
        self.assertEqual(cfg.risk.max_order_notional, Money("20.00", USD))
        self.assertEqual(cfg.risk.risk_fraction_per_trade, Decimal("0.01"))

    def test_base_currency_switch_rescales_defaults(self):
        cfg = TradingConfig.from_mapping({"base_currency": "BTC"})
        self.assertEqual(cfg.base_currency, BTC)
        self.assertEqual(cfg.risk.max_order_notional.currency, BTC)

    def test_unknown_currency_rejected(self):
        with self.assertRaises(ConfigurationError):
            TradingConfig.from_mapping({"base_currency": "DOGE"})


class TestSecretsInConfig(unittest.TestCase):
    def tearDown(self):
        global_redactor().forget_all()

    def test_str_credentials_are_wrapped_in_secret(self):
        cfg = TradingConfig.from_mapping({"api_key": SECRET_VALUE})
        self.assertIsInstance(cfg.api_key, Secret)
        self.assertEqual(cfg.api_key.reveal(), SECRET_VALUE)

    def test_bare_str_credential_rejected_by_constructor(self):
        with self.assertRaises(ConfigurationError) as ctx:
            TradingConfig(api_key=SECRET_VALUE)  # type: ignore[arg-type]
        self.assertIn("Secret", str(ctx.exception))

    def test_config_repr_does_not_leak(self):
        cfg = TradingConfig.from_mapping({"api_key": SECRET_VALUE, "api_secret": "another_secret_val"})
        self.assertNotIn(SECRET_VALUE, repr(cfg))
        self.assertNotIn("another_secret_val", repr(cfg))

    def test_redacted_summary_does_not_leak(self):
        cfg = TradingConfig.from_mapping({"api_key": SECRET_VALUE})
        summary = cfg.redacted_summary()
        self.assertNotIn(SECRET_VALUE, repr(summary))
        self.assertEqual(summary["api_key_fingerprint"], cfg.api_key.fingerprint())
        self.assertFalse(summary["live_trading"])

    def test_empty_credential_becomes_none(self):
        cfg = TradingConfig.from_mapping({"api_key": ""})
        self.assertIsNone(cfg.api_key)

    def test_summary_reports_absent_credentials(self):
        summary = TradingConfig().redacted_summary()
        self.assertIsNone(summary["api_key_fingerprint"])
        self.assertFalse(summary["api_secret_present"])


class TestTomlLoading(unittest.TestCase):
    def _write(self, text: str) -> str:
        handle = tempfile.NamedTemporaryFile(
            "w", suffix=".toml", delete=False, encoding="utf-8"
        )
        handle.write(text)
        handle.close()
        self.addCleanup(os.unlink, handle.name)
        return handle.name

    def test_loads_quoted_decimals(self):
        path = self._write(
            '[trading]\n'
            'max_order_notional = "25.00"\n'
            'max_position_notional = "50.00"\n'
            'max_gross_exposure = "100.00"\n'
            'risk_fraction_per_trade = "0.02"\n'
        )
        cfg = TradingConfig.from_toml_file(path)
        self.assertEqual(cfg.risk.max_order_notional, Money("25.00", USD))
        self.assertFalse(cfg.is_live_authorized)

    def test_toml_float_is_rejected(self):
        path = self._write('[trading]\nmax_daily_loss = 50.0\n')
        with self.assertRaises(ConfigurationError) as ctx:
            TradingConfig.from_toml_file(path)
        self.assertIn("float", str(ctx.exception))

    def test_missing_file_raises_configuration_error(self):
        with self.assertRaises(ConfigurationError):
            TradingConfig.from_toml_file("/nonexistent/path/to/config.toml")

    def test_malformed_toml_raises_configuration_error(self):
        path = self._write("[trading\nbroken")
        with self.assertRaises(ConfigurationError):
            TradingConfig.from_toml_file(path)

    def test_toml_cannot_enable_live_without_confirmation(self):
        path = self._write('[trading]\nlive_trading = true\n')
        with self.assertRaises(ConfigurationError):
            TradingConfig.from_toml_file(path)


if __name__ == "__main__":
    unittest.main()
