"""The composition root for a runnable paper-trading stack.

``tests/harness.py:build_rig`` wires the whole system for tests; this is the
production-shaped equivalent, for actually running a paper session. It assembles
exactly the same safety kernel -- the gateway and its ten gates, the risk
engine, the idempotency registry, the reconciliation gate, the kill switch, the
breakers, the mode machine, and the audit log -- behind a
:class:`~trading.adapters.paper.broker.PaperBroker` fed by an
:class:`~trading.adapters.memory.quote_feed.InMemoryQuoteFeed`, in
:attr:`~trading.core.modes.TradingMode.PAPER` with live trading off.

Nothing here is a new kernel component. Every object constructed below already
exists and is already tested; this module only decides the wiring. The result is
a frozen :class:`PaperStack` of read-only handles that
:class:`~trading.app.paper_session.PaperTradingSession` drives.

Safety posture, by construction:

* ``live_trading=False`` and no confirmation phrase, so
  :attr:`~trading.core.config.TradingConfig.is_live_authorized` is False
  (INVARIANTS 1/2).
* The mode machine is walked only as far as ``PAPER``.
* The broker is the in-process paper venue: no network, no credentials.
* Four distinct principals plus a system identity, because several invariants
  are precisely about one of them being unable to do another's job.
"""

from __future__ import annotations

import os
from dataclasses import dataclass

from ..adapters.memory.quote_feed import InMemoryQuoteFeed
from ..adapters.operations import (
    OperationalMonitor,
    StartupCoordinator,
    StartupReport,
)
from ..adapters.paper.broker import PaperBroker
from ..adapters.reconciliation import ReconciliationCoordinator
from ..adapters.recovery import RestartRecoveryCoordinator
from ..adapters.persistence import (
    SQLiteAuditSink,
    SQLiteOrderStore,
    SQLitePortfolio,
    SQLitePositionLedger,
    SQLiteReservationRepository,
    SQLiteRiskEngine,
    SQLiteState,
)
from ..core.audit import AuditLog, AuditSink, InMemoryAuditSink
from ..core.authz import Principal, Role
from ..core.breaker import BreakerRegistry, CircuitBreaker
from ..core.clock import Clock, ManualClock
from ..core.config import RiskConfig, TradingConfig
from ..core.dedupe import IdempotencyRegistry, ReservationStore
from ..core.gateway import ExecutionGateway
from ..core.killswitch import KillSwitch
from ..core.marketdata import StalenessPolicy
from ..core.modes import TradingMode, TradingModeMachine
from ..core.money import USD, Money
from ..core.orders import OrderStore
from ..core.portfolio import Portfolio
from ..core.reconciliation import PositionLedger, ReconciliationGate
from ..core.risk import RiskEngine

__all__ = ["PaperStack", "build_paper_stack"]

#: The paper portfolio's starting cash. Large, round, and obviously not real --
#: a paper account is not making a statement about position size.
DEFAULT_STARTING_CASH: Money = Money("1000000.00", USD)


@dataclass(frozen=True)
class PaperStack:
    """Every wired component a paper session needs. Read-only handles.

    Returned by :func:`build_paper_stack`; never constructed by hand. Holding the
    components (rather than hiding them behind the gateway) is deliberate: the
    session reads the portfolio and ledger to size and report, and a test needs
    to reach the feed to publish quotes and the audit sink to assert on records.
    None of these handles is an execution path -- only the gateway executes.
    """

    clock: Clock
    audit: AuditLog
    sink: AuditSink
    persistence: SQLiteState | None
    config: TradingConfig
    feed: InMemoryQuoteFeed
    broker: PaperBroker
    orders: OrderStore
    ledger: PositionLedger
    portfolio: Portfolio
    reconciliation: ReconciliationGate
    risk: RiskEngine
    dedupe: IdempotencyRegistry
    kill_switch: KillSwitch
    breakers: BreakerRegistry
    modes: TradingModeMachine
    gateway: ExecutionGateway
    recovery: RestartRecoveryCoordinator
    reconciliation_coordinator: ReconciliationCoordinator
    startup: StartupReport
    monitor: OperationalMonitor
    staleness: StalenessPolicy
    strategy_id: Principal
    risk_id: Principal
    gateway_id: Principal
    operator_id: Principal
    system_id: Principal
    bot: object = None


