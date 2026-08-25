"""Deribit WebSocket feed — sub-second real-time BTC/ETH option chain.

This is the primary (low-latency) counterpart of `deribit_feed.DeribitFeed`
which polls REST every 2s. Both classes expose the same public surface so
`__main__.py` can swap between them based on config or CLI flag.

Connection details:
  - Deribit testnet WSS: wss://test.deribit.com/ws/api/v2
  - Deribit prod WSS:    wss://www.deribit.com/ws/api/v2
  - No auth required for public subscriptions.

Channel format (JSON-RPC 2.0 over WebSocket):
  - Spot index:    deribit_price_index.btc_usd    (1s, {timestamp, price})
  - Ticker:        ticker.{INSTRUMENT}.100ms      (100ms, mark_iv, bid, ask, oi, greeks, ...)
  - Book:          book.{INSTRUMENT}.none.10.100ms (heavy — use for active positions only)

Subscribe message:
  {"jsonrpc": "2.0", "method": "public/subscribe",
   "params": {"channels": ["deribit_price_index.btc_usd",
                           "ticker.BTC-26AUG26-82000-C.100ms"]}, "id": 1}

Subscription updates (continuous):
  {"jsonrpc": "2.0", "method": "subscription",
   "params": {"channel": "deribit_price_index.btc_usd",
              "data": {"timestamp": 1234567890, "price": 78800.5}}}

Public surface (mirrors DeribitFeed):
    feed = DeribitWebSocketFeed(env="testnet", currencies=["BTC"], max_strikes_per_underlying=3)
    feed.start()
    feed.subscribe(["BTC-26AUG26-82000-C"])
    feed.on_tick(callback)
    feed.get_ltp("BTC")
    feed.get_oi_map("BTC")
    feed.get_nearest_expiry("BTC")
    feed.get_atm_strike("BTC")
    feed.is_connected()
    feed.get_dvol("BTC")
    feed.stop()
"""
from __future__ import annotations

import json
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from typing import Callable, Optional

import websocket  # websocket-client
from loguru import logger

from .deribit_feed import (
    PROD_BASE,
    TESTNET_BASE,
    _http_get_json,
    format_deribit_instrument,
    parse_deribit_instrument,
)


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
TESTNET_WS_URL = "wss://test.deribit.com/ws/api/v2"
PROD_WS_URL = "wss://www.deribit.com/ws/api/v2"

# Same instrument regex used by the REST feed (kept here to avoid an import cycle
# in case the upstream helpers move). Matches: BTC-26AUG26-82000-C.
_DERIBIT_INST_RE = re.compile(
    r"^(BTC|ETH)-(\d{2}[A-Z]{3}\d{2})-(\d+)-([CP])$"
)
_MONTHS = {"JAN": 1, "FEB": 2, "MAR": 3, "APR": 4, "MAY": 5, "JUN": 6,
           "JUL": 7, "AUG": 8, "SEP": 9, "OCT": 10, "NOV": 11, "DEC": 12}

DEFAULT_TIMEOUT = 15


