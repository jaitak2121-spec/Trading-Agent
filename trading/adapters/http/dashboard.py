"""Loopback HTTP adapter for the infrastructure-free operator view model."""

from __future__ import annotations

import json
import os
import secrets
import socketserver
import threading
import time
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Final, Mapping
from urllib.parse import urlsplit

from ...core.errors import ConfigurationError, SafetyViolation

__all__ = ["DashboardServer", "OperatorDashboard"]

_LOOPBACK_HOSTS: Final = frozenset({"127.0.0.1", "::1", "localhost"})


def wait_for_paper_readiness(pid, *, port=8766, timeout_seconds=30,
                             origin='real_market_paper'):
    """Wait for this child, not an unrelated listener or an old SQLite file."""
    import json
    import urllib.request
    if type(pid) is not int or pid <= 0:
        raise ValueError('positive child PID required')
    if not 0 < timeout_seconds <= 120 or type(port) is not int or not 0 < port < 65536:
        raise ValueError('invalid readiness timeout or port')
    deadline = time.monotonic() + timeout_seconds
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    while time.monotonic() < deadline:
        os.kill(pid, 0)  # Refuse immediately when the owned child is gone.
        try:
            with opener.open(f'http://127.0.0.1:{port}/readyz',
                             timeout=min(1, max(.001, deadline-time.monotonic()))) as response:
                value = json.load(response)
            if (isinstance(value, dict) and value.get('ready') is True
                    and value.get('pid') == pid
                    and isinstance(value.get('data_origin'), dict)
                    and value['data_origin'].get('kind') == origin):
                return
        except (OSError, ValueError):
            pass
        time.sleep(min(.1, max(0, deadline-time.monotonic())))
    raise TimeoutError('paper startup/snapshot readiness not confirmed; learning not started')



