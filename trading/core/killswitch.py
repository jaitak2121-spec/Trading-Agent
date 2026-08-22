"""Kill switch.

INVARIANT 10: the kill switch prevents new orders.

Three properties make it trustworthy:

* **Latching.** Once engaged it stays engaged until an operator explicitly
  releases it. A transient condition that trips the switch does not un-trip
  itself when the condition clears -- a human decides when trading resumes.
* **Fail closed.** If the switch's state cannot be determined (an I/O error
  probing the trigger file), it reports ENGAGED. "I don't know" must never mean
  "carry on trading".
* **Asymmetric authority.** Every operational role may engage it; only
  ``Role.OPERATOR`` may release it. Stopping is never gated on privilege.

The optional trigger file is an out-of-band control: an operator can stop the
system by touching a path, with no API call, no login, and no working
application code required.
"""

from __future__ import annotations

import os
import threading
from typing import Callable, Final

from .audit import AuditCategory, AuditLog, AuditOutcome
from .authz import Action, Principal, authorize
from .clock import Clock, SystemClock
from .errors import KillSwitchEngaged, SafetyViolation

__all__ = ["KillSwitch", "PresenceProbe", "file_presence_probe"]

#: Returns True if the trigger is present, False if absent. May raise OSError,
#: which the switch treats as "engaged".
PresenceProbe = Callable[[str], bool]


def file_presence_probe(path: str) -> bool:
    """Default probe. Uses ``os.stat`` so real I/O errors surface as ``OSError``.

    ``os.path.exists`` would swallow a permission or I/O error and return
    ``False``, which is exactly the wrong default for a safety control.
    """
    try:
        os.stat(path)
    except FileNotFoundError:
        return False
    return True


class KillSwitch:
    """A latching, fail-closed stop control."""

    def __init__(
        self,
        audit: AuditLog,
        *,
        trigger_path: str | None = None,
        clock: Clock | None = None,
        presence_probe: PresenceProbe | None = None,
    ) -> None:
        self._audit = audit
        self._trigger_path = trigger_path
        self._clock = clock if clock is not None else SystemClock()
        self._probe = presence_probe if presence_probe is not None else file_presence_probe
        self._engaged = False
        self._reason: str | None = None
        self._engaged_by: str | None = None
        self._engaged_at: str | None = None
        self._lock = threading.RLock()

    # -- state -------------------------------------------------------------
    @property
    def trigger_path(self) -> str | None:
        return self._trigger_path

    @property
    def reason(self) -> str | None:
        with self._lock:
            return self._reason

    @property
    def engaged_by(self) -> str | None:
        with self._lock:
            return self._engaged_by

    @property
    def is_engaged(self) -> bool:
        """True if trading must stop.

        Checks the in-memory latch first, then the trigger file. A file that
        appears latches the in-memory flag, so removing the file afterwards
        does not silently resume trading.
        """
        with self._lock:
            if self._engaged:
                return True
            if self._trigger_path is None:
                return False
            try:
                present = self._probe(self._trigger_path)
            except OSError as exc:
                # Cannot determine state -> assume the worst.
                self._latch(
                    reason=f"trigger file could not be probed: {type(exc).__name__}",
                    actor="system",
                    cause="probe_error",
                )
                return True
            if present:
                self._latch(
                    reason=f"trigger file present at {self._trigger_path}",
                    actor="external",
                    cause="trigger_file",
                )
                return True
            return False

    def _latch(self, *, reason: str, actor: str, cause: str) -> None:
        """Set the latch and audit it. Caller holds the lock."""
        if self._engaged:
            return
        self._engaged = True
        self._reason = reason
        self._engaged_by = actor
        self._engaged_at = self._clock.now().isoformat()
        self._audit.record(
            AuditCategory.KILL_SWITCH,
            "kill_switch_engaged",
            outcome=AuditOutcome.REFUSED,
            actor=actor,
            details={"reason": reason, "cause": cause},
        )

    # -- controls ----------------------------------------------------------
    def engage(self, principal: Principal, *, reason: str) -> None:
        """Engage the switch. Permitted to every operational role."""
        authorize(principal, Action.ENGAGE_KILL_SWITCH)
        if not reason or not reason.strip():
            raise ValueError("engaging the kill switch requires a reason")
        with self._lock:
            if self._engaged:
                self._audit.record(
                    AuditCategory.KILL_SWITCH,
                    "kill_switch_engage_noop",
                    outcome=AuditOutcome.INFO,
                    actor=principal.principal_id,
                    details={"reason": reason, "already_engaged_because": self._reason},
                )
                return
            self._latch(reason=reason, actor=principal.principal_id, cause="manual")

    def release(self, principal: Principal, *, reason: str) -> None:
        """Release the switch. Operator only.

        Refuses while the trigger file is still present: releasing would be a
        lie, since the next :attr:`is_engaged` check would immediately re-latch.
        """
        authorize(principal, Action.RELEASE_KILL_SWITCH)
        if not reason or not reason.strip():
            raise ValueError("releasing the kill switch requires a reason")
        with self._lock:
            if self._trigger_path is not None:
                try:
                    still_present = self._probe(self._trigger_path)
                except OSError as exc:
                    self._audit.record(
                        AuditCategory.KILL_SWITCH,
                        "kill_switch_release_refused",
                        outcome=AuditOutcome.REFUSED,
                        actor=principal.principal_id,
                        details={"cause": "probe_error", "error": type(exc).__name__},
                    )
                    raise SafetyViolation(
                        "cannot release the kill switch: the trigger file state "
                        f"could not be determined ({type(exc).__name__})"
                    ) from exc
                if still_present:
                    self._audit.record(
                        AuditCategory.KILL_SWITCH,
                        "kill_switch_release_refused",
                        outcome=AuditOutcome.REFUSED,
                        actor=principal.principal_id,
                        details={"cause": "trigger_file_still_present"},
                    )
                    raise SafetyViolation(
                        f"cannot release the kill switch while {self._trigger_path} "
                        "exists; remove the trigger file first"
                    )
            was_engaged = self._engaged
            previous_reason = self._reason
            self._engaged = False
            self._reason = None
            self._engaged_by = None
            self._engaged_at = None
            self._audit.record(
                AuditCategory.KILL_SWITCH,
                "kill_switch_released",
                outcome=AuditOutcome.ALLOWED,
                actor=principal.principal_id,
                details={
                    "reason": reason,
                    "was_engaged": was_engaged,
                    "previous_reason": previous_reason,
                },
            )

    # -- gate --------------------------------------------------------------
    def require_not_engaged(self) -> None:
        """Raise :class:`KillSwitchEngaged` if trading must stop."""
        if self.is_engaged:
            raise KillSwitchEngaged(
                f"kill switch is engaged: {self.reason}. No new orders will be "
                "accepted until an operator releases it (INVARIANT 10)."
            )
