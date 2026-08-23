"""Secret handling and log redaction.

INVARIANT 9: secrets never appear in logs.

Two independent mechanisms, because either one alone is insufficient:

1. **Containment** -- :class:`Secret` wraps sensitive material so that the
   obvious accidents are inert. ``str()``, ``repr()``, f-string interpolation,
   and ``%``-formatting all yield ``***REDACTED***``. Pickling raises. Reading
   the real value requires calling :meth:`Secret.reveal`, which is greppable in
   review.

2. **Scrubbing** -- :class:`Redactor` removes secret material from text that
   has *already* escaped containment, e.g. a credential pasted into a config
   string, an HTTP header echoed by a server, or a secret embedded in a
   third-party exception message. :class:`RedactingFilter` installs this on a
   :mod:`logging` handler.

Mechanism 1 stops mistakes in our own code. Mechanism 2 stops mistakes in code
we do not control. Neither is sufficient alone, and neither is a guarantee --
see the "Limits of redaction" section in docs/SAFETY.md.
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import re
import threading
from typing import Final, Iterable

__all__ = [
    "REDACTED",
    "Secret",
    "Redactor",
    "RedactingFilter",
    "install_redaction",
    "global_redactor",
]

REDACTED: Final = "***REDACTED***"

#: Registered secret values shorter than this are NOT added to the scrub list.
#: A 3-character secret would match constantly and turn every log line into
#: confetti, which destroys the audit trail we depend on. Short secrets still
#: benefit from :class:`Secret` containment.
MIN_SCRUB_LENGTH: Final = 6


class Secret:
    """An opaque wrapper around sensitive material.

    ``Secret`` is deliberately not a ``str`` subclass: inheriting from ``str``
    would make every string operation a disclosure path.
    """

    __slots__ = ("_value", "_label", "_reveal_count")

    def __init__(self, value: str, *, label: str = "secret", register: bool = True) -> None:
        if isinstance(value, Secret):  # pragma: no cover - defensive
            raise TypeError("cannot wrap a Secret in another Secret")
        if not isinstance(value, str):
            raise TypeError(f"Secret requires a str, got {type(value).__name__}")
        self._value = value
        self._label = label
        self._reveal_count = 0
        if register:
            global_redactor().register(value)

    # -- disclosure --------------------------------------------------------
    def reveal(self) -> str:
        """Return the underlying value.

        Every call site is a disclosure point. Keep them few, keep them close
        to the boundary that actually needs the credential, and never pass the
        result to a logger.
        """
        self._reveal_count += 1
        return self._value

    @property
    def reveal_count(self) -> int:
        """How many times :meth:`reveal` has been called (diagnostics only)."""
        return self._reveal_count

    @property
    def label(self) -> str:
        return self._label

    def fingerprint(self, length: int = 8) -> str:
        """A short, stable, non-reversible tag for correlating without leaking.

        Lets an operator confirm "the key I deployed is the key in use" from
        logs alone. Truncated SHA-256 -- not a proof of possession.
        """
        digest = hashlib.sha256(self._value.encode("utf-8")).hexdigest()
        return digest[:length]

    @property
    def is_empty(self) -> bool:
        return self._value == ""

    def __len__(self) -> int:
        # Length alone is not considered sensitive and helps validation.
        return len(self._value)

    # -- containment -------------------------------------------------------
    def __str__(self) -> str:
        return REDACTED

    def __repr__(self) -> str:
        return f"Secret({self._label!r}, {REDACTED})"

    def __format__(self, format_spec: str) -> str:
        # Ignores the format spec entirely; no spec may widen disclosure.
        return REDACTED

    def __reduce__(self):
        raise TypeError(
            "Secret cannot be pickled: serialising it would write the "
            "credential to disk or the network in plaintext"
        )

    def __getstate__(self):
        raise TypeError("Secret cannot be serialised")

    def __eq__(self, other: object) -> bool:
        """Constant-time comparison against another Secret or a plain str."""
        if isinstance(other, Secret):
            return hmac.compare_digest(self._value, other._value)
        if isinstance(other, str):
            return hmac.compare_digest(self._value, other)
        return NotImplemented

    def __hash__(self) -> int:
        # Hash the digest, not the value, so the secret cannot be recovered
        # from a hash table dump.
        return hash(("Secret", hashlib.sha256(self._value.encode("utf-8")).digest()))

    def __bool__(self) -> bool:
        return bool(self._value)


# --------------------------------------------------------------------------
# Scrubbing
# --------------------------------------------------------------------------

# Pattern-based redaction is defence in depth for material never registered as
# a Secret. Each pattern keeps its label and replaces only the value group.
_KEYED_PATTERN: Final = re.compile(
    r"(?i)\b(authorization|api[-_ ]?key|api[-_ ]?secret|access[-_ ]?token|"
    r"refresh[-_ ]?token|secret[-_ ]?key|client[-_ ]?secret|private[-_ ]?key|"
    r"password|passwd|pwd|passphrase|signature|secret|token)\b"
    r"(\s*[:=]\s*|\"\s*:\s*)"
    r"(\"[^\"]*\"|'[^']*'|[^\s,;&})\]]+)"
)
_BEARER_PATTERN: Final = re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._\-~+/]{8,}=*")
_JWT_PATTERN: Final = re.compile(r"\beyJ[A-Za-z0-9_-]{4,}\.[A-Za-z0-9_-]{4,}\.[A-Za-z0-9_-]{4,}\b")
_URL_CREDENTIALS_PATTERN: Final = re.compile(r"(?i)\b([a-z][a-z0-9+.\-]*://)([^/\s:@]+):([^/\s@]+)@")
_LONG_HEX_PATTERN: Final = re.compile(r"\b[0-9a-fA-F]{32,}\b")
_PEM_PATTERN: Final = re.compile(
    r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----",
    re.DOTALL,
)


class Redactor:
    """Removes secret material from text.

    Exact registered values are scrubbed first (highest precision), then
    structural patterns.
    """

    def __init__(self) -> None:
        self._values: set[str] = set()
        self._lock = threading.RLock()

    def register(self, value: str) -> None:
        """Add a literal value to the scrub list."""
        if not isinstance(value, str):
            raise TypeError("register requires a str")
        if len(value) < MIN_SCRUB_LENGTH:
            return
        with self._lock:
            self._values.add(value)

    def register_all(self, values: Iterable[str]) -> None:
        for value in values:
            self.register(value)

    def forget_all(self) -> None:
        """Clear the scrub list. Test-support only."""
        with self._lock:
            self._values.clear()

    @property
    def registered_count(self) -> int:
        with self._lock:
            return len(self._values)

    def _redact_evidenced(self, text: str) -> str:
        """Scrub registered values and every self-labelling credential shape.

        Each pattern applied here carries its own positive evidence that it has
        found a secret: a PEM header, a JWT's three segments, credentials in a
        URL's authority, the literal word ``password``. None of them fires on an
        arbitrary opaque string.
        """
        if not isinstance(text, str):
            text = str(text)
        if not text:
            return text

        with self._lock:
            # Longest first: if one secret is a substring of another, redacting
            # the shorter one first would leave the longer one's tail exposed.
            values = sorted(self._values, key=len, reverse=True)
        for value in values:
            if value and value in text:
                text = text.replace(value, REDACTED)

        text = _PEM_PATTERN.sub(REDACTED, text)
        text = _URL_CREDENTIALS_PATTERN.sub(rf"\1\2:{REDACTED}@", text)
        text = _JWT_PATTERN.sub(REDACTED, text)
        text = _BEARER_PATTERN.sub(f"Bearer {REDACTED}", text)
        text = _KEYED_PATTERN.sub(rf"\1\g<2>{REDACTED}", text)
        return text

    def redact(self, text: str) -> str:
        """Scrub every secret shape, including the high-entropy heuristic.

        This is the default and what every caller outside :mod:`trading.core.audit`
        should use.
        """
        text = self._redact_evidenced(text)
        if not text:
            return text
        return _LONG_HEX_PATTERN.sub(REDACTED, text)

    def redact_identifier(self, text: str) -> str:
        """Scrub ``text`` that the caller knows to be a structural identifier.

        Identical to :meth:`redact` except that :data:`_LONG_HEX_PATTERN` is not
        applied. That pattern is a *shape* check with no evidence behind it, and
        a content-derived identifier -- a SHA-256 idempotency key, say -- has
        exactly the shape it flags. Everything else still runs, so a credential
        that happens to be passed under an identifier's name is still caught.

        Only :mod:`trading.core.audit` calls this, for the closed set of detail
        keys it declares to be identifiers. It is a narrowing of one heuristic,
        not an exemption from redaction.
        """
        return self._redact_evidenced(text)


_GLOBAL_REDACTOR = Redactor()


def global_redactor() -> Redactor:
    """The process-wide redactor that :class:`Secret` registers into."""
    return _GLOBAL_REDACTOR


class RedactingFilter(logging.Filter):
    """A logging filter that scrubs secrets from every record it sees.

    Installed as a filter rather than a formatter so that it applies no matter
    which formatter a handler uses. The filter eagerly merges ``msg`` and
    ``args`` into a single redacted string, and pre-renders exception text,
    because a secret can hide in either.
    """

    def __init__(self, redactor: Redactor | None = None) -> None:
        super().__init__()
        self._redactor = redactor if redactor is not None else global_redactor()

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            message = record.getMessage()
        except Exception:  # pragma: no cover - malformed args
            message = str(record.msg)
        record.msg = self._redactor.redact(message)
        record.args = ()

        # A secret can live in an exception message. The formatter renders
        # exc_info lazily into exc_text, so render and scrub it here; the
        # formatter reuses a pre-populated exc_text.
        if record.exc_info:
            if not record.exc_text:
                import traceback

                record.exc_text = "".join(traceback.format_exception(*record.exc_info))
            record.exc_info = None
        if record.exc_text:
            record.exc_text = self._redactor.redact(record.exc_text)
        if record.stack_info:
            record.stack_info = self._redactor.redact(record.stack_info)

        # Structured extras are a common leak path.
        for key, value in list(record.__dict__.items()):
            if key in _RESERVED_RECORD_KEYS:
                continue
            if isinstance(value, str):
                record.__dict__[key] = self._redactor.redact(value)
            elif isinstance(value, Secret):
                record.__dict__[key] = REDACTED
        return True


_RESERVED_RECORD_KEYS: Final = frozenset(
    {
        "name", "msg", "args", "levelname", "levelno", "pathname", "filename",
        "module", "exc_info", "exc_text", "stack_info", "lineno", "funcName",
        "created", "msecs", "relativeCreated", "thread", "threadName",
        "processName", "process", "taskName",
    }
)


def install_redaction(logger: logging.Logger | None = None) -> RedactingFilter:
    """Attach a :class:`RedactingFilter` to ``logger`` and all its handlers.

    Filters on a logger do not apply to records that propagate up from child
    loggers, so the filter is attached to the handlers as well -- handlers see
    every record that reaches them regardless of origin.
    """
    target = logger if logger is not None else logging.getLogger()
    filt = RedactingFilter()
    target.addFilter(filt)
    for handler in target.handlers:
        handler.addFilter(filt)
    return filt
