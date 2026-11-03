"""CoinSwitch public market-data adapters.

Only the unauthenticated Spot Socket.IO stream is implemented here.  This
package contains no order transport and accepts no API credentials.
"""

from __future__ import annotations

from .spot_stream import CoinSwitchSpotStream
from .spot_contract import CoinSwitchSpotResponseTranslator

__all__ = ["CoinSwitchSpotResponseTranslator", "CoinSwitchSpotStream"]
