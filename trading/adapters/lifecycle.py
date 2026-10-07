"""The schedule that makes a later fill arrive without anyone asking.

``ExecutionGateway.sync_order`` is the route a fill discovered after the ack
takes into the portfolio, and ``PaperBroker.fetch_order_state`` gives it
something true to read. Both halves existed before this module; what was missing
was the *schedule*. Until something called ``sync_order`` on its own, a resting
order advanced only when an operator thought to ask, and an unattended system
booked nothing.

**This is a driver, not a kernel component.** Every component in
:mod:`trading.core` answers a question; the gateway is the only one that acts.
A poller answers nothing -- it converts the passage of time into gateway calls,
and it spawns a thread to do it. Both of those are runtime concerns, so it lives
here. The kernel only ever *takes* locks so that it stays correct when something
like this calls it from another thread.

**It has no lifecycle opinions of its own,** which is the property that keeps a
second interpretation of order state from growing here. It does not decide what
is pollable: :meth:`~trading.core.orders.OrderStore.open_orders` does, and
``is_open`` is exactly PENDING_NEW, ACCEPTED and PARTIALLY_FILLED -- so every
terminal state *and* every UNKNOWN order is already excluded, without this module
naming a single state. It does not decide what is authorized: ``authorize`` does,
inside the gateway. It does not interpret the venue's answer, book a fill, or
write an audit record; ``sync_order`` does all three, under the gateway lock.
Everything below is candidate selection, exception routing, and a thread.

That division is also why the candidate list is read *outside* the gateway lock
and never rechecked here. ``sync_order`` re-reads the order's state under the
lock, so an order that finishes between selection and the call is refused by the
gateway rather than by a guess made here -- the refusal lands in the report and
the audit trail, and the sweep continues. Re-reading it here would be the
duplicate check, and it would still be racy.

Repeating a sweep is safe for the same reason repeating a single sync is safe:
the venue's answer is cumulative and only the delta is booked, so a second sweep
over the same orders books nothing. Two concurrent sweeps are therefore also
safe -- the gateway serialises them and the loser sees refusals, not double
fills -- which is why nothing here holds a lock across a sweep.

No network, no credentials, no live-trading capability: this module cannot do
anything an operator calling ``sync_order`` by hand could not do.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass

from ..core.authz import Principal
from ..core.errors import ConfigurationError, SafetyViolation, UnauthorizedAction
from ..core.gateway import ExecutionGateway
from ..core.orders import OrderStore

__all__ = [
    "DEFAULT_INTERVAL_SECONDS",
    "DEFAULT_STOP_TIMEOUT_SECONDS",
    "LifecyclePoller",
    "PollNote",
    "PollReport",
]

#: How often the worker sweeps. At the paper venue this is also the granularity
#: of a fill, since nothing there advances between polls -- see SAFETY.md §5.
DEFAULT_INTERVAL_SECONDS = 5.0

#: How long :meth:`LifecyclePoller.stop` waits for the worker to finish its
#: current sweep. Long enough for a slow venue read, short enough that a hung
#: worker is reported rather than waited on forever.
DEFAULT_STOP_TIMEOUT_SECONDS = 30.0


@dataclass(frozen=True, slots=True)
class PollNote:
    """One order the sweep did not sync, and why it did not."""

    order_id: str
    reason: str


@dataclass(frozen=True, slots=True)
class PollReport:
    """What one sweep did.

    The three buckets are kept apart deliberately. ``refused`` is the system
    working -- the gateway declining an order that finished, or an answer that
    contradicts the book -- and ``failed`` is the venue or the wiring
    misbehaving, where nothing is known about the order at all. Collapsing them
    into one count would be exactly the silent conversion of "we could not find
    out" into "nothing to worry about" that this poller must not perform.
    """

    synced: tuple[str, ...] = ()
    refused: tuple[PollNote, ...] = ()
    failed: tuple[PollNote, ...] = ()

    @property
    def considered(self) -> int:
        """How many open orders this sweep offered to the gateway."""
        return len(self.synced) + len(self.refused) + len(self.failed)


class LifecyclePoller:
    """Sweeps the open orders and asks the gateway to sync each one.

    ``poll_once`` is the whole of the behaviour and is synchronous, so it can be
    driven directly by a test or by an operator. ``start``/``stop`` add nothing
    but a thread that calls it on an interval, which is why almost nothing can
    go wrong in the threaded path that cannot be reproduced without a thread.

    ``identity`` must hold :attr:`~trading.core.authz.Action.RECONCILE`;
    ``Role.SYSTEM`` holds it precisely so an unattended poller can. It is *not*
    checked here. The gateway's ``authorize`` is the one place that decides who
    may reconcile, and a second copy of that rule here could drift from it; a
    misconfigured identity instead raises
    :class:`~trading.core.errors.UnauthorizedAction` out of the first sweep,
    loudly, which is what the propagation rule below is for.
    """

    def __init__(
        self,
        *,
        gateway: ExecutionGateway,
        orders: OrderStore,
        identity: Principal,
        interval_seconds: float = DEFAULT_INTERVAL_SECONDS,
    ) -> None:
        interval = float(interval_seconds)
        # ``not interval > 0`` rather than ``interval <= 0``: NaN fails every
        # comparison, and Event.wait(NaN) is a busy loop with no error.
        if not interval > 0:
            raise ConfigurationError(
                f"interval_seconds must be positive, got {interval_seconds!r}"
            )

        self._gateway = gateway
        self._orders = orders
        self._identity = identity
        self._interval_seconds = interval
        #: Guards the worker bookkeeping below, and nothing else. Never held
        #: across a call into the gateway.
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._last_report: PollReport | None = None
        self._last_error: BaseException | None = None
        self._passes = 0

    # -- configuration ----------------------------------------------------

    @property
    def identity(self) -> Principal:
        return self._identity

    @property
    def interval_seconds(self) -> float:
        return self._interval_seconds

    # -- one sweep --------------------------------------------------------

    def poll_once(self) -> PollReport:
        """Offer every open order to ``sync_order``, and report what happened.

        Candidate selection is ``open_orders()`` and nothing else, so a terminal
        order is never fetched and an UNKNOWN one is never touched -- not
        because this method checks, but because it cannot see them.

        Exception routing is the only judgement here, and it has three rules:

        * :class:`~trading.core.errors.UnauthorizedAction` **propagates.** It
          means this poller may not reconcile at all, so every remaining order
          would fail the same way; and ``authorize`` refuses *without* auditing,
          so swallowing it would leave a misconfigured poller sweeping nothing,
          forever, with no trace in the audit log or anywhere else.
        * A :class:`~trading.core.errors.SafetyViolation` is one order's
          answer, not the sweep's. The gateway has already audited it. It lands
          in ``refused`` and the sweep continues.
        * Anything else -- a venue read that raised -- lands in ``failed`` and
          the sweep continues. The order is left exactly as it was: a read that
          failed is not an order we cannot account for, so it is emphatically
          not marked UNKNOWN here, and it is not treated as finished either.
        """
        synced: list[str] = []
        refused: list[PollNote] = []
        failed: list[PollNote] = []

        for order in self._orders.open_orders():
            try:
                self._gateway.sync_order(order, operator=self._identity)
            except UnauthorizedAction:
                # Ordered before SafetyViolation, which it subclasses.
                raise
            except SafetyViolation as exc:
                refused.append(PollNote(order.order_id, str(exc)))
            except Exception as exc:
                failed.append(
                    PollNote(order.order_id, f"{type(exc).__name__}: {exc}")
                )
            else:
                synced.append(order.order_id)

        report = PollReport(tuple(synced), tuple(refused), tuple(failed))
        with self._lock:
            self._last_report = report
            self._passes += 1
        return report

    # -- the worker -------------------------------------------------------

    def start(self) -> None:
        """Run ``poll_once`` on the interval until :meth:`stop`.

        A second ``start`` raises rather than quietly doing nothing. Two workers
        against one gateway would not corrupt anything -- the delta logic makes
        the duplicate sweep a no-op -- but it means two components each believe
        they own the poller, and that is a wiring bug worth hearing about.
        """
        with self._lock:
            if self._thread is not None:
                raise SafetyViolation(
                    "lifecycle poller is already running; starting a second "
                    "worker means something else already owns this one"
                )
            self._stop.clear()
            self._last_error = None
            # Daemon: a poller holds no state that must be flushed on the way
            # out, since the gateway audits each order synchronously before the
            # sweep moves on. A non-daemon worker would just delay shutdown.
            self._thread = threading.Thread(
                target=self._run, name="lifecycle-poller", daemon=True
            )
            # Started under the lock so stop() can never see a thread it is not
            # yet allowed to join.
            self._thread.start()

    def stop(self, *, timeout_seconds: float = DEFAULT_STOP_TIMEOUT_SECONDS) -> None:
        """Ask the worker to finish its sweep and exit, and wait for it.

        Idempotent, and a no-op when nothing is running -- asymmetric with
        :meth:`start` on purpose. Starting twice produces a second worker;
        stopping twice produces nothing, and making it raise would only make
        cleanup harder to write correctly.

        Returning means the worker is done. If it will not stop, that is
        reported rather than ignored -- and the worker stays on the books, so
        ``is_running`` keeps saying True and a fresh :meth:`start` keeps
        refusing. A poller that would not stop is not a poller that stopped.
        """
        # Set before the bookkeeping, so the worker sees it at the earliest
        # opportunity even if it is mid-sweep.
        self._stop.set()
        with self._lock:
            thread = self._thread
        if thread is None:
            return
        thread.join(timeout_seconds)
        if thread.is_alive():
            raise SafetyViolation(
                f"lifecycle poller did not stop within {timeout_seconds} "
                "seconds; a sweep is still in progress"
            )
        with self._lock:
            # Only if it is still ours: a worker that failed on its own has
            # already cleared this, and nothing else may have replaced it,
            # since start() refuses while a thread is on the books.
            if self._thread is thread:
                self._thread = None

    def _run(self) -> None:
        """The worker body. Paced by the stop event, so stop() is prompt.

        The interval is real time from :class:`threading.Event`, not the
        injected clock: a sleeping thread is not something a ``ManualClock`` can
        wake, and pacing is a schedule rather than a safety control. Every
        behaviour worth asserting lives in ``poll_once``, which needs no clock
        at all.
        """
        try:
            while not self._stop.is_set():
                self.poll_once()
                self._stop.wait(self._interval_seconds)
        except BaseException as exc:  # noqa: BLE001 - recorded, see below
            # Only an unauthorized identity or a bug in this module reaches
            # here; poll_once handles everything an order can raise. Record it
            # and exit, so a broken poller reads as stopped-with-an-error
            # rather than either spinning on the same failure forever or
            # vanishing into a stderr traceback. Surfacing `last_error` to an
            # operator belongs to the monitoring stage.
            self._stop.set()
            with self._lock:
                self._last_error = exc
                self._thread = None

    # -- observation ------------------------------------------------------

    @property
    def is_running(self) -> bool:
        with self._lock:
            thread = self._thread
        return thread is not None and thread.is_alive()

    @property
    def passes(self) -> int:
        """Completed sweeps. A worker that is alive and not advancing shows here."""
        with self._lock:
            return self._passes

    @property
    def last_report(self) -> PollReport | None:
        with self._lock:
            return self._last_report

    @property
    def last_error(self) -> BaseException | None:
        """Why the worker stopped, if it stopped on its own. ``None`` otherwise."""
        with self._lock:
            return self._last_error

    def __repr__(self) -> str:
        return (
            f"LifecyclePoller(identity={self._identity.principal_id!r}, "
            f"interval_seconds={self._interval_seconds!r}, "
            f"running={self.is_running})"
        )
