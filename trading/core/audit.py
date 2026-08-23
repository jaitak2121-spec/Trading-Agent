"""Append-only, tamper-evident audit log.

INVARIANT 13 (audit): every safety decision is recorded before it takes
effect, and the record cannot be silently altered afterwards.

Design notes:

* **Hash chained.** Each record embeds the hash of its predecessor, so
  deleting or editing any record invalidates every hash after it.
  :meth:`AuditLog.verify` detects that. This makes tampering *evident*; it does
  not make it *impossible* (an attacker with write access can recompute the
  whole chain). Genuine tamper-proofing needs an append-only external store,
  which is a later-stage concern -- see docs/SAFETY.md.
* **Redacted at the boundary.** Every string that enters a record passes
  through the redactor, so INVARIANT 9 holds for the audit trail too.
* **Sinks are pluggable.** :class:`AuditSink` is the seam where a PostgreSQL
  writer drops in later without touching the safety core.
* **Ordering matters.** Callers record an intended action *before* performing
  it. A crash then leaves evidence of intent, which is what reconciliation
  needs. Recording after the fact would lose exactly the case we care about.
* **Fail closed.** A sink that raises propagates the error. Callers that
  cannot audit must refuse to act rather than act unobserved.
"""

from __future__ import annotations

import datetime as _dt
import hashlib
import json
import threading
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from decimal import Decimal
from enum import Enum
from typing import Any, Final, Mapping, Sequence

from .clock import Clock, SystemClock
from .secrets import REDACTED, Redactor, Secret, global_redactor

__all__ = [
    "GENESIS_HASH",
    "AuditCategory",
    "AuditOutcome",
    "AuditRecord",
    "AuditSink",
    "InMemoryAuditSink",
    "JsonlFileAuditSink",
    "MultiSink",
    "AuditLog",
]

GENESIS_HASH: Final = "0" * 64


class AuditCategory(str, Enum):
    SYSTEM = "system"
    CONFIG = "config"
    MODE = "mode"
    AUTH = "auth"
    #: A strategy proposing an intent. Distinct from ORDER: nothing exists yet.
    SIGNAL = "signal"
    ORDER = "order"
    RISK = "risk"
    KILL_SWITCH = "kill_switch"
    BREAKER = "breaker"
    RECONCILIATION = "reconciliation"


class AuditOutcome(str, Enum):
    ALLOWED = "allowed"
    REFUSED = "refused"
    INFO = "info"
    ERROR = "error"


#: Detail keys whose values are structural identifiers, not credentials.
#:
#: The redactor's last pattern flags any 32+ character hex run as a probable
#: secret. That is the right default for text we do not control, but an
#: idempotency key is a SHA-256 of the intent (INVARIANT 5) and so has exactly
#: that shape. Redacting it makes every UNKNOWN-order record identical, and
#: reconciling an UNKNOWN order (INVARIANT 12) means matching a record to one
#: specific key -- the audit trail would lose the one field it exists to carry.
#:
#: Narrow on purpose, and not an exemption from redaction: values under these
#: keys still have registered secrets scrubbed and still run every
#: self-labelling credential pattern. Only the shape-based heuristic is skipped.
#: Two omissions are deliberate. ``key`` is a generic name used for actual
#: credentials elsewhere, and the ambiguity resolves in favour of redacting.
#: ``token_id`` names a *capability*, not an identifier -- ``ExecutionToken``
#: truncates it even in its own ``repr`` -- so it stays redacted.
_IDENTIFIER_KEYS: Final = frozenset(
    {"idempotency_key", "order_id", "broker_order_id", "approval_id"}
)


def _redact_detail(text: str, redactor: Redactor, key: str | None) -> str:
    """Scrub a detail value, respecting :data:`_IDENTIFIER_KEYS`."""
    if key in _IDENTIFIER_KEYS:
        return redactor.redact_identifier(text)
    return redactor.redact(text)


