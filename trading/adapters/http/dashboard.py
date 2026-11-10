"""Loopback HTTP adapter for the infrastructure-free operator view model."""

from __future__ import annotations

import json
import socketserver
import threading
from datetime import datetime, timedelta, timezone
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Final
from urllib.parse import urlsplit

from ...core.errors import ConfigurationError, SafetyViolation

__all__ = ["DashboardServer", "OperatorDashboard"]

_LOOPBACK_HOSTS: Final = frozenset({"127.0.0.1", "::1", "localhost"})


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
        market_metadata = self._market_metadata_state()
        if market_metadata is not None and market_metadata["blocked"]:
            operations["ready"] = False
            reasons = list(operations["blocked_reasons"])
            if "market_metadata_expired" not in reasons:
                reasons.append("market_metadata_expired")
            operations["blocked_reasons"] = sorted(reasons)
        snapshot = {
            "application": {
                "name": "Trading App",
                "environment": self._stack.config.environment,
                "mode": self._stack.modes.mode.value,
                "live_trading": self._stack.config.is_live_authorized,
                "strategy": self._strategy_name,
            },
            "supervisor": supervisor_status.as_details(),
            "operations": operations,
            "startup": self._stack.startup.as_details(),
            "portfolio": {
                "cash": str(self._stack.portfolio.cash.amount),
                "currency": self._stack.portfolio.base_currency.code,
                "realized_pnl": str(self._stack.portfolio.realized().amount),
                "positions": [position.as_details() for position in positions],
            },
            "orders": [order.as_details() for order in orders[-self._order_limit :]],
            "audit": [record.to_dict() for record in records[-self._audit_limit :]],
        }
        if self._market_data is not None:
            last_error = getattr(self._market_data, "last_error", None)
            snapshot["market_data"] = {
                "symbol": getattr(self._market_data, "symbol", None),
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
            and self._stack.monitor.status().ready
            and market_healthy
            and metadata_healthy
        )

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
                "blocked": False,
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
                "blocked": False,
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
            handler = self._handler_type(self._dashboard)
            server = _DashboardHTTPServer((self._host, self._port), handler)
            thread = threading.Thread(
                target=server.serve_forever,
                kwargs={"poll_interval": 0.1},
                name="operator-dashboard",
                daemon=True,
            )
            self._server = server
            self._thread = thread
            thread.start()

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
    def _handler_type(dashboard: OperatorDashboard):
        class Handler(BaseHTTPRequestHandler):
            server_version = "TradingAppDashboard/1"

            def do_GET(self) -> None:  # noqa: N802 - stdlib callback name
                path = urlsplit(self.path).path
                if path == "/":
                    self._send(HTTPStatus.OK, _HTML.encode(), "text/html; charset=utf-8")
                    return
                if path == "/api/snapshot":
                    body = json.dumps(
                        dashboard.snapshot(), sort_keys=True, separators=(",", ":")
                    ).encode()
                    self._send(HTTPStatus.OK, body, "application/json")
                    return
                if path == "/healthz":
                    healthy = dashboard.healthy()
                    self._send(
                        HTTPStatus.OK if healthy else HTTPStatus.SERVICE_UNAVAILABLE,
                        json.dumps({"healthy": healthy}).encode(),
                        "application/json",
                    )
                    return
                self._send(HTTPStatus.NOT_FOUND, b'{"error":"not found"}', "application/json")

            def do_POST(self) -> None:  # noqa: N802 - stdlib callback name
                self._send(
                    HTTPStatus.METHOD_NOT_ALLOWED,
                    b'{"error":"dashboard is read-only"}',
                    "application/json",
                )

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
:root{color-scheme:dark;--bg:#09111f;--panel:#111d30;--line:#263a55;--text:#e8eef8;--muted:#91a4bd;--ok:#55d6a8;--bad:#ff758f}*{box-sizing:border-box}body{margin:0;background:linear-gradient(135deg,#07101c,#0e1a2c);color:var(--text);font:14px system-ui,sans-serif}main{max-width:1200px;margin:auto;padding:28px}header{display:flex;justify-content:space-between;align-items:end;margin-bottom:22px}h1{margin:0;font-size:28px}h2{font-size:15px;margin:0 0 14px}.muted{color:var(--muted)}.badge{padding:7px 11px;border:1px solid var(--line);border-radius:999px}.ok{color:var(--ok)}.bad{color:var(--bad)}.grid{display:grid;grid-template-columns:repeat(4,1fr);gap:14px}.panel{background:#111d30e6;border:1px solid var(--line);border-radius:14px;padding:17px}.wide{grid-column:span 2;align-self:start}.full{grid-column:1/-1}.metric{font-size:24px;font-weight:700;margin-top:6px}.table-scroll{max-height:360px;overflow:auto}table{width:100%;border-collapse:collapse}th,td{text-align:left;padding:9px 7px;border-bottom:1px solid var(--line)}th{color:var(--muted);position:sticky;top:0;background:var(--panel)}pre{white-space:pre-wrap;max-height:310px;overflow:auto}@media(max-width:800px){.grid{grid-template-columns:1fr}.wide,.full{grid-column:auto}}</style></head><body><main><header><div><h1>Trading App</h1><div class="muted">Local paper operations dashboard</div></div><div id="health" class="badge">Connecting…</div></header><section class="grid"><article class="panel"><h2>Mode</h2><div id="mode" class="metric">—</div></article><article class="panel"><h2>Readiness</h2><div id="ready" class="metric">—</div></article><article class="panel"><h2>Cash</h2><div id="cash" class="metric">—</div></article><article class="panel"><h2>Cycles</h2><div id="cycles" class="metric">—</div></article><article class="panel full"><h2>Market metadata</h2><div id="metadata" class="muted">Not applicable</div></article><article class="panel wide"><h2>Positions</h2><div class="table-scroll"><table><thead><tr><th>Symbol</th><th>Quantity</th><th>Average entry</th></tr></thead><tbody id="positions"></tbody></table></div></article><article class="panel wide"><h2>Orders</h2><div class="table-scroll"><table><thead><tr><th>Status</th><th>Symbol</th><th>Side</th><th>Filled / ordered</th></tr></thead><tbody id="orders"></tbody></table></div></article><article class="panel full"><h2>Alerts</h2><pre id="alerts">No transitions yet.</pre></article><article class="panel full"><h2>Last cycle</h2><pre id="cycle">No cycle yet.</pre></article><article class="panel full"><h2>Blocked reasons</h2><div id="blocked" class="muted">—</div></article></section></main><script>
const q=id=>document.getElementById(id),cell=(r,v)=>{const d=document.createElement('td');d.textContent=v??'—';r.appendChild(d)};function rows(id,x,fs){const b=q(id);b.replaceChildren();for(const i of x){const r=document.createElement('tr');for(const f of fs)cell(r,f(i));b.appendChild(r)}if(!x.length){const r=document.createElement('tr');cell(r,'None');b.appendChild(r)}}async function refresh(){try{const r=await fetch('/api/snapshot',{cache:'no-store'}),d=await r.json(),m=d.market_metadata,h=d.supervisor.healthy&&d.operations.ready&&(!d.market_data||(d.market_data.running&&!d.market_data.last_error))&&(!m||!m.blocked);q('health').textContent=h?'Healthy':'Attention required';q('health').className='badge '+(h?'ok':'bad');q('mode').textContent=d.application.mode.toUpperCase()+' · '+(d.application.strategy||'unknown strategy');q('ready').textContent=d.operations.ready?'READY':'BLOCKED';q('ready').className='metric '+(d.operations.ready?'ok':'bad');q('cash').textContent=d.portfolio.cash+' '+d.portfolio.currency;q('cycles').textContent=d.supervisor.passes;q('metadata').textContent=m?(m.state.toUpperCase()+' · '+(m.source||'no source')+' · '+(m.seconds_remaining===null?'no expiry':Math.round(m.seconds_remaining/60)+' min remaining')):'Not applicable';q('metadata').className=m&&(m.warning||m.blocked)?'bad':'muted';q('alerts').textContent=d.alerts.length?JSON.stringify(d.alerts.slice(-10),null,2):'No transitions yet.';rows('positions',d.portfolio.positions,[x=>x.symbol,x=>x.quantity+' '+x.asset,x=>x.average_entry_price]);rows('orders',d.orders,[x=>x.state,x=>x.symbol,x=>x.side,x=>x.filled_quantity+' / '+x.ordered_quantity]);q('cycle').textContent=d.supervisor.last_cycle?JSON.stringify(d.supervisor.last_cycle,null,2):'No cycle yet.';q('blocked').textContent=d.operations.blocked_reasons.join(', ')||'None'}catch(e){q('health').textContent='Disconnected';q('health').className='badge bad'}}refresh();setInterval(refresh,2000);</script></body></html>"""
