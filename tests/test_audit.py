"""Tests for the hash-chained audit log."""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from dataclasses import replace
from decimal import Decimal

from trading.core.audit import (
    GENESIS_HASH,
    AuditCategory,
    AuditLog,
    AuditOutcome,
    AuditRecord,
    AuditSink,
    InMemoryAuditSink,
    JsonlFileAuditSink,
    MultiSink,
)
from trading.core.clock import ManualClock
from trading.core.money import USD, Money, Quantity
from trading.core.secrets import REDACTED, Secret, global_redactor

SECRET_VALUE = "audit_leak_key_998877665544"


class ExplodingSink(AuditSink):
    def __init__(self):
        self.attempts = 0

    def emit(self, record):
        self.attempts += 1
        raise OSError("disk full")


class TestAuditBasics(unittest.TestCase):
    def setUp(self):
        self.clock = ManualClock()
        self.sink = InMemoryAuditSink()
        self.log = AuditLog(self.sink, clock=self.clock)

    def test_record_is_appended(self):
        rec = self.log.record(AuditCategory.SYSTEM, "startup")
        self.assertEqual(rec.seq, 1)
        self.assertEqual(len(self.sink), 1)
        self.assertEqual(rec.outcome, AuditOutcome.INFO.value)

    def test_sequence_increments(self):
        for i in range(1, 6):
            self.assertEqual(self.log.record(AuditCategory.SYSTEM, "tick").seq, i)
        self.assertEqual(self.log.count, 5)

    def test_first_record_links_to_genesis(self):
        rec = self.log.record(AuditCategory.SYSTEM, "startup")
        self.assertEqual(rec.prev_hash, GENESIS_HASH)

    def test_timestamp_comes_from_injected_clock(self):
        rec = self.log.record(AuditCategory.SYSTEM, "startup")
        self.assertTrue(rec.timestamp.startswith("2026-01-01T00:00:00"))
        self.clock.advance(60)
        rec2 = self.log.record(AuditCategory.SYSTEM, "startup")
        self.assertTrue(rec2.timestamp.startswith("2026-01-01T00:01:00"))

    def test_category_and_outcome_must_be_enums(self):
        with self.assertRaises(TypeError):
            self.log.record("order", "submit")  # type: ignore[arg-type]
        with self.assertRaises(TypeError):
            self.log.record(AuditCategory.ORDER, "submit", outcome="allowed")  # type: ignore[arg-type]

    def test_details_are_preserved(self):
        rec = self.log.record(
            AuditCategory.ORDER,
            "submit",
            outcome=AuditOutcome.ALLOWED,
            actor="gateway",
            details={"order_id": "ORD-1", "qty": Quantity("0.5", "BTC")},
        )
        self.assertEqual(rec.details["order_id"], "ORD-1")
        self.assertEqual(rec.details["qty"], "0.5 BTC")


class TestDecimalSurvivesSerialization(unittest.TestCase):
    def setUp(self):
        self.log = AuditLog(InMemoryAuditSink(), clock=ManualClock())

    def test_decimal_becomes_string_not_float(self):
        rec = self.log.record(
            AuditCategory.RISK, "check", details={"limit": Decimal("0.1")}
        )
        self.assertEqual(rec.details["limit"], "0.1")
        self.assertIsInstance(rec.details["limit"], str)

    def test_money_becomes_string(self):
        rec = self.log.record(
            AuditCategory.RISK, "check", details={"cap": Money("100.00", USD)}
        )
        self.assertEqual(rec.details["cap"], "100.00 USD")

    def test_no_float_appears_as_a_json_number(self):
        rec = self.log.record(
            AuditCategory.RISK, "check", details={"exact": Decimal("0.30")}
        )
        parsed = json.loads(rec.to_json())
        self.assertIsInstance(parsed["details"]["exact"], str)

    def test_float_details_are_stringified(self):
        rec = self.log.record(AuditCategory.SYSTEM, "latency", details={"ms": 1.5})
        self.assertIsInstance(rec.details["ms"], str)

    def test_nested_structures_are_coerced(self):
        rec = self.log.record(
            AuditCategory.RISK,
            "check",
            details={"limits": {"a": Decimal("1.5")}, "tags": ["x", "y"]},
        )
        self.assertEqual(rec.details["limits"]["a"], "1.5")
        self.assertEqual(rec.details["tags"], ["x", "y"])


