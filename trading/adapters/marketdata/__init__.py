"""Market-data ingestion: the boundary that turns raw inbound messages into
validated, timestamped quotes and survives malformed messages and connection
interruptions without crashing or fabricating prices.

A data-plane adapter. It writes to a
:class:`~trading.adapters.memory.quote_feed.InMemoryQuoteFeed` and holds no
gateway, broker, or token -- so market data informs decisions but can never be
one, and a feed outage becomes an ordinary risk refusal through the existing
``MARK_PRICE_AVAILABLE`` gate rather than a new path.

* :class:`~trading.adapters.marketdata.source.MarketDataSource` -- the injected,
  network-free inbound boundary (a real feed is deferred behind a verified
  vendor contract).
* :class:`~trading.adapters.marketdata.normalizer.QuoteNormalizer` -- raw mapping
  to :class:`~trading.core.marketdata.Quote`, refusing anything it cannot read.
* :class:`~trading.adapters.marketdata.ingestor.MarketDataIngestor` -- the
  deterministic ``poll_once`` driver that applies the valid, counts the rest,
  and marks data unavailable on a disconnect.

Bars have the parallel set: :class:`~trading.adapters.marketdata.candle_normalizer.CandleNormalizer`
(raw mapping to :class:`~trading.core.marketdata.Candle`),
:class:`~trading.adapters.marketdata.candle_store.InMemoryCandleStore`, and
:class:`~trading.adapters.marketdata.candle_ingestor.CandleIngestor`.
"""

from __future__ import annotations

from .candle_ingestor import BarGap, CandleIngestReport, CandleIngestor
from .candle_normalizer import CandleNormalizer
from .candle_store import InMemoryCandleStore
from .ingestor import (
    FeedState,
    IngestReport,
    MarketDataIngestor,
    RejectedMessage,
    SequenceGap,
)
from .normalizer import MalformedMessage, QuoteNormalizer
from .source import (
    FeedDisconnected,
    FeedError,
    FeedTimeout,
    FeedUnavailable,
    MarketDataSource,
    RawMessage,
)
from .timestamps import BadTimestamp, parse_timestamp

__all__ = [
    "BadTimestamp",
    "BarGap",
    "CandleIngestReport",
    "CandleIngestor",
    "CandleNormalizer",
    "FeedDisconnected",
    "FeedError",
    "FeedState",
    "FeedTimeout",
    "FeedUnavailable",
    "InMemoryCandleStore",
    "IngestReport",
    "MalformedMessage",
    "MarketDataIngestor",
    "MarketDataSource",
    "QuoteNormalizer",
    "RawMessage",
    "RejectedMessage",
    "SequenceGap",
    "parse_timestamp",
]
