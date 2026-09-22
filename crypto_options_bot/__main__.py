"""Crypto Options Trading Bot — entry point.

Subcommands:
  paper     Run a paper-trading session (default; --feed ws|rest)
  live      Run a live-trading session (requires DERIBIT_CLIENT_ID/SECRET
            and DERIBIT_LIVE_CONFIRMED=YES in the env)
  status    Print current account state (works for both paper and live)
  reset     Clear paper state

Common flags:
  --max-runtime N    Stop after N seconds (smoke tests). 0 = forever.
  --feed ws|rest     Data feed: WS (real-time) or REST (2s polling fallback).
  --verbose          Per-tick INFO log streaming.
  --dashboard-port N Start the read-only HTTP dashboard (default 8511).
                     Use 0 to disable.
  --config PATH      Path to YAML config (default: config/settings.yaml).

Architecture:

   Deribit public WSS (wss://test.deribit.com/ws/api/v2)  default
   Deribit public REST (test.deribit.com/api/v2)          fallback
            |
            |  WS: spot @1s, ticker.{INSTRUMENT}.100ms
            |  REST: get_book_summary @2s, get_ticker (mark_iv) @30s
            v
   DeribitWebSocketFeed / DeribitFeed
            |  on_tick(dict)
            v
   PaperClient  (paper)   OR   DeribitClient (live)
            |                |
            v                v
   Strategy (5 strategies)
            |  build_plan(ctx, account)
            |  SignalContext <- spot, strikes, LTPs, IVs, DVOL, regime
            v
   RiskEngine.check_trade(plan)
            |  allowed? qty?  preset (aggressive/base/defensive)
            v
   OrderManager.execute_plan(plan)
            |
            v
   Target/Stop monitor  -> auto-close on plan.target / -plan.stop
            |
            v
   data_cache/paper_state.json + trades_state.json  crash recovery
   logs/pnl_history.csv + logs/trade_events.csv      telemetry
   (optional) TelegramAlerter                        notifications
   (optional) DashboardServer http://127.0.0.1:8511/  read-only UI
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import signal
import sys
import threading
import time
from collections import deque
from datetime import datetime, timezone, date
from pathlib import Path
from typing import Optional

import yaml
from dotenv import load_dotenv
from loguru import logger

from .broker.base import Tick
from .broker.paper_client import PaperClient
from .data.deribit_feed import DeribitFeed
from .execution.order_manager import OrderManager
from .risk.engine import RiskEngine
from .strategy.base import SignalContext, StrategyName, TradePlan
from .strategy.iron_condor import IronCondorStrategy
from .strategy.short_strangle import ShortStrangleStrategy
from .strategy.short_call import ShortCallStrategy
from .strategy.directional_debit import DirectionalDebitStrategy
from .strategy.calendar_spread import CalendarSpreadStrategy
from .strategy.long_straddle import LongStraddleStrategy
from .utils.logger import setup_logger

# Type hint for the union of the two feed types. Avoid importing the WS feed
# at module level so a broken WSS stack (e.g. missing websocket-client) doesn't
# block the REST path.
from typing import TYPE_CHECKING
if TYPE_CHECKING:
    from .data.deribit_ws import DeribitWebSocketFeed


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
def load_config(path: str = "config/settings.yaml") -> dict:
    """Load YAML config; missing file -> empty dict (use defaults)."""
    p = Path(path)
    if not p.exists():
        logger.warning(f"config not found at {path}, using defaults")
        return {}
    with open(p, "r", encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


# ---------------------------------------------------------------------------
# Tick adapter: data feed (raw dict) -> broker.Tick
# ---------------------------------------------------------------------------
def _build_tick_from_dict(d: dict, mark_price_proxy: bool = False) -> Optional[Tick]:
    """Convert a feed tick dict to a broker.Tick. Returns None if invalid.

    Args:
        d: the raw tick dict from the feed
        mark_price_proxy: when True, if both bid and ask are 0 but LTP > 0,
            use LTP as both bid and ask (so the limit-fill simulator can fill
            even on testnet strikes that have no real bid/ask).
    """
    try:
        ltp = float(d.get("ltp", 0) or 0)
        bid = float(d.get("bid", 0) or 0)
        ask = float(d.get("ask", 0) or 0)
        if mark_price_proxy and ltp > 0 and bid == 0 and ask == 0:
            # Synthesise a 0.5% spread so the limit-fill simulator can run.
            spread = max(0.0001, ltp * 0.0025)
            bid = max(0.0, ltp - spread)
            ask = ltp + spread
        return Tick(
            symbol=d["symbol"],
            ltp=ltp,
            bid=bid,
            ask=ask,
            volume=int(d.get("volume", 0) or 0),
            oi=int(d.get("oi", 0) or 0),
            timestamp=datetime.now(timezone.utc),
            exchange=d.get("exchange", "DERIBIT"),
            strike=float(d.get("strike", 0) or 0),
            option_type=d.get("option_type"),
            expiry=d.get("expiry"),
            underlying=d.get("underlying"),
            iv=float(d.get("iv", 0) or 0),
        )
    except Exception as e:
        logger.debug(f"bad tick dict, skipping: {e}")
        return None


# ---------------------------------------------------------------------------
# Per-cycle P&L log writer (CSV)
# ---------------------------------------------------------------------------
class _PnLWriter:
    """Appends per-cycle P&L to logs/pnl_history.csv and trade events to
    logs/trade_events.csv. Thread-safe."""

    HEADER_PNL = ["timestamp", "cycle", "equity", "realized", "unrealized", "open_positions", "cash", "dvol", "iv_rank", "preset"]
    HEADER_EVENT = ["timestamp", "event", "trade_id", "plan_summary", "pnl"]

    def __init__(self, log_dir: str = "logs"):
        d = Path(log_dir)
        d.mkdir(parents=True, exist_ok=True)
        self.pnl_path = d / "pnl_history.csv"
        self.event_path = d / "trade_events.csv"
        self._lock = threading.Lock()
        for p, header in ((self.pnl_path, self.HEADER_PNL), (self.event_path, self.HEADER_EVENT)):
            if not p.exists():
                with p.open("w", newline="", encoding="utf-8") as f:
                    csv.writer(f).writerow(header)

    def log_cycle(self, *, cycle: int, equity: float, realized: float,
                  unrealized: float, open_positions: int, cash: float,
                  dvol: float, iv_rank: float, preset: str) -> None:
        with self._lock:
            try:
                with self.pnl_path.open("a", newline="", encoding="utf-8") as f:
                    csv.writer(f).writerow([
                        datetime.now(timezone.utc).isoformat(timespec="seconds"),
                        cycle,
                        round(equity, 4),
                        round(realized, 4),
                        round(unrealized, 4),
                        int(open_positions),
                        round(cash, 4),
                        round(dvol, 4),
                        round(iv_rank, 2),
                        preset,
                    ])
            except Exception as e:
                logger.debug(f"pnl_history write failed: {e}")

    def log_event(self, *, event: str, trade_id: str, plan_summary: str, pnl: float = 0.0) -> None:
        with self._lock:
            try:
                with self.event_path.open("a", newline="", encoding="utf-8") as f:
                    csv.writer(f).writerow([
                        datetime.now(timezone.utc).isoformat(timespec="seconds"),
                        event,
                        trade_id,
                        plan_summary[:200],
                        round(pnl, 4),
                    ])
            except Exception as e:
                logger.debug(f"trade_events write failed: {e}")


# ---------------------------------------------------------------------------
# Signal log buffer (in-memory; the dashboard reads from this)
# ---------------------------------------------------------------------------
class _SignalLog:
    """Bounded ring buffer of recent strategy signals for the dashboard."""

    def __init__(self, maxlen: int = 200):
        self._buf: deque = deque(maxlen=maxlen)
        self._lock = threading.Lock()

    def append(self, *, strategy: str, underlying: str, status: str, reason: str) -> None:
        with self._lock:
            self._buf.append({
                "time": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                "strategy": strategy,
                "underlying": underlying,
                "status": status,
                "reason": (reason or "")[:200],
            })

    def last_24h(self) -> list:
        cutoff = time.time() - 24 * 3600
        with self._lock:
            out = []
            for item in self._buf:
                try:
                    ts = datetime.fromisoformat(item["time"]).timestamp()
                except Exception:
                    continue
                if ts >= cutoff:
                    out.append(item)
            return out


# ---------------------------------------------------------------------------
# Main loop
# ---------------------------------------------------------------------------
class PaperRunner:
    """The paper/live trading session: feed -> broker, strategies -> risk -> orders.

    Works for both paper and live modes; the only thing that changes is the
    ``broker`` instance (PaperClient vs DeribitClient).
    """

    def __init__(
        self,
        cfg: dict,
        feed_mode: str = "ws",
        verbose: bool = False,
        mode: str = "paper",
        alerter=None,
        dashboard=None,
        pnl_writer: Optional[_PnLWriter] = None,
        signal_log: Optional[_SignalLog] = None,
        trader=None,                # NEW: optional Trader (agent layer) gate
        recover_orphan: bool = False,
    ):
        self.cfg = cfg
        self.feed_mode = feed_mode  # "ws" or "rest"
        self.verbose = verbose      # per-tick INFO log streaming
        self.mode = mode            # "paper" or "live"
        self.alerter = alerter      # optional TelegramAlerter
        self.dashboard = dashboard  # optional DashboardServer
        self.pnl_writer = pnl_writer or _PnLWriter()
        self.signal_log = signal_log or _SignalLog()
        self.trader = trader        # NEW: None = rule-based path only
        self.recover_orphan = bool(recover_orphan)  # auto-fix journal/broker drift on startup
        self._stop = threading.Event()
        self._signaled_exit: bool = False  # set True by SIGINT/SIGTERM handler
        self._last_heartbeat = 0.0
        self._cycle_count = 0
        self._last_plan_at: dict[str, float] = {}  # strategy_name -> ts of last fire
        self._cycle_plans_produced: bool = False  # reset per cycle; True if anything executed
        self._cooldown_sec = float(cfg.get("strategy", {}).get("cooldown_sec", 300))
        self._first_chain_tick_logged = False
        # Mark-price proxy flag (read once, used in the tick adapter)
        self._mark_price_proxy = bool(
            cfg.get("data", {}).get("mark_price_proxy", True)
        )
        # Min IV rank gate (skips strategies when data quality is bad)
        self._min_iv_rank_to_trade = float(
            cfg.get("data", {}).get("min_iv_rank_to_trade", 30.0)
        )
        # Min DTE gate (skip legs with DTE < this — no theta runway for 0DTE/1DTE)
        self._min_dte_to_trade = float(
            cfg.get("data", {}).get("min_dte_to_trade", 2.0)
        )
        # IV-regime circuit breaker. The bot pauses trading on a currency
        # when EITHER DVOL < min_dvol OR iv_rank < min_iv_rank for that
        # currency. Per-currency thresholds because BTC and ETH have
        # different vol regimes.
        rg_cfg = cfg.get("data", {}).get("iv_regime_gate", {}) or {}
        self._regime_gate_enabled = bool(rg_cfg.get("enabled", True))
        self._regime_gate_log_cooldown_sec = float(rg_cfg.get("log_cooldown_sec", 300))
        self._regime_gate_thresholds: dict[str, tuple[float, float]] = {}
        for cur in ("BTC", "ETH"):
            cur_cfg = rg_cfg.get(cur, {}) or {}
            self._regime_gate_thresholds[cur] = (
                float(cur_cfg.get("min_dvol", 50.0)),
                float(cur_cfg.get("min_iv_rank", 40.0)),
            )
        # Per-(currency, reason) timestamp of last "REGIME GATE engaged" log
        # so we don't spam when the gate is closed for hours.
        self._regime_gate_last_log: dict[tuple[str, str], float] = {}
        # Hot-reload: track the last time we read settings.yaml so we can
        # re-read it when the file changes. This lets DVOL floors, iv_rank
        # floors, and other knobs be tuned without restarting the bot.
        self._settings_yaml_path = "config/settings.yaml"
        self._settings_yaml_mtime: float = 0.0

    def _maybe_reload_config(self) -> None:
        """Reload regime gate thresholds from settings.yaml when the file changes.

        Cheap to call every cycle (just a stat + dict compare). Lets the user
        tune DVOL / iv_rank / cooldown floors without restarting the bot.
        """
        try:
            p = Path(self._settings_yaml_path)
            if not p.exists():
                return
            mtime = p.stat().st_mtime
            if mtime == self._settings_yaml_mtime:
                return
            self._settings_yaml_mtime = mtime
            cfg = load_config(self._settings_yaml_path)
            rg_cfg = cfg.get("data", {}).get("iv_regime_gate", {}) or {}
            self._regime_gate_enabled = bool(rg_cfg.get("enabled", self._regime_gate_enabled))
            self._regime_gate_log_cooldown_sec = float(
                rg_cfg.get("log_cooldown_sec", self._regime_gate_log_cooldown_sec)
            )
            self._cooldown_sec = float(
                cfg.get("strategy", {}).get("cooldown_sec", self._cooldown_sec)
            )
            for cur in ("BTC", "ETH"):
                cur_cfg = rg_cfg.get(cur, {}) or {}
                self._regime_gate_thresholds[cur] = (
                    float(cur_cfg.get("min_dvol", self._regime_gate_thresholds.get(cur, (50.0, 40.0))[0])),
                    float(cur_cfg.get("min_iv_rank", self._regime_gate_thresholds.get(cur, (50.0, 40.0))[1])),
                )
            logger.info(
                "config reloaded: cooldown=%.0fs  regime_gate=%s  thresholds=%s",
                self._cooldown_sec,
                "enabled" if self._regime_gate_enabled else "disabled",
                self._regime_gate_thresholds,
            )
        except Exception as e:  # noqa: BLE001
            logger.warning(f"config reload failed (using cached values): {e}")

    def _make_verbose_tick_callback(self) -> callable:
        """Return a tick callback that logs each tick at INFO if --verbose."""
        def _on_verbose_tick(t: dict):
            if not self.verbose:
                return
            sym = t.get("symbol", "?")
            ltp = float(t.get("ltp", 0) or 0)
            bid = float(t.get("bid", 0) or 0)
            ask = float(t.get("ask", 0) or 0)
            iv = float(t.get("iv", 0) or 0)
            logger.info(
                f"tick {sym} ltp={ltp:.4f} bid={bid:.4f} ask={ask:.4f} iv={iv:.3f}"
            )
        return _on_verbose_tick

    def _connect_tick_pipeline(self):
        """Wire the data feed -> broker tick pipeline and construct the rest.

        Returns ``(broker, feed, order_mgr, risk)``. The broker is a
        PaperClient (paper mode) or DeribitClient (live mode).
        """
        broker_cfg = self.cfg.get("broker", {})
        data_cfg = self.cfg.get("data", {})
        ws_cfg = data_cfg.get("ws", {})
        risk_cfg = self.cfg.get("risk", {})

        if self.mode == "live":
            from .broker.deribit_client import DeribitClient
            env = data_cfg.get("deribit_env", "testnet")
            live_cfg = broker_cfg.get("live", {}) or {}
            broker = DeribitClient(
                env=env,
                client_id=os.environ.get("DERIBIT_CLIENT_ID"),
                client_secret=os.environ.get("DERIBIT_CLIENT_SECRET"),
                live_confirmed=True,
                persist_path=live_cfg.get("persist_path", "data_cache/live_state.json"),
            )
        else:
            broker = PaperClient(
                starting_capital=float(broker_cfg.get("paper_capital", 100_000.0)),
                slippage_bps=float(broker_cfg.get("slippage_bps", 5.0)),
                limit_fill_spread_pct=float(broker_cfg.get("limit_fill_spread_pct", 0.1)),
                limit_fill_min_spread=float(broker_cfg.get("limit_fill_min_spread", 0.01)),
                fill_mode=broker_cfg.get("fill_mode", "market_like"),
                persist_path=broker_cfg.get("persist_path", "data_cache/paper_state.json"),
            )
        broker.connect()

        feed = self._build_feed(data_cfg, ws_cfg)

        # tick plumbing: feed dict -> broker Tick
        def _on_feed_tick(d: dict):
            t = _build_tick_from_dict(d, mark_price_proxy=self._mark_price_proxy)
            if t is not None:
                broker.inject_tick(t)
                if t.option_type and not self._first_chain_tick_logged:
                    self._first_chain_tick_logged = True
                    logger.success(
                        f"first chain tick: {t.symbol} ltp={t.ltp} bid={t.bid} ask={t.ask} iv={t.iv:.4f}"
                    )

        feed.on_tick(_on_feed_tick)
        # Verbose per-tick streaming (INFO logs when --verbose is set)
        feed.on_tick(self._make_verbose_tick_callback())
        feed.start()
        feed.subscribe(feed.currencies)  # spot ticks first; chain follows naturally

        risk = RiskEngine(risk_cfg)
        risk.update_capital(broker.starting_capital)

        order_mgr = OrderManager(broker, persist_path="data_cache/trades_state.json")
        # Pinned leg strikes for any open trades (so the feed keeps them)
        pinned = set()
        for t in order_mgr.open_trades():
            for o in t.orders:
                if o.symbol and o.symbol not in feed.currencies:
                    pinned.add(o.symbol)
        if pinned:
            try:
                feed.keep_alive_subscribe(list(pinned))
                logger.info(f"[KEEP-ALIVE] pinned {len(pinned)} open-trade leg symbols on startup")
            except Exception as e:
                logger.debug(f"keep_alive_subscribe failed: {e}")

        # Startup reconciliation: detect drift between journal (true history of
        # all trades) and broker (in-memory positions). If a bot restart lost
        # broker state but the journal still has open trades, the broker will
        # think it has 0 positions while the journal has N open. Recover either
        # automatically (--recover-orphan) or just warn loudly.
        try:
            journal_open = len(order_mgr.open_trades())
            broker_positions = len(broker.get_positions()) if hasattr(broker, "get_positions") else 0
            if journal_open > 0 and broker_positions == 0:
                msg = (
                    f"[STARTUP-DRIFT] journal has {journal_open} open trade(s) but broker "
                    f"has 0 positions — likely a previous bot restart lost broker state "
                    f"before paper_state.json was flushed."
                )
                if self.recover_orphan:
                    msg += " --recover-orphan set; rebuilding from journal…"
                    logger.warning(msg)
                    if self._recover_orphan(broker, order_mgr):
                        # Re-check after rebuild
                        broker_positions = (
                            len(broker.get_positions())
                            if hasattr(broker, "get_positions") else 0
                        )
                        logger.success(
                            f"[STARTUP-DRIFT] recovered: broker now has {broker_positions} positions"
                        )
                else:
                    msg += (
                        " Re-run with --recover-orphan to auto-fix, or run: "
                        "python scripts/rebuild_broker_positions.py --force"
                    )
                    logger.warning(msg)
            elif journal_open > 0 and broker_positions != journal_open * 2:
                # Short-strangle → 2 legs per open trade, so broker ~= 2 × journal_open.
                # If that ratio doesn't hold, there's drift worth logging but maybe not
                # auto-fixing (could be from partial fills or closures).
                logger.info(
                    f"[STARTUP-CHECK] journal_open={journal_open} broker_positions="
                    f"{broker_positions} (expect ~{journal_open * 2} for full strangle coverage)"
                )
            else:
                logger.info(
                    f"[STARTUP-CHECK] journal_open={journal_open} broker_positions="
                    f"{broker_positions} — clean"
                )
        except Exception as e:
            logger.debug(f"startup reconcile check failed: {e}")

        # wire trade events: alerter + pnl telemetry
        order_mgr.set_event_callback(self._on_trade_event)
        return broker, feed, order_mgr, risk

    def _recover_orphan(self, broker, order_mgr) -> bool:
        """Re-derive broker._positions from the trade journal and persist.

        Mirrors scripts/rebuild_broker_positions.py but runs inline so the live
        bot picks up the rebuilt state immediately (no restart needed). Returns
        True on success.
        """
        try:
            trades_path = Path(getattr(order_mgr, "persist_path", "data_cache/trades_state.json"))
            paper_path = Path(getattr(broker, "persist_path", "data_cache/paper_state.json"))
            if not trades_path.exists():
                logger.warning(f"[recover_orphan] no {trades_path}")
                return False

            trades_state = json.loads(trades_path.read_text(encoding="utf-8"))
            trades = trades_state.get("trades", {}) or {}

            # Aggregate per-symbol net qty + VWAP fill price across COMPLETE orders
            # in OPEN trades only (closed trades already unwound themselves).
            agg: dict[str, dict] = {}
            for tid, td in trades.items():
                if td.get("closed_at"):
                    continue
                for od in td.get("orders", []) or []:
                    # OrderStatus enum values are lowercase: "complete", "open", etc.
                    # Be tolerant of case to handle older journals.
                    if str(od.get("status", "")).strip().lower() != "complete":
                        continue
                    sym = od.get("symbol")
                    if not sym:
                        continue
                    side = od.get("side", "BUY")
                    qty = float(od.get("filled_qty") or od.get("qty") or 0)
                    avg = float(od.get("avg_fill_price") or 0)
                    if sym not in agg:
                        agg[sym] = {
                            "qty": 0.0,
                            "notional": 0.0,
                            "strike": od.get("strike"),
                            "option_type": od.get("option_type"),
                            "expiry": od.get("expiry"),
                            "underlying": od.get("underlying"),
                            "exchange": od.get("exchange", "DERIBIT"),
                            "entry_time": td.get("opened_at"),
                        }
                    sign = 1.0 if side.upper().startswith("B") else -1.0
                    agg[sym]["qty"] += sign * qty
                    agg[sym]["notional"] += sign * qty * avg

            positions: dict[str, dict] = {}
            now_iso = datetime.now(timezone.utc).isoformat()
            for sym, a in agg.items():
                if abs(a["qty"]) < 1e-9:
                    continue
                avg_price = round(a["notional"] / a["qty"], 6) if a["qty"] != 0 else 0.0
                positions[sym] = {
                    "symbol": sym,
                    "qty": int(round(a["qty"])),
                    "avg_price": avg_price,
                    "ltp": avg_price,
                    "exchange": a["exchange"] or "DERIBIT",
                    "pnl": 0.0,
                    "strike": a["strike"],
                    "option_type": a["option_type"],
                    "expiry": a["expiry"],
                    "underlying": a["underlying"],
                    "contract_size": 1.0,
                    "entry_time": a["entry_time"] or now_iso,
                }

            # Mutate broker in-memory state directly (broker._load_state was
            # already called at construction; we mirror that contract here).
            try:
                broker._positions.clear()
                for sym, pd_dict in positions.items():
                    from .broker.base import Position
                    pos = Position(
                        symbol=pd_dict["symbol"],
                        qty=pd_dict["qty"],
                        avg_price=pd_dict["avg_price"],
                        ltp=pd_dict["ltp"],
                        exchange=pd_dict["exchange"],
                        pnl=pd_dict["pnl"],
                        strike=pd_dict["strike"],
                        option_type=pd_dict["option_type"],
                        expiry=pd_dict["expiry"],
                        underlying=pd_dict["underlying"],
                        contract_size=pd_dict["contract_size"],
                        entry_time=pd_dict["entry_time"],
                    )
                    broker._positions[sym] = pos
                # Persist the rebuilt state immediately
                broker._save_state()
            except Exception as inner:
                # Last-resort: write paper_state.json directly if broker
                # internals aren't accessible.
                logger.warning(
                    f"[recover_orphan] broker-direct update failed ({inner}); "
                    f"falling back to direct file write"
                )
                paper_state = {
                    "cash": getattr(broker, "_cash", 100_000.0),
                    "realized_pnl": getattr(broker, "_realized_pnl", 0.0),
                    "orders": {},
                    "positions": positions,
                }
                if paper_path.exists():
                    tmp = paper_path.with_suffix(".tmp")
                    tmp.write_text(json.dumps(paper_state, indent=2, default=str), encoding="utf-8")
                    os.replace(tmp, paper_path)
                else:
                    paper_path.parent.mkdir(parents=True, exist_ok=True)
                    paper_path.write_text(json.dumps(paper_state, indent=2, default=str), encoding="utf-8")

            logger.success(
                f"[recover_orphan] derived {len(positions)} position(s) from journal:"
            )
            for sym, pd_dict in positions.items():
                logger.success(
                    f"  {sym:32s} qty={pd_dict['qty']:+d} avg={pd_dict['avg_price']:.4f}"
                )
            return True
        except Exception as e:
            logger.exception(f"[recover_orphan] failed: {e}")
            return False

    def _on_trade_event(self, event: str, trade) -> None:
        """Callback from OrderManager — invoked when a trade is opened/closed."""
        try:
            plan = getattr(trade, "plan", None)
            strategy_name = plan.strategy.value if plan else "?"
            underlying = plan.underlying if plan else "?"
            if event == "opened":
                # PnL log
                self.pnl_writer.log_event(
                    event="opened",
                    trade_id=trade.trade_id,
                    plan_summary=f"{strategy_name} {underlying} {len(plan.legs) if plan else 0} legs",
                )
                if self.alerter is not None and plan is not None:
                    fills = list(getattr(trade, "orders", []))
                    self.alerter.notify_trade_opened(plan, fills)
            elif event == "closed":
                # realized P&L is updated by the close, but PaperClient's
                # _realized_pnl is the source of truth. Read it now.
                realized = float(getattr(self._broker_ref(), "_realized_pnl", 0.0) or 0.0)
                self.pnl_writer.log_event(
                    event="closed",
                    trade_id=trade.trade_id,
                    plan_summary=f"{strategy_name} {underlying} exit={getattr(trade, 'exit_reason', '?')}",
                    pnl=realized,
                )
                if self.alerter is not None:
                    self.alerter.notify_trade_closed(trade)
                reason = getattr(trade, "exit_reason", "?")
                if reason in ("target_hit", "stop_hit") and self.alerter is not None:
                    self.alerter.notify_target_stop(trade, reason)
                # Adaptive preset tracking
                try:
                    pnl = float(getattr(trade, "realized_pnl", 0.0) or 0.0)
                    if self._risk_ref is not None:
                        self._risk_ref.record_trade_result(pnl)
                except Exception as e:
                    logger.debug(f"record_trade_result: {e}")
        except Exception as e:
            logger.exception(f"on_trade_event error: {e}")

    def _broker_ref(self):
        return getattr(self, "_broker", None)

    def _build_feed(self, data_cfg: dict, ws_cfg: dict):
        """Construct the data feed: WS by default, REST fallback on failure."""
        env = data_cfg.get("deribit_env", "testnet")
        currencies = data_cfg.get("currencies", ["BTC", "ETH"])
        if self.feed_mode == "rest":
            logger.info(f"Data feed: REST polling (forced via --feed rest)")
            return DeribitFeed(
                env=env,
                currencies=currencies,
                poll_interval_sec=float(data_cfg.get("poll_interval_sec", 2.0)),
                strike_window_pct=float(data_cfg.get("strike_window_pct", 0.20)),
                iv_cache_sec=float(data_cfg.get("iv_cache_sec", 30.0)),
            )
        # Try WS, fall back to REST on any error
        try:
            from .data.deribit_ws import DeribitWebSocketFeed
        except Exception as e:
            logger.warning(f"WS feed import failed ({e}), falling back to REST")
            return self._rest_feed(data_cfg, env, currencies)
        try:
            ws_feed = DeribitWebSocketFeed(
                env=env,
                currencies=currencies,
                strike_window_pct=float(data_cfg.get("strike_window_pct", 0.20)),
                max_strikes_per_underlying=int(ws_cfg.get("max_strikes_per_underlying", 21)),
                max_strikes_per_expiry=int(ws_cfg.get("max_strikes_per_expiry", 5)),
                expiry_count=int(ws_cfg.get("expiry_count", 5)),
                min_dte=int(data_cfg.get("min_dte_to_trade", 2)),
                reconnect_delay_sec=float(ws_cfg.get("reconnect_delay_sec", 2.0)),
                max_reconnect_attempts=int(ws_cfg.get("max_reconnect_attempts", 10000)),
                dvol_cache_sec=float(ws_cfg.get("dvol_cache_sec", 300.0)),
            )
            ws_feed.start()
            # Give it up to 15 seconds to connect. Deribit's WSS handshake
            # can take 5-8s on a slow network and we now have a keepalive
            # thread (public/test_request every 30s) that prevents the
            # 2-min idle reconnect storm. Fall back to REST if WS still
            # hasn't connected so the bot can trade immediately; it can
            # be upgraded back to WS by a future restart.
            connected = False
            deadline = time.time() + 15.0
            while time.time() < deadline:
                if ws_feed.is_connected():
                    connected = True
                    break
                time.sleep(0.1)
            if not connected:
                logger.warning("WS feed failed to connect within 15s, falling back to REST")
                ws_feed.stop()
                return self._rest_feed(data_cfg, env, currencies)
            logger.success(f"Data feed: WS (real-time) on {ws_feed.ws_url}")
            return ws_feed
        except Exception as e:
            logger.warning(f"WS feed init failed ({e}), falling back to REST")
            return self._rest_feed(data_cfg, env, currencies)

    @staticmethod
    def _rest_feed(data_cfg: dict, env: str, currencies: list[str]) -> DeribitFeed:
        """Construct the REST polling feed (used as fallback)."""
        logger.info(f"Data feed: REST polling (fallback) env={env}")
        return DeribitFeed(
            env=env,
            currencies=currencies,
            poll_interval_sec=float(data_cfg.get("poll_interval_sec", 2.0)),
            strike_window_pct=float(data_cfg.get("strike_window_pct", 0.20)),
            iv_cache_sec=float(data_cfg.get("iv_cache_sec", 30.0)),
        )

    def _build_strategies(self) -> list:
        """Construct strategy instances from config."""
        strat_cfg = self.cfg.get("strategy", {})
        strategies = []
        if "iron_condor" in strat_cfg:
            strategies.append(IronCondorStrategy(strat_cfg.get("iron_condor", {})))
        if "short_strangle" in strat_cfg:
            strategies.append(ShortStrangleStrategy(strat_cfg.get("short_strangle", {})))
        if "short_call" in strat_cfg:
            strategies.append(ShortCallStrategy(strat_cfg.get("short_call", {})))
        if "directional_debit" in strat_cfg:
            strategies.append(DirectionalDebitStrategy(strat_cfg.get("directional_debit", {})))
        if "calendar_spread" in strat_cfg:
            strategies.append(CalendarSpreadStrategy(strat_cfg.get("calendar_spread", {})))
        if "long_straddle" in strat_cfg:
            strategies.append(LongStraddleStrategy(strat_cfg.get("long_straddle", {})))
        return strategies

    def _build_signal_context(self, underlying, feed, broker) -> Optional[SignalContext]:
        """Build a SignalContext for `underlying` from current feed state."""
        spot = feed.get_spot(underlying)
        if spot <= 0:
            return None
        oi_map = feed.get_oi_map(underlying)
        if not oi_map:
            return None
        strikes = sorted(oi_map.keys())
        option_ltps: dict = {}
        option_ivs: dict = {}
        # Strategy uses REAL bid/ask-derived prices only. The previous
        # "fall back to BS synthetic" path produced prices that looked
        # real but were actually for stale quotes — the Trader LLM
        # correctly identified them as broken data and VETOED every
        # cycle. The only path to real trades is to require REAL
        # bid/ask quotes for the legs we use.
        for s in strikes:
            ce = oi_map[s].get("ce_ltp", 0.0)
            pe = oi_map[s].get("pe_ltp", 0.0)
            ce_iv = oi_map[s].get("ce_iv", 0.0)
            pe_iv = oi_map[s].get("pe_iv", 0.0)
            # Only include legs with a real (non-zero) last-price. This
            # filters out testnet strikes with bid=0/ask=0.0001 that
            # would otherwise trick the strategy into thinking the
            # option is genuinely at $0.
            if ce > 0:
                option_ltps[(s, "C")] = ce
                option_ivs[(s, "C")] = ce_iv
            if pe > 0:
                option_ltps[(s, "P")] = pe
                option_ivs[(s, "P")] = pe_iv
        atm = feed.get_atm_strike(underlying)
        atm_iv = 0.0
        if atm:
            atm_iv = (option_ivs.get((atm, "C"), 0.0) + option_ivs.get((atm, "P"), 0.0)) / 2.0

        # DVOL-based IV rank
        dvol_pct = 0.0
        try:
            dvol_pct = float(feed.get_dvol(underlying)) * 100.0
        except (AttributeError, Exception):
            dvol_pct = 0.0
        if dvol_pct > 0 and atm_iv > 0:
            ratio = dvol_pct / (atm_iv * 100.0)
            iv_rank = float(min(100.0, max(0.0, ratio * 50.0)))
        else:
            if atm_iv <= 0.0:
                iv_rank = 50.0
            elif atm_iv < 0.20:
                iv_rank = 40.0
            elif atm_iv < 0.35:
                iv_rank = 55.0
            elif atm_iv < 0.55:
                iv_rank = 70.0
            elif atm_iv < 0.80:
                iv_rank = 80.0
            else:
                iv_rank = 90.0

        # Quality gate: when data is sparse (IV all zero) iv_rank is hard-coded
        # to 50, which can let strategies fire. Suppress that by tagging the
        # data quality and clamping below the user-defined threshold.
        data_quality_bad = (atm_iv <= 0.0 and dvol_pct <= 0.0)
        if data_quality_bad and iv_rank < self._min_iv_rank_to_trade:
            iv_rank = self._min_iv_rank_to_trade  # explicit "no signal" tag

        # crude regime from price momentum (windowed)
        mom = feed.get_momentum(underlying, window=20)
        adx = 20.0 + min(40.0, abs(mom) * 1000)
        trend = max(-1.0, min(1.0, mom * 50))
        regime = "range"
        if abs(trend) > 0.3 and abs(mom) > 0.001:
            regime = "trending"
        if atm_iv > 0.90:
            regime = "volatile"

        if dvol_pct > 0:
            logger.info(
                f"[dvol] {underlying} dvol={dvol_pct:.2f} atm_iv={atm_iv:.3f} "
                f"iv_rank={iv_rank:.0f}"
            )

        ctx = SignalContext(
            underlying=underlying,
            spot=spot,
            dvol=dvol_pct if dvol_pct > 0 else atm_iv * 100,
            iv_rank=iv_rank,
            adx=adx,
            trend_strength=trend,
            regime=regime,
            timestamp=datetime.now(timezone.utc),
            strikes=strikes,
            option_ltps=option_ltps,
            option_ivs=option_ivs,
            expiry_ddmmyy=feed.get_nearest_expiry(underlying) or "",
        )
        # Stash side-channel data strategies may consult
        ctx._momentum = mom  # type: ignore[attr-defined]
        ctx._data_quality_bad = data_quality_bad  # type: ignore[attr-defined]
        return ctx

    def _process_strategy(self, strategy, ctx, broker, feed, order_mgr, risk) -> None:
        """Run one strategy against the current context. Place trades if eligible.

        Order of gates:
        1. Strategy cooldown
        2. Strategy eligibility + build_plan
        3. Risk engine (hard rails — never overridable)
        4. Trader LLM (discretionary veto / downsize — only if self.trader is set)
        5. Execute
        """
        name = strategy.name.value
        last = self._last_plan_at.get(name, 0.0)
        if time.time() - last < self._cooldown_sec:
            return

        account_state: dict = {
            "capital": broker.starting_capital,
            "realized_pnl": broker._realized_pnl,
            "unrealized_pnl": sum(p.pnl for p in broker.get_positions()),
            "open_positions": len(order_mgr.open_trades()),
            "momentum": getattr(ctx, "_momentum", 0.0),
        }
        # Data-quality gate — if the IV rank is exactly the floor AND
        # data is bad, refuse to fire. Strategies themselves also check
        # the floor, but we short-circuit here to keep the log clean.
        if (
            getattr(ctx, "_data_quality_bad", False)
            and ctx.iv_rank <= self._min_iv_rank_to_trade
        ):
            self.signal_log.append(
                strategy=name, underlying=ctx.underlying,
                status="rejected", reason="data_quality_bad / min_iv_rank gate",
            )
            return

        # IV-regime circuit breaker (NEW 2026-09-18).
        # Pauses the bot when the market regime is too quiet for short
        # premium to be profitable. Per-currency thresholds because BTC
        # and ETH have different vol regimes. The gate is evaluated for
        # the strategy's underlying; ETH in a high-vol regime can still
        # trade while BTC in a low-vol regime cannot.
        if self._regime_gate_enabled:
            cur = (ctx.underlying or "").upper()
            min_dvol, min_iv_rank = self._regime_gate_thresholds.get(
                cur, (50.0, 40.0)
            )
            dvol = float(getattr(ctx, "dvol", 0.0) or 0.0)
            ivr = float(getattr(ctx, "iv_rank", 0.0) or 0.0)
            reasons = []
            if dvol > 0 and dvol < min_dvol:
                reasons.append(f"dvol={dvol:.0f}<{min_dvol:.0f}")
            if ivr > 0 and ivr < min_iv_rank:
                reasons.append(f"iv_rank={ivr:.0f}<{min_iv_rank:.0f}")
            if reasons:
                reason = " / ".join(reasons)
                now = time.time()
                last_log_key = (cur, reason)
                last_ts = self._regime_gate_last_log.get(last_log_key, 0.0)
                if now - last_ts >= self._regime_gate_log_cooldown_sec:
                    logger.info(
                        f"[{name}] REGIME GATE closed for {cur}: {reason}. "
                        f"Waiting for vol to recover (no trades)."
                    )
                    self._regime_gate_last_log[last_log_key] = now
                self.signal_log.append(
                    strategy=name, underlying=cur,
                    status="rejected", reason=f"regime_gate: {reason}",
                )
                return

        try:
            plan = strategy.build_plan(ctx, account_state=account_state)
        except Exception as e:
            logger.exception(f"strategy {name} build_plan error: {e}")
            return
        if plan is None:
            return

        # Resolve the nearest expiry as ISO date ONCE, before dedupe. We need
        # this both for the dedupe symbol comparison and for the actual order
        # placement downstream. NOTE: TradePlan.expiry defaults to "" and the
        # short_strangle strategy does NOT set it (see short_strangle.py), so
        # reading plan.expiry here would always give "" and the dedupe would
        # silently no-op. Compute it from the feed directly.
        ddmmyy = feed.get_nearest_expiry(ctx.underlying)
        expiry_iso = ""
        if ddmmyy:
            try:
                d_str = ddmmyy[0:2]
                m_str = ddmmyy[2:5]
                y_str = ddmmyy[5:7]
                _MONTHS = {"JAN":1,"FEB":2,"MAR":3,"APR":4,"MAY":5,"JUN":6,
                           "JUL":7,"AUG":8,"SEP":9,"OCT":10,"NOV":11,"DEC":12}
                expiry_iso = date(2000 + int(y_str), _MONTHS[m_str], int(d_str)).isoformat()
            except Exception:
                expiry_iso = ""
        plan.expiry = expiry_iso

        # DEDUPE: skip if any open trade already has the same strike+type
        # for the same underlying. Without this, the same short_strangle
        # fires every cooldown (5 min) and accumulates 3-6 identical
        # positions in a session. See issue: 2026-09-22 triple strangle.
        #
        # Both sides of the comparison use the SAME expiry source (expiry_iso
        # computed above) and the SAME canonical Deribit symbol format
        # "DDMMMYY" (e.g. "23SEP26") so the strings actually match.
        try:
            cur_open = order_mgr.open_trades()
            cur_symbols = set()
            for t in cur_open:
                for o in t.orders:
                    if getattr(o, "underlying", "") == ctx.underlying:
                        cur_symbols.add(str(o.symbol))
            new_symbols = set()
            if expiry_iso:
                try:
                    exp_canonical = (
                        date.fromisoformat(expiry_iso).strftime("%d%b%y").upper()
                    )
                except (ValueError, TypeError):
                    exp_canonical = expiry_iso.replace("-", "")
                for leg in plan.legs:
                    strike = int(leg.get("strike", 0))
                    opt = leg.get("opt_type", "?")
                    if strike:
                        new_symbols.add(
                            f"{ctx.underlying}-{exp_canonical}-{strike}-{opt}"
                        )
            dup = new_symbols & cur_symbols
            if dup:
                logger.info(
                    f"[{name}] skipped: dedupe — already holding {sorted(dup)}"
                )
                self.signal_log.append(
                    strategy=name, underlying=ctx.underlying,
                    status="rejected", reason=f"dedupe: already holding {sorted(dup)}",
                )
                return
        except Exception as e:  # noqa: BLE001
            logger.debug(f"dedupe check failed (proceeding): {e}")

        # Stash a snapshot of legs for the signal log (so we know what fired)
        legs_summary = " ".join(
            f"{leg.get('side','?')}{leg.get('opt_type','?')}{int(leg.get('strike',0))}"
            for leg in plan.legs
        )

        # risk check
        decision = risk.check_trade(plan, account_state=account_state)
        if not decision.allowed:
            logger.info(f"[{name}] blocked by risk: {decision.reason}")
            self.signal_log.append(
                strategy=name, underlying=ctx.underlying,
                status="rejected", reason=f"risk: {decision.reason}",
            )
            return
        if decision.suggested_qty <= 0:
            logger.info(f"[{name}] blocked by risk: qty=0")
            self.signal_log.append(
                strategy=name, underlying=ctx.underlying,
                status="rejected", reason="qty=0",
            )
            return

        logger.success(
            f"[{name}] {ctx.underlying} PLAN: {plan.reason} "
            f"(max_loss={plan.stop:.4f}, target={plan.target:.4f}, qty={decision.suggested_qty}, "
            f"preset={decision.preset})"
        )
        self.signal_log.append(
            strategy=name, underlying=ctx.underlying,
            status="accepted", reason=f"{plan.reason} | legs: {legs_summary}",
        )

        # -----------------------------------------------------------
        # Trader LLM gate (NEW)
        # -----------------------------------------------------------
        # The 5-strategy + risk-engine pipeline above is unchanged.
        # This adds an OPTIONAL discretionary layer: the Trader asks
        # the LLM whether to APPROVE / VETO / DOWNSIZE / HOLD this plan.
        # Hard rails (risk engine caps) are already applied above; the
        # Trader can only further restrict, never widen.
        # -----------------------------------------------------------
        target_qty = decision.suggested_qty
        if self.trader is not None:
            try:
                from .agent.trader import TradeAction as _TradeAction  # local import
                trader_decision = self.trader.decide_cycle(
                    signal_context={
                        "underlying": ctx.underlying,
                        "spot": ctx.spot,
                        "dvol": ctx.dvol,
                        "iv_rank": ctx.iv_rank,
                        "regime": ctx.regime,
                        "momentum": getattr(ctx, "_momentum", 0.0),
                        "timestamp": ctx.timestamp.isoformat() if ctx.timestamp else "",
                    },
                    candidate_plans=[{
                        "strategy": name,
                        "underlying": ctx.underlying,
                        "reason": plan.reason,
                        "target": plan.target,
                        "stop": plan.stop,
                        "legs": plan.legs,
                        "risk_qty": decision.suggested_qty,
                        "preset": decision.preset,
                    }],
                    account_state=account_state,
                    health_summary={"bot_alive": True, "ws_subscribed": True},
                )
                act = trader_decision.action
                # Map string or enum
                act_str = act.value if hasattr(act, "value") else str(act)
                rationale = trader_decision.rationale or ""
                if act_str == _TradeAction.VETO.value:
                    logger.warning(
                        f"[{name}] TRADER.VETO: {rationale}  (risk would have APPROVED qty={decision.suggested_qty})"
                    )
                    self.signal_log.append(
                        strategy=name, underlying=ctx.underlying,
                        status="vetoed",
                        reason=f"trader.llm.veto: {rationale[:200]}",
                    )
                    return  # do NOT execute
                if act_str == _TradeAction.DOWNSIZE.value:
                    new_qty = max(0, min(int(trader_decision.target_qty or 1), decision.suggested_qty))
                    if new_qty < decision.suggested_qty:
                        logger.warning(
                            f"[{name}] TRADER.DOWNSIZE: {rationale}  (qty {decision.suggested_qty} -> {new_qty})"
                        )
                        self.signal_log.append(
                            strategy=name, underlying=ctx.underlying,
                            status="downsized",
                            reason=f"trader.llm.downsize: qty={decision.suggested_qty}->{new_qty} | {rationale[:200]}",
                        )
                        target_qty = new_qty
                    else:
                        logger.info(f"[{name}] TRADER.APPROVE (downsize suggested but risk cap already at min): {rationale}")
                        self.signal_log.append(
                            strategy=name, underlying=ctx.underlying,
                            status="approved",
                            reason=f"trader.llm.approve (downsize-clamped): {rationale[:200]}",
                        )
                else:  # APPROVE or HOLD-as-approve
                    logger.info(f"[{name}] TRADER.APPROVE: {rationale}")
                    self.signal_log.append(
                        strategy=name, underlying=ctx.underlying,
                        status="approved",
                        reason=f"trader.llm.approve: {rationale[:200]}",
                    )
            except Exception as exc:  # noqa: BLE001
                logger.warning(f"trader.decide_cycle failed; falling through to risk-approved: {exc}")
                self.signal_log.append(
                    strategy=name, underlying=ctx.underlying,
                    status="approved",
                    reason=f"trader.llm.unavailable (fallback to risk-approved): {str(exc)[:160]}",
                )

        try:
            order_mgr.execute_plan(plan, qty=target_qty, expiry=expiry_iso)
            self._last_plan_at[name] = time.time()
            self._cycle_plans_produced = True
        except Exception as e:
            logger.exception(f"execute_plan failed: {e}")

    def _heartbeat(self, broker, order_mgr, feed, risk) -> None:
        if time.time() - self._last_heartbeat < 60:
            return
        self._last_heartbeat = time.time()
        margins = broker.get_margins()
        positions = broker.get_positions()
        open_trades = order_mgr.open_trades()
        realized = margins.get("realized_pnl", 0.0)
        unrealized = margins.get("unrealized_pnl", 0.0)
        ws_connected = getattr(feed, "is_connected", lambda: True)()
        feed_label = feed.__class__.__name__
        risk_status = risk.status() if risk is not None else {}
        logger.info(
            f"[heartbeat] cycle={self._cycle_count} mode={self.mode} feed={feed_label} "
            f"ws_connected={ws_connected} open_trades={len(open_trades)} "
            f"positions={len(positions)} cash=${margins.get('available', 0):,.2f} "
            f"realized=${realized:,.2f} unrealized=${unrealized:,.2f} "
            f"total_pnl=${realized+unrealized:,.2f} preset={risk_status.get('preset','?')}"
        )
        if positions:
            for p in positions[:6]:
                logger.info(
                    f"  pos: {p.symbol} qty={p.qty:+d} avg={p.avg_price:.4f} "
                    f"ltp={p.ltp:.4f} pnl={p.pnl:,.2f}"
                )

        # NEW: write a heartbeat file for the Sentinel agent.
        # The sentinel runs in a separate process and cannot reach into
        # this one's feed singleton, so we publish what it needs to disk.
        try:
            ws_subscribed = len(getattr(feed, "_subscribed_channels", set()) or set())
            payload = {
                "ts": time.time(),
                "cycle": self._cycle_count,
                "mode": self.mode,
                "feed": feed_label,
                "ws_connected": bool(ws_connected),
                "ws_subscribed": int(ws_subscribed),
                "open_trades": len(open_trades),
                "positions": len(positions),
                "realized_pnl": float(realized),
                "unrealized_pnl": float(unrealized),
                "preset": risk_status.get("preset", "?"),
                "pid": os.getpid(),
            }
            hb_path = os.path.join("data_cache", "heartbeat.json")
            tmp = hb_path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(payload, fh, default=str)
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp, hb_path)
        except Exception as e:  # noqa: BLE001
            logger.debug(f"heartbeat.json write failed (non-fatal): {e}")

    def _build_idle_context(self, feed, broker, risk):
        """Build the (signal_context, account_state, health_summary) triple
        used by ``Trader.decide_idle`` when no plans were produced this cycle.
        Returns ``None`` if feed data isn't ready.
        """
        try:
            margins = broker.get_margins()
            positions = broker.get_positions()
            realized = float(margins.get("realized_pnl", 0.0))
            unrealized = float(margins.get("unrealized_pnl", 0.0))
            spot_btc = None
            spot_eth = None
            dvol_btc = None
            dvol_eth = None
            try:
                if "BTC" in feed.currencies:
                    spot_btc = feed.get_ltp("BTC")
                    dvol_btc = feed.get_dvol("BTC")
            except Exception:
                pass
            try:
                if "ETH" in feed.currencies:
                    spot_eth = feed.get_ltp("ETH")
                    dvol_eth = feed.get_dvol("ETH")
            except Exception:
                pass
            signal_context = {
                "underlying": "ALL",
                "spot_btc": spot_btc,
                "spot_eth": spot_eth,
                "dvol_btc": dvol_btc,
                "dvol_eth": dvol_eth,
                "regime": "low_vol" if (dvol_btc is not None and dvol_btc < 50) else "normal",
            }
            account_state = {
                "cash": float(margins.get("available", 0.0)),
                "total": float(margins.get("total", 0.0)),
                "positions": len(positions),
                "realized_pnl": realized,
                "unrealized_pnl": unrealized,
            }
            risk_status = risk.status() if risk is not None else {}
            health_summary = {
                "bot_alive": True,
                "preset": risk_status.get("preset", "?"),
                "ws_connected": bool(getattr(feed, "_connected", False)),
                "open_trades": len(getattr(broker, "_managed_trades", {}) or {}),
            }
            return {
                "signal_context": signal_context,
                "account_state": account_state,
                "health_summary": health_summary,
            }
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"_build_idle_context failed: {type(exc).__name__}: {exc}")
            return None

    def _monitor_targets_stops(self, broker, order_mgr) -> None:
        """Auto-close open trades whose combined P&L hits target or stop,
        OR whose expiry has already passed."""
        positions = broker.get_positions()
        pos_pnl = {p.symbol: float(p.pnl) for p in positions}
        today = date.today()
        for trade in list(order_mgr.open_trades()):
            plan = trade.plan
            if not plan:
                continue

            # Expiry auto-close: if the trade's expiry has passed, close
            # it with reason="expired". Without this, expired Sep 18 trades
            # stay "open" in journal forever, inflating the position cap.
            expiry_iso = getattr(plan, "expiry", None) or ""
            if expiry_iso:
                try:
                    exp_date = date.fromisoformat(expiry_iso)
                    if exp_date < today:
                        logger.info(
                            f"[monitor] trade {trade.trade_id} expired on "
                            f"{expiry_iso}, auto-closing"
                        )
                        try:
                            order_mgr.close_trade(
                                trade.trade_id, reason="expired"
                            )
                        except Exception as e:  # noqa: BLE001
                            logger.exception(
                                f"close_trade(expired={trade.trade_id}) failed: {e}"
                            )
                        continue
                except (ValueError, TypeError):
                    pass

            leg_pnl = 0.0
            for order in trade.orders:
                if order.symbol in pos_pnl:
                    leg_pnl += pos_pnl[order.symbol]
            total_pnl = leg_pnl + float(trade.realized_pnl or 0.0)
            target = float(plan.target or 0.0)
            stop = abs(float(plan.stop or 0.0))
            if target > 0 and total_pnl >= target:
                logger.info(
                    f"[monitor] trade {trade.trade_id} target_hit pnl={total_pnl:.2f} "
                    f"(target={target:.2f})"
                )
                try:
                    order_mgr.close_trade(trade.trade_id, reason="target_hit")
                except Exception as e:
                    logger.exception(f"close_trade({trade.trade_id}) failed: {e}")
                continue
            if stop > 0 and total_pnl <= -stop:
                logger.info(
                    f"[monitor] trade {trade.trade_id} stop_hit pnl={total_pnl:.2f} "
                    f"(stop={stop:.2f})"
                )
                try:
                    order_mgr.close_trade(trade.trade_id, reason="stop_hit")
                except Exception as e:
                    logger.exception(f"close_trade({trade.trade_id}) failed: {e}")
                continue

    def _build_status_payload(self, broker, order_mgr, feed, risk) -> dict:
        """Build the JSON payload for /api/status."""
        margins = broker.get_margins()
        positions = broker.get_positions()
        open_trades = order_mgr.open_trades()
        dvol = 0.0
        try:
            # Pull DVOL from the first currency (best-effort)
            if feed.currencies:
                dvol = float(feed.get_dvol(feed.currencies[0])) * 100.0
        except Exception:
            dvol = 0.0
        return {
            "mode": self.mode,
            "feed": feed.__class__.__name__,
            "feed_health": {
                "ws_connected": bool(getattr(feed, "is_connected", lambda: True)()),
                "dvol": round(dvol, 2),
            },
            "account": {
                "cash": float(margins.get("available", 0) or 0),
                "total": float(margins.get("total", 0) or 0),
                "realized_pnl": float(margins.get("realized_pnl", 0) or 0),
                "unrealized_pnl": float(margins.get("unrealized_pnl", 0) or 0),
            },
            "risk": (risk.status() if risk is not None else {}),
            "trades": [
                {
                    "trade_id": t.trade_id,
                    "strategy": t.plan.strategy.value if t.plan else "",
                    "underlying": t.plan.underlying if t.plan else "",
                    "leg_count": len(t.orders),
                    "is_multi_leg": len(t.orders) > 1,
                    "target": float(t.plan.target) if t.plan else 0.0,
                    "stop": float(t.plan.stop) if t.plan else 0.0,
                    "pnl": float(t.realized_pnl or 0.0),
                    "opened_at": t.opened_at.isoformat(timespec="seconds") if t.opened_at else None,
                }
                for t in open_trades
            ],
            "positions": [
                {
                    "symbol": p.symbol,
                    "qty": int(p.qty),
                    "avg_price": float(p.avg_price),
                    "ltp": float(p.ltp),
                    "pnl": float(p.pnl),
                    "underlying": p.underlying or "",
                    "option_type": p.option_type or "",
                }
                for p in positions
            ],
            "signals": self.signal_log.last_24h(),
        }

    def _build_ticks_payload(self, n: int) -> dict:
        """Build the JSON payload for /api/ticks."""
        try:
            feed = self._feed
        except AttributeError:
            return {"ticks": []}
        try:
            latest = feed.get_latest if hasattr(feed, "get_latest") else None
        except Exception:
            latest = None
        if not latest:
            return {"ticks": []}
        # We don't have a get_recent_n API; just dump get_latest for each subscribed
        # instrument is too heavy. The feed stores the last 1000 ticks in
        # _latest (a dict keyed by symbol). Return that.
        out: list = []
        try:
            latest_dict = getattr(feed, "_latest", {}) or {}
        except Exception:
            latest_dict = {}
        for sym, t in list(latest_dict.items())[-n:]:
            out.append({
                "symbol": sym,
                "ltp": float(t.get("ltp", 0) or 0),
                "bid": float(t.get("bid", 0) or 0),
                "ask": float(t.get("ask", 0) or 0),
                "iv": float(t.get("iv", 0) or 0),
                "timestamp": datetime.fromtimestamp(t.get("ts", time.time())).isoformat(timespec="seconds"),
            })
        return {"ticks": out}

    def run(self) -> int:
        broker, feed, order_mgr, risk = self._connect_tick_pipeline()
        # Stash refs for callbacks
        self._broker = broker
        self._feed = feed
        self._risk_ref = risk

        # NEW: instantiate the Trader LLM gate if one wasn't passed in.
        # The Trader gates every plan with a discretionary APPROVE /
        # VETO / DOWNSIZE / HOLD decision from the configured LLM.
        # `trader=False` from the CLI means explicitly disabled; leave
        # as-is (rule-based path only).
        if self.trader is None:
            try:
                from .agent.llm import LLMClient
                from .agent.memory import Memory
                from .agent.trader import Trader
                from pathlib import Path
                settings_path = Path("config/settings.yaml")
                llm = LLMClient(settings_path=settings_path)
                mem = Memory(root=Path("memory"))
                self.trader = Trader(
                    project_root=Path(".").resolve(),
                    memory=mem,
                    llm=llm,
                    fallback_enabled=True,
                )
                logger.info(
                    "Trader LLM gate ENABLED "
                    f"(model={self.trader.model}, providers={len(llm.provider_status())})"
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning(f"Trader LLM gate could not start ({exc}); rule-based path only")
                self.trader = None
        elif self.trader is False:
            logger.info("Trader LLM gate DISABLED via --no-trader")
            self.trader = None

        strategies = self._build_strategies()
        feed_url = getattr(feed, "ws_url", None) or getattr(feed, "base_url", "?")
        logger.info(
            f"{self.mode} session started | feed={feed_url} "
            f"currencies={feed.currencies} strategies={[s.name.value for s in strategies]}"
        )
        if not strategies:
            logger.warning("no strategies configured - running as a data feed only")

        # If a dashboard is attached, wire its data callbacks now that we have refs
        if self.dashboard is not None:
            try:
                self.dashboard.set_status(
                    lambda: self._build_status_payload(broker, order_mgr, feed, risk)
                )
                self.dashboard.set_ticks(self._build_ticks_payload)
            except Exception as e:
                logger.debug(f"dashboard hookup: {e}")

        self._first_chain_tick_logged = False

        def _shutdown(signum, frame):
            logger.info(f"received signal {signum}, shutting down...")
            self._stop.set()
            self._signaled_exit = True  # exit non-zero so NSSM auto-restarts us

        try:
            signal.signal(signal.SIGINT, _shutdown)
            signal.signal(signal.SIGTERM, _shutdown)
        except (ValueError, OSError):
            pass

        last_pnl_log_cycle = 0
        try:
            while not self._stop.is_set():
                self._cycle_count += 1
                # Hot-reload settings.yaml when the file changes. Lets us
                # tune regime gate floors, cooldowns, etc. without restart.
                self._maybe_reload_config()
                self._cycle_plans_produced = False
                positions = broker.get_positions()
                risk.update_open_positions(len(order_mgr.open_trades()))
                risk.update_daily_pnl(broker._realized_pnl + sum(p.pnl for p in positions))
                for underlying in feed.currencies:
                    ctx = self._build_signal_context(underlying, feed, broker)
                    if ctx is None:
                        continue
                    risk.update_market_state(dvol=ctx.dvol, iv_rank=ctx.iv_rank)
                    for strat in strategies:
                        self._process_strategy(strat, ctx, broker, feed, order_mgr, risk)
                self._monitor_targets_stops(broker, order_mgr)
                self._heartbeat(broker, order_mgr, feed, risk)

                # Throttled idle-mode LLM check: when nothing was executed this
                # cycle (e.g. regime gate closed everything), still ask the
                # Trader LLM whether it agrees with staying flat. Generates a
                # real decision + journal entry so the agent layer is observably
                # alive even when no trades occur.
                if (
                    not self._cycle_plans_produced
                    and self.trader is not None
                    and self.trader.idle_check_interval_sec > 0
                    and (
                        time.time() - self.trader.last_idle_check_at
                        >= self.trader.idle_check_interval_sec
                    )
                ):
                    try:
                        idle_ctx = self._build_idle_context(feed, broker, risk)
                        if idle_ctx is None:
                            logger.warning(
                                "trader.idle_check: _build_idle_context returned None "
                                "(feed may be uninitialised)"
                            )
                            continue
                        idle_decision = self.trader.decide_idle(
                            signal_context=idle_ctx["signal_context"],
                            account_state=idle_ctx["account_state"],
                            health_summary=idle_ctx["health_summary"],
                        )
                        if idle_decision is not None:
                            logger.info(
                                "trader.llm.idle: action=" + idle_decision.action.value
                                + "  qty=" + str(idle_decision.target_qty)
                                + "  rationale=" + (idle_decision.rationale or "")[:200].replace("\n", " ")
                            )
                            self.signal_log.append(
                                strategy="idle_check",
                                underlying="ALL",
                                status=idle_decision.action.value,
                                reason=("trader.llm.idle: " + (idle_decision.rationale or "")[:200]),
                            )
                        else:
                            logger.warning(
                                "trader.idle_check: decide_idle returned None "
                                "(LLM response unparseable or throttle blocked)"
                            )
                    except Exception as exc:  # noqa: BLE001
                        logger.warning(f"trader.idle_check failed: {type(exc).__name__}: {exc}")

                # Per-cycle P&L log (every cycle; not gated by heartbeat)
                if self._cycle_count - last_pnl_log_cycle >= 1:
                    last_pnl_log_cycle = self._cycle_count
                    try:
                        margins = broker.get_margins()
                        self.pnl_writer.log_cycle(
                            cycle=self._cycle_count,
                            equity=float(margins.get("total", 0) or 0)
                                    + float(margins.get("unrealized_pnl", 0) or 0),
                            realized=float(margins.get("realized_pnl", 0) or 0),
                            unrealized=float(margins.get("unrealized_pnl", 0) or 0),
                            open_positions=len(order_mgr.open_trades()),
                            cash=float(margins.get("available", 0) or 0),
                            dvol=float(getattr(risk.state, "dvol", 0.0) or 0.0),
                            iv_rank=float(getattr(risk.state, "iv_rank", 0.0) or 0.0),
                            preset=str(risk.status().get("preset", "?")),
                        )
                    except Exception as e:
                        logger.debug(f"pnl cycle log: {e}")

                self._stop.wait(timeout=5.0)
        finally:
            logger.info("stopping feed and saving state...")
            feed.stop()
            broker.disconnect()
            self._save_state_summary(broker, order_mgr)
        # Exit non-zero if we were signaled (so NSSM treats it as a crash and
        # auto-restarts). Exit 0 only on clean shutdown via keyboard.
        return 1 if getattr(self, "_signaled_exit", False) else 0

    @staticmethod
    def _save_state_summary(broker, order_mgr) -> None:
        margins = broker.get_margins()
        positions = broker.get_positions()
        open_trades = order_mgr.open_trades()
        logger.info("=" * 60)
        logger.info("FINAL STATE")
        logger.info(
            f"  open trades: {len(open_trades)} | positions: {len(positions)} | "
            f"cash: ${margins.get('available', 0):,.2f}"
        )
        logger.info(
            f"  realized P&L: ${margins.get('realized_pnl', 0):,.2f} | "
            f"unrealized P&L: ${margins.get('unrealized_pnl', 0):,.2f}"
        )
        logger.info("=" * 60)


# ---------------------------------------------------------------------------
# Subcommands
# ---------------------------------------------------------------------------
def cmd_paper(args) -> int:
    cfg = load_config(args.config)
    setup_logger(
        level=cfg.get("logging", {}).get("level", "INFO"),
        log_file=cfg.get("logging", {}).get("file", "logs/bot.log"),
    )
    if os.environ.get("DERIBIT_ENV"):
        cfg.setdefault("data", {})["deribit_env"] = os.environ["DERIBIT_ENV"]
    if os.environ.get("DERIBIT_CURRENCIES"):
        cfg.setdefault("data", {})["currencies"] = [
            c.strip() for c in os.environ["DERIBIT_CURRENCIES"].split(",") if c.strip()
        ]
    if os.environ.get("DERIBIT_POLL_SEC"):
        cfg.setdefault("data", {})["poll_interval_sec"] = float(os.environ["DERIBIT_POLL_SEC"])
    feed_mode = args.feed or cfg.get("data", {}).get("feed_mode", "ws")
    feed_mode = str(feed_mode).lower().strip()
    if feed_mode not in ("ws", "rest"):
        logger.warning(f"unknown --feed value {feed_mode!r}, defaulting to 'ws'")
        feed_mode = "ws"
    verbose = bool(args.verbose)

    # Optional Telegram alerter
    alerter = None
    if cfg.get("alerts", {}).get("telegram", {}).get("enabled", False) or \
       (os.environ.get("TELEGRAM_BOT_TOKEN") and os.environ.get("TELEGRAM_CHAT_ID")):
        try:
            from .alerts.telegram import TelegramAlerter
            alerter = TelegramAlerter()
        except Exception as e:
            logger.warning(f"telegram alerter init failed: {e}")

    # Optional dashboard
    dashboard = None
    dashboard_port = int(args.dashboard_port) if args.dashboard_port is not None else \
        int(cfg.get("dashboard", {}).get("port", 8511) or 0)
    if dashboard_port and dashboard_port > 0:
        try:
            from .dashboard.server import start_dashboard
            dashboard = start_dashboard(port=dashboard_port)
        except Exception as e:
            logger.warning(f"dashboard init failed: {e}")

    pnl_writer = _PnLWriter()
    signal_log = _SignalLog()
    runner = PaperRunner(
        cfg, feed_mode=feed_mode, verbose=verbose,
        mode="paper", alerter=alerter, dashboard=dashboard,
        pnl_writer=pnl_writer, signal_log=signal_log,
        trader=False if getattr(args, "no_trader", False) else None,
        recover_orphan=bool(getattr(args, "recover_orphan", False)),
    )
    if args.max_runtime:
        def _stop_after():
            time.sleep(args.max_runtime)
            logger.info(f"--max-runtime {args.max_runtime}s reached, stopping...")
            runner._stop.set()
        threading.Thread(target=_stop_after, daemon=True).start()
    try:
        return runner.run()
    finally:
        if alerter is not None:
            try:
                alerter.stop()
            except Exception:
                pass
        if dashboard is not None:
            try:
                dashboard.stop()
            except Exception:
                pass


def cmd_live(args) -> int:
    """Live trading — same as paper but with DeribitClient + safety guard."""
    if os.environ.get("DERIBIT_LIVE_CONFIRMED", "").strip().upper() != "YES":
        logger.error(
            "Live mode refused: DERIBIT_LIVE_CONFIRMED=YES is required in the env. "
            "This is the safety guard against accidental live orders."
        )
        return 1
    if not os.environ.get("DERIBIT_CLIENT_ID") or not os.environ.get("DERIBIT_CLIENT_SECRET"):
        logger.error(
            "Live mode refused: DERIBIT_CLIENT_ID and DERIBIT_CLIENT_SECRET are required."
        )
        return 1
    cfg = load_config(args.config)
    cfg["mode"] = "live"
    setup_logger(
        level=cfg.get("logging", {}).get("level", "INFO"),
        log_file=cfg.get("logging", {}).get("file", "logs/bot.log"),
    )
    feed_mode = args.feed or cfg.get("data", {}).get("feed_mode", "ws")
    feed_mode = str(feed_mode).lower().strip()
    if feed_mode not in ("ws", "rest"):
        feed_mode = "ws"

    alerter = None
    if os.environ.get("TELEGRAM_BOT_TOKEN") and os.environ.get("TELEGRAM_CHAT_ID"):
        try:
            from .alerts.telegram import TelegramAlerter
            alerter = TelegramAlerter()
        except Exception as e:
            logger.warning(f"telegram alerter init failed: {e}")

    dashboard = None
    dashboard_port = int(args.dashboard_port) if args.dashboard_port is not None else \
        int(cfg.get("dashboard", {}).get("port", 8511) or 0)
    if dashboard_port and dashboard_port > 0:
        try:
            from .dashboard.server import start_dashboard
            dashboard = start_dashboard(port=dashboard_port)
        except Exception as e:
            logger.warning(f"dashboard init failed: {e}")

    pnl_writer = _PnLWriter()
    signal_log = _SignalLog()
    runner = PaperRunner(
        cfg, feed_mode=feed_mode, verbose=bool(args.verbose),
        mode="live", alerter=alerter, dashboard=dashboard,
        pnl_writer=pnl_writer, signal_log=signal_log,
        trader=False if getattr(args, "no_trader", False) else None,
        recover_orphan=bool(getattr(args, "recover_orphan", False)),
    )
    if args.max_runtime:
        def _stop_after():
            time.sleep(args.max_runtime)
            logger.info(f"--max-runtime {args.max_runtime}s reached, stopping...")
            runner._stop.set()
        threading.Thread(target=_stop_after, daemon=True).start()
    try:
        return runner.run()
    finally:
        if alerter is not None:
            try:
                alerter.stop()
            except Exception:
                pass
        if dashboard is not None:
            try:
                dashboard.stop()
            except Exception:
                pass


def cmd_status(args) -> int:
    cfg = load_config(args.config)
    setup_logger(level="WARNING", log_file="")
    broker = PaperClient(
        starting_capital=float(cfg.get("broker", {}).get("paper_capital", 100_000.0)),
        persist_path=cfg.get("broker", {}).get("persist_path", "data_cache/paper_state.json"),
    )
    broker.connect()
    margins = broker.get_margins()
    positions = broker.get_positions()
    print("=" * 60)
    print("PAPER STATE")
    print(f"  capital:      ${margins.get('total', 0):,.2f}")
    print(f"  available:    ${margins.get('available', 0):,.2f}")
    print(f"  used margin:  ${margins.get('used', 0):,.2f}")
    print(f"  realized P&L: ${margins.get('realized_pnl', 0):,.2f}")
    print(f"  unrealized:   ${margins.get('unrealized_pnl', 0):,.2f}")
    print(f"  positions:    {len(positions)}")
    for p in positions:
        print(
            f"    {p.symbol:32s} qty={p.qty:+4d} avg={p.avg_price:>10.4f} "
            f"ltp={p.ltp:>10.4f} pnl=${p.pnl:>10,.2f}"
        )
    print("=" * 60)
    broker.disconnect()
    return 0


def cmd_reset(args) -> int:
    setup_logger(level="WARNING", log_file="")
    cfg = load_config(args.config)
    broker = PaperClient(
        starting_capital=float(cfg.get("broker", {}).get("paper_capital", 100_000.0)),
        persist_path=cfg.get("broker", {}).get("persist_path", "data_cache/paper_state.json"),
    )
    broker.connect()
    broker.reset()
    print("Paper state cleared.")
    return 0


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------
def main(argv: list[str] | None = None) -> int:
    load_dotenv()
    parser = argparse.ArgumentParser(
        prog="crypto_options_bot",
        description="Crypto options trading bot (Deribit). Paper by default; "
                    "use 'live' subcommand with DERIBIT_LIVE_CONFIRMED=YES for real orders.",
    )
    parser.add_argument(
        "--config", "-c", default="config/settings.yaml",
        help="Path to YAML config (default: config/settings.yaml)",
    )
    sub = parser.add_subparsers(dest="cmd", required=False)

    p_paper = sub.add_parser("paper", help="Run a paper trading session")
    p_paper.add_argument("--max-runtime", type=float, default=0.0)
    p_paper.add_argument("--feed", choices=["ws", "rest"], default=None)
    p_paper.add_argument("--verbose", action="store_true")
    p_paper.add_argument(
        "--dashboard-port", type=int, default=None,
        help="Start the dashboard on this port (default from config or 8511). 0 disables.",
    )
    p_paper.add_argument(
        "--no-trader", action="store_true",
        help="Disable the Trader LLM gate; rule-based path only.",
    )
    p_paper.add_argument(
        "--recover-orphan", action="store_true",
        help="On startup, if journal has open trades but broker has 0 positions, "
             "rebuild broker positions from the journal automatically.",
    )
    p_paper.set_defaults(func=cmd_paper)

    p_live = sub.add_parser("live", help="Run a LIVE trading session (safety guard required)")
    p_live.add_argument("--max-runtime", type=float, default=0.0)
    p_live.add_argument("--feed", choices=["ws", "rest"], default=None)
    p_live.add_argument("--verbose", action="store_true")
    p_live.add_argument(
        "--no-trader", action="store_true",
        help="Disable the Trader LLM gate; rule-based path only.",
    )
    p_live.add_argument("--dashboard-port", type=int, default=None)
    p_live.add_argument(
        "--recover-orphan", action="store_true",
        help="On startup, if journal has open trades but broker has 0 positions, "
             "rebuild broker positions from the journal automatically.",
    )
    p_live.set_defaults(func=cmd_live)

    p_status = sub.add_parser("status", help="Print current paper state")
    p_status.set_defaults(func=cmd_status)

    p_reset = sub.add_parser("reset", help="Clear paper state")
    p_reset.set_defaults(func=cmd_reset)

    p_operator = sub.add_parser(
        "operator",
        help="Run the 6-agent self-evolving / self-healing operator loop",
    )
    p_operator.add_argument("--memory-dir", default="memory")
    p_operator.add_argument("--sentinel-interval", type=float, default=60.0)
    p_operator.add_argument("--evolver-interval", type=float, default=6 * 3600.0)
    p_operator.add_argument("--reflector-at", default="00:05",
                            help="Daily HH:MM (UTC) to run Reflector")
    p_operator.add_argument("--heartbeat-interval", type=float, default=30.0,
                            help="Operator's own liveness ping interval (s)")
    p_operator.add_argument("--llm-model", default="minimax/MiniMax-M3")
    p_operator.add_argument("--no-trader", action="store_true",
                            help="Disable Trader (rule-based path only)")
    p_operator.add_argument("--no-evolver", action="store_true")
    p_operator.add_argument("--no-reflector", action="store_true")
    p_operator.add_argument("--max-runtime", type=float, default=0.0,
                            help="Stop operator after N seconds (0=forever)")
    p_operator.set_defaults(func=cmd_operator)

    args = parser.parse_args(argv)
    if not getattr(args, "cmd", None):
        parser.print_help()
        return 1
    return args.func(args)


# ---------------------------------------------------------------------------
# operator subcommand
# ---------------------------------------------------------------------------
def cmd_operator(args: argparse.Namespace) -> int:
    """Entry point for ``python -m crypto_options_bot operator``."""
    from .agent.operator import Operator, OperatorConfig

    project_root = Path(args.config).resolve().parent.parent
    cfg = OperatorConfig(
        project_root=project_root,
        memory_dir=project_root / args.memory_dir,
        sentinel_interval_sec=args.sentinel_interval,
        evolver_interval_sec=args.evolver_interval,
        reflector_at_hhmm=args.reflector_at,
        heartbeat_interval_sec=args.heartbeat_interval,
        llm_model=args.llm_model,
        enable_trader=not args.no_trader,
        enable_evolver=not args.no_evolver,
        enable_reflector=not args.no_reflector,
    )
    op = Operator(config=cfg)

    if args.max_runtime and args.max_runtime > 0:
        import threading
        stop = threading.Event()
        timer = threading.Timer(args.max_runtime, stop.set)
        timer.daemon = True
        timer.start()
        try:
            op.run_until(stop)
        finally:
            timer.cancel()
    else:
        op.run_forever()
    return 0


if __name__ == "__main__":
    sys.exit(main())

