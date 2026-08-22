"""Duplicate-order prevention.

INVARIANT 12: duplicate order submission must be prevented, or moved into an
UNKNOWN/reconciliation state.

The registry is a small state machine over *idempotency keys*, not over orders.
A key is claimed before anything is sent, and the claim's lifecycle records how
far the submission got:

.. code-block:: text

    (new) --reserve--> RESERVED --mark_submitted--> SUBMITTED --mark_settled--> SETTLED
                          |                            |
                    release_unsent               mark_unknown
                          |                            |
                       (freed)                      UNKNOWN

The asymmetry is the whole point:

* ``release_unsent`` frees a key, but is only legal from RESERVED -- i.e. when
  we are certain nothing left the process. A pre-submission risk rejection can
  safely free its key.
* Once a request has been sent (SUBMITTED), the key can never be freed. It
  either settles or becomes UNKNOWN. There is no path that lets a retry reuse a
  key whose request may have reached the venue, because that is exactly how you
  end up long twice.

A key in UNKNOWN also blocks the whole registry via :meth:`has_unknown`, which
the gateway consults before accepting anything new.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass
from enum import Enum
from typing import Final, Mapping

from .audit import AuditCategory, AuditLog, AuditOutcome
from .clock import Clock
from .errors import DuplicateOrderRejected, SafetyViolation

__all__ = ["ReservationState", "Reservation", "IdempotencyRegistry"]


class ReservationState(Enum):
    #: Key claimed locally; nothing sent yet.
    RESERVED = "reserved"
    #: A request carrying this key has left the process.
    SUBMITTED = "submitted"
    #: Outcome known and final.
    SETTLED = "settled"
    #: Outcome unknown. Blocks new orders until reconciled.
    UNKNOWN = "unknown"


_RESERVATION_TRANSITIONS: Final[Mapping[ReservationState, frozenset[ReservationState]]] = {
    ReservationState.RESERVED: frozenset(
        {ReservationState.SUBMITTED, ReservationState.SETTLED, ReservationState.UNKNOWN}
    ),
    ReservationState.SUBMITTED: frozenset(
        {ReservationState.SETTLED, ReservationState.UNKNOWN}
    ),
    ReservationState.SETTLED: frozenset(),
    # A key whose fate is unknown stays unknown until reconciliation clears it,
    # which happens through resolve_unknown(), not through an ordinary update.
    ReservationState.UNKNOWN: frozenset(),
}


@dataclass(frozen=True, slots=True)
class Reservation:
    key: str
    order_id: str
    state: ReservationState
    reserved_at: str
    updated_at: str
    note: str = ""

    def as_details(self) -> dict[str, object]:
        return {
            "idempotency_key": self.key,
            "order_id": self.order_id,
            "state": self.state.value,
            "reserved_at": self.reserved_at,
            "updated_at": self.updated_at,
            "note": self.note,
        }


class IdempotencyRegistry:
    """Thread-safe claim registry for idempotency keys."""

    def __init__(self, audit: AuditLog, *, clock: Clock) -> None:
        self._audit = audit
        self._clock = clock
        self._reservations: dict[str, Reservation] = {}
        self._lock = threading.RLock()

    # -- claiming ----------------------------------------------------------
    def reserve(self, key: str, order_id: str) -> Reservation:
        """Claim ``key`` for ``order_id``, or raise :class:`DuplicateOrderRejected`.

        Check-and-set happens under one lock, so two threads racing with the
        same key produce exactly one winner.
        """
        if not key or not key.strip():
            raise ValueError("idempotency key is required")
        if not order_id or not order_id.strip():
            raise ValueError("order_id is required")

        with self._lock:
            existing = self._reservations.get(key)
            if existing is not None:
                self._audit.record(
                    AuditCategory.ORDER,
                    "duplicate_order_rejected",
                    outcome=AuditOutcome.REFUSED,
                    actor="system",
                    details={
                        "idempotency_key": key,
                        "rejected_order_id": order_id,
                        "existing_order_id": existing.order_id,
                        "existing_state": existing.state.value,
                    },
                )
                raise DuplicateOrderRejected(
                    f"idempotency key {key[:16]}... is already claimed by order "
                    f"{existing.order_id} (state={existing.state.value}); refusing "
                    f"to submit {order_id} (INVARIANT 12)"
                )
            now = self._clock.now().isoformat()
            reservation = Reservation(
                key=key,
                order_id=order_id,
                state=ReservationState.RESERVED,
                reserved_at=now,
                updated_at=now,
            )
            self._reservations[key] = reservation
            self._audit.record(
                AuditCategory.ORDER,
                "idempotency_key_reserved",
                outcome=AuditOutcome.ALLOWED,
                actor="system",
                details={"idempotency_key": key, "order_id": order_id},
            )
            return reservation

    def release_unsent(self, key: str, *, reason: str) -> None:
        """Free a key that was claimed but never sent anywhere.

        Legal only from :data:`ReservationState.RESERVED`. Attempting it after
        submission raises, because we cannot prove the venue never saw it.
        """
        with self._lock:
            existing = self._reservations.get(key)
            if existing is None:
                raise KeyError(f"no reservation for key {key[:16]}...")
            if existing.state is not ReservationState.RESERVED:
                self._audit.record(
                    AuditCategory.ORDER,
                    "idempotency_release_refused",
                    outcome=AuditOutcome.REFUSED,
                    actor="system",
                    details={
                        "idempotency_key": key,
                        "state": existing.state.value,
                        "reason": reason,
                    },
                )
                raise SafetyViolation(
                    f"refusing to release idempotency key for order "
                    f"{existing.order_id}: state is {existing.state.value}, so the "
                    "request may have reached the venue. Freeing it would permit a "
                    "duplicate (INVARIANT 12)."
                )
            del self._reservations[key]
            self._audit.record(
                AuditCategory.ORDER,
                "idempotency_key_released",
                outcome=AuditOutcome.INFO,
                actor="system",
                details={
                    "idempotency_key": key,
                    "order_id": existing.order_id,
                    "reason": reason,
                },
            )

    # -- lifecycle ---------------------------------------------------------
    def _advance(
        self, key: str, target: ReservationState, *, note: str, event: str,
        outcome: AuditOutcome,
    ) -> Reservation:
        with self._lock:
            existing = self._reservations.get(key)
            if existing is None:
                raise KeyError(f"no reservation for key {key[:16]}...")
            if target is existing.state:
                return existing
            if target not in _RESERVATION_TRANSITIONS[existing.state]:
                raise SafetyViolation(
                    f"reservation for order {existing.order_id}: "
                    f"{existing.state.value} -> {target.value} is not a valid "
                    "reservation transition"
                )
            updated = Reservation(
                key=existing.key,
                order_id=existing.order_id,
                state=target,
                reserved_at=existing.reserved_at,
                updated_at=self._clock.now().isoformat(),
                note=note,
            )
            self._reservations[key] = updated
            self._audit.record(
                AuditCategory.ORDER,
                event,
                outcome=outcome,
                actor="system",
                details={
                    "idempotency_key": key,
                    "order_id": existing.order_id,
                    "from": existing.state.value,
                    "to": target.value,
                    "note": note,
                },
            )
            return updated

    def mark_submitted(self, key: str, *, note: str = "") -> Reservation:
        """Record that a request carrying this key has left the process."""
        return self._advance(
            key,
            ReservationState.SUBMITTED,
            note=note,
            event="idempotency_key_submitted",
            outcome=AuditOutcome.INFO,
        )

    def mark_settled(self, key: str, *, note: str = "") -> Reservation:
        """Record a known, final outcome."""
        return self._advance(
            key,
            ReservationState.SETTLED,
            note=note,
            event="idempotency_key_settled",
            outcome=AuditOutcome.ALLOWED,
        )

    def mark_unknown(self, key: str, *, note: str = "") -> Reservation:
        """Record that the outcome is undetermined. Blocks new orders."""
        return self._advance(
            key,
            ReservationState.UNKNOWN,
            note=note,
            event="idempotency_key_unknown",
            outcome=AuditOutcome.REFUSED,
        )

    def resolve_unknown(self, key: str, *, resolution: str) -> Reservation:
        """Clear an UNKNOWN reservation after reconciliation established the truth.

        Separate from :meth:`mark_settled` on purpose: the transition table
        forbids ``UNKNOWN -> SETTLED``, so the only way out is this explicitly
        named reconciliation path, which is greppable in the audit log.
        """
        if not resolution or not resolution.strip():
            raise ValueError("resolving an UNKNOWN reservation requires a resolution")
        with self._lock:
            existing = self._reservations.get(key)
            if existing is None:
                raise KeyError(f"no reservation for key {key[:16]}...")
            if existing.state is not ReservationState.UNKNOWN:
                raise SafetyViolation(
                    f"reservation for order {existing.order_id} is "
                    f"{existing.state.value}, not unknown; nothing to resolve"
                )
            updated = Reservation(
                key=existing.key,
                order_id=existing.order_id,
                state=ReservationState.SETTLED,
                reserved_at=existing.reserved_at,
                updated_at=self._clock.now().isoformat(),
                note=f"reconciled: {resolution}",
            )
            self._reservations[key] = updated
            self._audit.record(
                AuditCategory.RECONCILIATION,
                "idempotency_key_reconciled",
                outcome=AuditOutcome.ALLOWED,
                actor="system",
                details={
                    "idempotency_key": key,
                    "order_id": existing.order_id,
                    "resolution": resolution,
                },
            )
            return updated

    # -- queries -----------------------------------------------------------
    def get(self, key: str) -> Reservation | None:
        with self._lock:
            return self._reservations.get(key)

    def is_claimed(self, key: str) -> bool:
        with self._lock:
            return key in self._reservations

    def unknown_reservations(self) -> list[Reservation]:
        with self._lock:
            return [
                r for r in self._reservations.values()
                if r.state is ReservationState.UNKNOWN
            ]

    def has_unknown(self) -> bool:
        return bool(self.unknown_reservations())

    def in_flight(self) -> list[Reservation]:
        with self._lock:
            return [
                r for r in self._reservations.values()
                if r.state in (ReservationState.RESERVED, ReservationState.SUBMITTED)
            ]

    def __len__(self) -> int:
        with self._lock:
            return len(self._reservations)
