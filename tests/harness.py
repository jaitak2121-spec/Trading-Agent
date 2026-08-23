"""A fully wired system, for tests that need the whole chain rather than a part.

``build_rig()`` assembles every safety component around a
:class:`~trading.core.gateway.ExecutionGateway` with a
:class:`~trading.adapters.memory.SimulatedBroker` behind it. Everything is
deterministic: one :class:`~trading.core.clock.ManualClock` drives the audit
log, the tokens, the breakers, the rate window, and the reconciliation staleness
check, so a test moves time explicitly and never sleeps.

Identities are separate on purpose -- the strategy, the risk engine, the gateway,
and the operator are four different principals, because several invariants are
about one of them being unable to do another's job.

This module is not collected by ``unittest discover`` (the pattern is
``test*.py``), so it can be imported freely by the test modules that need it.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from typing import Mapping

from trading.adapters.memory import SimulatedBroker, StaticMarketData
from trading.core.audit import AuditLog, InMemoryAuditSink
from trading.core.authz import Principal, Role
from trading.core.breaker import BreakerRegistry, CircuitBreaker
from trading.core.clock import ManualClock
from trading.core.config import REQUIRED_LIVE_CONFIRMATION, RiskConfig, TradingConfig
from trading.core.dedupe import IdempotencyRegistry
from trading.core.gateway import ExecutionGateway
from trading.core.killswitch import KillSwitch
from trading.core.modes import TradingMode, TradingModeMachine
from trading.core.money import USD, Money, Price, Quantity
from trading.core.orders import OrderIntent, OrderSide, OrderStore, OrderType
from trading.core.reconciliation import PositionLedger, ReconciliationGate
from trading.core.risk import RiskEngine
from trading.ports.broker import AckOutcome

#: The venue price used throughout. 0.001 BTC = 50 USD, comfortably inside the
#: default 100 USD per-order ceiling, so a plain order succeeds and a test has to
#: work to breach a limit.
DEFAULT_PRICE = Price("50000", USD)
DEFAULT_QUANTITY = Quantity("0.001", "BTC")
SYMBOL = "BTCUSD"
ASSET = "BTC"


@dataclass
class Rig:
    """Every component, wired together."""

    clock: ManualClock
    sink: InMemoryAuditSink
    audit: AuditLog
    config: TradingConfig
    modes: TradingModeMachine
    orders: OrderStore
    positions: PositionLedger
    reconciliation: ReconciliationGate
    risk: RiskEngine
    dedupe: IdempotencyRegistry
    kill_switch: KillSwitch
    breakers: BreakerRegistry
    breaker: CircuitBreaker
    broker: SimulatedBroker
    market_data: StaticMarketData
    gateway: ExecutionGateway
    strategy_id: Principal
    risk_id: Principal
    gateway_id: Principal
    operator_id: Principal
    _counter: list[int] = field(default_factory=lambda: [0])

    # -- convenience -------------------------------------------------------

    def intent(
        self,
        *,
        signal_id: str | None = None,
        symbol: str = SYMBOL,
        side: OrderSide = OrderSide.BUY,
        quantity: Quantity | str = DEFAULT_QUANTITY,
        asset: str | None = None,
        strategy_id: str = "strat-1",
    ) -> OrderIntent:
        """A fresh intent with a unique signal id, so keys do not collide."""
        if signal_id is None:
            self._counter[0] += 1
            signal_id = f"sig-{self._counter[0]}"
        if not isinstance(quantity, Quantity):
            quantity = Quantity(quantity, asset or symbol.removesuffix("USD"))
        return OrderIntent(
            strategy_id=strategy_id,
            signal_id=signal_id,
            symbol=symbol,
            side=side,
            order_type=OrderType.MARKET,
            quantity=quantity,
        )

    def prices(self, **overrides: Price) -> Mapping[str, Price]:
        """Mark prices covering the default symbol plus any overrides."""
        result: dict[str, Price] = {SYMBOL: DEFAULT_PRICE}
        result.update(overrides)
        return result

    def submit(self, intent: OrderIntent | None = None, **kwargs):
        """Submit through the gateway as the strategy principal."""
        prices = kwargs.pop("mark_prices", None)
        proposer = kwargs.pop("proposer", self.strategy_id)
        if intent is None:
            intent = self.intent(**kwargs)
        return self.gateway.submit(
            intent,
            proposer=proposer,
            mark_prices=self.prices() if prices is None else prices,
        )

    def go_live(self) -> None:
        """Walk the mode machine to LIVE. Only works on a live-authorized rig."""
        self.modes.transition_to(
            TradingMode.LIVE, actor=self.operator_id.principal_id, reason="test"
        )

    def actions(self) -> list[str]:
        """Every audited action, in order."""
        return [record.action for record in self.sink.records]


def build_rig(
    *,
    mode: TradingMode = TradingMode.PAPER,
    default_outcome: AckOutcome = AckOutcome.FILLED,
    live_authorized: bool = False,
    risk: RiskConfig | None = None,
    max_staleness_seconds: float = 300.0,
    token_ttl_seconds: int = 30,
) -> Rig:
    """Wire a complete system.

    Defaults to PAPER with immediately-filling acks, which is the configuration
    in which a well-formed order is expected to succeed -- so any test that sees
    a refusal knows the refusal came from the gate it was probing.
    """
    clock = ManualClock()
    sink = InMemoryAuditSink()
    audit = AuditLog(sink, clock=clock)

    config = TradingConfig(
        live_trading=live_authorized,
        live_confirmation=REQUIRED_LIVE_CONFIRMATION if live_authorized else "",
        risk=risk or RiskConfig(),
    )

    strategy_id = Principal("strategy-1", Role.STRATEGY)
    risk_id = Principal("risk-1", Role.RISK_MANAGER)
    gateway_id = Principal("gateway-1", Role.EXECUTION_GATEWAY)
    operator_id = Principal("operator-1", Role.OPERATOR)

    orders = OrderStore()
    positions = PositionLedger()
    reconciliation = ReconciliationGate(
        positions,
        orders,
        audit,
        clock=clock,
        max_staleness_seconds=max_staleness_seconds,
    )
    risk_engine = RiskEngine(
        config.risk,
        identity=risk_id,
        order_store=orders,
        audit=audit,
        clock=clock,
    )
    dedupe = IdempotencyRegistry(audit, clock=clock)
    # No trigger file: the probe would look at the filesystem, and a test must
    # not depend on paths. engage() is the only way in here.
    kill_switch = KillSwitch(audit, clock=clock, presence_probe=lambda _path: False)
    breakers = BreakerRegistry()
    breaker = breakers.add(
        CircuitBreaker("broker", clock=clock, audit=audit, failure_threshold=3)
    )

    market_data = StaticMarketData({SYMBOL: DEFAULT_PRICE})
    broker = SimulatedBroker(
        clock=clock,
        default_outcome=default_outcome,
        fill_prices={SYMBOL: DEFAULT_PRICE},
    )

    modes = TradingModeMachine(config, audit)
    if mode is not TradingMode.DISABLED:
        if mode is TradingMode.LIVE:
            modes.transition_to(
                TradingMode.PAPER, actor=operator_id.principal_id, reason="rig setup"
            )
        modes.transition_to(mode, actor=operator_id.principal_id, reason="rig setup")

    gateway = ExecutionGateway(
        identity=gateway_id,
        broker=broker,
        orders=orders,
        positions=positions,
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
    )

    return Rig(
        clock=clock,
        sink=sink,
        audit=audit,
        config=config,
        modes=modes,
        orders=orders,
        positions=positions,
        reconciliation=reconciliation,
        risk=risk_engine,
        dedupe=dedupe,
        kill_switch=kill_switch,
        breakers=breakers,
        breaker=breaker,
        broker=broker,
        market_data=market_data,
        gateway=gateway,
        strategy_id=strategy_id,
        risk_id=risk_id,
        gateway_id=gateway_id,
        operator_id=operator_id,
    )


def equity(amount: str = "10000.00") -> Money:
    return Money(Decimal(amount), USD)