class TestAuditRedaction(unittest.TestCase):
    """INVARIANT 9 extends to the audit trail."""

    def setUp(self):
        global_redactor().forget_all()
        self.sink = InMemoryAuditSink()
        self.log = AuditLog(self.sink, clock=ManualClock())

    def tearDown(self):
        global_redactor().forget_all()

    def test_registered_secret_scrubbed_from_details(self):
        Secret(SECRET_VALUE)
        self.log.record(
            AuditCategory.AUTH, "authenticate", details={"key": SECRET_VALUE}
        )
        self.assertNotIn(SECRET_VALUE, self.sink.rendered())

    def test_secret_object_in_details_is_redacted(self):
        s = Secret(SECRET_VALUE)
        rec = self.log.record(AuditCategory.AUTH, "authenticate", details={"key": s})
        self.assertEqual(rec.details["key"], REDACTED)

    def test_secret_scrubbed_from_action_and_actor(self):
        Secret(SECRET_VALUE)
        rec = self.log.record(
            AuditCategory.AUTH, f"login {SECRET_VALUE}", actor=f"user {SECRET_VALUE}"
        )
        self.assertNotIn(SECRET_VALUE, rec.action)
        self.assertNotIn(SECRET_VALUE, rec.actor)

    def test_pattern_secret_scrubbed_without_registration(self):
        rec = self.log.record(
            AuditCategory.AUTH, "call", details={"hdr": "Bearer qqqq1111wwww2222"}
        )
        self.assertNotIn("qqqq1111wwww2222", rec.details["hdr"])

    def test_nested_secret_scrubbed(self):
        Secret(SECRET_VALUE)
        self.log.record(
            AuditCategory.AUTH, "call", details={"outer": {"inner": SECRET_VALUE}}
        )
        self.assertNotIn(SECRET_VALUE, self.sink.rendered())


class TestHashChain(unittest.TestCase):
    def setUp(self):
        self.sink = InMemoryAuditSink()
        self.log = AuditLog(self.sink, clock=ManualClock())

    def test_each_record_links_to_predecessor(self):
        a = self.log.record(AuditCategory.SYSTEM, "one")
        b = self.log.record(AuditCategory.SYSTEM, "two")
        c = self.log.record(AuditCategory.SYSTEM, "three")
        self.assertEqual(b.prev_hash, a.record_hash)
        self.assertEqual(c.prev_hash, b.record_hash)

    def test_hash_matches_recomputation(self):
        rec = self.log.record(AuditCategory.SYSTEM, "one")
        self.assertEqual(rec.record_hash, rec.compute_hash())

    def test_clean_chain_verifies(self):
        for _ in range(5):
            self.log.record(AuditCategory.SYSTEM, "tick")
        self.log.verify()  # must not raise

    def test_empty_chain_verifies(self):
        self.log.verify()

    def test_modified_detail_is_detected(self):
        self.log.record(AuditCategory.ORDER, "submit", details={"qty": "1"})
        self.log.record(AuditCategory.ORDER, "submit", details={"qty": "2"})
        chain = list(self.log.records())
        tampered = replace(chain[0], details={"qty": "999"})
        with self.assertRaises(ValueError) as ctx:
            self.log.verify([tampered, chain[1]])
        self.assertIn("record_hash mismatch", str(ctx.exception))

    def test_deleted_record_is_detected(self):
        for _ in range(3):
            self.log.record(AuditCategory.SYSTEM, "tick")
        chain = list(self.log.records())
        with self.assertRaises(ValueError):
            self.log.verify([chain[0], chain[2]])

    def test_reordered_records_are_detected(self):
        for _ in range(3):
            self.log.record(AuditCategory.SYSTEM, "tick")
        chain = list(self.log.records())
        with self.assertRaises(ValueError):
            self.log.verify([chain[1], chain[0], chain[2]])

    def test_appended_forged_record_is_detected(self):
        self.log.record(AuditCategory.SYSTEM, "tick")
        chain = list(self.log.records())
        forged = AuditRecord(
            seq=2,
            timestamp="2026-01-01T00:00:00+00:00",
            category="order",
            action="submit",
            outcome="allowed",
            actor="attacker",
            details={},
            prev_hash=chain[0].record_hash,
            record_hash="deadbeef",
        )
        with self.assertRaises(ValueError):
            self.log.verify([chain[0], forged])

    def test_last_hash_tracks_head(self):
        self.assertEqual(self.log.last_hash, GENESIS_HASH)
        rec = self.log.record(AuditCategory.SYSTEM, "tick")
        self.assertEqual(self.log.last_hash, rec.record_hash)