class OperatorDashboard:
    """Build a JSON-safe snapshot from an assembled stack and supervisor.

    Both collaborators are accepted structurally so this inbound adapter does
    not reverse the dependency arrow by importing the application composition
    package. The runtime composition remains responsible for passing the real
    objects.
    """

    def __init__(
        self,
        stack,
        supervisor,
        *,
        audit_limit: int = 100,
        order_limit: int = 100,
        market_data=None,
        market_specification=None,
        clock=None,
        alert_limit: int = 100,
        strategy_name: str | None = None,
        research_summary=None,
        model_metadata=None,
        prediction_monitor=None,
        learning_status_provider=None,
        data_origin=None,
    ) -> None:
        required = ("monitor", "audit", "portfolio", "orders", "config", "modes", "startup")
        if any(not hasattr(stack, name) for name in required):
            raise TypeError("stack does not provide the operator observation surface")
        if not callable(getattr(supervisor, "status", None)):
            raise TypeError("supervisor must provide status()")
        if isinstance(audit_limit, bool) or not isinstance(audit_limit, int):
            raise TypeError("audit_limit must be an int")
        if audit_limit <= 0:
            raise ValueError("audit_limit must be positive")
        if isinstance(order_limit, bool) or not isinstance(order_limit, int):
            raise TypeError("order_limit must be an int")
        if order_limit <= 0:
            raise ValueError("order_limit must be positive")
        if isinstance(alert_limit, bool) or not isinstance(alert_limit, int):
            raise TypeError("alert_limit must be an int")
        if alert_limit <= 0:
            raise ValueError("alert_limit must be positive")
        if strategy_name is not None and (
            not isinstance(strategy_name, str) or not strategy_name.strip()
        ):
            raise ValueError("strategy_name must be a non-empty string or None")
        self._stack = stack
        self._supervisor = supervisor
        self._audit_limit = audit_limit
        self._order_limit = order_limit
        self._market_data = market_data
        self._market_specification = market_specification
        self._clock = clock
        self._alert_limit = alert_limit
        self._alert_lock = threading.RLock()
        self._alert_states: dict[str, object] = {}
        self._alerts: list[dict[str, object]] = []
        self._alert_sequence = 0
        self._strategy_name = strategy_name
        self._research_summary = research_summary
        self._model_metadata = model_metadata
        self._prediction_monitor = prediction_monitor
        self._learning_status_provider=learning_status_provider
        self._data_origin=data_origin or {'kind':'unverified_paper','provider':'unspecified'}
        self._peak_equity: Decimal | None = None
        if stack.persistence is not None:
            with stack.persistence.read() as connection:
                row = connection.execute(
                    'SELECT payload FROM equity_snapshots ORDER BY id DESC LIMIT 1'
                ).fetchone()
            if row:
                peak = json.loads(row[0]).get('peak_equity')
                self._peak_equity = None if peak is None else Decimal(peak)

    def snapshot(self) -> dict[str, object]:
        persistence = getattr(self._stack, "persistence", None)
        revision = getattr(persistence, "snapshot_revision", None)
        read = getattr(persistence, "read", None)
        if callable(revision):
            for _ in range(3):
                before = revision()
                snapshot = self._snapshot()
                if revision() == before:
                    return snapshot
        if callable(read):  # continuous writes: take one consistent fallback
            with read():
                return self._snapshot()
        return self._snapshot()

    def _snapshot(self) -> dict[str, object]:
        status = self._stack.monitor.status()
        records = self._stack.audit.records()
        positions = self._stack.portfolio.open_positions()
        orders = self._stack.orders.all_orders()
        supervisor_status = self._supervisor.status()
        operations = status.as_details()
        risk = self._stack.config.risk
        if self._stack.bot.state != 'running':
            operations['ready'] = False
            operations['blocked_reasons'] = list(operations['blocked_reasons']) + [
                'paper_bot_' + self._stack.bot.state
            ]
        market_metadata = self._market_metadata_state()
        if market_metadata is not None and market_metadata["blocked"]:
            operations["ready"] = False
            reasons = list(operations["blocked_reasons"])
            state=market_metadata['state']
            reason='market_metadata_'+('missing' if state=='not_configured' else state)
            if reason not in reasons:
                reasons.append(reason)
            operations["blocked_reasons"] = sorted(reasons)
        portfolio = self._portfolio_state(positions)
        filled_orders = []
        for order in orders:
            details = order.as_details()
            if Decimal(str(details["filled_quantity"])) > 0:
                notional = order.filled_notional()
                details["filled_notional"] = (
                    None if notional is None else str(notional.amount)
                )
                filled_orders.append(details)
        snapshot = {
            "application": {
                "name": "Trading App",
                "environment": self._stack.config.environment,
                "mode": self._stack.modes.mode.value,
                "live_trading": self._stack.config.is_live_authorized,
                "strategy": self._strategy_name,
                "bot_state": self._stack.bot.state,
            },
            "supervisor": supervisor_status.as_details(),
            "operations": operations,
            "startup": self._stack.startup.as_details(),
            "portfolio": portfolio,
            "data_origin":self._data_origin,
            "paper_execution_costs":{'fee_bps_per_fill':getattr(self._stack.broker,'fee_bps',0),
                'slippage_bps_per_fill':getattr(self._stack.broker,'slippage_bps',0),
                'spread':'cross observed bid/ask; no additional fixed spread',
                'limitations':'simulated fills; no exchange queue/latency or tax settlement guarantee'},
            "orders": [order.as_details() for order in orders[-self._order_limit :]],
            "trades": filled_orders[-self._order_limit :],
            "risk": {
                "limits": {
                    "max_order_notional": str(risk.max_order_notional.amount),
                    "max_position_notional": str(risk.max_position_notional.amount),
                    "max_gross_exposure": str(risk.max_gross_exposure.amount),
                    "max_daily_loss": str(risk.max_daily_loss.amount),
                    "max_orders_per_minute": risk.max_orders_per_minute,
                    "max_open_orders": risk.max_open_orders,
                    "risk_fraction_per_trade": str(risk.risk_fraction_per_trade),
                    'max_drawdown_fraction': None if risk.max_drawdown_fraction is None else str(risk.max_drawdown_fraction),
                    'max_positions': risk.max_positions,
                    'trade_cooldown_seconds': risk.trade_cooldown_seconds,
                    'max_candle_range_fraction': None if risk.max_candle_range_fraction is None else str(risk.max_candle_range_fraction),
                },
                "kill_switch": {
                    "engaged": self._stack.kill_switch.is_engaged,
                    "reason": self._stack.kill_switch.reason,
                    "engaged_by": self._stack.kill_switch.engaged_by,
                },
                "circuit_breakers": [
                    breaker.as_details() for breaker in self._stack.breakers.snapshots()
                ],
            },
            "ml": self._ml_state(records),
            'research':self._research_summary,
            'dataset_health':(None if supervisor_status.last_cycle is None else
                              supervisor_status.last_cycle.as_details()['candles']),
            "audit": [record.to_dict() for record in records[-self._audit_limit :]],
        }
        snapshot['portfolio']['result_type']=self._data_origin['kind']
        store=getattr(getattr(self._supervisor,'_service',None),'_store',None)
        snapshot['experience']=(store.status() if callable(getattr(store,'status',None)) else {'persistent':False})
        if self._learning_status_provider is not None:
            try:snapshot['ml']['learning']=self._learning_status_provider()
            except (ValueError,OSError,KeyError,TypeError):snapshot['ml']['learning']={'status':'error','deployed':False}
        snapshot['equity_history'] = []
        if self._prediction_monitor is not None:
            try:
                snapshot['ml']['monitor'] = self._prediction_monitor()
            except (ValueError, TypeError, KeyError, ArithmeticError) as exc:
                snapshot['ml']['monitor'] = {'status':'error','error_type':type(exc).__name__}
        if self._stack.persistence is not None:
            with self._stack.persistence.read() as connection:
                history = connection.execute('SELECT payload FROM equity_snapshots ORDER BY id DESC LIMIT 120').fetchall()
            snapshot['equity_history'] = [json.loads(row[0]) for row in reversed(history)]
        if self._market_data is not None:
            last_error = getattr(self._market_data, "last_error", None)
            last_quote=self._stack.feed.quote(self._market_data.symbol)
            snapshot["market_data"] = {
                "symbol": getattr(self._market_data, "symbol", None),
                "last_quote":None if last_quote is None else last_quote.as_details(),
                "exchange": getattr(self._market_data, "exchange", None),
                "running": bool(getattr(self._market_data, "is_running", False)),
                "reconnect_attempts": int(
                    getattr(self._market_data, "reconnect_attempts", 0)
                ),
                "successful_reconnects": int(
                    getattr(self._market_data, "successful_reconnects", 0)
                ),
                "last_error": (
                    None
                    if last_error is None
                    else type(last_error).__name__
                ),
            }
            snapshot["market_metadata"] = market_metadata
        snapshot["alerts"] = self._observe_alerts(snapshot)
        return snapshot

    def _portfolio_state(self, positions) -> dict[str, object]:
        marks = {}
        rendered = []
        unrealized_total = Decimal(0)
        unrealized_known = True
        now = self._stack.clock.now()
        for position in positions:
            details = position.as_details()
            quote = self._stack.feed.quote(position.symbol)
            mark = None
            if (
                quote is not None
                and self._stack.staleness.assess(quote, now=now).is_usable
            ):
                mark = quote.mid
                marks[position.symbol] = mark
            unrealized = None if mark is None else position.unrealized_pnl(mark)
            market_value = None if mark is None else position.market_value(mark)
            details.update(
                {
                    "mark_price": None if mark is None else str(mark.amount),
                    "market_value": (
                        None if market_value is None else str(market_value.amount)
                    ),
                    "unrealized_pnl": (
                        None if unrealized is None else str(unrealized.amount)
                    ),
                }
            )
            if unrealized is None:
                unrealized_known = False
            else:
                unrealized_total += unrealized.amount
            rendered.append(details)
        equity = None
        if len(marks) == len(positions):
            equity = self._stack.portfolio.equity(marks).amount
        drawdown = None
        with self._alert_lock:
            if equity is not None:
                self._peak_equity = (
                    equity
                    if self._peak_equity is None
                    else max(self._peak_equity, equity)
                )
                drawdown = (
                    Decimal(0)
                    if self._peak_equity == 0
                    else (self._peak_equity - equity) / self._peak_equity
                )
        return {
            "cash": str(self._stack.portfolio.cash.amount),
            "starting_cash": str(self._stack.portfolio.starting_cash.amount),
            "fees": str(self._stack.portfolio.fees.amount),
            "gross_exposure": (
                None if equity is None else str(sum(
                    (abs(Decimal(p['market_value'])) for p in rendered), Decimal(0)
                ))
            ),
            "currency": self._stack.portfolio.base_currency.code,
            "equity": None if equity is None else str(equity),
            "peak_equity": (
                None if self._peak_equity is None else str(self._peak_equity)
            ),
            "drawdown": None if drawdown is None else str(drawdown),
            "realized_pnl": str(self._stack.portfolio.realized().amount),
            "unrealized_pnl": str(unrealized_total) if unrealized_known else None,
            "positions": rendered,
        }

    def _ml_state(self, records) -> dict[str, object]:
        active = bool(
            self._strategy_name
            and self._strategy_name.startswith("ml-direction-paper-trial:")
        )
        prediction = None
        for record in reversed(records):
            evidence = (
                record.details if record.action == 'ml.prediction' else record.details.get("evidence")
                if record.action in {"strategy.signalled", 'ml.prediction'}
                else None
            )
            if isinstance(evidence, Mapping) and "up_probability" in evidence:
                prediction = {
                    "at": record.timestamp,
                    "probability": evidence["up_probability"],
                    "threshold": evidence.get("threshold"),
                    "model_sha256": evidence.get("model_sha256"),
                    "signal_id": record.details.get("signal_id"),
                }
                break
        return {
            "active": active,
            "status": "paper_trial" if active else "no_active_approved_model",
            "strategy": self._strategy_name,
            "last_prediction": prediction,
            'training_evaluation':self._model_metadata,
        }

    def healthy(self) -> bool:
        supervisor = self._supervisor.status()
        market_healthy = True
        if self._market_data is not None and hasattr(self._market_data, "last_error"):
            market_healthy = (
                bool(getattr(self._market_data, "is_running", False))
                and self._market_data.last_error is None
            )
        market_metadata = self._market_metadata_state()
        metadata_healthy = not (
            market_metadata is not None and market_metadata["blocked"]
        )
        return (
            supervisor.healthy
            and self._stack.bot.state == 'running'
            and self._stack.monitor.status().ready
            and market_healthy
            and metadata_healthy
        )

    def startup_ready(self) -> bool:
        """Collection readiness is independent of permission to trade."""
        status = self._supervisor.status()
        return self._stack.startup.ok and status.healthy and status.passes > 0

    def _market_metadata_state(self) -> dict[str, object] | None:
        if self._market_data is None:
            return None
        market = self._market_specification
        if market is None:
            return {
                "configured": False,
                "source": None,
                "captured_at": None,
                "valid_until": None,
                "seconds_remaining": None,
                "state": "not_configured",
                "warning": True,
                "blocked": self._data_origin.get('kind')=='real_market_paper',
            }
        source = getattr(market, "metadata_source", None)
        captured_at = getattr(market, "captured_at", None)
        valid_until = getattr(market, "valid_until", None)
        if valid_until is None:
            return {
                "configured": True,
                "source": source,
                "captured_at": None,
                "valid_until": None,
                "seconds_remaining": None,
                "state": "unverified",
                "warning": True,
                "blocked": self._data_origin.get('kind')=='real_market_paper',
            }
        now = (
            self._clock.now()
            if self._clock is not None
            else datetime.now(timezone.utc)
        )
        remaining = (valid_until - now).total_seconds()
        expired = remaining < 0
        warning_threshold = timedelta(hours=6).total_seconds()
        if captured_at is not None:
            lifetime = (valid_until - captured_at).total_seconds()
            warning_threshold = min(warning_threshold, max(0.0, lifetime / 4))
        warning = not expired and remaining <= warning_threshold
        return {
            "configured": True,
            "source": source,
            "captured_at": (
                captured_at.isoformat() if captured_at is not None else None
            ),
            "valid_until": valid_until.isoformat(),
            "seconds_remaining": round(remaining, 3),
            "state": "expired" if expired else "warning" if warning else "fresh",
            "warning": warning,
            "blocked": expired,
        }

    def _observe_alerts(self, snapshot: dict[str, object]) -> list[dict[str, object]]:
        operations = snapshot["operations"]
        supervisor = snapshot["supervisor"]
        states: dict[str, object] = {
            "readiness": operations["ready"],
            "supervisor": supervisor["healthy"],
        }
        market_data = snapshot.get("market_data")
        if isinstance(market_data, dict):
            states["market_data"] = bool(
                market_data["running"] and market_data["last_error"] is None
            )
        metadata = snapshot.get("market_metadata")
        if isinstance(metadata, dict):
            states["market_metadata"] = metadata["state"]

        with self._alert_lock:
            for name, current in states.items():
                if name not in self._alert_states:
                    self._alert_states[name] = current
                    continue
                previous = self._alert_states[name]
                if previous == current:
                    continue
                self._alert_states[name] = current
                alert = self._transition_alert(name, previous, current)
                if alert is not None:
                    self._alert_sequence += 1
                    alert["sequence"] = self._alert_sequence
                    alert["at"] = self._now().isoformat()
                    self._alerts.append(alert)
                    del self._alerts[: -self._alert_limit]
            return [dict(alert) for alert in self._alerts]

    def _now(self) -> datetime:
        return (
            self._clock.now()
            if self._clock is not None
            else datetime.now(timezone.utc)
        )

    @staticmethod
    def _transition_alert(
        name: str, previous: object, current: object
    ) -> dict[str, object] | None:
        if name == "readiness":
            return {
                "severity": "info" if current else "critical",
                "kind": "readiness_restored" if current else "readiness_blocked",
                "message": (
                    "operational readiness restored"
                    if current
                    else "operational readiness became blocked"
                ),
            }
        if name == "supervisor":
            return {
                "severity": "info" if current else "critical",
                "kind": "supervisor_recovered" if current else "supervisor_failed",
                "message": (
                    "paper service supervisor recovered"
                    if current
                    else "paper service supervisor is unhealthy"
                ),
            }
        if name == "market_data":
            return {
                "severity": "info" if current else "critical",
                "kind": (
                    "market_data_recovered"
                    if current
                    else "market_data_disconnected"
                ),
                "message": (
                    "market data connection recovered"
                    if current
                    else "market data connection is unavailable"
                ),
            }
        if name == "market_metadata":
            if current == "warning":
                return {
                    "severity": "warning",
                    "kind": "market_metadata_expiring",
                    "message": "market metadata is approaching expiry",
                }
            if current == "expired":
                return {
                    "severity": "critical",
                    "kind": "market_metadata_expired",
                    "message": "market metadata expired; new orders are blocked",
                }
            if previous in {"warning", "expired"} and current == "fresh":
                return {
                    "severity": "info",
                    "kind": "market_metadata_refreshed",
                    "message": "fresh market metadata is active",
                }
        return None