def _coerce(value: Any, redactor: Redactor, key: str | None = None) -> Any:
    """Convert ``value`` into something canonically JSON-serialisable.

    ``Decimal`` and the money types become strings so exactness survives the
    round trip -- ``json`` would otherwise turn them into floats and quietly
    violate INVARIANT 8 inside the audit trail itself.

    ``key`` is the detail name the value was filed under, propagated so that
    :data:`_IDENTIFIER_KEYS` can be honoured at any nesting depth -- the gateway
    nests a broker ack's fields one level down.
    """
    if isinstance(value, Secret):
        return REDACTED
    if value is None or isinstance(value, bool):
        return value
    if isinstance(value, int):
        return value
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, float):
        # Not rejected outright (audit details are diagnostics, not
        # arithmetic), but stringified so no float ever reaches a JSON number
        # field that a later consumer might parse back as money.
        return f"{value!r}"
    if isinstance(value, str):
        return _redact_detail(value, redactor, key)
    if isinstance(value, Enum):
        return _coerce(value.value, redactor, key)
    if isinstance(value, _dt.datetime):
        return value.astimezone(_dt.timezone.utc).isoformat()
    if isinstance(value, Mapping):
        return {
            str(k): _coerce(v, redactor, str(k))
            for k, v in sorted(value.items(), key=lambda kv: str(kv[0]))
        }
    if isinstance(value, (list, tuple, set, frozenset)):
        items = list(value)
        if isinstance(value, (set, frozenset)):
            items = sorted(items, key=str)
        # The key carries into the elements: a list filed under an identifier
        # name is a list of identifiers.
        return [_coerce(v, redactor, key) for v in items]
    return _redact_detail(str(value), redactor, key)


def _canonical_json(payload: Mapping[str, Any]) -> str:
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


@dataclass(frozen=True, slots=True)
class AuditRecord:
    """One immutable audit entry."""

    seq: int
    timestamp: str
    category: str
    action: str
    outcome: str
    actor: str
    details: Mapping[str, Any] = field(default_factory=dict)
    prev_hash: str = GENESIS_HASH
    record_hash: str = ""

    def payload_without_hash(self) -> dict[str, Any]:
        return {
            "seq": self.seq,
            "timestamp": self.timestamp,
            "category": self.category,
            "action": self.action,
            "outcome": self.outcome,
            "actor": self.actor,
            "details": dict(self.details),
            "prev_hash": self.prev_hash,
        }

    def compute_hash(self) -> str:
        body = _canonical_json(self.payload_without_hash())
        return hashlib.sha256(body.encode("utf-8")).hexdigest()

    def to_dict(self) -> dict[str, Any]:
        payload = self.payload_without_hash()
        payload["record_hash"] = self.record_hash
        return payload

    def to_json(self) -> str:
        return _canonical_json(self.to_dict())


class AuditSink(ABC):
    """Destination for audit records.

    The seam for a durable store in a later stage. Implementations must be
    thread-safe or document that they are not.
    """

    @abstractmethod
    def emit(self, record: AuditRecord) -> None:
        """Persist ``record``. Raise on failure; callers fail closed."""


class InMemoryAuditSink(AuditSink):
    """Keeps records in a list. Used by tests and by the Stage 1 runtime."""

    def __init__(self) -> None:
        self._records: list[AuditRecord] = []
        self._lock = threading.RLock()

    def emit(self, record: AuditRecord) -> None:
        with self._lock:
            self._records.append(record)

    @property
    def records(self) -> list[AuditRecord]:
        with self._lock:
            return list(self._records)

    def __len__(self) -> int:
        with self._lock:
            return len(self._records)

    def find(self, action: str) -> list[AuditRecord]:
        return [r for r in self.records if r.action == action]

    def rendered(self) -> str:
        """All records as one string. Handy for leak assertions in tests."""
        return "\n".join(r.to_json() for r in self.records)


class JsonlFileAuditSink(AuditSink):
    """Appends newline-delimited JSON to a file.

    Opens in append mode and flushes on every write, so a crash loses at most
    the record in flight. ``os.fsync`` is deliberately NOT called per record --
    that trade-off is revisited when a durable store replaces this.
    """

    def __init__(self, path: str) -> None:
        self._path = path
        self._lock = threading.RLock()

    @property
    def path(self) -> str:
        return self._path

    def emit(self, record: AuditRecord) -> None:
        line = record.to_json()
        with self._lock:
            with open(self._path, "a", encoding="utf-8") as handle:
                handle.write(line + "\n")
                handle.flush()