def build_paper_stack(
    *,
    starting_cash: Money = DEFAULT_STARTING_CASH,
    risk: RiskConfig | None = None,
    slippage_bps: int = 0,
    fee_bps: int = 0,
    staleness: StalenessPolicy | None = None,
    clock: Clock | None = None,
    max_staleness_seconds: float = 300.0,
    token_ttl_seconds: int = 30,
    breaker_failure_threshold: int = 3,
    sqlite_path: str | os.PathLike[str] | None = None,
    run_startup: bool = True,
) -> PaperStack:
    """Wire a complete paper-trading system and return its components.

    The construction order mirrors ``tests/harness.py:build_rig`` so the one
    proven way to assemble this kernel stays the only way. ``clock`` defaults to
    a :class:`~trading.core.clock.ManualClock` so a caller (a test, or the
    deterministic CLI demo) drives time explicitly; pass a
    :class:`~trading.core.clock.SystemClock` for wall-clock operation.
    """
    if isinstance(fee_bps, bool) or not isinstance(fee_bps, int) or not 0 <= fee_bps < 10000:
        raise ValueError('fee_bps must be an integer from 0 to 9999')
    clock = clock or ManualClock()
    persistence = SQLiteState(sqlite_path) if sqlite_path is not None else None
    if persistence is None:
        sink: AuditSink = InMemoryAuditSink()
        audit = AuditLog(sink, clock=clock)
    else:
        sqlite_sink = SQLiteAuditSink(persistence)
        sink = sqlite_sink
        audit = AuditLog(
            sink, clock=clock, initial_records=sqlite_sink.records
        )

    # environment="paper" is one of the names StartupCoordinator recognises and
    # is honest about what this is. live_trading stays False (INVARIANT 1).
    config = TradingConfig(
        live_trading=False,
        live_confirmation="",
        risk=risk or RiskConfig(),
        environment="paper",
    )
    staleness = staleness or StalenessPolicy()

    strategy_id = Principal("strategy-1", Role.STRATEGY)
    risk_id = Principal("risk-1", Role.RISK_MANAGER)
    gateway_id = Principal("gateway-1", Role.EXECUTION_GATEWAY)
    operator_id = Principal("operator-1", Role.OPERATOR)
    system_id = Principal("system-1", Role.SYSTEM)

    reservation_repository = ReservationStore()
    if persistence is None:
        orders = OrderStore()
        ledger = PositionLedger()
        # The portfolio wraps the ledger: fills are booked through the portfolio
        # so a cost basis is recorded, and reconciliation reads the same ledger.
        portfolio = Portfolio(starting_cash, ledger=ledger)
    else:
        orders = SQLiteOrderStore(persistence, clock=clock)
        ledger = SQLitePositionLedger(persistence)
        portfolio = SQLitePortfolio(
            starting_cash, ledger=ledger, state=persistence
        )
        reservation_repository = SQLiteReservationRepository(persistence)
    reconciliation = ReconciliationGate(
        ledger, orders, audit, clock=clock, max_staleness_seconds=max_staleness_seconds
    )
    portfolio.fee_bps = fee_bps
    if persistence is None:
        risk_engine = RiskEngine(
            config.risk, identity=risk_id, order_store=orders, audit=audit, clock=clock
        )
    else:
        risk_engine = SQLiteRiskEngine(
            persistence,
            config.risk,
            identity=risk_id,
            order_store=orders,
            audit=audit,
            clock=clock,
        )
    dedupe = IdempotencyRegistry(
        audit, clock=clock, repository=reservation_repository
    )
    if config.risk.max_drawdown_fraction is not None:
        risk_engine.observe_equity(portfolio.starting_cash)
    # No trigger file: the probe never reports a kill engaged from the filesystem.
    # engage() is the only way in, which is what a paper session wants.
    kill_switch = KillSwitch(audit, clock=clock, presence_probe=lambda _path: False)
    for record in reversed(audit.records()):
        if record.action == 'kill_switch_released':
            break
        if record.action == 'kill_switch_engaged':
            kill_switch.engage(operator_id, reason=record.details['reason'])
            break
    breakers = BreakerRegistry()
    breakers.add(
        CircuitBreaker(
            "broker", clock=clock, audit=audit, failure_threshold=breaker_failure_threshold
        )
    )

    feed = InMemoryQuoteFeed(clock=clock, source="paper")
    broker = PaperBroker(
        clock=clock, quotes=feed, slippage_bps=slippage_bps, staleness=staleness
    )
    if persistence is not None:
        from ..adapters.persistence.paper_broker import SQLitePaperBroker
        broker = SQLitePaperBroker(
            state=persistence, clock=clock, quotes=feed,
            slippage_bps=slippage_bps, staleness=staleness,
        )
    broker.available_cash = lambda: portfolio.cash
    broker.available_positions = portfolio.snapshot
    broker.fee_bps = fee_bps

    modes = TradingModeMachine(config, audit)
    modes.transition_to(
        TradingMode.PAPER, actor=operator_id.principal_id, reason="paper session startup"
    )

    gateway = ExecutionGateway(
        identity=gateway_id,
        broker=broker,
        orders=orders,
        positions=portfolio,
        reconciliation=reconciliation,
        risk=risk_engine,
        dedupe=dedupe,
        kill_switch=kill_switch,
        breakers=breakers,
        modes=modes,
        config=config,
        audit=audit,
        clock=clock,
        token_ttl_seconds=token_ttl_seconds,
        transaction_factory=(
            None if persistence is None else persistence.transaction
        ),
    )

    recovery = RestartRecoveryCoordinator(
        gateway=gateway,
        orders=orders,
        reservations=reservation_repository,
    )
    reconciliation_coordinator = ReconciliationCoordinator(
        gateway=gateway,
        orders=orders,
        broker=broker,
        reconciliation=reconciliation,
        audit=audit,
        clock=clock,
        identity=system_id,
    )
    startup = StartupCoordinator(
        config=config,
        audit=audit,
        recovery=recovery,
        coordinator=reconciliation_coordinator,
    ).run(sweep=run_startup)

    # The monitor composes component state into a redacted, fail-closed status.
    # It is handed dedupe so its readiness matches the gateway's reservation-level
    # UNKNOWN backstop. It holds no execution surface.
    monitor = OperationalMonitor(
        config=config,
        orders=orders,
        reconciliation=reconciliation,
        kill_switch=kill_switch,
        breakers=breakers,
        modes=modes,
        dedupe=dedupe,
        coordinator=reconciliation_coordinator,
        recovery=startup,
        audit=audit,
    )

    stack = PaperStack(
        clock=clock,
        audit=audit,
        sink=sink,
        persistence=persistence,
        config=config,
        feed=feed,
        broker=broker,
        orders=orders,
        ledger=ledger,
        portfolio=portfolio,
        reconciliation=reconciliation,
        risk=risk_engine,
        dedupe=dedupe,
        kill_switch=kill_switch,
        breakers=breakers,
        modes=modes,
        gateway=gateway,
        recovery=recovery,
        reconciliation_coordinator=reconciliation_coordinator,
        startup=startup,
        monitor=monitor,
        staleness=staleness,
        strategy_id=strategy_id,
        risk_id=risk_id,
        gateway_id=gateway_id,
        operator_id=operator_id,
        system_id=system_id,
    )
    from .paper_state import PaperBotState
    bot = PaperBotState(stack)
    object.__setattr__(stack, 'bot', bot)
    gateway.submission_guard = bot.require_running
    if persistence is not None:
        from ..adapters.persistence.sqlite import read_paper_origin
        origin=read_paper_origin(persistence.path)
        if origin and origin.get('kind')=='real_market_paper':
            broker.market_rules_ready=lambda:False  # Recovery-only composition cannot authorize fills.
    broker.allow_fills = lambda: (bot.state == 'running' and not kill_switch.is_engaged
        and getattr(broker,'market_rules_ready',lambda:True)())
    return stack