class _DashboardHTTPServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def server_bind(self) -> None:
        """Bind without the stdlib HTTPServer's blocking reverse-DNS lookup."""
        socketserver.TCPServer.server_bind(self)
        host, port = self.server_address[:2]
        self.server_name = str(host)
        self.server_port = int(port)


class DashboardServer:
    """Serve the operator dashboard and JSON snapshot on a background thread."""

    def __init__(
        self,
        dashboard: OperatorDashboard,
        *,
        host: str = "127.0.0.1",
        port: int = 8765,
        allow_remote: bool = False,
        paper_controls: bool = False,
    ) -> None:
        if not isinstance(dashboard, OperatorDashboard):
            raise TypeError("dashboard must be an OperatorDashboard")
        if not isinstance(host, str) or not host.strip():
            raise ValueError("host must be a non-empty string")
        if not allow_remote and host not in _LOOPBACK_HOSTS:
            raise ConfigurationError(
                "the operator dashboard is loopback-only by default; pass "
                "allow_remote=True only behind authenticated TLS infrastructure"
            )
        if isinstance(port, bool) or not isinstance(port, int):
            raise TypeError("port must be an int")
        if port < 0 or port > 65535:
            raise ValueError("port must be between 0 and 65535")
        self._dashboard = dashboard
        if paper_controls and host not in _LOOPBACK_HOSTS:
            raise ConfigurationError('paper controls require a loopback listener')
        self._control_token = secrets.token_urlsafe(32) if paper_controls else None
        self._host = host
        self._port = port
        self._lock = threading.RLock()
        self._server: _DashboardHTTPServer | None = None
        self._thread: threading.Thread | None = None

    @property
    def is_running(self) -> bool:
        with self._lock:
            thread = self._thread
        return thread is not None and thread.is_alive()

    @property
    def address(self) -> tuple[str, int] | None:
        with self._lock:
            server = self._server
        if server is None:
            return None
        host, port = server.server_address[:2]
        return str(host), int(port)

    @property
    def url(self) -> str | None:
        address = self.address
        if address is None:
            return None
        host, port = address
        rendered_host = f"[{host}]" if ":" in host else host
        return f"http://{rendered_host}:{port}/"

    def start(self) -> None:
        with self._lock:
            if self._server is not None or self._thread is not None:
                raise SafetyViolation("operator dashboard is already running")
            handler = self._handler_type(self._dashboard, self._control_token)
            server = _DashboardHTTPServer((self._host, self._port), handler)
            thread = threading.Thread(
                target=server.serve_forever,
                kwargs={"poll_interval": 0.1},
                name="operator-dashboard",
                daemon=True,
            )
            self._server = server
            self._thread = thread
            try:
                thread.start()
            except BaseException:
                server.server_close()
                self._server = self._thread = None
                raise

    def stop(self) -> None:
        with self._lock:
            server = self._server
            thread = self._thread
        if server is None:
            return
        server.shutdown()
        server.server_close()
        if thread is not None:
            thread.join(5.0)
        with self._lock:
            self._server = None
            self._thread = None

    @staticmethod
    def _handler_type(dashboard: OperatorDashboard, control_token=None):
        class Handler(BaseHTTPRequestHandler):
            server_version = "TradingAppDashboard/1"

            def do_GET(self) -> None:  # noqa: N802 - stdlib callback name
                if self.server.server_address[0] in _LOOPBACK_HOSTS and not self._local_host():
                    self._send(HTTPStatus.FORBIDDEN, b'{}', 'application/json')
                    return
                path = urlsplit(self.path).path
                if path == "/":
                    html = _HTML
                    if control_token:
                        controls = '<nav aria-label="Paper bot controls">'
                        for state, label in [('running','Start / resume'), ('paused','Pause'), ('stopped','Stop bot'), ('emergency_stopped','Emergency stop')]:
                            controls += f'<button type="button" onclick="controlPaper(\'{state}\')">{label}</button> '
                        controls += '<span id="control-result" role="status"></span></nav>'
                        script = '''<script>async function controlPaper(state){const reason=prompt('Reason for this paper bot action:');if(!reason)return;const r=await fetch('/api/paper-control',{method:'POST',headers:{'Content-Type':'application/json','X-Paper-Control':TOKEN},body:JSON.stringify({state,reason})});const d=await r.json();document.getElementById('control-result').textContent=r.ok?'Bot: '+d.state:d.error;}</script>'''.replace('TOKEN', json.dumps(control_token))
                        html = html.replace(' · read-only', ' · local paper controls').replace('<section class="grid">', controls+'<section class="grid">') + script
                    self._send(HTTPStatus.OK, html.encode(), "text/html; charset=utf-8")
                    return
                if path in {"/api/snapshot", "/readyz"}:
                    try:
                        snapshot = dashboard.snapshot()
                        # Check serializability even for the lightweight probe.
                        body = json.dumps(snapshot, sort_keys=True, separators=(",", ":")).encode()
                        ready = path == "/api/snapshot" or dashboard.startup_ready()
                        if path == "/readyz":
                            body = json.dumps({'ready': ready, 'pid': os.getpid(),
                                'data_origin': snapshot.get('data_origin')}).encode()
                        code = HTTPStatus.OK if ready else HTTPStatus.SERVICE_UNAVAILABLE
                    except Exception:
                        code = HTTPStatus.SERVICE_UNAVAILABLE
                        body = b'{"ready":false,"error":"snapshot unavailable"}'
                    self._send(code, body, "application/json")
                    return
                if path == "/healthz":
                    try:
                        healthy = dashboard.healthy()
                    except Exception:
                        healthy = False
                    self._send(
                        HTTPStatus.OK if healthy else HTTPStatus.SERVICE_UNAVAILABLE,
                        json.dumps({"healthy": healthy}).encode(),
                        "application/json",
                    )
                    return
                self._send(HTTPStatus.NOT_FOUND, b'{"error":"not found"}', "application/json")

            def do_POST(self) -> None:  # noqa: N802 - stdlib callback name
                if control_token and self.path == '/api/paper-control':
                    origin = self.headers.get('Origin')
                    expected = 'http://' + self.headers.get('Host', '')
                    token = self.headers.get('X-Paper-Control', '')
                    if not self._local_host() or origin != expected or not secrets.compare_digest(token, control_token):
                        self._send(HTTPStatus.FORBIDDEN, b'{"error":"control authentication failed"}', 'application/json')
                        return
                    try:
                        length = int(self.headers.get('Content-Length', '0'))
                        if not 0 < length <= 4096 or self.headers.get('Content-Type') != 'application/json':
                            raise ValueError('invalid control request')
                        request = json.loads(self.rfile.read(length))
                        dashboard._stack.bot.transition(request['state'], dashboard._stack.operator_id,
                                                        reason=request['reason'])
                        self._send(HTTPStatus.OK, json.dumps({'state': dashboard._stack.bot.state}).encode(), 'application/json')
                    except (KeyError, TypeError, ValueError, SafetyViolation) as exc:
                        self._send(HTTPStatus.BAD_REQUEST, json.dumps({'error':str(exc)}).encode(), 'application/json')
                    return
                self._send(
                    HTTPStatus.METHOD_NOT_ALLOWED,
                    b'{"error":"dashboard is read-only"}',
                    "application/json",
                )

            def _local_host(self):
                authority = urlsplit('http://' + self.headers.get('Host', ''))
                try:
                    return authority.hostname in _LOOPBACK_HOSTS and authority.port == self.server.server_port
                except ValueError:
                    return False

            def _send(self, status: HTTPStatus, body: bytes, content_type: str) -> None:
                self.send_response(status)
                self.send_header("Content-Type", content_type)
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "no-store")
                self.send_header("X-Content-Type-Options", "nosniff")
                self.send_header("X-Frame-Options", "DENY")
                self.send_header("Referrer-Policy", "no-referrer")
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, _format: str, *args: object) -> None:
                return

        return Handler


