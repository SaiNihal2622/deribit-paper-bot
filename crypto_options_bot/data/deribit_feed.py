"""Deribit feed — real BTC/ETH option chain from Deribit's public REST API.

This is the crypto equivalent of the Kotak Neo bot's KotakProdFeed. Same public
shape (start/stop/subscribe/keep_alive_subscribe/on_tick/get_ltp/get_latest/
get_oi_map/get_nearest_expiry/get_atm_strike) but adapted for Deribit:

  - Deribit testnet base URL: https://test.deribit.com/api/v2
  - Deribit prod base URL:    https://www.deribit.com/api/v2
  - No auth required for public market data (paper trading).
  - Symbol format: BTC-26DEC25-100000-C (Deribit native). We use this as the
    Tick.symbol — it's the standard, no conversion needed.
  - We poll /public/get_book_summary_by_currency for the entire option chain
    in a single call (real bid/ask/mark_price).
  - We poll /public/ticker lazily for each instrument to get mark_iv (cached
    for ~30s to stay under rate limits).

Public surface:
    feed = DeribitFeed(env="testnet", currencies=["BTC", "ETH"], poll_interval_sec=2.0)
    feed.start()
    feed.subscribe(["BTC", "ETH"])
    feed.on_tick(callback)
    feed.get_ltp("BTC")
    feed.get_oi_map("BTC")
    feed.get_nearest_expiry("BTC")
    feed.get_atm_strike("BTC")
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

from loguru import logger

from ..broker.base import Tick


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
TESTNET_BASE = "https://test.deribit.com/api/v2"
PROD_BASE = "https://www.deribit.com/api/v2"
DEFAULT_TIMEOUT = 15

# Deribit instrument: BTC-26DEC25-100000-C
_DERIBIT_INST_RE = re.compile(
    r"^(BTC|ETH)-(\d{2}[A-Z]{3}\d{2})-(\d+)-([CP])$"
)
_MONTHS = {"JAN": 1, "FEB": 2, "MAR": 3, "APR": 4, "MAY": 5, "JUN": 6,
           "JUL": 7, "AUG": 8, "SEP": 9, "OCT": 10, "NOV": 11, "DEC": 12}


# ---------------------------------------------------------------------------
# HTTP helper
# ---------------------------------------------------------------------------
def _http_get_json(url: str, timeout: int = DEFAULT_TIMEOUT) -> Optional[dict]:
    """GET a JSON URL. Returns parsed dict on 200, None on any error."""
    req = urllib.request.Request(url, headers={"User-Agent": "crypto-options-bot/deribit-feed"})
    try:
        r = urllib.request.urlopen(req, timeout=timeout)
        return json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        logger.debug(f"DeribitFeed HTTP {e.code} for {url[:120]}: {e.reason}")
        return None
    except Exception as e:
        logger.debug(f"DeribitFeed transport error for {url[:120]}: {e}")
        return None
    return None  # unreachable — placates type checker


# ---------------------------------------------------------------------------
# Symbol helpers
# ---------------------------------------------------------------------------
def parse_deribit_instrument(name: str) -> Optional[dict]:
    """Parse a Deribit instrument name into structured metadata.

    Example: BTC-26DEC25-100000-C
        → {symbol: 'BTC-26DEC25-100000-C', underlying: 'BTC', strike: 100000,
           opt_type: 'C', expiry_iso: '2025-12-26', ddmmyy: '26DEC25'}
    """
    m = _DERIBIT_INST_RE.match(name)
    if not m:
        return None
    underlying, ddmmyy, strike_s, cp = m.groups()
    dd = ddmmyy[0:2]
    mmm = ddmmyy[2:5]
    yy = ddmmyy[5:7]
    try:
        exp_dt = datetime(2000 + int(yy), _MONTHS[mmm], int(dd)).date()
    except (KeyError, ValueError):
        return None
    return {
        "symbol": name,
        "underlying": underlying,
        "strike": int(strike_s),
        "opt_type": cp,  # "C" or "P"
        "expiry_iso": exp_dt.isoformat(),
        "ddmmyy": ddmmyy,
    }


def format_deribit_instrument(underlying: str, expiry_iso: str, strike: int, opt_type: str) -> str:
    """Build a Deribit-format instrument name from parts.

    Example: format_deribit_instrument("BTC", "2025-12-26", 100000, "C")
        → "BTC-26DEC25-100000-C"
    """
    # Deribit uses single-letter C/P
    cp = "C" if opt_type.upper().startswith("C") else "P"
    try:
        dt = datetime.strptime(expiry_iso, "%Y-%m-%d")
    except Exception:
        # If caller passed "26DEC25" already, return as-is
        return f"{underlying}-{expiry_iso}-{int(strike)}-{cp}"
    ddmmyy = dt.strftime("%d%b%y").upper()
    return f"{underlying}-{ddmmyy}-{int(strike)}-{cp}"


# ---------------------------------------------------------------------------
# Main class
# ---------------------------------------------------------------------------
class DeribitFeed:
    """Polls Deribit's public REST API for BTC/ETH option chain + spot."""

    def __init__(
        self,
        env: str = "testnet",
        currencies: Optional[list[str]] = None,
        poll_interval_sec: float = 2.0,
        strike_window_pct: float = 0.20,
        iv_cache_sec: float = 30.0,
        heartbeat_sec: float = 60.0,
        timeout: int = DEFAULT_TIMEOUT,
    ):
        """
        Args:
            env: "testnet" or "prod"
            currencies: list of underlying currencies, e.g. ["BTC", "ETH"]
            poll_interval_sec: seconds between polls
            strike_window_pct: only emit ticks for strikes within +/- this
                fraction of spot (e.g. 0.20 = +/-20%)
            iv_cache_sec: how long to cache mark_iv per instrument
            heartbeat_sec: heartbeat log interval
            timeout: HTTP timeout in seconds
        """
        if env == "testnet":
            self.base_url = TESTNET_BASE
        elif env == "prod":
            self.base_url = PROD_BASE
        else:
            raise ValueError(f"env must be 'testnet' or 'prod', got: {env}")

        self.currencies = [c.upper() for c in (currencies or ["BTC", "ETH"])]
        self.poll_interval = max(0.5, float(poll_interval_sec))
        self.strike_window_pct = float(strike_window_pct)
        self.iv_cache_sec = float(iv_cache_sec)
        self.heartbeat_sec = float(heartbeat_sec)
        self.timeout = int(timeout)

        self._latest: dict[str, dict] = {}        # symbol → {ltp, bid, ask, oi, vol, iv, ts}
        self._price_history: dict[str, list[float]] = {}
        self._iv_cache: dict[str, tuple[float, float]] = {}  # symbol → (ts, iv)
        self._book_summary_cache: dict[str, tuple[float, list]] = {}  # currency → (ts, data)
        self._instruments_cache: dict[str, tuple[float, list]] = {}   # currency → (ts, instruments)
        self._spot: dict[str, float] = {}         # "BTC" / "ETH" → last spot
        self._lock = threading.RLock()
        self._running = False
        self._thread: Optional[threading.Thread] = None
        self._last_heartbeat = 0.0
        self._tick_count = 0
        self._subscribed: set[str] = set()
        self._callbacks: list[Callable[[dict], None]] = []

    # --------------- public API ---------------
    def start(self) -> None:
        """Start the poll thread. Idempotent."""
        with self._lock:
            if self._running:
                return
            self._running = True
            self._thread = threading.Thread(target=self._poll_loop, name="deribit-feed", daemon=True)
            self._thread.start()
            logger.info(
                f"DeribitFeed started (env={self.base_url}, currencies={self.currencies}, "
                f"poll={self.poll_interval}s, strike_window=+/-{self.strike_window_pct:.0%})"
            )

    def stop(self) -> None:
        """Stop the poll thread. Joins within 3s."""
        with self._lock:
            self._running = False
        if self._thread:
            self._thread.join(timeout=3)
            logger.info("DeribitFeed stopped")

    def subscribe(self, symbols: list[str]) -> None:
        """Add symbols to the subscription set (idempotent)."""
        with self._lock:
            for s in symbols or []:
                if s:
                    self._subscribed.add(s.upper())
            logger.debug(f"DeribitFeed subscribe: {symbols} (total {len(self._subscribed)})")

    def keep_alive_subscribe(self, symbols: list[str]) -> None:
        """Pin symbols to the subscription set. Same as subscribe() in this
        implementation; the interface exists for parity with KotakProdFeed."""
        self.subscribe(symbols)

    def on_tick(self, callback: Callable[[dict], None]) -> None:
        """Register a tick callback. Callback receives the tick dict (same
        shape as the broker.Tick.__dict__ so the paper client can build a Tick)."""
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

    def get_oi_map(self, underlying: str) -> dict:
        """Return `{strike: {ce_oi, pe_oi, ce_ltp, pe_ltp, ce_iv, pe_iv,
        ce_bid, pe_bid, ce_ask, pe_ask}}` for the latest emitted strikes.

        Used by OI heatmap / max-pain logic.
        """
        underlying = underlying.upper()
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
        """Return the nearest *future* (or today's) expiry as 'DDMMMYY'
        (e.g. '26DEC25'). Returns None if no live instruments are known."""
        currency = currency.upper()
        # Use the cached instruments list; if missing, fall back to the
        # book summary which has instrument_name on each entry.
        with self._lock:
            cached = self._instruments_cache.get(currency)
            if cached and (time.time() - cached[0]) < 3600:
                names = [ins.get("instrument_name", "") for ins in cached[1]]
            else:
                # derive from _latest
                names = [s for s in self._latest.keys()
                         if s.startswith(f"{currency}-")]
        if not names:
            return None
        # parse ddmmyy from each name
        today = datetime.now(timezone.utc).date()
        best: Optional[tuple] = None  # (days_until, ddmmyy)
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

        Uses a coarse round-to-nearest heuristic (~spot / 1000 for BTC,
        ~spot / 50 for ETH). Good enough for sampling; strategies refine
        using the actual OTM delta they want.
        """
        with self._lock:
            spot = self._spot.get(currency.upper(), 0.0)
        if spot <= 0:
            return 0
        # Pick a round step based on the underlying's typical scale
        if spot > 10_000:    # BTC regime
            step = 1000
        elif spot > 1_000:   # mid
            step = 100
        else:                # ETH regime
            step = 50
        return int(round(spot / step) * step)

    def get_spot(self, currency: str) -> float:
        """Return the latest spot index price for `currency`."""
        with self._lock:
            return float(self._spot.get(currency.upper(), 0.0))

    def is_running(self) -> bool:
        with self._lock:
            return self._running

    # --------------- internals ---------------
    def _emit(self, tick_dict: dict) -> None:
        """Store a tick, update history, fire callbacks."""
        sym = tick_dict["symbol"].upper()
        with self._lock:
            self._latest[sym] = tick_dict
            self._tick_count += 1
            ltp = tick_dict.get("ltp", 0.0) or 0.0
            if ltp > 0:
                hist = self._price_history.setdefault(sym, [])
                hist.append(ltp)
                # cap history to last 1000 points to keep memory bounded
                if len(hist) > 1000:
                    del hist[: len(hist) - 1000]
        for cb in self._callbacks:
            try:
                cb(tick_dict)
            except Exception as e:
                logger.exception(f"DeribitFeed tick callback error: {e}")

    def _http_get(self, path: str, params: dict | None = None) -> Optional[dict]:
        url = f"{self.base_url}{path}"
        if params:
            url += "?" + urllib.parse.urlencode(params)
        return _http_get_json(url, timeout=self.timeout)

    def _fetch_index_price(self, currency: str) -> Optional[float]:
        """Fetch the spot index price for a currency."""
        index_name = f"{currency.lower()}_usd"
        data = self._http_get("/public/get_index_price", {"index_name": index_name})
        if not data or "result" not in data:
            return None
        try:
            return float(data["result"].get("index_price", 0))
        except (TypeError, ValueError):
            return None

    def _fetch_instruments(self, currency: str) -> list[dict]:
        """Fetch (and cache for 1h) the list of live option instruments."""
        with self._lock:
            cached = self._instruments_cache.get(currency)
            if cached and (time.time() - cached[0]) < 3600:
                return cached[1]
        data = self._http_get(
            "/public/get_instruments",
            {"currency": currency, "kind": "option", "expired": "false"},
        )
        result = data.get("result", []) if data else []
        with self._lock:
            self._instruments_cache[currency] = (time.time(), result)
        return result

    def _fetch_book_summary(self, currency: str) -> list[dict]:
        """Fetch the bulk book summary for a currency's options.

        Returns the list of `result` entries. Cached for 1s to avoid hammering.
        """
        with self._lock:
            cached = self._book_summary_cache.get(currency)
            if cached and (time.time() - cached[0]) < 1.0:
                return cached[1]
        data = self._http_get(
            "/public/get_book_summary_by_currency",
            {"currency": currency, "kind": "option"},
        )
        result = data.get("result", []) if data else []
        with self._lock:
            self._book_summary_cache[currency] = (time.time(), result)
        return result

    def _fetch_ticker_iv(self, symbol: str) -> float:
        """Fetch (and cache) the mark_iv for a single instrument via /public/ticker."""
        with self._lock:
            cached = self._iv_cache.get(symbol)
            if cached and (time.time() - cached[0]) < self.iv_cache_sec:
                return cached[1]
        data = self._http_get("/public/ticker", {"instrument_name": symbol})
        iv = 0.0
        if data and "result" in data:
            try:
                iv = float(data["result"].get("mark_iv", 0) or 0) / 100.0
            except (TypeError, ValueError):
                iv = 0.0
        with self._lock:
            self._iv_cache[symbol] = (time.time(), iv)
        return iv

    def _poll_loop(self) -> None:
        """Main poll loop. Polls each currency + spot in turn."""
        # initial startup sleep to let other components initialise
        time.sleep(0.2)
        while True:
            with self._lock:
                if not self._running:
                    return
            try:
                for currency in self.currencies:
                    self._poll_currency(currency)
                self._maybe_heartbeat()
            except Exception as e:
                # never die — log and continue
                logger.exception(f"DeribitFeed poll loop error: {e}")
            time.sleep(self.poll_interval)

    def _poll_currency(self, currency: str) -> None:
        """Poll spot + chain for one currency."""
        # 1) spot
        spot = self._fetch_index_price(currency)
        if spot and spot > 0:
            with self._lock:
                self._spot[currency] = spot
            self._emit({
                "symbol": currency,
                "ltp": float(spot),
                "bid": 0.0,
                "ask": 0.0,
                "oi": 0,
                "volume": 0,
                "iv": 0.0,
                "underlying": currency,
                "exchange": "DERIBIT",
                "strike": 0.0,
                "option_type": None,
                "expiry": None,
                "ts": time.time(),
            })

        # 2) option chain
        summary = self._fetch_book_summary(currency)
        if not summary:
            return

        # 3) filter to strike window if we have a spot
        now_spot = spot if spot and spot > 0 else 0.0
        lo = now_spot * (1.0 - self.strike_window_pct) if now_spot > 0 else 0.0
        hi = now_spot * (1.0 + self.strike_window_pct) if now_spot > 0 else 0.0

        for entry in summary:
            inst_name = entry.get("instrument_name", "")
            meta = parse_deribit_instrument(inst_name)
            if not meta or meta["underlying"] != currency:
                continue
            # only emit strikes within the window (when we have a spot)
            if now_spot > 0 and (meta["strike"] < lo or meta["strike"] > hi):
                continue
            mark = entry.get("mark_price") or 0.0
            bid = entry.get("best_bid_price") or 0.0
            ask = entry.get("best_ask_price") or 0.0
            if mark <= 0 and bid <= 0 and ask <= 0:
                continue
            ltp = mark if mark > 0 else (bid + ask) / 2.0
            # 4) lazily fetch mark_iv (cached)
            iv = self._fetch_ticker_iv(inst_name)
            self._emit({
                "symbol": inst_name,
                "ltp": float(ltp),
                "bid": float(bid),
                "ask": float(ask),
                "oi": int(entry.get("open_interest", 0) or 0),
                "volume": int(entry.get("volume", 0) or 0),
                "iv": float(iv),
                "underlying": currency,
                "exchange": "DERIBIT",
                "strike": float(meta["strike"]),
                "option_type": meta["opt_type"],
                "expiry": meta["expiry_iso"],
                "ts": time.time(),
            })

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
        logger.info(
            f"DeribitFeed heartbeat subscribed=N/A latest={latest_count} "
            f"tick_count={tick_count} spot={spot}"
        )