class MultiSink(AuditSink):
    """Fans out to several sinks.

    If any sink raises, the error propagates after all sinks have been
    attempted, so one broken destination cannot silently suppress the others.
    """

    def __init__(self, *sinks: AuditSink) -> None:
        self._sinks = list(sinks)

    def emit(self, record: AuditRecord) -> None:
        errors: list[BaseException] = []
        for sink in self._sinks:
            try:
                sink.emit(record)
            except BaseException as exc:  # noqa: BLE001 - re-raised below
                errors.append(exc)
        if errors:
            raise errors[0]


class AuditLog:
    """Hash-chained audit log.

    Thread-safe: sequence allocation, hashing, and emission happen under one
    lock so the chain cannot interleave.
    """

    def __init__(
        self,
        sink: AuditSink | None = None,
        *,
        clock: Clock | None = None,
        redactor: Redactor | None = None,
    ) -> None:
        self._sink = sink if sink is not None else InMemoryAuditSink()
        self._clock = clock if clock is not None else SystemClock()
        self._redactor = redactor if redactor is not None else global_redactor()
        self._lock = threading.RLock()
        self._seq = 0
        self._last_hash = GENESIS_HASH
        self._chain: list[AuditRecord] = []

    @property
    def sink(self) -> AuditSink:
        return self._sink

    @property
    def last_hash(self) -> str:
        with self._lock:
            return self._last_hash

    @property
    def count(self) -> int:
        with self._lock:
            return self._seq

    def record(
        self,
        category: AuditCategory,
        action: str,
        *,
        outcome: AuditOutcome = AuditOutcome.INFO,
        actor: str = "system",
        details: Mapping[str, Any] | None = None,
    ) -> AuditRecord:
        """Append a record and return it.

        Raises whatever the sink raises. Callers must treat an audit failure as
        a reason to refuse the action they were about to take.
        """
        if not isinstance(category, AuditCategory):
            raise TypeError("category must be an AuditCategory")
        if not isinstance(outcome, AuditOutcome):
            raise TypeError("outcome must be an AuditOutcome")

        coerced = _coerce(dict(details or {}), self._redactor)
        with self._lock:
            seq = self._seq + 1
            record = AuditRecord(
                seq=seq,
                timestamp=self._clock.now().isoformat(),
                category=category.value,
                action=self._redactor.redact(action),
                outcome=outcome.value,
                actor=self._redactor.redact(actor),
                details=coerced,
                prev_hash=self._last_hash,
            )
            record = AuditRecord(
                **{**record.payload_without_hash(), "record_hash": record.compute_hash()}
            )
            # Emit BEFORE advancing internal state: if the sink fails, the log
            # does not pretend the record exists.
            self._sink.emit(record)
            self._seq = seq
            self._last_hash = record.record_hash
            self._chain.append(record)
            return record

    def records(self) -> Sequence[AuditRecord]:
        with self._lock:
            return list(self._chain)

    def verify(self, records: Sequence[AuditRecord] | None = None) -> None:
        """Raise :class:`ValueError` if the chain has been tampered with."""
        chain = list(records) if records is not None else self.records()
        expected_prev = GENESIS_HASH
        for index, record in enumerate(chain, start=1):
            if record.seq != index:
                raise ValueError(
                    f"audit chain broken: expected seq {index}, found {record.seq}"
                )
            if record.prev_hash != expected_prev:
                raise ValueError(
                    f"audit chain broken at seq {record.seq}: prev_hash mismatch"
                )
            recomputed = record.compute_hash()
            if recomputed != record.record_hash:
                raise ValueError(
                    f"audit chain broken at seq {record.seq}: record_hash mismatch "
                    "(record was modified after being written)"
                )
            expected_prev = record.record_hash

    @staticmethod
    def load_jsonl(path: str) -> list[AuditRecord]:
        """Read records back from a :class:`JsonlFileAuditSink` file."""
        out: list[AuditRecord] = []
        with open(path, "r", encoding="utf-8") as handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                data = json.loads(line)
                out.append(
                    AuditRecord(
                        seq=data["seq"],
                        timestamp=data["timestamp"],
                        category=data["category"],
                        action=data["action"],
                        outcome=data["outcome"],
                        actor=data["actor"],
                        details=data.get("details", {}),
                        prev_hash=data["prev_hash"],
                        record_hash=data.get("record_hash", ""),
                    )
                )
        return out
