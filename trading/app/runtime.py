"""Composition and lifecycle of the runnable local paper application."""

from __future__ import annotations

import datetime as _dt
from dataclasses import dataclass
from decimal import Decimal
from os import PathLike

from ..adapters.marketdata import (
    CandleIngestor,
    CandleNormalizer,
    InMemoryCandleStore,
    MarketDataIngestor,
    QuoteNormalizer,
)
from ..adapters.http import DashboardServer, OperatorDashboard
from ..adapters.coinswitch import CoinSwitchSpotStream
from ..core.clock import ManualClock, SystemClock, UTC
from ..core.config import RiskConfig
from ..core.money import Currency, INR, Money, USD, USDT
from ..strategy import MovingAverageCrossover, SignalStrategy
from .composition import PaperStack, build_paper_stack
from .demo_stream import DemoMarketStream
from .paper_service import RealtimePaperService
from .order_policy import MarketableLimitPolicy, VenueMarketSpecification
from .coinswitch_market import CoinSwitchTradeInfoTranslator
from .supervisor import PaperServiceSupervisor

__all__ = [
    "PaperApplication",
    "build_coinswitch_paper_application",
    "build_demo_paper_application",
]


def _application_risk(currency: Currency = USD) -> RiskConfig:
    return RiskConfig(
        base_currency=currency,
        max_order_notional=Money("50000.00", currency),
        max_position_notional=Money("100000.00", currency),
        max_gross_exposure=Money("200000.00", currency),
        max_daily_loss=Money("2000.00", currency),
        max_orders_per_minute=60,
        max_open_orders=5,
        risk_fraction_per_trade=Decimal("0.001"),
    )


@dataclass(slots=True)
class PaperApplication:
    stack: PaperStack
    stream: object
    service: RealtimePaperService
    supervisor: PaperServiceSupervisor
    dashboard: OperatorDashboard
    server: DashboardServer
    _closed: bool = False

    def start(self) -> None:
        if self._closed:
            raise RuntimeError("paper application has already been closed")
        stream_start = getattr(self.stream, "start", None)
        stream_stop = getattr(self.stream, "stop", None)
        if callable(stream_start):
            stream_start()
        try:
            self.supervisor.start()
            try:
                self.server.start()
            except BaseException:
                self.supervisor.stop()
                raise
        except BaseException:
            if callable(stream_stop):
                stream_stop()
            raise

    def stop(self) -> None:
        if self._closed:
            return
        try:
            self.server.stop()
        finally:
            self.supervisor.stop()
            stream_stop = getattr(self.stream, "stop", None)
            if callable(stream_stop):
                stream_stop()
            if self.stack.persistence is not None:
                self.stack.persistence.close()
            self._closed = True

    def __enter__(self) -> "PaperApplication":
        self.start()
        return self

    def __exit__(self, *_exc: object) -> None:
        self.stop()


def build_demo_paper_application(
    *,
    starting_cash: Money = Money("100000.00", USD),
    slippage_bps: int = 0,
    sqlite_path: str | PathLike[str] | None = None,
    poll_interval_seconds: float = 1.0,
    host: str = "127.0.0.1",
    port: int = 8765,
    risk: RiskConfig | None = None,
) -> PaperApplication:
    """Build a continuous synthetic paper app with no network market dependency."""
    if risk is not None:
        if not isinstance(risk, RiskConfig):
            raise TypeError("risk must be a RiskConfig or None")
        if risk.base_currency != starting_cash.currency:
            raise ValueError("risk base currency must match starting cash")
    now = _dt.datetime.now(UTC).replace(microsecond=0)
    clock = ManualClock(now)
    stack = build_paper_stack(
        starting_cash=starting_cash,
        risk=risk or _application_risk(),
        slippage_bps=slippage_bps,
        sqlite_path=sqlite_path,
        clock=clock,
    )
    stream = DemoMarketStream(clock)
    candles = InMemoryCandleStore()
    quotes = MarketDataIngestor(
        source=stream.quote_source,
        normalizer=QuoteNormalizer(clock=clock, source="synthetic-demo"),
        feed=stack.feed,
        clock=clock,
        symbols=[stream.symbol],
    )
    bars = CandleIngestor(
        source=stream.candle_source,
        normalizer=CandleNormalizer(
            clock=clock,
            volume_asset=stream.asset,
            source="synthetic-demo",
            default_interval_seconds=stream.interval_seconds,
        ),
        store=candles,
        clock=clock,
    )
    strategy = MovingAverageCrossover(
        fast=3,
        slow=5,
        atr_period=3,
        symbols=[stream.symbol],
    )
    service = RealtimePaperService(
        stack,
        strategy,
        quote_ingestor=quotes,
        candle_ingestor=bars,
        candles=candles,
        instruments={stream.symbol: stream.asset},
    )
    supervisor = PaperServiceSupervisor(
        service, interval_seconds=poll_interval_seconds
    )
    dashboard = OperatorDashboard(
        stack, supervisor, strategy_name=service.strategy_name
    )
    server = DashboardServer(dashboard, host=host, port=port)
    return PaperApplication(stack, stream, service, supervisor, dashboard, server)


