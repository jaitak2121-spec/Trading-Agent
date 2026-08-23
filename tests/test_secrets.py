"""Tests for secret containment and log redaction.

Covers INVARIANT 9: secrets never appear in logs.
"""

from __future__ import annotations

import io
import logging
import pickle
import unittest

from trading.core.secrets import (
    REDACTED,
    RedactingFilter,
    Redactor,
    Secret,
    global_redactor,
    install_redaction,
)

SECRET_VALUE = "sk_live_9f8e7d6c5b4a3f2e1d0c"


class SecretIsolationMixin:
    """Keeps the process-wide redactor from leaking between tests."""

    def setUp(self):
        super().setUp()
        global_redactor().forget_all()

    def tearDown(self):
        global_redactor().forget_all()
        super().tearDown()


class TestSecretContainment(SecretIsolationMixin, unittest.TestCase):
    def test_str_is_redacted(self):
        self.assertEqual(str(Secret(SECRET_VALUE)), REDACTED)

    def test_repr_is_redacted(self):
        r = repr(Secret(SECRET_VALUE, label="api_key"))
        self.assertIn(REDACTED, r)
        self.assertNotIn(SECRET_VALUE, r)
        self.assertIn("api_key", r)

    def test_fstring_is_redacted(self):
        s = Secret(SECRET_VALUE)
        self.assertNotIn(SECRET_VALUE, f"{s}")
        self.assertEqual(f"{s}", REDACTED)

    def test_format_spec_cannot_widen_disclosure(self):
        s = Secret(SECRET_VALUE)
        self.assertNotIn(SECRET_VALUE, f"{s:>60}")
        self.assertNotIn(SECRET_VALUE, f"{s!s}")
        self.assertNotIn(SECRET_VALUE, "{}".format(s))
        self.assertNotIn(SECRET_VALUE, "%s" % (s,))

    def test_percent_formatting_is_redacted(self):
        s = Secret(SECRET_VALUE)
        self.assertNotIn(SECRET_VALUE, "key=%s" % s)
        self.assertNotIn(SECRET_VALUE, "key=%r" % s)

    def test_reveal_returns_the_value(self):
        s = Secret(SECRET_VALUE)
        self.assertEqual(s.reveal(), SECRET_VALUE)

    def test_reveal_is_counted(self):
        s = Secret(SECRET_VALUE)
        self.assertEqual(s.reveal_count, 0)
        s.reveal()
        s.reveal()
        self.assertEqual(s.reveal_count, 2)

    def test_pickle_is_blocked(self):
        s = Secret(SECRET_VALUE)
        with self.assertRaises(TypeError):
            pickle.dumps(s)

    def test_equality_against_secret_and_str(self):
        a = Secret(SECRET_VALUE)
        b = Secret(SECRET_VALUE)
        c = Secret("different-value-entirely")
        self.assertEqual(a, b)
        self.assertEqual(a, SECRET_VALUE)
        self.assertNotEqual(a, c)
        self.assertNotEqual(a, "nope")

    def test_hash_does_not_expose_value(self):
        s = Secret(SECRET_VALUE)
        self.assertNotIn(SECRET_VALUE, str(hash(s)))
        self.assertEqual(hash(s), hash(Secret(SECRET_VALUE)))

    def test_fingerprint_is_stable_and_not_the_secret(self):
        s = Secret(SECRET_VALUE)
        fp = s.fingerprint()
        self.assertEqual(len(fp), 8)
        self.assertEqual(fp, Secret(SECRET_VALUE).fingerprint())
        self.assertNotIn(fp, SECRET_VALUE)
        self.assertNotEqual(fp, SECRET_VALUE[:8])

    def test_rejects_non_str(self):
        with self.assertRaises(TypeError):
            Secret(12345)  # type: ignore[arg-type]

    def test_bool_and_len(self):
        self.assertTrue(Secret(SECRET_VALUE))
        self.assertFalse(Secret("", register=False))
        self.assertTrue(Secret("", register=False).is_empty)
        self.assertEqual(len(Secret("abcdef")), 6)

    def test_secret_is_not_a_str_subclass(self):
        # Inheriting from str would make every string method a leak path.
        self.assertNotIsInstance(Secret(SECRET_VALUE), str)


