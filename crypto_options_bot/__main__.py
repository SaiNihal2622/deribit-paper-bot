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
    ):
        self.cfg = cfg
        self.feed_mode = feed_mode  # "ws" or "rest"
        self.verbose = verbose      # per-tick INFO log streaming
        self.mode = mode            # "paper" or "live"
        self.alerter = alerter      # optional TelegramAlerter
        self.dashboard = dashboard  # optional DashboardServer
        self.pnl_writer = pnl_writer or _PnLWriter()
        self.signal_log = signal_log or _SignalLog()
        self._stop = threading.Event()
        self._last_heartbeat = 0.0
        self._cycle_count = 0
        self._last_plan_at: dict[str, float] = {}  # strategy_name -> ts of last fire
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

        # wire trade events: alerter + pnl telemetry
        order_mgr.set_event_callback(self._on_trade_event)
        return broker, feed, order_mgr, risk

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
                max_strikes_per_underlying=int(ws_cfg.get("max_strikes_per_underlying", 9)),
                reconnect_delay_sec=float(ws_cfg.get("reconnect_delay_sec", 2.0)),
                max_reconnect_attempts=int(ws_cfg.get("max_reconnect_attempts", 10)),
                dvol_cache_sec=float(ws_cfg.get("dvol_cache_sec", 300.0)),
            )
            ws_feed.start()
            # Give it up to 3 seconds to connect.
            connected = False
            for _ in range(30):
                if ws_feed.is_connected():
                    connected = True
                    break
                time.sleep(0.1)
            if not connected:
                logger.warning("WS feed failed to connect within 3s, falling back to REST")
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
        for s in strikes:
            ce = oi_map[s].get("ce_ltp", 0.0)
            pe = oi_map[s].get("pe_ltp", 0.0)
            ce_iv = oi_map[s].get("ce_iv", 0.0)
            pe_iv = oi_map[s].get("pe_iv", 0.0)
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
        )
        # Stash side-channel data strategies may consult
        ctx._momentum = mom  # type: ignore[attr-defined]
        ctx._data_quality_bad = data_quality_bad  # type: ignore[attr-defined]
        return ctx

    def _process_strategy(self, strategy, ctx, broker, feed, order_mgr, risk) -> None:
        """Run one strategy against the current context. Place trades if eligible."""
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

        try:
            plan = strategy.build_plan(ctx, account_state=account_state)
        except Exception as e:
            logger.exception(f"strategy {name} build_plan error: {e}")
            return
        if plan is None:
            return

        # Stash a snapshot of legs for the signal log (so we know what fired)
        legs_summary = " ".join(
            f"{leg.get('side','?')}{leg.get('opt_type','?')}{int(leg.get('strike',0))}"
            for leg in plan.legs
        )

        # nearest expiry as ISO date
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
        try:
            order_mgr.execute_plan(plan, qty=decision.suggested_qty, expiry=expiry_iso)
            self._last_plan_at[name] = time.time()
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

    def _monitor_targets_stops(self, broker, order_mgr) -> None:
        """Auto-close open trades whose combined P&L hits target or stop."""
        positions = broker.get_positions()
        pos_pnl = {p.symbol: float(p.pnl) for p in positions}
        for trade in list(order_mgr.open_trades()):
            plan = trade.plan
            if not plan:
                continue
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

        try:
            signal.signal(signal.SIGINT, _shutdown)
            signal.signal(signal.SIGTERM, _shutdown)
        except (ValueError, OSError):
            pass

        last_pnl_log_cycle = 0
        try:
            while not self._stop.is_set():
                self._cycle_count += 1
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
        return 0

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
    p_paper.set_defaults(func=cmd_paper)

    p_live = sub.add_parser("live", help="Run a LIVE trading session (safety guard required)")
    p_live.add_argument("--max-runtime", type=float, default=0.0)
    p_live.add_argument("--feed", choices=["ws", "rest"], default=None)
    p_live.add_argument("--verbose", action="store_true")
    p_live.add_argument("--dashboard-port", type=int, default=None)
    p_live.set_defaults(func=cmd_live)

    p_status = sub.add_parser("status", help="Print current paper state")
    p_status.set_defaults(func=cmd_status)

    p_reset = sub.add_parser("reset", help="Clear paper state")
    p_reset.set_defaults(func=cmd_reset)

    args = parser.parse_args(argv)
    if not getattr(args, "cmd", None):
        parser.print_help()
        return 1
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())

