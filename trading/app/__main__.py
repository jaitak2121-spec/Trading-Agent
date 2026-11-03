"""``python -m trading.app`` -- run a paper demo or supervised local dashboard.

The first path a human can run. It builds a paper stack, replays a short,
deterministic synthetic price series through the reference
:class:`~trading.strategy.examples.MovingAverageCrossover` strategy, and prints
the resulting :class:`~trading.app.report.SessionReport`. No network, no
credentials, no real money -- the venue is the in-process paper broker and live
trading is off by construction. An explicit ``--coinswitch-symbol`` mode swaps
only the synthetic market-data source for CoinSwitch's public Spot stream; its
orders still terminate at the in-process paper broker.

The series is synthetic and shaped to exercise the full chain (a crossover that
produces an entry, then a reversal that produces an exit); it is a demonstration
that the system runs end to end, not a market simulation or a backtest. The
numbers it prints are the paper venue's optimistic estimate, always -- see
``docs/SAFETY.md``.
"""

from __future__ import annotations

import argparse
import datetime as _dt
import sys
from decimal import Decimal
from pathlib import Path
from typing import Sequence

from ..core.money import INR, USDT, USD, Money, Price, Quantity
from ..strategy import MovingAverageCrossover
from .composition import build_paper_stack
from ..core.config import RiskConfig, TradingConfig
from ..core.errors import ConfigurationError
from .paper_session import MarketFrame, PaperTradingSession, SymbolBar
from .runtime import build_coinswitch_paper_application, build_demo_paper_application
from .soak import run_soak, run_timed_soak
from .restart_drill import run_restart_drill
from .paper_recovery import recover_interrupted_paper_position
from ..adapters.process import GracefulShutdown
from ..adapters.coinswitch import CoinSwitchSpotStream
from ..adapters.observation_evidence import write_observation_evidence
from ..adapters.soak_evidence import write_soak_evidence
from .coinswitch_observation import run_coinswitch_observation

_SYMBOL = "DEMO"
_ASSET = "DEMO"
_INTERVAL_SECONDS = 60

#: A decline (fast mean below slow), a rally (fast crosses above -> long entry),
#: then a fall (fast crosses below -> exit). Strings, not floats: money is
#: Decimal-only (INVARIANT 8), and the quote feed rejects a float outright.
_DEMO_CLOSES: tuple[str, ...] = (
    "100", "99.5", "99", "98.5", "98", "97.5", "97",
    "98", "100", "103", "106", "109", "112",
    "110", "107", "103", "99", "95", "92",
)


def _demo_series(closes: Sequence[str] = _DEMO_CLOSES) -> list[MarketFrame]:
    """Turn a path of closing prices into frames with coherent OHLC and quotes."""
    frames: list[MarketFrame] = []
    prev = Decimal(closes[0])
    half_range = Decimal("0.5")  # gives ATR something to measure
    half_spread = Decimal("0.1")  # a real cost to cross, so a round trip is not free
    for raw in closes:
        close = Decimal(raw)
        open_ = prev
        high = max(open_, close) + half_range
        low = min(open_, close) - half_range
        mid = close
        bar = SymbolBar(
            open=Price(open_, USD),
            high=Price(high, USD),
            low=Price(low, USD),
            close=Price(close, USD),
            volume=Quantity("1", _ASSET),
            bid=Price(mid - half_spread, USD),
            ask=Price(mid + half_spread, USD),
        )
        frames.append(MarketFrame(interval_seconds=_INTERVAL_SECONDS, bars={_SYMBOL: bar}))
        prev = close
    return frames


def _demo_risk() -> RiskConfig:
    """Paper-sized limits for the demo.

    ``risk_fraction_per_trade`` is small so the position is sized by *risk*
    (the stop distance), not clamped to the per-order notional ceiling. The
    ceilings are then set well above that risk-sized position, so the entry has
    headroom and the exit -- which closes the same quantity, possibly at a higher
    price -- is not itself refused for exceeding a limit the entry sat right on.
    Every value is still a hard ceiling; none is a target.
    """
    return RiskConfig(
        base_currency=USD,
        max_order_notional=Money("50000.00", USD),
        max_position_notional=Money("100000.00", USD),
        max_gross_exposure=Money("200000.00", USD),
        max_daily_loss=Money("2000.00", USD),
        max_orders_per_minute=6,
        max_open_orders=5,
        risk_fraction_per_trade=Decimal("0.001"),
    )


