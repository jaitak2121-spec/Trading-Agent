"""The runnable application layer: composition root, paper session, reporting.

This package is a *driver*, exactly like :mod:`trading.adapters.lifecycle`: it
imports downward into :mod:`trading.core`, :mod:`trading.ports`,
:mod:`trading.adapters` and :mod:`trading.strategy`, assembles them into a system
a human can actually run, and is imported by nothing under ``trading/``. It adds
no execution path -- every order still goes through
:meth:`trading.core.gateway.ExecutionGateway.submit` -- and it touches no file in
the pure kernel, so no safety invariant moves.

What it provides:

* :func:`~trading.app.composition.build_paper_stack` -- the production-shaped
  equivalent of the test harness's ``build_rig``: a full safety kernel behind a
  :class:`~trading.adapters.paper.broker.PaperBroker`, in PAPER mode with live
  trading off.
* :class:`~trading.app.paper_session.PaperTradingSession` -- the loop that feeds
  market data to a strategy, sizes its signals under the risk engine, submits
  the result through the gateway, and progresses resting orders.
* :class:`~trading.app.report.SessionReport` -- a redacted, read-only picture of
  what the session did: orders, fills, positions, P&L, and readiness.
"""

from __future__ import annotations

from .composition import PaperStack, build_paper_stack
from ..adapters.http import DashboardServer
from .dashboard import OperatorDashboard
from .paper_session import MarketFrame, PaperTradingSession, SymbolBar
from .paper_service import PaperServiceCycle, RealtimePaperService
from .order_policy import MarketableLimitPolicy, VenueMarketSpecification
from .coinswitch_market import (
    CoinSwitchMarketMetadataMalformed,
    CoinSwitchTradeInfoTranslator,
)
from .coinswitch_observation import (
    CoinSwitchObservationReport,
    run_coinswitch_observation,
)
from .report import OrderOutcome, PositionLine, SessionReport
from .runtime import (
    PaperApplication,
    build_coinswitch_paper_application,
    build_demo_paper_application,
)
from .soak import SoakReport, run_soak, run_timed_soak
from .supervisor import PaperServiceSupervisor, SupervisorStatus
from ..adapters.process import GracefulShutdown
from .restart_drill import RestartDrillReport, run_restart_drill
from .release import ReleaseArtifactReport, verify_release_wheel
from .paper_recovery import (
    PAPER_POSITION_RESET_CONFIRMATION,
    PaperPositionRecoveryReport,
    recover_interrupted_paper_position,
)

__all__ = [
    "MarketFrame",
    "MarketableLimitPolicy",
    "CoinSwitchMarketMetadataMalformed",
    "CoinSwitchObservationReport",
    "CoinSwitchTradeInfoTranslator",
    "VenueMarketSpecification",
    "DashboardServer",
    "OrderOutcome",
    "OperatorDashboard",
    "PaperStack",
    "PaperTradingSession",
    "PaperServiceCycle",
    "PaperServiceSupervisor",
    "PaperApplication",
    "RealtimePaperService",
    "PositionLine",
    "SessionReport",
    "SupervisorStatus",
    "GracefulShutdown",
    "RestartDrillReport",
    "ReleaseArtifactReport",
    "PAPER_POSITION_RESET_CONFIRMATION",
    "PaperPositionRecoveryReport",
    "SoakReport",
    "SymbolBar",
    "build_paper_stack",
    "build_coinswitch_paper_application",
    "build_demo_paper_application",
    "run_soak",
    "run_coinswitch_observation",
    "run_timed_soak",
    "run_restart_drill",
    "verify_release_wheel",
    "recover_interrupted_paper_position",
]