class TestFailClosed(unittest.TestCase):
    def test_sink_failure_propagates(self):
        log = AuditLog(ExplodingSink(), clock=ManualClock())
        with self.assertRaises(OSError):
            log.record(AuditCategory.ORDER, "submit")

    def test_failed_record_does_not_advance_the_chain(self):
        sink = ExplodingSink()
        log = AuditLog(sink, clock=ManualClock())
        with self.assertRaises(OSError):
            log.record(AuditCategory.ORDER, "submit")
        # The log must not pretend a record exists that was never persisted.
        self.assertEqual(log.count, 0)
        self.assertEqual(log.last_hash, GENESIS_HASH)
        self.assertEqual(len(log.records()), 0)

    def test_multisink_reports_failure_but_tries_every_sink(self):
        good = InMemoryAuditSink()
        bad = ExplodingSink()
        other = InMemoryAuditSink()
        log = AuditLog(MultiSink(good, bad, other), clock=ManualClock())
        with self.assertRaises(OSError):
            log.record(AuditCategory.SYSTEM, "tick")
        self.assertEqual(len(good), 1)
        self.assertEqual(len(other), 1)
        self.assertEqual(bad.attempts, 1)


class TestJsonlSink(unittest.TestCase):
    def setUp(self):
        handle = tempfile.NamedTemporaryFile(
            "w", suffix=".jsonl", delete=False, encoding="utf-8"
        )
        handle.close()
        self.path = handle.name
        self.addCleanup(os.unlink, self.path)

    def test_records_round_trip_and_verify(self):
        log = AuditLog(JsonlFileAuditSink(self.path), clock=ManualClock())
        log.record(AuditCategory.ORDER, "submit", details={"qty": Decimal("0.5")})
        log.record(AuditCategory.ORDER, "fill", details={"qty": Decimal("0.5")})
        loaded = AuditLog.load_jsonl(self.path)
        self.assertEqual(len(loaded), 2)
        log.verify(loaded)

    def test_tampered_file_fails_verification(self):
        log = AuditLog(JsonlFileAuditSink(self.path), clock=ManualClock())
        log.record(AuditCategory.ORDER, "submit", details={"qty": "1"})
        log.record(AuditCategory.ORDER, "submit", details={"qty": "2"})
        with open(self.path, "r", encoding="utf-8") as handle:
            lines = handle.readlines()
        doctored = json.loads(lines[0])
        doctored["details"]["qty"] = "9999"
        lines[0] = json.dumps(doctored) + "\n"
        with open(self.path, "w", encoding="utf-8") as handle:
            handle.writelines(lines)
        with self.assertRaises(ValueError):
            log.verify(AuditLog.load_jsonl(self.path))

    def test_blank_lines_are_skipped(self):
        log = AuditLog(JsonlFileAuditSink(self.path), clock=ManualClock())
        log.record(AuditCategory.SYSTEM, "tick")
        with open(self.path, "a", encoding="utf-8") as handle:
            handle.write("\n\n")
        self.assertEqual(len(AuditLog.load_jsonl(self.path)), 1)


if __name__ == "__main__":
    unittest.main()