_HTML = """<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Trading App</title><style>
:root{color-scheme:dark;--bg:#09111f;--panel:#111d30;--line:#263a55;--text:#e8eef8;--muted:#91a4bd;--ok:#55d6a8;--bad:#ff758f}*{box-sizing:border-box}body{margin:0;background:linear-gradient(135deg,#07101c,#0e1a2c);color:var(--text);font:14px system-ui,sans-serif}main{max-width:1200px;margin:auto;padding:28px}header{display:flex;justify-content:space-between;align-items:end;margin-bottom:22px}h1{margin:0;font-size:28px}h2{font-size:15px;margin:0 0 14px}.muted{color:var(--muted)}.badge{padding:7px 11px;border:1px solid var(--line);border-radius:999px}.ok{color:var(--ok)}.bad{color:var(--bad)}.grid{display:grid;grid-template-columns:repeat(4,1fr);gap:14px}.panel{background:#111d30e6;border:1px solid var(--line);border-radius:14px;padding:17px}.wide{grid-column:span 2;align-self:start}.full{grid-column:1/-1}.metric{font-size:24px;font-weight:700;margin-top:6px}.table-scroll{max-height:360px;overflow:auto}table{width:100%;border-collapse:collapse}th,td{text-align:left;padding:9px 7px;border-bottom:1px solid var(--line)}th{color:var(--muted);position:sticky;top:0;background:var(--panel)}pre{white-space:pre-wrap;max-height:310px;overflow:auto}@media(max-width:800px){main{padding:14px}.grid{grid-template-columns:1fr}.wide,.full{grid-column:auto}}</style></head><body><main><header><div><h1>Trading App</h1><div class="muted">Local paper operations dashboard · read-only</div></div><div id="health" class="badge">Connecting…</div></header><section class="grid"><article class="panel"><h2>Mode</h2><div id="mode" class="metric">—</div></article><article class="panel"><h2>Readiness</h2><div id="ready" class="metric">—</div></article><article class="panel"><h2>Equity</h2><div id="equity" class="metric">—</div></article><article class="panel"><h2>Drawdown</h2><div id="drawdown" class="metric">—</div></article><article class="panel"><h2>Cash</h2><div id="cash" class="metric">—</div></article><article class="panel"><h2>Realized P&amp;L</h2><div id="realized" class="metric">—</div></article><article class="panel"><h2>Unrealized P&amp;L</h2><div id="unrealized" class="metric">—</div></article><article class="panel"><h2>Cycles</h2><div id="cycles" class="metric">—</div></article><article class="panel full"><h2>Market metadata</h2><div id="metadata" class="muted">Not applicable</div></article><article class="panel wide"><h2>Positions</h2><div class="table-scroll"><table><thead><tr><th>Symbol</th><th>Quantity</th><th>Average entry</th><th>Mark</th><th>Unrealized P&amp;L</th></tr></thead><tbody id="positions"></tbody></table></div></article><article class="panel wide"><h2>Orders</h2><div class="table-scroll"><table><thead><tr><th>Status</th><th>Symbol</th><th>Side</th><th>Filled / ordered</th></tr></thead><tbody id="orders"></tbody></table></div></article><article class="panel full"><h2>Filled trade history</h2><div class="table-scroll"><table><thead><tr><th>Updated</th><th>Symbol</th><th>Side</th><th>Quantity</th><th>Average fill</th><th>Notional</th></tr></thead><tbody id="trades"></tbody></table></div></article><article class="panel wide"><h2>Risk controls</h2><pre id="risk">—</pre></article><article class="panel wide"><h2>ML status</h2><pre id="ml">—</pre></article><article class="panel full"><h2>Alerts</h2><pre id="alerts">No transitions yet.</pre></article><article class="panel full"><h2>Last cycle</h2><pre id="cycle">No cycle yet.</pre></article><article class="panel full"><h2>Blocked reasons</h2><div id="blocked" class="muted">—</div></article></section></main><script>
const q=id=>document.getElementById(id),cell=(r,v)=>{const d=document.createElement('td');d.textContent=v??'—';r.appendChild(d)};function rows(id,x,fs){const b=q(id);b.replaceChildren();for(const i of x){const r=document.createElement('tr');for(const f of fs)cell(r,f(i));b.appendChild(r)}if(!x.length){const r=document.createElement('tr');cell(r,'None');b.appendChild(r)}}async function refresh(){try{const r=await fetch('/api/snapshot',{cache:'no-store'}),d=await r.json(),m=d.market_metadata,h=d.supervisor.healthy&&d.operations.ready&&(!d.market_data||(d.market_data.running&&!d.market_data.last_error))&&(!m||!m.blocked),p=d.portfolio;q('health').textContent=h?'Healthy':'Attention required';q('health').className='badge '+(h?'ok':'bad');q('mode').textContent=(d.data_origin?.kind==='real_market_paper'?'REAL-MARKET PAPER':d.data_origin?.kind==='synthetic_demo'?'SYNTHETIC DEMO':d.application.mode.toUpperCase())+' · '+(d.application.strategy||'unknown strategy');q('ready').textContent=d.operations.ready?'READY':'BLOCKED';q('ready').className='metric '+(d.operations.ready?'ok':'bad');q('cash').textContent=p.cash+' '+p.currency;q('equity').textContent=p.equity===null?'Unavailable':p.equity+' '+p.currency;q('drawdown').textContent=p.drawdown===null?'Unavailable':(Number(p.drawdown)*100).toFixed(2)+'%';q('realized').textContent=p.realized_pnl+' '+p.currency;q('unrealized').textContent=p.unrealized_pnl===null?'Unavailable':p.unrealized_pnl+' '+p.currency;q('cycles').textContent=d.supervisor.passes;q('metadata').textContent=m?(m.state.toUpperCase()+' · '+(m.source||'no source')+' · '+(m.seconds_remaining===null?'no expiry':Math.round(m.seconds_remaining/60)+' min remaining')):'Not applicable';q('metadata').className=m&&(m.warning||m.blocked)?'bad':'muted';q('alerts').textContent=d.alerts.length?JSON.stringify(d.alerts.slice(-10),null,2):'No transitions yet.';rows('positions',p.positions,[x=>x.symbol,x=>x.quantity+' '+x.asset,x=>x.average_entry_price,x=>x.mark_price,x=>x.unrealized_pnl]);rows('orders',d.orders,[x=>x.state,x=>x.symbol,x=>x.side,x=>x.filled_quantity+' / '+x.ordered_quantity]);rows('trades',d.trades,[x=>x.updated_at,x=>x.symbol,x=>x.side,x=>x.filled_quantity,x=>x.average_fill_price,x=>x.filled_notional]);renderDetails(d);q('risk').textContent=JSON.stringify(d.risk,null,2);q('ml').textContent=JSON.stringify(d.ml,null,2);q('cycle').textContent=d.supervisor.last_cycle?JSON.stringify(d.supervisor.last_cycle,null,2):'No cycle yet.';q('blocked').textContent=d.operations.blocked_reasons.join(', ')||'None'}catch(e){q('health').textContent='Disconnected';q('health').className='badge bad'}}function renderDetails(d){const items=[['equity-history','Equity history',d.equity_history],['research-results','Strategy research',d.research??{status:'No development report selected'}],['live-feed','Market feed',d.market_data],['paper-costs','Paper execution costs',d.paper_execution_costs],['data-origin','Result provenance',d.data_origin],['experience-health','Collected experience',d.experience],['dataset-health','Dataset health',d.dataset_health],['audit-history','Audit log',d.audit.slice(-20)]];for(const [id,title,value] of items){let p=q(id);if(!p){const a=document.createElement('article');a.className='panel wide';const h=document.createElement('h2');h.textContent=title;p=document.createElement('pre');p.id=id;a.append(h,p);document.querySelector('.grid').append(a)}p.textContent=JSON.stringify(value,null,2)}}refresh();setInterval(refresh,2000);</script></body></html>"""