# ---------------------------------------------------------------------------
# Main class
# ---------------------------------------------------------------------------
class DeribitWebSocketFeed:
    """Subscribes to Deribit WSS for sub-second real-time option chain ticks.

    Threading model: a single daemon thread runs the recv loop. The public
    methods are safe to call from any thread (an RLock guards shared state).
    On disconnect the thread auto-reconnects up to `max_reconnect_attempts`
    times with `reconnect_delay_sec` between attempts. After exhaustion the
    thread exits; callers can detect this via `is_connected()` and fall back
    to the REST feed.
    """

    # Class-level URL constants (mirror the DeribitFeed TESTNET_BASE/PROD_BASE).
    TESTNET_WS_URL = TESTNET_WS_URL
    PROD_WS_URL = PROD_WS_URL

    def __init__(
        self,
        env: str = "testnet",
        currencies: Optional[list[str]] = None,
        strike_window_pct: float = 0.20,
        max_strikes_per_underlying: int = 9,
        reconnect_delay_sec: float = 2.0,
        max_reconnect_attempts: int = 10,
        heartbeat_sec: float = 60.0,
        dvol_cache_sec: float = 300.0,
        timeout: int = DEFAULT_TIMEOUT,
    ):
        """Initialize the WS feed.

        Args:
            env: "testnet" or "prod" — selects WSS URL and DVOL REST host.
            currencies: list of underlying currencies, e.g. ["BTC", "ETH"].
            strike_window_pct: subscription filter for strikes around ATM
                (currently informational; channels are subscribed for an
                ATM +/- max_strikes_per_underlying//2 strike window).
            max_strikes_per_underlying: how many strike ticks to subscribe
                per currency. 9 means ATM +/- 4 strikes.
            reconnect_delay_sec: sleep between reconnect attempts.
            max_reconnect_attempts: number of reconnect tries before giving up.
            heartbeat_sec: heartbeat log interval.
            dvol_cache_sec: how long to cache the DVOL index between REST polls.
            timeout: WSS connect timeout in seconds.
        """
        if env == "testnet":
            self.ws_url = TESTNET_WS_URL
            self.rest_base = TESTNET_BASE
        elif env == "prod":
            self.ws_url = PROD_WS_URL
            self.rest_base = PROD_BASE
        else:
            raise ValueError(f"env must be 'testnet' or 'prod', got: {env}")

        self.env = env
        self.currencies = [c.upper() for c in (currencies or ["BTC", "ETH"])]
        self.strike_window_pct = float(strike_window_pct)
        self.max_strikes_per_underlying = int(max(1, max_strikes_per_underlying))
        self.reconnect_delay_sec = max(0.1, float(reconnect_delay_sec))
        self.max_reconnect_attempts = int(max(0, max_reconnect_attempts))
        self.heartbeat_sec = max(5.0, float(heartbeat_sec))
        self.dvol_cache_sec = max(5.0, float(dvol_cache_sec))
        self.timeout = int(timeout)

        # Shared state (guarded by _lock)
        self._latest: dict[str, dict] = {}        # symbol -> tick dict
        self._price_history: dict[str, list[float]] = {}
        self._spot: dict[str, float] = {}         # "BTC"/"ETH" -> last spot
        self._instruments_cache: dict[str, tuple[float, list]] = {}
        self._dvol_cache: dict[str, tuple[float, float]] = {}  # currency -> (ts, dvol)

        # Subscription tracking
        self._subscribed: set[str] = set()               # user-requested (uppercase)
        self._keep_alive: set[str] = set()               # pinned (open-position legs)
        self._subscribed_channels: set[str] = set()      # actually subscribed WS channels

        self._callbacks: list[Callable[[dict], None]] = []

        # Threading
        self._lock = threading.RLock()
        self._running = False
        self._thread: Optional[threading.Thread] = None
        self._ws: Optional[websocket.WebSocket] = None
        self._connected = False
        self._last_heartbeat = 0.0
        self._tick_count = 0
        self._reconnect_attempts = 0

    # -----------------------------------------------------------------------
    # Public API — mirrors DeribitFeed
    # -----------------------------------------------------------------------
    def start(self) -> None:
        """Start the WS background thread. Idempotent."""
        with self._lock:
            if self._running:
                return
            self._running = True
            self._reconnect_attempts = 0
            self._thread = threading.Thread(
                target=self._run_loop, name="deribit-ws", daemon=True
            )
            self._thread.start()
            logger.info(
                f"DeribitWebSocketFeed started (env={self.env}, url={self.ws_url}, "
                f"currencies={self.currencies}, max_strikes={self.max_strikes_per_underlying})"
            )

    def stop(self) -> None:
        """Stop the WS thread. Closes the socket and joins within 3s."""
        with self._lock:
            self._running = False
        self._close_ws()
        if self._thread:
            self._thread.join(timeout=3)
            logger.info("DeribitWebSocketFeed stopped")

    def subscribe(self, symbols: list[str]) -> None:
        """Add symbols to the subscription set (idempotent).

        Accepts:
          - bare currency codes ("BTC", "ETH") — adds the spot channel.
          - Deribit instrument names ("BTC-26AUG26-82000-C") — adds a ticker
            channel for that instrument.
        """
        new_channels: list[str] = []
        with self._lock:
            for s in symbols or []:
                if not s:
                    continue
                up = s.upper()
                self._subscribed.add(up)
                ch = self._symbol_to_channel(up)
                if ch and ch not in self._subscribed_channels:
                    new_channels.append(ch)
        if new_channels:
            self._send_subscribe(new_channels)

    def keep_alive_subscribe(self, symbols: list[str]) -> None:
        """Pin specific symbols so they survive any resubscription on reconnect.

        Implemented by adding the symbol to both `_subscribed` and `_keep_alive`
        — on every (re)connect we ensure all `_keep_alive` channels are open
        even if the caller later unsubscribes them.
        """
        with self._lock:
            for s in symbols or []:
                if s:
                    self._keep_alive.add(s.upper())
        self.subscribe(symbols)

    def on_tick(self, callback: Callable[[dict], None]) -> None:
        """Register a tick callback. Multiple callbacks are supported."""
        self._callbacks.append(callback)

    def get_ltp(self, symbol: str) -> float:
        """Return last LTP for `symbol` (0.0 if unseen)."""
        with self._lock:
            t = self._latest.get(symbol.upper())
            return float(t.get("ltp", 0.0)) if t else 0.0

    def get_latest(self, symbol: str) -> Optional[dict]:
        """Return the last raw tick dict for `symbol` (or None)."""
        with self._lock:
            t = self._latest.get(symbol.upper())
            return dict(t) if t else None

    def get_price_history(self, symbol: str) -> list[float]:
        """Return the recent price history (newest last) for `symbol`."""
        with self._lock:
            return list(self._price_history.get(symbol.upper(), []))

    def get_momentum(self, symbol: str, window: int = 20) -> float:
        """Return fractional momentum over the last `window` samples.

        Computed as (last - oldest) / oldest. Returns 0.0 if insufficient data.
        """
        with self._lock:
            hist = self._price_history.get(symbol.upper(), [])
            if len(hist) < 5:
                return 0.0
            recent = hist[-window:] if len(hist) >= window else hist
            if not recent or recent[0] <= 0:
                return 0.0
            return (recent[-1] - recent[0]) / recent[0]

    def get_oi_map(self, currency: str) -> dict:
        """Return `{strike: {ce_oi, pe_oi, ce_ltp, pe_ltp, ce_iv, pe_iv,
        ce_bid, pe_bid, ce_ask, pe_ask}}` aggregated across the latest
        emitted strikes. Mirrors DeribitFeed.get_oi_map.
        """
        underlying = currency.upper()
        result: dict[int, dict] = {}
        with self._lock:
            for sym, t in self._latest.items():
                meta = parse_deribit_instrument(sym)
                if not meta or meta["underlying"] != underlying:
                    continue
                strike = meta["strike"]
                rec = result.setdefault(strike, {})
                key = "ce" if meta["opt_type"] == "C" else "pe"
                rec[f"{key}_oi"] = t.get("oi", 0)
                rec[f"{key}_ltp"] = t.get("ltp", 0.0)
                rec[f"{key}_iv"] = t.get("iv", 0.0)
                rec[f"{key}_bid"] = t.get("bid", 0.0)
                rec[f"{key}_ask"] = t.get("ask", 0.0)
        return {int(k): v for k, v in result.items()}

    def get_nearest_expiry(self, currency: str) -> Optional[str]:
        """Return the nearest *future* (or today's) expiry as 'DDMMMYY'.

        Pulled from the cached instruments list. Returns None if no live
        instruments are known.
        """
        currency = currency.upper()
        with self._lock:
            cached = self._instruments_cache.get(currency)
            if cached and (time.time() - cached[0]) < 3600:
                names = [ins.get("instrument_name", "") for ins in cached[1]]
            else:
                names = [s for s in self._latest.keys()
                         if s.startswith(f"{currency}-")]
        if not names:
            return None
        today = datetime.now(timezone.utc).date()
        best: Optional[tuple] = None
        for name in names:
            meta = parse_deribit_instrument(name)
            if not meta:
                continue
            try:
                exp_dt = datetime.strptime(meta["expiry_iso"], "%Y-%m-%d").date()
            except Exception:
                continue
            days = (exp_dt - today).days
            if days < 0:
                continue
            if best is None or days < best[0]:
                best = (days, meta["ddmmyy"])
        return best[1] if best else None

    def get_atm_strike(self, currency: str) -> int:
        """Return the integer ATM strike based on the latest spot price.

        Uses the same heuristic as DeribitFeed: round-to-nearest with a
        currency-specific step (BTC: 1000, mid: 100, ETH: 50).
        """
        with self._lock:
            spot = self._spot.get(currency.upper(), 0.0)
        if spot <= 0:
            return 0
        if spot > 10_000:
            step = 1000
        elif spot > 1_000:
            step = 100
        else:
            step = 50
        return int(round(spot / step) * step)

    def get_spot(self, currency: str) -> float:
        """Return the latest spot index price for `currency`."""
        with self._lock:
            return float(self._spot.get(currency.upper(), 0.0))

    def is_running(self) -> bool:
        """Return True if the background thread is alive."""
        with self._lock:
            return self._running

    def is_connected(self) -> bool:
        """Return True if the WS is currently open.

        This is the new health-check method used by __main__.py to decide
        whether to fall back to the REST feed.
        """
        with self._lock:
            return self._connected

    def get_dvol(self, currency: str) -> float:
        """Return the Deribit Volatility Index (DVOL) for `currency`.

        Cached for `dvol_cache_sec`. Returns 0.0 on any error. DVOL is an
        annualized decimal (e.g. 0.65 = 65%).
        """
        currency = currency.upper()
        now = time.time()
        with self._lock:
            cached = self._dvol_cache.get(currency)
            if cached and (now - cached[0]) < self.dvol_cache_sec:
                return float(cached[1])
        dvol = self._fetch_dvol(currency)
        with self._lock:
            self._dvol_cache[currency] = (now, float(dvol))
        return float(dvol)

    # -----------------------------------------------------------------------
    # Internals
    # -----------------------------------------------------------------------
    def _symbol_to_channel(self, symbol: str) -> Optional[str]:
        """Map a bare currency or Deribit instrument name to a WS channel."""
        if not symbol:
            return None
        s = symbol.upper()
        if s in self.currencies:
            return f"deribit_price_index.{s.lower()}_usd"
        # Expect a Deribit-format instrument name
        meta = parse_deribit_instrument(s)
        if not meta:
            return None
        return f"ticker.{s}.100ms"

    def _close_ws(self) -> None:
        """Close the underlying WebSocket connection if open."""
        with self._lock:
            ws = self._ws
            self._ws = None
            self._connected = False
        if ws is not None:
            try:
                ws.close()
            except Exception as e:
                logger.debug(f"ws close error (ignored): {e}")

    def _send_subscribe(self, channels: list[str]) -> bool:
        """Send a public/subscribe message for the given channels.

        Filters out channels we already have open. Updates
        `_subscribed_channels` and returns True on success.
        """
        if not channels:
            return True
        with self._lock:
            ws = self._ws
            if ws is None or not self._connected:
                # Caller is expected to retry after connect; mark as desired.
                return False
            to_add = [c for c in channels if c not in self._subscribed_channels]
        if not to_add:
            return True
        msg = {
            "jsonrpc": "2.0",
            "method": "public/subscribe",
            "params": {"channels": to_add},
            "id": int(time.time() * 1000) & 0x7FFFFFFF,
        }
        try:
            ws.send(json.dumps(msg))
            with self._lock:
                self._subscribed_channels.update(to_add)
            logger.info(
                f"DeribitWebSocketFeed subscribed to {len(to_add)} new channel(s) "
                f"(total {len(self._subscribed_channels)})"
            )
            return True
        except Exception as e:
            logger.warning(f"DeribitWebSocketFeed subscribe failed: {e}")
            return False

    def _send_unsubscribe(self, channels: list[str]) -> None:
        """Send a public/unsubscribe message (best-effort)."""
        with self._lock:
            ws = self._ws
            if ws is None or not self._connected:
                return
            to_remove = [c for c in channels if c in self._subscribed_channels]
        if not to_remove:
            return
        msg = {
            "jsonrpc": "2.0",
            "method": "public/unsubscribe",
            "params": {"channels": to_remove},
            "id": int(time.time() * 1000) & 0x7FFFFFFF,
        }
        try:
            ws.send(json.dumps(msg))
            with self._lock:
                for c in to_remove:
                    self._subscribed_channels.discard(c)
            logger.debug(f"DeribitWebSocketFeed unsubscribed from {len(to_remove)} channel(s)")
        except Exception as e:
            logger.debug(f"DeribitWebSocketFeed unsubscribe failed (ignored): {e}")

    def _run_loop(self) -> None:
        """Background thread entry point. Loops connect -> recv -> disconnect."""
        time.sleep(0.1)  # let other components initialise
        while True:
            with self._lock:
                if not self._running:
                    return
            try:
                self._connect_and_consume()
            except Exception as e:
                logger.warning(f"DeribitWebSocketFeed loop error: {e}")
            # Disconnected path
            self._close_ws()
            if not self._should_reconnect():
                return
            time.sleep(self.reconnect_delay_sec)
            self._reconnect_attempts += 1

    def _should_reconnect(self) -> bool:
        """Decide whether to keep trying after a disconnect."""
        with self._lock:
            if not self._running:
                return False
            if self._reconnect_attempts >= self.max_reconnect_attempts:
                logger.error(
                    f"DeribitWebSocketFeed giving up after {self._reconnect_attempts} "
                    f"reconnect attempts"
                )
                self._running = False
                return False
            return True

    def _connect_and_consume(self) -> None:
        """Open the WS, send initial subscriptions, then read messages until error."""
        logger.info(f"DeribitWebSocketFeed connecting to {self.ws_url}")
        ws = websocket.create_connection(
            self.ws_url,
            timeout=self.timeout,
            header=[
                "User-Agent: crypto-options-bot/deribit-ws",
            ],
        )
        try:
            with self._lock:
                self._ws = ws
                self._connected = True
                self._reconnect_attempts = 0  # success — reset the counter
            # Set socket timeout so recv() doesn't block forever
            ws.settimeout(15)
            logger.success("DeribitWebSocketFeed connected")
            # Initial subscription: spot channels for all currencies + ticker
            # channels for ATM +/- max_strikes_per_underlying//2 strikes of the
            # nearest expiry.
            initial_channels = self._build_initial_channels()
            ok = self._send_subscribe(initial_channels)
            if not ok:
                logger.warning("DeribitWebSocketFeed initial subscribe failed")
            # Re-subscribe any keep-alive channels
            self._re_apply_keep_alive()
            # Read loop
            self._recv_loop(ws)
        finally:
            with self._lock:
                self._connected = False
                self._ws = None

    def _re_apply_keep_alive(self) -> None:
        """Re-subscribe any keep-alive (pinned) symbols after a connect."""
        with self._lock:
            pinned = list(self._keep_alive)
        if not pinned:
            return
        # Ensure they're treated as subscribed so we don't lose them
        self.subscribe(pinned)

    def _build_initial_channels(self) -> list[str]:
        """Build the initial channel list: spot + ATM strip of nearest expiry."""
        channels: list[str] = []
        for currency in self.currencies:
            channels.append(f"deribit_price_index.{currency.lower()}_usd")
        # Pre-seed ATM strip subscription for the nearest expiry if we have spot
        spot_to_strip = self._strip_instruments()
        for ch in spot_to_strip:
            if ch not in self._subscribed_channels:
                channels.append(ch)
        return channels

    def _strip_instruments(self) -> list[str]:
        """Return channel names for ATM-strip instruments (best-effort).

        Uses the instruments cache if present (populated lazily); otherwise
        returns just spot channels and lets tick-driven expansion fill in
        more on first refresh.
        """
        # Without a quick instruments lookup we'd otherwise subscribe to
        # nothing for the option chain on first start. The REST fallback in
        # _fetch_instruments() (used opportunistically) seeds this cache.
        out: list[str] = []
        for currency in self.currencies:
            instruments = self._get_cached_instruments(currency)
            if not instruments:
                continue
            nearest = self._nearest_expiry_from_instruments(instruments, currency)
            if not nearest:
                continue
            # spot snapshot to decide the strip
            with self._lock:
                spot = self._spot.get(currency, 0.0)
            strikes = self._strip_for_expiry(instruments, currency, nearest, spot)
            for s in strikes:
                out.append(f"ticker.{s}.100ms")
        return out

    def _get_cached_instruments(self, currency: str) -> list[dict]:
        """Return instruments from cache, refreshing if stale."""
        with self._lock:
            cached = self._instruments_cache.get(currency)
            if cached and (time.time() - cached[0]) < 3600:
                return cached[1]
        # fetch from REST
        try:
            url = (
                f"{self.rest_base}/public/get_instruments?currency={currency}"
                f"&kind=option&expired=false"
            )
            data = _http_get_json(url, timeout=self.timeout)
        except Exception as e:
            logger.debug(f"instruments fetch error: {e}")
            return []
        result = (data or {}).get("result", [])
        with self._lock:
            self._instruments_cache[currency] = (time.time(), result)
        return result

    @staticmethod
    def _nearest_expiry_from_instruments(instruments: list[dict], currency: str) -> Optional[str]:
        """Pick the nearest future expiry's ISO date from an instruments list."""
        today = datetime.now(timezone.utc).date()
        best: Optional[tuple] = None
        for ins in instruments:
            name = ins.get("instrument_name", "")
            meta = parse_deribit_instrument(name)
            if not meta or meta["underlying"] != currency:
                continue
            try:
                exp_dt = datetime.strptime(meta["expiry_iso"], "%Y-%m-%d").date()
            except Exception:
                continue
            days = (exp_dt - today).days
            if days < 0:
                continue
            if best is None or days < best[0]:
                best = (days, meta["expiry_iso"], meta["ddmmyy"])
        return best[1] if best else None

    def _strip_for_expiry(
        self,
        instruments: list[dict],
        currency: str,
        expiry_iso: str,
        spot: float,
    ) -> list[str]:
        """Pick the ATM-strip instruments for a given expiry.

        Returns up to max_strikes_per_underlying instrument names centered on
        the ATM strike. If we don't have a spot yet, just take the first
        max_strikes_per_underlying for the expiry.
        """
        names = []
        for ins in instruments:
            name = ins.get("instrument_name", "")
            meta = parse_deribit_instrument(name)
            if not meta or meta["underlying"] != currency or meta["expiry_iso"] != expiry_iso:
                continue
            names.append((meta["strike"], meta["opt_type"], name))
        if not names:
            return []
        # Step: pick a strike step based on spot (matches get_atm_strike)
        if spot > 10_000:
            step = 1000
        elif spot > 1_000:
            step = 100
        else:
            step = 50
        if spot <= 0:
            return [n for _, _, n in sorted(names)[: self.max_strikes_per_underlying]]
        atm = int(round(spot / step) * step)
        half = self.max_strikes_per_underlying // 2
        lo = atm - half * step
        hi = atm + (self.max_strikes_per_underlying - 1 - half) * step
        out = []
        for strike, _opt, n in sorted(names):
            if lo <= strike <= hi:
                out.append(n)
                if len(out) >= self.max_strikes_per_underlying:
                    break
        return out

    def _recv_loop(self, ws: websocket.WebSocket) -> None:
        """Blocking recv loop — runs until error / disconnect."""
        while True:
            with self._lock:
                if not self._running:
                    return
            try:
                raw = ws.recv()
            except websocket.WebSocketTimeoutException:
                # Periodic timeout — just loop and re-check
                self._maybe_heartbeat()
                continue
            except Exception as e:
                logger.warning(f"DeribitWebSocketFeed recv error: {e}")
                return
            if not raw:
                continue
            try:
                msg = json.loads(raw)
            except (TypeError, ValueError) as e:
                logger.debug(f"DeribitWebSocketFeed bad JSON: {e}")
                continue
            self._handle_message(msg)
            self._maybe_heartbeat()

    def _handle_message(self, msg: dict) -> None:
        """Dispatch a parsed JSON message to the right tick handler."""
        if not isinstance(msg, dict):
            return
        if msg.get("method") != "subscription":
            # Response to our subscribe/unsubscribe etc. — just log at debug.
            if "result" in msg:
                logger.debug(f"DeribitWebSocketFeed ack: {msg.get('id')} -> {len(msg.get('result', []))} chans")
            return
        params = msg.get("params") or {}
        channel = params.get("channel", "")
        data = params.get("data") or {}
        if channel.startswith("deribit_price_index."):
            self._on_spot(channel, data)
        elif channel.startswith("ticker."):
            self._on_ticker(channel, data)
        # else: book or other — ignore for now

    def _on_spot(self, channel: str, data: dict) -> None:
        """Handle a spot index tick."""
        # channel: deribit_price_index.btc_usd
        try:
            price = float(data.get("price", 0) or 0)
        except (TypeError, ValueError):
            return
        if price <= 0:
            return
        # Derive currency from channel suffix
        suffix = channel.rsplit(".", 1)[-1]  # btc_usd
        currency = suffix.split("_", 1)[0].upper()  # BTC
        now = time.time()
        with self._lock:
            self._spot[currency] = price
            self._latest[currency] = {
                "symbol": currency,
                "ltp": float(price),
                "bid": float(price),   # spot has no real bid/ask; reuse price
                "ask": float(price),
                "iv": 0.0,
                "oi": 0,
                "volume": 0,
                "underlying": currency,
                "exchange": "DERIBIT",
                "strike": 0.0,
                "option_type": None,
                "expiry": None,
                "ts": now,
            }
            self._tick_count += 1
            hist = self._price_history.setdefault(currency, [])
            hist.append(float(price))
            if len(hist) > 1000:
                del hist[: len(hist) - 1000]
            tick = dict(self._latest[currency])
        self._fire_callbacks(tick)

    def _on_ticker(self, channel: str, data: dict) -> None:
        """Handle a ticker.{INSTRUMENT}.100ms update."""
        # channel: ticker.BTC-26AUG26-82000-C.100ms
        parts = channel.split(".")
        if len(parts) < 2:
            return
        instrument = parts[1]
        meta = parse_deribit_instrument(instrument)
        if not meta:
            return
        mark = float(data.get("mark_price") or 0) or 0.0
        bid = float(data.get("best_bid_price") or 0) or 0.0
        ask = float(data.get("best_ask_price") or 0) or 0.0
        # mark_iv in Deribit ticker is in percent (e.g. 65.0 for 65%) — convert
        try:
            mark_iv = float(data.get("mark_iv") or 0) / 100.0
        except (TypeError, ValueError):
            mark_iv = 0.0
        oi = int(data.get("open_interest") or 0) or 0
        volume = int(data.get("volume") or 0) or 0
        try:
            underlying_price = float(data.get("underlying_price") or 0) or 0.0
        except (TypeError, ValueError):
            underlying_price = 0.0
        # If we got a fresh underlying_price, also update the spot cache so
        # ATM-strip subscription decisions stay correct.
        if underlying_price > 0 and meta["underlying"] in self.currencies:
            with self._lock:
                # Only overwrite if we don't have a more recent spot tick
                self._spot.setdefault(meta["underlying"], underlying_price)
        # Compute ltp: prefer mark, fall back to mid
        ltp = mark if mark > 0 else ((bid + ask) / 2.0 if (bid > 0 and ask > 0) else 0.0)
        if ltp <= 0 and bid <= 0 and ask <= 0:
            return  # nothing useful
        now = time.time()
        tick = {
            "symbol": instrument,
            "ltp": float(ltp),
            "bid": float(bid),
            "ask": float(ask),
            "iv": float(mark_iv),
            "oi": int(oi),
            "volume": int(volume),
            "underlying": meta["underlying"],
            "exchange": "DERIBIT",
            "strike": float(meta["strike"]),
            "option_type": meta["opt_type"],
            "expiry": meta["expiry_iso"],
            "ts": now,
        }
        with self._lock:
            self._latest[instrument] = tick
            self._tick_count += 1
            if ltp > 0:
                hist = self._price_history.setdefault(instrument, [])
                hist.append(float(ltp))
                if len(hist) > 1000:
                    del hist[: len(hist) - 1000]
        self._fire_callbacks(tick)

    def _fire_callbacks(self, tick: dict) -> None:
        """Fan out a tick to all registered callbacks (failures logged)."""
        for cb in self._callbacks:
            try:
                cb(tick)
            except Exception as e:
                logger.exception(f"DeribitWebSocketFeed tick callback error: {e}")

    def _maybe_heartbeat(self) -> None:
        """Emit a periodic heartbeat log line."""
        now = time.time()
        with self._lock:
            if now - self._last_heartbeat < self.heartbeat_sec:
                return
            self._last_heartbeat = now
            tick_count = self._tick_count
            latest_count = len(self._latest)
            spot = dict(self._spot)
            connected = self._connected
            n_channels = len(self._subscribed_channels)
        logger.info(
            f"DeribitWebSocketFeed heartbeat authed=N/A subscribed={n_channels} "
            f"latest={latest_count} tick_count={tick_count} spot={spot} "
            f"ws_connected={connected}"
        )

    def _fetch_dvol(self, currency: str) -> float:
        """Fetch the latest DVOL index value from Deribit's public endpoint.

        Returns 0.0 on any error. Cached by `get_dvol`. The result candle
        format is [timestamp, open, high, low, close] — we use the *close*
        of the most recent bar and normalise from percent to decimal.
        """
        # Last hour at 60s resolution is enough — we just need the latest bar.
        end_ms = int(time.time() * 1000)
        start_ms = end_ms - 60 * 60 * 1000
        url = (
            f"{self.rest_base}/public/get_volatility_index_data"
            f"?currency={currency}&start_timestamp={start_ms}&end_timestamp={end_ms}&resolution=60"
        )
        data = _http_get_json(url, timeout=self.timeout)
        if not data or "result" not in data:
            return 0.0
        result = data.get("result") or {}
        rows = result.get("data") if isinstance(result, dict) else None
        if not rows:
            return 0.0
        try:
            last = rows[-1]
            # candle = [timestamp, open, high, low, close]; use close (index 4)
            close = float(last[4])
        except (IndexError, TypeError, ValueError):
            return 0.0
        # DVOL is published in percent (e.g. 65.0) — normalise to decimal
        if close > 3.0:
            close = close / 100.0
        return max(0.0, close)


__all__ = ["DeribitWebSocketFeed", "TESTNET_WS_URL", "PROD_WS_URL"]
