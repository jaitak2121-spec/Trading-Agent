"""An in-process quote feed, including the ways a real one fails.

``StaticMarketData`` in this package answers "what is the price?"; this answers
"what is the price, and when was it true?" -- which is the question a live feed
has to answer and the one staleness handling depends on.

The failure modes are first-class here, not an afterthought, because they are the
ones that are hard to arrange against a real venue and easy to get wrong:

* :meth:`InMemoryQuoteFeed.freeze` -- the feed keeps answering with a quote whose
  timestamp stops advancing. This is what a dead websocket looks like from the
  inside: no error, no gap, just a number that no longer moves. The most
  dangerous market-data failure there is, and the reason
  :class:`~trading.core.marketdata.StalenessPolicy` exists.
* :meth:`InMemoryQuoteFeed.go_dark` -- the symbol disappears entirely. Honest,
  and therefore the easy case.
* :meth:`InMemoryQuoteFeed.set_clock_skew` -- the venue's clock runs ahead of
  ours, so quotes arrive stamped in the future.

Out-of-order delivery is handled rather than simulated: a publish carrying a
sequence number at or below the one already stored is discarded, because a
late-arriving older tick must not overwrite a newer one.
"""

from __future__ import annotations

import datetime as _dt
import threading
from typing import Iterable

from ...core.clock import Clock
from ...core.marketdata import Quote
from ...core.money import Price
from ...ports.market_data import QuoteFeedPort

__all__ = ["InMemoryQuoteFeed"]


class InMemoryQuoteFeed(QuoteFeedPort):
    """A quote feed backed by a dict, with the clock injected.

    Timestamps come from the injected :class:`~trading.core.clock.Clock` unless a
    publisher supplies one, so a test can age a quote by moving the clock instead
    of sleeping.
    """

    def __init__(self, *, clock: Clock, source: str = "memory") -> None:
        if not isinstance(clock, Clock):
            raise TypeError("clock must be a Clock")
        if not isinstance(source, str) or not source.strip():
            raise ValueError("source must be a non-empty string")
        self._clock = clock
        self._source = source
        self._lock = threading.Lock()
        self._quotes: dict[str, Quote] = {}
        self._frozen: set[str] = set()
        self._skew_seconds = 0.0
        self._rejected_out_of_order = 0

    # -- the port ---------------------------------------------------------

    def quote(self, symbol: str) -> Quote | None:
        with self._lock:
            return self._quotes.get(symbol)

    def symbols(self) -> Iterable[str]:
        with self._lock:
            return sorted(self._quotes)

    # -- publishing -------------------------------------------------------

    def publish(
        self,
        symbol: str,
        bid: Price,
        ask: Price,
        *,
        as_of: _dt.datetime | None = None,
        sequence: int | None = None,
    ) -> Quote | None:
        """Store a quote. Returns it, or ``None`` if it was discarded as stale.

        Rejects a non-``Price`` before it can reach the store (INVARIANT 8), so a
        float cannot enter the system through a feed any more than through a
        constructor.
        """
        for name, value in (("bid", bid), ("ask", ask)):
            if not isinstance(value, Price):
                raise TypeError(
                    f"{name} must be a Price, got {type(value).__name__}; "
                    "the feed boundary rejects floats (INVARIANT 8)"
                )
        stamp = as_of if as_of is not None else self._stamp()
        quote = Quote(
            symbol=symbol,
            bid=bid,
            ask=ask,
            as_of=stamp,
            source=self._source,
            sequence=sequence,
        )
        with self._lock:
            existing = self._quotes.get(symbol)
            if (
                existing is not None
                and existing.sequence is not None
                and sequence is not None
                and sequence <= existing.sequence
            ):
                # A tick that took a slower path than one we already have.
                # Applying it would move the book backwards in time.
                self._rejected_out_of_order += 1
                return None
            self._quotes[symbol] = quote
            self._frozen.discard(symbol)
            return quote

    def publish_last(
        self, symbol: str, last: Price, *, spread_bps: int = 2, **kwargs
    ) -> Quote | None:
        """Convenience for a one-sided source: synthesise a book around ``last``.

        Some venues publish only a last-traded price. Widening it into a
        symmetric book is a *fabrication*, and it is confined to this test
        adapter deliberately -- a real adapter must publish the venue's actual
        bid and ask, because a synthesised spread understates the cost of
        crossing and would make every risk check optimistic.
        """
        if not isinstance(last, Price):
            raise TypeError("last must be a Price")
        half = last.amount * spread_bps / 20_000
        return self.publish(
            symbol,
            Price(last.amount - half, last.currency, max_scale=12),
            Price(last.amount + half, last.currency, max_scale=12),
            **kwargs,
        )

    # -- failure injection ------------------------------------------------

    def freeze(self, symbol: str) -> None:
        """Stop the symbol's timestamp advancing: a live-looking dead feed.

        Nothing more is needed to simulate it -- the stored quote simply is not
        replaced, so it ages while the clock moves. Recorded explicitly so a test
        reads as the scenario it means, and so :attr:`frozen_symbols` can report
        it.
        """
        with self._lock:
            if symbol not in self._quotes:
                raise KeyError(f"cannot freeze {symbol!r}: no quote to freeze")
            self._frozen.add(symbol)

    def go_dark(self, symbol: str) -> None:
        """Drop the symbol entirely, as a feed that stops covering it would."""
        with self._lock:
            self._quotes.pop(symbol, None)
            self._frozen.discard(symbol)

    def go_dark_entirely(self) -> None:
        """Drop every symbol: total feed loss."""
        with self._lock:
            self._quotes.clear()
            self._frozen.clear()

    def set_clock_skew(self, seconds: float) -> None:
        """Offset the venue's stamping clock from ours.

        Positive means the venue runs ahead, so quotes arrive stamped in the
        future. The staleness policy must distrust those rather than count them
        as maximally fresh.
        """
        if isinstance(seconds, bool) or not isinstance(seconds, (int, float)):
            raise TypeError("seconds must be a number")
        with self._lock:
            self._skew_seconds = float(seconds)

    # -- introspection ----------------------------------------------------

    @property
    def frozen_symbols(self) -> list[str]:
        with self._lock:
            return sorted(self._frozen)

    @property
    def rejected_out_of_order(self) -> int:
        """How many publishes were discarded for arriving late."""
        with self._lock:
            return self._rejected_out_of_order

    def _stamp(self) -> _dt.datetime:
        with self._lock:
            skew = self._skew_seconds
        return self._clock.now() + _dt.timedelta(seconds=skew)