def build_coinswitch_paper_application(
    *,
    symbol: str | None = None,
    starting_cash: Money = Money("100000.00", USDT),
    exchange: str | None = None,
    slippage_bps: int = 0,
    sqlite_path: str | PathLike[str] | None = None,
    poll_interval_seconds: float = 1.0,
    host: str = "127.0.0.1",
    port: int = 8765,
    stream: CoinSwitchSpotStream | None = None,
    limit_protection_bps: int = 10,
    limit_price_decimals: int = 2,
    quantity_step: str | Decimal | None = None,
    min_order_notional: str | Decimal | None = None,
    max_order_notional: str | Decimal | None = None,
    market_info_body: bytes | None = None,
    market_info_max_age_hours: int = 24,
    risk: RiskConfig | None = None,
    strategy: SignalStrategy | None = None,
) -> PaperApplication:
    """Build a real CoinSwitch-data application with paper-only execution."""
    normalized_symbol = (
        stream.symbol if stream is not None and symbol is None
        else (symbol or "BTC/USDT").strip().upper()
    )
    quote_code = normalized_symbol.rsplit("/", 1)[-1]
    selected_exchange = exchange or (
        "coinswitchx" if quote_code == "INR" else "c2c1"
    )
    market_stream = stream or CoinSwitchSpotStream(
        symbol=normalized_symbol, exchange=selected_exchange
    )
    if market_stream.symbol != normalized_symbol:
        raise ValueError("injected stream symbol does not match requested symbol")
    currencies = {"INR": INR, "USDT": USDT}
    try:
        currency = currencies[market_stream.quote_asset]
    except KeyError as exc:
        raise ValueError(
            "CoinSwitch paper mode currently supports INR and USDT quote currencies"
        ) from exc
    if starting_cash.currency != currency:
        raise ValueError(
            f"starting cash must be denominated in {currency.code} for {symbol}"
        )
    if risk is not None:
        if not isinstance(risk, RiskConfig):
            raise TypeError("risk must be a RiskConfig or None")
        if risk.base_currency != currency:
            raise ValueError(
                "risk base currency must match the CoinSwitch quote currency"
            )
    if isinstance(market_info_max_age_hours, bool) or not isinstance(
        market_info_max_age_hours, int
    ):
        raise TypeError("market_info_max_age_hours must be an int")
    if not 1 <= market_info_max_age_hours <= 720:
        raise ValueError("market_info_max_age_hours must be between 1 and 720")

    clock = SystemClock()
    specification_values = (
        quantity_step,
        min_order_notional,
        max_order_notional,
    )
    if market_info_body is not None and any(
        value is not None for value in specification_values
    ):
        raise ValueError(
            "captured market info and manual market constraints are mutually exclusive"
        )
    if any(value is not None for value in specification_values) and not all(
        value is not None for value in specification_values
    ):
        raise ValueError(
            "quantity_step, min_order_notional, and max_order_notional must "
            "be supplied together"
        )
    market = None
    if market_info_body is not None:
        market = CoinSwitchTradeInfoTranslator().translate_capture(
            capture=market_info_body,
            exchange=market_stream.exchange,
            symbol=market_stream.symbol,
            currency=currency,
            now=clock.now(),
            max_age=_dt.timedelta(hours=market_info_max_age_hours),
        )
    elif all(value is not None for value in specification_values):
        market = VenueMarketSpecification(
            price_decimals=limit_price_decimals,
            quantity_step=quantity_step,  # type: ignore[arg-type]
            min_notional=Money(min_order_notional, currency),
            max_notional=Money(max_order_notional, currency),
            metadata_source="manual-cli",
        )
    order_policy = MarketableLimitPolicy(
        protection_bps=limit_protection_bps,
        price_decimals=limit_price_decimals,
        market=market,
    )

    stack = build_paper_stack(
        starting_cash=starting_cash,
        risk=risk or _application_risk(currency),
        slippage_bps=slippage_bps,
        sqlite_path=sqlite_path,
        clock=clock,
    )
    candles = InMemoryCandleStore()
    source_name = f"coinswitch-{market_stream.exchange}"
    quotes = MarketDataIngestor(
        source=market_stream.quote_source,
        normalizer=QuoteNormalizer(
            clock=clock, currency=currency, source=source_name
        ),
        feed=stack.feed,
        clock=clock,
        symbols=[market_stream.symbol],
    )
    bars = CandleIngestor(
        source=market_stream.candle_source,
        normalizer=CandleNormalizer(
            clock=clock,
            currency=currency,
            volume_asset=market_stream.base_asset,
            source=source_name,
            default_interval_seconds=60,
        ),
        store=candles,
        clock=clock,
    )
    if strategy is not None and not isinstance(strategy, SignalStrategy):
        raise TypeError("strategy must be a SignalStrategy or None")
    selected_strategy = strategy or MovingAverageCrossover(
        fast=3, slow=5, atr_period=3, symbols=[market_stream.symbol]
    )
    service = RealtimePaperService(
        stack,
        selected_strategy,
        quote_ingestor=quotes,
        candle_ingestor=bars,
        candles=candles,
        instruments={market_stream.symbol: market_stream.base_asset},
        order_policy=order_policy,
    )
    supervisor = PaperServiceSupervisor(
        service, interval_seconds=poll_interval_seconds
    )
    dashboard = OperatorDashboard(
        stack,
        supervisor,
        market_data=market_stream,
        market_specification=market,
        clock=clock,
        strategy_name=service.strategy_name,
    )
    server = DashboardServer(dashboard, host=host, port=port)
    return PaperApplication(
        stack, market_stream, service, supervisor, dashboard, server
    )
