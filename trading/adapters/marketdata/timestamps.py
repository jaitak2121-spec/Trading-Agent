"""Shared timestamp parsing for market-data ingestion.

Both the quote and the candle normalizers face the same problem: a venue sends a
time as an ISO-8601 string or an epoch number (seconds, milliseconds, or a
float), or omits it, and the kernel's value types demand a timezone-aware UTC
datetime. This is that parser, in one place, so the two boundaries cannot drift.

The rule is the one in :class:`~trading.ports.market_data.QuoteFeedPort`'s
contract: honor the venue's own stamp where present, fall back to receipt time
only where it is absent, and never override a present stamp with "now" -- a
receipt time written over a replayed older stamp makes stale data look fresh.

The parser raises a neutral :class:`BadTimestamp`; each normalizer catches it and
re-raises as its own ``MalformedMessage``, so this module needs no dependency on
either normalizer and no import cycle can form.
"""

from __future__ import annotations

import datetime as _dt

from ...core.clock import UTC, Clock

__all__ = ["BadTimestamp", "EPOCH_MS_THRESHOLD", "parse_timestamp"]

#: An epoch at or above this is milliseconds; below it, seconds. 1e12 seconds is
#: the year 33658, and 1e12 milliseconds is 2001 -- no real feed is ambiguous.
EPOCH_MS_THRESHOLD = 1_000_000_000_000


class BadTimestamp(ValueError):
    """A raw timestamp could not be read. Callers wrap this as a MalformedMessage."""


def parse_timestamp(value: object, *, clock: Clock, field: str) -> _dt.datetime:
    """Parse ``value`` into a timezone-aware UTC datetime, or raise :class:`BadTimestamp`.

    ``None`` means the message carried no stamp, so the honest time is receipt
    time (``clock.now()``). Everything else is the venue's own stamp and is parsed,
    never replaced with now.
    """
    if value is None:
        return clock.now()
    if isinstance(value, bool):
        raise BadTimestamp(f"{field!r} must be a time, not a bool")
    if isinstance(value, (int, float)):
        return _from_epoch(value, field)
    if isinstance(value, str):
        return _from_string(value, field)
    raise BadTimestamp(f"{field!r} must be an epoch number or an ISO-8601 string")


def _from_epoch(value: int | float, field: str) -> _dt.datetime:
    seconds = value / 1000 if value >= EPOCH_MS_THRESHOLD else value
    try:
        return _dt.datetime.fromtimestamp(seconds, tz=UTC)
    except (OverflowError, OSError, ValueError) as exc:
        raise BadTimestamp(f"{field!r} {value!r} is out of range") from exc


def _from_string(value: str, field: str) -> _dt.datetime:
    text = value.strip()
    candidate = text[:-1] + "+00:00" if text.endswith("Z") else text
    try:
        parsed = _dt.datetime.fromisoformat(candidate)
    except ValueError:
        # Some feeds put an epoch in a string field. Try that before giving up.
        try:
            return _from_epoch(int(text), field)
        except (ValueError, BadTimestamp) as exc:
            raise BadTimestamp(
                f"{field!r} {value!r} is neither ISO-8601 nor an epoch"
            ) from exc
    if parsed.tzinfo is None:
        # A naive stamp is assumed UTC rather than rejected: most feeds that omit
        # an offset mean UTC, and attaching one is less surprising than refusing
        # every tick. It is still a real stamp, not "now".
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)