def _serve_application(application, *, description: str) -> int:
    """Run until SIGINT/SIGTERM, then close every owned component cleanly."""
    with GracefulShutdown() as shutdown:
        application.start()
        print(
            f"paper application running at {application.server.url} "
            f"({description}, live trading off)"
        )
        print("Press Ctrl-C to stop.")
        try:
            shutdown.wait()
        finally:
            reason = shutdown.reason or "shutdown requested"
            print(f"\nstopping paper application ({reason})")
            application.stop()
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m trading.app",
        description="Run a deterministic paper session or continuous local dashboard.",
    )
    parser.add_argument(
        "--cash",
        default="100000.00",
        help=(
            "starting paper cash in the selected quote currency "
            "(USD for synthetic mode; default: 100000.00)"
        ),
    )
    parser.add_argument(
        "--slippage-bps",
        type=int,
        default=0,
        help="paper fill slippage in basis points against the order (default: 0)",
    )
    parser.add_argument(
        "--state-db",
        default=None,
        help="optional SQLite path for transactional restart state",
    )
    parser.add_argument(
        "--config",
        metavar="PATH",
        default=None,
        help="validated paper-risk TOML profile (secrets and live mode forbidden)",
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--dashboard",
        action="store_true",
        help="run the continuous synthetic paper service and local dashboard",
    )
    mode.add_argument(
        "--soak-cycles",
        type=int,
        default=None,
        help="run N complete synthetic application cycles and verify safety",
    )
    mode.add_argument(
        "--soak-seconds",
        type=float,
        default=None,
        help="run a paced synthetic safety soak for N wall-clock seconds",
    )
    mode.add_argument(
        "--restart-drill",
        metavar="SQLITE_PATH",
        default=None,
        help="run, close, rebuild, and verify recovery on one SQLite state file",
    )
    mode.add_argument(
        "--recover-paper-position",
        metavar="SQLITE_PATH",
        default=None,
        help="explicitly adopt a rebuilt PaperBroker position snapshot",
    )
    mode.add_argument(
        "--coinswitch-symbol",
        metavar="BASE/QUOTE",
        help=(
            "run the dashboard on public CoinSwitch Spot data with paper-only "
            "execution (supported quote currencies: INR, USDT)"
        ),
    )
    parser.add_argument(
        "--host",
        default="127.0.0.1",
        help="dashboard host; loopback only (default: 127.0.0.1)",
    )
    parser.add_argument(
        "--coinswitch-exchange",
        choices=("coinswitchx", "c2c1", "c2c2"),
        default=None,
        help=(
            "CoinSwitch venue (default: coinswitchx for INR, c2c1 for USDT)"
        ),
    )
    parser.add_argument(
        "--limit-protection-bps",
        type=int,
        default=10,
        help=(
            "CoinSwitch paper LIMIT price protection in basis points "
            "(default: 10)"
        ),
    )
    parser.add_argument(
        "--limit-price-decimals",
        type=int,
        default=2,
        help=(
            "CoinSwitch venue price decimals; must match the selected market "
            "(default: 2)"
        ),
    )
    parser.add_argument(
        "--quantity-step",
        default=None,
        help="CoinSwitch base-quantity step from venue metadata",
    )
    parser.add_argument(
        "--min-order-notional",
        default=None,
        help="CoinSwitch minimum order notional in the quote currency",
    )
    parser.add_argument(
        "--max-order-notional",
        default=None,
        help="CoinSwitch maximum order notional in the quote currency",
    )
    parser.add_argument(
        "--coinswitch-market-info-file",
        metavar="PATH",
        default=None,
        help=(
            "offline captured /trade/api/v2/tradeInfo JSON; replaces the three "
            "manual market-constraint flags"
        ),
    )
    parser.add_argument(
        "--market-info-max-age-hours",
        type=int,
        default=24,
        help="maximum accepted age of a CoinSwitch metadata capture (default: 24)",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=8765,
        help="dashboard port (default: 8765)",
    )
    parser.add_argument(
        "--poll-interval",
        type=float,
        default=1.0,
        help="paper service cycle interval in seconds (default: 1.0)",
    )
    parser.add_argument(
        "--restart-drill-cycles",
        type=int,
        default=19,
        help="cycles per side of a restart drill (default: 19)",
    )
    parser.add_argument(
        "--soak-report",
        metavar="PATH",
        default=None,
        help="atomically write JSON evidence for --soak-cycles/--soak-seconds",
    )
    parser.add_argument(
        "--coinswitch-observe-seconds",
        type=float,
        default=None,
        help=(
            "observe public CoinSwitch quotes/candles for a finite duration; "
            "requires --coinswitch-symbol and performs no trading"
        ),
    )
    parser.add_argument(
        "--observation-report",
        metavar="PATH",
        default=None,
        help="atomically write evidence for --coinswitch-observe-seconds",
    )
    parser.add_argument(
        "--confirm-paper-position-reset",
        default=None,
        help="exact confirmation phrase required for paper position recovery",
    )
    parser.add_argument(
        "--paper-recovery-reason",
        default=None,
        help="mandatory operator reason recorded in the audit trail",
    )
    args = parser.parse_args(argv)

    if args.soak_report is not None and (
        args.soak_cycles is None and args.soak_seconds is None
    ):
        parser.error("--soak-report requires --soak-cycles or --soak-seconds")
    if args.coinswitch_observe_seconds is not None and args.coinswitch_symbol is None:
        parser.error("--coinswitch-observe-seconds requires --coinswitch-symbol")
    if args.observation_report is not None and args.coinswitch_observe_seconds is None:
        parser.error("--observation-report requires --coinswitch-observe-seconds")

    profile: TradingConfig | None = None
    if args.config is not None:
        try:
            profile = TradingConfig.from_toml_file(args.config)
            if profile.live_trading:
                raise ConfigurationError(
                    "application profiles must keep live_trading=false"
                )
            if profile.api_key is not None or profile.api_secret is not None:
                raise ConfigurationError(
                    "application profiles must not contain API credentials"
                )
        except ConfigurationError as exc:
            parser.error(f"invalid paper configuration profile: {exc}")
        if args.coinswitch_symbol is None and profile.risk.base_currency != USD:
            parser.error(
                "synthetic paper modes require a USD configuration profile"
            )

    if args.coinswitch_symbol is not None:
        try:
            quote_code = args.coinswitch_symbol.rsplit("/", 1)[1].upper()
            currency = {"INR": INR, "USDT": USDT}[quote_code]
            cash = Money(args.cash, currency)
            selected_exchange = args.coinswitch_exchange or (
                "coinswitchx" if quote_code == "INR" else "c2c1"
            )
            if (quote_code == "INR") != (selected_exchange == "coinswitchx"):
                raise ValueError(
                    "coinswitchx is for INR; c2c1/c2c2 are for USDT"
                )
        except (IndexError, KeyError, TypeError, ValueError) as exc:
            parser.error(
                "--coinswitch-symbol must be BASE/INR or BASE/USDT and --cash "
                f"must be valid for that currency ({exc})"
            )
        if args.coinswitch_observe_seconds is not None:
            stream = CoinSwitchSpotStream(
                symbol=args.coinswitch_symbol,
                exchange=selected_exchange,
            )
            observation_started = _dt.datetime.now(tz=_dt.timezone.utc)
            report = run_coinswitch_observation(
                stream,
                duration_seconds=args.coinswitch_observe_seconds,
                poll_interval_seconds=args.poll_interval,
            )
            observation_finished = _dt.datetime.now(tz=_dt.timezone.utc)
            if args.observation_report is not None:
                write_observation_evidence(
                    args.observation_report,
                    report=report.as_details(),
                    started_at=observation_started,
                    finished_at=observation_finished,
                )
            print(report.render())
            return 0 if report.passed else 2
        try:
            market_info_body = (
                Path(args.coinswitch_market_info_file).read_bytes()
                if args.coinswitch_market_info_file is not None
                else None
            )
            application = build_coinswitch_paper_application(
                symbol=args.coinswitch_symbol,
                exchange=selected_exchange,
                starting_cash=cash,
                slippage_bps=args.slippage_bps,
                sqlite_path=args.state_db,
                poll_interval_seconds=args.poll_interval,
                host=args.host,
                port=args.port,
                limit_protection_bps=args.limit_protection_bps,
                limit_price_decimals=args.limit_price_decimals,
                quantity_step=args.quantity_step,
                min_order_notional=args.min_order_notional,
                max_order_notional=args.max_order_notional,
                market_info_body=market_info_body,
                market_info_max_age_hours=args.market_info_max_age_hours,
                risk=None if profile is None else profile.risk,
            )
        except (OSError, TypeError, ValueError) as exc:
            parser.error(f"invalid CoinSwitch paper configuration: {exc}")
        return _serve_application(
            application,
            description=(
                f"real CoinSwitch data for {application.stream.symbol}, "
                "paper execution only"
            ),
        )

    if args.soak_cycles is not None:
        soak_started = _dt.datetime.now(tz=_dt.timezone.utc)
        application = build_demo_paper_application(
            starting_cash=Money(args.cash, USD),
            slippage_bps=args.slippage_bps,
            sqlite_path=args.state_db,
            poll_interval_seconds=args.poll_interval,
            host=args.host,
            port=args.port,
            risk=None if profile is None else profile.risk,
        )
        try:
            report = run_soak(application, cycles=args.soak_cycles)
        finally:
            application.stop()
        soak_finished = _dt.datetime.now(tz=_dt.timezone.utc)
        if args.soak_report is not None:
            write_soak_evidence(
                args.soak_report,
                report=report.as_details(),
                started_at=soak_started,
                finished_at=soak_finished,
                state_db=args.state_db,
            )
        print(report.render())
        return 0 if report.passed else 2

    if args.soak_seconds is not None:
        soak_started = _dt.datetime.now(tz=_dt.timezone.utc)
        application = build_demo_paper_application(
            starting_cash=Money(args.cash, USD),
            slippage_bps=args.slippage_bps,
            sqlite_path=args.state_db,
            poll_interval_seconds=args.poll_interval,
            host=args.host,
            port=args.port,
            risk=None if profile is None else profile.risk,
        )
        try:
            report = run_timed_soak(
                application,
                duration_seconds=args.soak_seconds,
                interval_seconds=args.poll_interval,
                cycle_multiple=19,
            )
        finally:
            application.stop()
        soak_finished = _dt.datetime.now(tz=_dt.timezone.utc)
        if args.soak_report is not None:
            write_soak_evidence(
                args.soak_report,
                report=report.as_details(),
                started_at=soak_started,
                finished_at=soak_finished,
                state_db=args.state_db,
            )
        print(report.render())
        return 0 if report.passed else 2

    if args.dashboard:
        application = build_demo_paper_application(
            starting_cash=Money(args.cash, USD),
            slippage_bps=args.slippage_bps,
            sqlite_path=args.state_db,
            poll_interval_seconds=args.poll_interval,
            host=args.host,
            port=args.port,
            risk=None if profile is None else profile.risk,
        )
        return _serve_application(
            application,
            description="synthetic data",
        )

    if args.restart_drill is not None:
        report = run_restart_drill(
            sqlite_path=args.restart_drill,
            cycles_per_run=args.restart_drill_cycles,
            starting_cash=Money(args.cash, USD),
            risk=None if profile is None else profile.risk,
        )
        print(report.render())
        return 0 if report.passed else 2

    if args.recover_paper_position is not None:
        try:
            report = recover_interrupted_paper_position(
                sqlite_path=args.recover_paper_position,
                confirmation=args.confirm_paper_position_reset or "",
                reason=args.paper_recovery_reason or "",
                starting_cash=Money(args.cash, USD),
                risk=None if profile is None else profile.risk,
            )
        except (RuntimeError, TypeError, ValueError) as exc:
            parser.error(f"paper position recovery refused: {exc}")
        print(report.render())
        return 0 if report.passed else 2

    stack = build_paper_stack(
        starting_cash=Money(args.cash, USD),
        risk=_demo_risk() if profile is None else profile.risk,
        slippage_bps=args.slippage_bps,
        sqlite_path=args.state_db,
    )
    strategy = MovingAverageCrossover(fast=3, slow=5, atr_period=3, symbols=[_SYMBOL])
    session = PaperTradingSession(stack, strategy, instruments={_SYMBOL: _ASSET})

    report = session.run(_demo_series())

    print(f"started {_dt.datetime.now().isoformat(timespec='seconds')}  "
          f"(paper mode, live trading off)")
    print()
    print(report.render())
    if stack.persistence is not None:
        stack.persistence.close()
    return 0


if __name__ == "__main__":  # pragma: no cover - exercised via subprocess in tests
    sys.exit(main())
