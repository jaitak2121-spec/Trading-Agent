"""Durable, crash-safe persistence for the pieces written through the ports.

Everything else in :mod:`trading.adapters.persistence` keeps its state in a dict
that dies with the process. These two adapters write their state to disk, so an
UNKNOWN reservation (INVARIANT 5), a claimed idempotency key (INVARIANT 12), and
the local position ledger (INVARIANT 6) survive a real process exit rather than
only a same-process ``_build_stack`` rebuild.

Why only reservations and positions
====================================

Durability here rides on an explicit write call. The idempotency registry calls
``add``/``update``/``delete`` on its reservation repository for *every* state
change, and the portfolio calls ``apply_fill``/``set_position`` on its ledger for
every position change -- so a subclass that persists after each of those calls
captures the whole truth with no change to the execution path.

Orders are deliberately absent. The gateway persists an order once, with
``orders.add(order)`` at ``DRAFT``, and then advances it *in place*
(``order.transition_to(...)``) without ever calling back into the store. A
durable order store would therefore capture only the ``DRAFT`` snapshot and miss
the PENDING_NEW/ACCEPTED/FILLED transitions that recovery needs. Closing that gap
means either a gateway ``update(order)`` call at the write-before-send checkpoint
or an on-mutation observer on :class:`~trading.core.orders.Order` -- both change
the execution chokepoint or the core order object, so both are the operator's
call, not a persistence-layer decision. Until then, durable orders are **not**
offered here, and ``docs/CLAUDE_HANDOFF.md`` records what activating them needs.

That split is itself fail-safe. After a real restart with these two adapters
wired in, a persisted UNKNOWN reservation still blocks every new order
(``has_unknown`` is read straight off disk), and a claimed key still refuses a
duplicate -- even though the order object behind it did not survive. The system
comes up *remembering it has something unresolved* rather than cheerfully
forgetting, which is the direction a safety kernel must err in.

Crash-safety
============

Each write serialises the whole collection and swaps it into place with
:func:`os.replace`, which is atomic on POSIX: a reader (including the next
process) sees either the complete old file or the complete new one, never a torn
write. The temp file is ``fsync``-ed before the swap so the bytes are on the disk
before the rename that publishes them.

A file that cannot be parsed is a **loud** failure, not a silent empty start.
Silently starting empty is precisely how a persisted UNKNOWN block would be lost,
so a corrupt reservation file raises rather than letting the system come up
unblocked.
"""

from __future__ import annotations

import json
import os
import threading
from decimal import Decimal
from pathlib import Path
from typing import Any

from ...core.dedupe import Reservation, ReservationState, ReservationStore
from ...core.money import Quantity
from ...core.reconciliation import PositionLedger
from ...ports.repository import PositionRepositoryPort
from ...ports.reservation_repository import ReservationRepositoryPort

__all__ = [
    "DurablePositionLedger",
    "DurableReservationRepository",
    "PersistenceError",
]

#: Bumped if the on-disk schema ever changes shape. A file with an unknown
#: version is refused rather than guessed at.
_SCHEMA_VERSION = 1


class PersistenceError(RuntimeError):
    """A durable store could not be read. Fail closed rather than start empty."""


def _atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    """Write ``payload`` to ``path`` so a crash leaves old-or-new, never torn.

    Temp file in the same directory (so :func:`os.replace` is a rename, not a
    cross-device copy), ``fsync`` before the swap, then a best-effort directory
    ``fsync`` so the rename itself is durable. A platform that refuses the
    directory ``fsync`` does not fail the write -- the file ``fsync`` plus the
    atomic rename is already the load-bearing guarantee.
    """
    tmp = path.with_suffix(path.suffix + ".tmp")
    data = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    with open(tmp, "wb") as handle:
        handle.write(data)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(tmp, path)
    try:
        dir_fd = os.open(str(path.parent), os.O_RDONLY)
        try:
            os.fsync(dir_fd)
        finally:
            os.close(dir_fd)
    except (OSError, AttributeError):  # pragma: no cover - platform dependent
        pass


def _load_json(path: Path) -> dict[str, Any] | None:
    """Read and validate the envelope, or ``None`` if the file does not exist."""
    if not path.exists():
        return None
    try:
        raw = path.read_text(encoding="utf-8")
        payload = json.loads(raw)
    except (OSError, ValueError) as exc:
        raise PersistenceError(
            f"could not read durable store at {path}: {exc}; refusing to start "
            "empty because that would silently drop any persisted safety state"
        ) from exc
    if not isinstance(payload, dict) or payload.get("version") != _SCHEMA_VERSION:
        raise PersistenceError(
            f"durable store at {path} has an unrecognised schema "
            f"(expected version {_SCHEMA_VERSION}); refusing to interpret it"
        )
    return payload