class TestRedactorExactValues(SecretIsolationMixin, unittest.TestCase):
    def test_registered_value_is_scrubbed(self):
        r = Redactor()
        r.register(SECRET_VALUE)
        out = r.redact(f"calling api with key {SECRET_VALUE} now")
        self.assertNotIn(SECRET_VALUE, out)
        self.assertIn(REDACTED, out)

    def test_constructing_a_secret_registers_it_globally(self):
        Secret(SECRET_VALUE)
        out = global_redactor().redact(f"leaked {SECRET_VALUE}")
        self.assertNotIn(SECRET_VALUE, out)

    def test_longest_match_wins(self):
        # If the shorter secret were replaced first, the longer one's tail
        # would survive in the output.
        r = Redactor()
        short = "abcdef123456"
        long = short + "EXTRATAIL"
        r.register(short)
        r.register(long)
        out = r.redact(f"value={long}")
        self.assertNotIn("EXTRATAIL", out)
        self.assertNotIn(short, out)

    def test_short_values_are_not_registered(self):
        r = Redactor()
        r.register("abc")
        self.assertEqual(r.registered_count, 0)
        self.assertEqual(r.redact("abc def"), "abc def")

    def test_register_rejects_non_str(self):
        with self.assertRaises(TypeError):
            Redactor().register(123)  # type: ignore[arg-type]

    def test_empty_text_passthrough(self):
        self.assertEqual(Redactor().redact(""), "")


class TestRedactorPatterns(SecretIsolationMixin, unittest.TestCase):
    """Defence in depth for material never registered as a Secret."""

    def setUp(self):
        super().setUp()
        self.r = Redactor()

    def test_keyed_assignment(self):
        for text, leaked in [
            ("api_key=supersecretvalue123", "supersecretvalue123"),
            ("api-key: supersecretvalue123", "supersecretvalue123"),
            ('{"secret": "supersecretvalue123"}', "supersecretvalue123"),
            ("password=hunter2hunter2", "hunter2hunter2"),
            ("client_secret = abcdefghijklmnop", "abcdefghijklmnop"),
            ("signature=deadbeefcafe", "deadbeefcafe"),
        ]:
            with self.subTest(text=text):
                out = self.r.redact(text)
                self.assertNotIn(leaked, out)
                self.assertIn(REDACTED, out)

    def test_keyed_assignment_keeps_the_label(self):
        out = self.r.redact("api_key=supersecretvalue123")
        self.assertIn("api_key", out)

    def test_bearer_token(self):
        out = self.r.redact("Authorization: Bearer abc123def456ghi789")
        self.assertNotIn("abc123def456ghi789", out)

    def test_jwt(self):
        jwt = "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.dozjgNryP4J3jVmNHl0w5N_XgL0n3I9PlFUP0THsR8U"
        out = self.r.redact(f"token {jwt}")
        self.assertNotIn(jwt, out)

    def test_url_embedded_credentials(self):
        out = self.r.redact("postgres://dbuser:sup3rs3cr3t@db.internal:5432/trading")
        self.assertNotIn("sup3rs3cr3t", out)
        # Host and user survive: they are needed for diagnosis.
        self.assertIn("db.internal", out)
        self.assertIn("dbuser", out)

    def test_long_hex_string(self):
        digest = "a" * 64
        out = self.r.redact(f"hmac {digest}")
        self.assertNotIn(digest, out)

    def test_pem_private_key(self):
        pem = (
            "-----BEGIN RSA PRIVATE KEY-----\n"
            "MIIEowIBAAKCAQEAxyz\nabc\n"
            "-----END RSA PRIVATE KEY-----"
        )
        out = self.r.redact(f"key: {pem}")
        self.assertNotIn("MIIEowIBAAKCAQEAxyz", out)

    def test_ordinary_text_is_left_alone(self):
        text = "submitted order 42 for 0.5 BTC at 30000.00 USD"
        self.assertEqual(self.r.redact(text), text)


