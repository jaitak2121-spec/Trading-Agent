"""CoinSwitch public market-data adapters.

The public stream, separate GET-only historical downloader and offline Spot
contract tools live here. There is no HTTP order transport or live launcher.
"""

from __future__ import annotations

from .spot_stream import CoinSwitchSpotStream
from .spot_contract import CoinSwitchSpotResponseTranslator

__all__ = ["CoinSwitchSpotResponseTranslator", "CoinSwitchSpotStream"]