class DurableReservationRepository(ReservationStore):
    """A :class:`~trading.core.dedupe.ReservationStore` that writes to disk.

    Subclasses the in-memory store so every read method and the state-machine
    contract are inherited unchanged; only the three mutators persist after they
    succeed. Reads never touch the disk -- the file is loaded once at
    construction and the in-memory dict is the working copy.
    """

    def __init__(self, path: str | os.PathLike[str]) -> None:
        super().__init__()
        self._path = Path(path)
        self._load()

    def _load(self) -> None:
        payload = _load_json(self._path)
        if payload is None:
            return
        entries = payload.get("reservations", [])
        if not isinstance(entries, list):
            raise PersistenceError(
                f"durable reservation store at {self._path} is malformed: "
                "'reservations' is not a list"
            )
        with self._lock:
            for entry in entries:
                reservation = self._reservation_from_dict(entry)
                self._reservations[reservation.key] = reservation

    def _reservation_from_dict(self, entry: Any) -> Reservation:
        if not isinstance(entry, dict):
            raise PersistenceError("a reservation entry is not an object")
        try:
            return Reservation(
                key=entry["key"],
                order_id=entry["order_id"],
                state=ReservationState(entry["state"]),
                reserved_at=entry["reserved_at"],
                updated_at=entry["updated_at"],
                note=entry.get("note", ""),
            )
        except (KeyError, ValueError) as exc:
            raise PersistenceError(
                f"durable reservation store at {self._path} has an unreadable "
                f"entry: {exc}"
            ) from exc

    def _flush(self) -> None:
        with self._lock:
            payload = {
                "version": _SCHEMA_VERSION,
                "reservations": [
                    {
                        "key": r.key,
                        "order_id": r.order_id,
                        "state": r.state.value,
                        "reserved_at": r.reserved_at,
                        "updated_at": r.updated_at,
                        "note": r.note,
                    }
                    for r in self._reservations.values()
                ],
            }
            _atomic_write_json(self._path, payload)

    def add(self, reservation: Reservation) -> Reservation:
        with self._lock:
            result = super().add(reservation)
            self._flush()
            return result

    def update(self, reservation: Reservation) -> Reservation:
        with self._lock:
            result = super().update(reservation)
            self._flush()
            return result

    def delete(self, key: str) -> None:
        with self._lock:
            super().delete(key)
            self._flush()


class DurablePositionLedger(PositionLedger):
    """A :class:`~trading.core.reconciliation.PositionLedger` that writes to disk.

    Subclasses the in-memory ledger; ``apply_fill`` and ``set_position`` persist
    after they succeed, so the local view of what we hold survives a restart and
    the reconciliation gate (INVARIANT 6) is comparing a real baseline rather
    than an empty one.
    """

    def __init__(self, path: str | os.PathLike[str]) -> None:
        super().__init__()
        self._path = Path(path)
        self._load()

    def _load(self) -> None:
        payload = _load_json(self._path)
        if payload is None:
            return
        positions = payload.get("positions", {})
        if not isinstance(positions, dict):
            raise PersistenceError(
                f"durable position ledger at {self._path} is malformed: "
                "'positions' is not an object"
            )
        with self._lock:
            for symbol, entry in positions.items():
                self._positions[symbol] = self._quantity_from_dict(symbol, entry)

    def _quantity_from_dict(self, symbol: str, entry: Any) -> Quantity:
        if not isinstance(entry, dict):
            raise PersistenceError(f"position entry for {symbol} is not an object")
        try:
            return Quantity(Decimal(entry["amount"]), entry["asset"])
        except (KeyError, ValueError, ArithmeticError, TypeError) as exc:
            raise PersistenceError(
                f"durable position ledger at {self._path} has an unreadable "
                f"entry for {symbol}: {exc}"
            ) from exc

    def _flush(self) -> None:
        with self._lock:
            payload = {
                "version": _SCHEMA_VERSION,
                "positions": {
                    symbol: {"amount": str(q.amount), "asset": q.asset}
                    for symbol, q in self._positions.items()
                },
            }
            _atomic_write_json(self._path, payload)

    def apply_fill(self, symbol: str, side: Any, quantity: Quantity) -> Quantity:
        with self._lock:
            result = super().apply_fill(symbol, side, quantity)
            self._flush()
            return result

    def set_position(self, symbol: str, quantity: Quantity) -> None:
        with self._lock:
            super().set_position(symbol, quantity)
            self._flush()


# Conformance, declared from the adapter side exactly as the in-memory adapters
# do it. The signature check in tests/test_ports.py is the real guarantee.
ReservationRepositoryPort.register(DurableReservationRepository)
PositionRepositoryPort.register(DurablePositionLedger)