class TestRedactingLogFilter(SecretIsolationMixin, unittest.TestCase):
    def setUp(self):
        super().setUp()
        self.stream = io.StringIO()
        self.handler = logging.StreamHandler(self.stream)
        self.handler.setFormatter(logging.Formatter("%(levelname)s %(message)s"))
        self.logger = logging.getLogger(f"test.redaction.{id(self)}")
        self.logger.handlers.clear()
        self.logger.addHandler(self.handler)
        self.logger.setLevel(logging.DEBUG)
        self.logger.propagate = False
        self.logger.addFilter(RedactingFilter())
        self.handler.addFilter(RedactingFilter())

    def tearDown(self):
        self.logger.handlers.clear()
        super().tearDown()

    def output(self) -> str:
        self.handler.flush()
        return self.stream.getvalue()

    def test_revealed_secret_in_message_is_scrubbed(self):
        s = Secret(SECRET_VALUE)
        self.logger.info("connecting with %s", s.reveal())
        self.assertNotIn(SECRET_VALUE, self.output())
        self.assertIn(REDACTED, self.output())

    def test_secret_object_in_args_is_scrubbed(self):
        s = Secret(SECRET_VALUE)
        self.logger.info("connecting with %s", s)
        self.assertNotIn(SECRET_VALUE, self.output())

    def test_secret_in_lazy_args_is_scrubbed(self):
        Secret(SECRET_VALUE)
        self.logger.warning("raw value: %s and %s", SECRET_VALUE, "harmless")
        out = self.output()
        self.assertNotIn(SECRET_VALUE, out)
        self.assertIn("harmless", out)

    def test_secret_in_exception_traceback_is_scrubbed(self):
        Secret(SECRET_VALUE)
        try:
            raise RuntimeError(f"upstream rejected key {SECRET_VALUE}")
        except RuntimeError:
            self.logger.exception("request failed")
        out = self.output()
        self.assertNotIn(SECRET_VALUE, out)
        self.assertIn("request failed", out)

    def test_secret_in_structured_extra_is_scrubbed(self):
        Secret(SECRET_VALUE)
        self.logger.info("submitting", extra={"credential": SECRET_VALUE})
        self.assertNotIn(SECRET_VALUE, self.output())

    def test_pattern_only_secret_is_scrubbed_without_registration(self):
        # Never wrapped in Secret, never registered -- patterns must still fire.
        self.logger.info("headers: Authorization: Bearer zzzz1111yyyy2222")
        self.assertNotIn("zzzz1111yyyy2222", self.output())

    def test_ordinary_messages_survive_intact(self):
        self.logger.info("order %s accepted", "ORD-123")
        self.assertIn("order ORD-123 accepted", self.output())

    def test_install_redaction_covers_handlers(self):
        stream = io.StringIO()
        handler = logging.StreamHandler(stream)
        logger = logging.getLogger(f"test.install.{id(self)}")
        logger.handlers.clear()
        logger.addHandler(handler)
        logger.setLevel(logging.DEBUG)
        logger.propagate = False
        install_redaction(logger)
        Secret(SECRET_VALUE)
        logger.info("value %s", SECRET_VALUE)
        handler.flush()
        self.assertNotIn(SECRET_VALUE, stream.getvalue())
        logger.handlers.clear()

    def test_child_logger_records_are_scrubbed_by_handler_filter(self):
        # Logger-level filters do not apply to records propagating up from a
        # child; the handler-level filter is what saves us.
        stream = io.StringIO()
        handler = logging.StreamHandler(stream)
        parent = logging.getLogger(f"test.parent.{id(self)}")
        parent.handlers.clear()
        parent.addHandler(handler)
        parent.setLevel(logging.DEBUG)
        parent.propagate = False
        install_redaction(parent)
        Secret(SECRET_VALUE)
        logging.getLogger(f"test.parent.{id(self)}.child").info("leak %s", SECRET_VALUE)
        handler.flush()
        self.assertNotIn(SECRET_VALUE, stream.getvalue())
        parent.handlers.clear()


if __name__ == "__main__":
    unittest.main()
