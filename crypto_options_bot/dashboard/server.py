"""Read-only HTTP status dashboard for the crypto options bot.

Uses only the stdlib ``http.server``. A single ``BaseHTTPRequestHandler``
serves the HTML page (single-file, vanilla JS, polls every 2s) plus two
JSON endpoints: ``/api/status`` and ``/api/ticks``.

The dashboard is intentionally READ-ONLY — no buttons to place orders, no
admin actions. v2 can add a "close all" kill-switch once we're sure the
paper loop is solid.

Public surface:
    server = DashboardServer(port=8511, get_status_callable=fn)
    server.start()        # daemon thread; non-blocking
    server.stop()         # joins within 2s
    start_dashboard(...)  # one-liner that builds + starts + returns
"""
from __future__ import annotations

import json
import threading
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Callable, Optional

from loguru import logger


# ---------------------------------------------------------------------------
# HTML page (vanilla JS, single-file, no external assets)
# ---------------------------------------------------------------------------
INDEX_HTML = r"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <title>crypto-options-bot dashboard</title>
  <style>
    body { font: 13px/1.4 -apple-system, Segoe UI, Roboto, sans-serif;
           background: #0e1116; color: #d4d4d4; margin: 0; padding: 0; }
    header { background: #161b22; padding: 12px 20px; border-bottom: 1px solid #30363d;
             display: flex; align-items: center; gap: 24px; }
    header h1 { font-size: 16px; margin: 0; color: #f0f6fc; }
    header .pill { background: #21262d; padding: 3px 9px; border-radius: 9px;
                   font-size: 11px; color: #8b949e; }
    header .pill.green { background: #033a16; color: #56d364; }
    header .pill.red { background: #3a0a0a; color: #f85149; }
    main { padding: 16px 20px; max-width: 1400px; }
    section { background: #161b22; border: 1px solid #30363d; border-radius: 6px;
              margin-bottom: 16px; padding: 12px 16px; }
    section h2 { font-size: 13px; margin: 0 0 8px 0; color: #f0f6fc;
                 text-transform: uppercase; letter-spacing: 0.5px; }
    table { width: 100%; border-collapse: collapse; font-size: 12px; }
    th, td { padding: 5px 8px; text-align: left; border-bottom: 1px solid #21262d; }
    th { color: #8b949e; font-weight: 500; }
    td.num, th.num { text-align: right; font-variant-numeric: tabular-nums; }
    .pnl-pos { color: #56d364; }
    .pnl-neg { color: #f85149; }
    .empty { color: #6e7681; font-style: italic; }
    .grid { display: grid; grid-template-columns: repeat(4, 1fr); gap: 12px; }
    .metric { background: #0d1117; border: 1px solid #21262d; padding: 10px 12px;
              border-radius: 5px; }
    .metric .label { color: #8b949e; font-size: 11px;
                     text-transform: uppercase; letter-spacing: 0.5px; }
    .metric .value { font-size: 18px; color: #f0f6fc; font-weight: 500;
                     font-variant-numeric: tabular-nums; margin-top: 2px; }
    pre { background: #0d1117; padding: 8px 10px; border-radius: 4px; overflow: auto;
          max-height: 200px; font-size: 11px; }
    .refresh-info { color: #6e7681; font-size: 11px; margin-left: auto; }
  </style>
</head>
<body>
  <header>
    <h1>crypto-options-bot</h1>
    <span class="pill" id="mode-pill">mode: ?</span>
    <span class="pill" id="feed-pill">feed: ?</span>
    <span class="pill" id="ws-pill">ws: ?</span>
    <span class="pill" id="dvol-pill">dvol: ?</span>
    <span class="refresh-info" id="refresh-info">refreshing every 2s</span>
  </header>
  <main>
    <section>
      <h2>Account</h2>
      <div class="grid" id="account-grid">
        <div class="metric"><div class="label">Cash</div><div class="value" id="m-cash">$0</div></div>
        <div class="metric"><div class="label">Equity</div><div class="value" id="m-equity">$0</div></div>
        <div class="metric"><div class="label">Realized P&amp;L</div><div class="value" id="m-realized">$0</div></div>
        <div class="metric"><div class="label">Unrealized P&amp;L</div><div class="value" id="m-unrealized">$0</div></div>
      </div>
    </section>

    <section>
      <h2>Risk state</h2>
      <div class="grid" id="risk-grid">
        <div class="metric"><div class="label">Open trades</div><div class="value" id="m-open">0/0</div></div>
        <div class="metric"><div class="label">Daily P&amp;L</div><div class="value" id="m-daily">$0</div></div>
        <div class="metric"><div class="label">DVOL</div><div class="value" id="m-dvol">-</div></div>
        <div class="metric"><div class="label">IV rank</div><div class="value" id="m-ivrank">-</div></div>
      </div>
    </section>

    <section>
      <h2>Open trades</h2>
      <table>
        <thead>
          <tr>
            <th>Trade ID</th><th>Strategy</th><th>Underlying</th><th>Legs</th>
            <th class="num">Target</th><th class="num">Stop</th>
            <th class="num">P&amp;L</th><th>Opened</th>
          </tr>
        </thead>
        <tbody id="trades-tbody"><tr><td colspan="8" class="empty">No open trades</td></tr></tbody>
      </table>
    </section>

    <section>
      <h2>Positions</h2>
      <table>
        <thead>
          <tr>
            <th>Symbol</th><th class="num">Qty</th><th class="num">Avg</th>
            <th class="num">LTP</th><th class="num">P&amp;L</th><th>Underlying</th>
          </tr>
        </thead>
        <tbody id="positions-tbody"><tr><td colspan="6" class="empty">No positions</td></tr></tbody>
      </table>
    </section>

    <section>
      <h2>Recent ticks <span class="refresh-info">(last 50)</span></h2>
      <pre id="ticks-pre">loading...</pre>
    </section>

    <section>
      <h2>Strategy signals (last 24h)</h2>
      <table>
        <thead>
          <tr>
            <th>Time</th><th>Strategy</th><th>Underlying</th>
            <th>Status</th><th>Reason</th>
          </tr>
        </thead>
        <tbody id="signals-tbody"><tr><td colspan="5" class="empty">No signals yet</td></tr></tbody>
      </table>
    </section>
  </main>

  <script>
    async function poll() {
      try {
        const [status, ticks] = await Promise.all([
          fetch("/api/status").then(r => r.json()),
          fetch("/api/ticks").then(r => r.json()),
        ]);
        render(status);
        renderTicks(ticks);
      } catch (e) {
        document.getElementById("refresh-info").textContent = "error: " + e;
      }
    }
    function fmtUsd(v) {
      if (v == null) return "$0";
      const n = Number(v);
      const sign = n < 0 ? "-" : "";
      return sign + "$" + Math.abs(n).toLocaleString(undefined, {minimumFractionDigits: 2, maximumFractionDigits: 2});
    }
    function fmtPnl(v) {
      const n = Number(v || 0);
      const cls = n > 0 ? "pnl-pos" : (n < 0 ? "pnl-neg" : "");
      return `<span class="${cls}">${fmtUsd(n)}</span>`;
    }
    function fmtNum(v, digits) {
      if (v == null) return "-";
      return Number(v).toLocaleString(undefined, {minimumFractionDigits: digits, maximumFractionDigits: digits});
    }
    function render(s) {
      // Header pills
      const modePill = document.getElementById("mode-pill");
      modePill.textContent = "mode: " + (s.mode || "?");
      if (s.mode === "live") modePill.classList.add("red"); else modePill.classList.remove("red");
      const feedPill = document.getElementById("feed-pill");
      feedPill.textContent = "feed: " + (s.feed || "?");
      const wsPill = document.getElementById("ws-pill");
      wsPill.textContent = "ws: " + (s.feed_health || {}).ws_connected;
      wsPill.className = "pill " + ((s.feed_health || {}).ws_connected ? "green" : "red");
      const dvolPill = document.getElementById("dvol-pill");
      dvolPill.textContent = "dvol: " + ((s.feed_health || {}).dvol || "?");

      // Account
      const ac = s.account || {};
      document.getElementById("m-cash").textContent = fmtUsd(ac.cash);
      document.getElementById("m-equity").textContent = fmtUsd(ac.total);
      const rEl = document.getElementById("m-realized");
      rEl.innerHTML = fmtPnl(ac.realized_pnl);
      const uEl = document.getElementById("m-unrealized");
      uEl.innerHTML = fmtPnl(ac.unrealized_pnl);

      // Risk
      const r = s.risk || {};
      document.getElementById("m-open").textContent = (r.open_positions || 0) + "/" + (r.max_open_positions || 0);
      const dailyEl = document.getElementById("m-daily");
      dailyEl.innerHTML = fmtPnl(r.daily_pnl);
      document.getElementById("m-dvol").textContent = r.dvol ? r.dvol.toFixed(2) : "-";
      document.getElementById("m-ivrank").textContent = r.iv_rank != null ? r.iv_rank.toFixed(0) : "-";

      // Trades
      const tbody = document.getElementById("trades-tbody");
      if (!s.trades || s.trades.length === 0) {
        tbody.innerHTML = '<tr><td colspan="8" class="empty">No open trades</td></tr>';
      } else {
        tbody.innerHTML = s.trades.map(t => {
          const legs = (t.leg_count || 0) + (t.is_multi_leg ? " legs" : " leg");
          return `<tr>
            <td>${t.trade_id || ""}</td>
            <td>${t.strategy || ""}</td>
            <td>${t.underlying || ""}</td>
            <td>${legs}</td>
            <td class="num">${fmtNum(t.target, 4)}</td>
            <td class="num">${fmtNum(t.stop, 4)}</td>
            <td class="num">${fmtPnl(t.pnl)}</td>
            <td>${t.opened_at || ""}</td>
          </tr>`;
        }).join("");
      }

      // Positions
      const ptbody = document.getElementById("positions-tbody");
      if (!s.positions || s.positions.length === 0) {
        ptbody.innerHTML = '<tr><td colspan="6" class="empty">No positions</td></tr>';
      } else {
        ptbody.innerHTML = s.positions.map(p => `<tr>
          <td>${p.symbol || ""}</td>
          <td class="num">${p.qty}</td>
          <td class="num">${fmtNum(p.avg_price, 4)}</td>
          <td class="num">${fmtNum(p.ltp, 4)}</td>
          <td class="num">${fmtPnl(p.pnl)}</td>
          <td>${p.underlying || ""}</td>
        </tr>`).join("");
      }

      // Signals
      const stbody = document.getElementById("signals-tbody");
      if (!s.signals || s.signals.length === 0) {
        stbody.innerHTML = '<tr><td colspan="5" class="empty">No signals yet</td></tr>';
      } else {
        stbody.innerHTML = s.signals.map(sig => `<tr>
          <td>${sig.time || ""}</td>
          <td>${sig.strategy || ""}</td>
          <td>${sig.underlying || ""}</td>
          <td>${sig.status || ""}</td>
          <td>${sig.reason || ""}</td>
        </tr>`).join("");
      }
    }
    function renderTicks(t) {
      const pre = document.getElementById("ticks-pre");
      if (!t.ticks || t.ticks.length === 0) { pre.textContent = "(no ticks yet)"; return; }
      pre.textContent = t.ticks.map(x => {
        const ts = (x.timestamp || "").toString().substring(11, 19);
        return `${ts}  ${x.symbol || "?"}  ltp=${(x.ltp || 0).toFixed(4)}  bid=${(x.bid || 0).toFixed(4)}  ask=${(x.ask || 0).toFixed(4)}  iv=${(x.iv || 0).toFixed(3)}`;
      }).reverse().join("\n");
    }
    poll();
    setInterval(poll, 2000);
  </script>
</body>
</html>
"""


# ---------------------------------------------------------------------------
# Request handler
# ---------------------------------------------------------------------------
# A single mutable container so the runner can swap in real callables
# AFTER the server has started (the handler class is built at start time,
# so we can't pass callables at request time without this indirection).
_STATE: dict = {
    "status_fn": lambda: {"mode": "?", "empty": True, "note": "dashboard starting"},
    "ticks_fn": lambda n=50: {"ticks": []},
}


class _DashboardHandler(BaseHTTPRequestHandler):
    """Routes /, /api/status, /api/ticks to JSON / HTML responses.

    Reads its data callables from the module-level ``_STATE`` dict on
    every request, so updates made by the runner after `start()` are
    picked up immediately without restarting the server.
    """

    def log_message(self, fmt: str, *args) -> None:  # noqa: A003
        # Quiet the default stderr access log — the bot log already covers us.
        logger.debug(f"dashboard: {fmt % args}")

    def do_GET(self) -> None:  # noqa: N802
        try:
            if self.path == "/" or self.path.startswith("/index"):
                body = INDEX_HTML.encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return
            if self.path.startswith("/api/status"):
                fn = _STATE.get("status_fn") or (lambda: {"mode": "?", "empty": True})
                try:
                    payload = fn()
                except Exception as e:
                    logger.exception(f"status_fn error: {e}")
                    payload = {"error": str(e)}
                body = json.dumps(payload, default=str).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(body)
                return
            if self.path.startswith("/api/ticks"):
                # Optional ?n=50
                n = 50
                if "?" in self.path:
                    _, _, qs = self.path.partition("?")
                    for kv in qs.split("&"):
                        if kv.startswith("n="):
                            try:
                                n = max(1, min(500, int(kv[2:])))
                            except ValueError:
                                pass
                fn = _STATE.get("ticks_fn") or (lambda n=50: {"ticks": []})
                try:
                    payload = fn(n)
                except Exception as e:
                    logger.exception(f"ticks_fn error: {e}")
                    payload = {"ticks": [], "error": str(e)}
                body = json.dumps(payload, default=str).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(body)
                return
            self.send_response(404)
            self.send_header("Content-Type", "text/plain")
            self.end_headers()
            self.wfile.write(b"not found")
        except Exception as e:
            logger.exception(f"dashboard handler error: {e}")
            try:
                self.send_response(500)
                self.end_headers()
            except Exception:
                pass


# Backwards-compat: the build_handler factory still works, but it now just
# mutates the shared state. We keep it for API parity.
def _build_handler(
    status_fn: Callable[[], dict],
    ticks_fn: Callable[[int], dict],
) -> type:
    _STATE["status_fn"] = status_fn
    _STATE["ticks_fn"] = ticks_fn
    return _DashboardHandler


# ---------------------------------------------------------------------------
# Server
# ---------------------------------------------------------------------------
class DashboardServer:
    """Threaded HTTP server wrapper. Start with ``.start()``."""

    def __init__(
        self,
        port: int = 8511,
        host: str = "127.0.0.1",
        get_status: Optional[Callable[[], dict]] = None,
        get_ticks: Optional[Callable[[int], dict]] = None,
    ):
        self.port = int(port)
        self.host = host
        # Setting these also pushes into the shared _STATE dict the handler
        # reads on every request, so changes made after start() are visible
        # immediately (no server restart).
        self.set_status(get_status or (lambda: {"mode": "?", "empty": True}))
        self.set_ticks(get_ticks or (lambda n=50: {"ticks": []}))

    def set_status(self, fn) -> None:
        """Set the status callback. Hot-swaps on the next request."""
        self._get_status = fn
        _STATE["status_fn"] = fn

    def set_ticks(self, fn) -> None:
        """Set the ticks callback. Hot-swaps on the next request."""
        self._get_ticks = fn
        _STATE["ticks_fn"] = fn
        self._httpd: Optional[ThreadingHTTPServer] = None
        self._thread: Optional[threading.Thread] = None

    def start(self) -> None:
        if self._httpd is not None:
            return
        handler_cls = _build_handler(self._get_status, self._get_ticks)
        try:
            self._httpd = ThreadingHTTPServer((self.host, self.port), handler_cls)
        except OSError as e:
            logger.error(f"dashboard: cannot bind {self.host}:{self.port} — {e}")
            return
        self._thread = threading.Thread(
            target=self._httpd.serve_forever,
            name="dashboard-httpd",
            daemon=True,
        )
        self._thread.start()
        logger.success(f"dashboard: serving http://{self.host}:{self.port}/ (read-only)")

    def stop(self) -> None:
        if self._httpd is None:
            return
        try:
            self._httpd.shutdown()
        except Exception as e:
            logger.debug(f"dashboard shutdown: {e}")
        self._httpd = None
        if self._thread:
            self._thread.join(timeout=2)
            self._thread = None
        logger.info("dashboard: stopped")


def start_dashboard(
    port: int = 8511,
    host: str = "127.0.0.1",
    get_status: Optional[Callable[[], dict]] = None,
    get_ticks: Optional[Callable[[int], dict]] = None,
) -> DashboardServer:
    """One-shot helper: build + start a DashboardServer."""
    srv = DashboardServer(port=port, host=host, get_status=get_status, get_ticks=get_ticks)
    srv.start()
    return srv


__all__ = ["DashboardServer", "start_dashboard", "INDEX_HTML"]
