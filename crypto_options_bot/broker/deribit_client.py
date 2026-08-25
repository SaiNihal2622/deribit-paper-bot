"""DeribitClient — live broker for Deribit via private REST API.

Implements the same BrokerClient ABC as PaperClient. Auth uses OAuth2
client-credentials flow against ``/public/auth``. Private endpoints are
called with ``Authorization: Bearer <access_token>``. Tokens are cached
and refreshed within 60s of expiry.

Required env vars:
  - DERIBIT_CLIENT_ID
  - DERIBIT_CLIENT_SECRET
  - DERIBIT_LIVE_CONFIRMED=YES  (safety guard; if not set, the client
                                  refuses to start in live mode)

Endpoints covered (testnet / prod share the same paths, only base URL differs):
  POST /private/buy                  — place buy order
  POST /private/sell                 — place sell order
  POST /private/cancel               — cancel an open order
  GET  /private/get_order_state      — single order details
  GET  /private/get_positions        — all open positions
  GET  /private/get_account_summary  — balances, margin, equity
  GET  /private/get_open_orders      — all open orders
  GET  /private/get_order_history_by_currency — recent order history

The class is thread-safe (RLock around token + state). Network errors
are propagated as exceptions; callers should catch and decide.
"""
from __future__ import annotations

import json
import os
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Optional

from loguru import logger

from .base import (
    BrokerClient,
    Order,
    OrderSide,
    OrderStatus,
    OrderType,
    Position,
    Tick,
)


# ---------------------------------------------------------------------------
# URLs
# ---------------------------------------------------------------------------
TESTNET_BASE = "https://test.deribit.com/api/v2"
PROD_BASE = "https://www.deribit.com/api/v2"
DEFAULT_TIMEOUT = 15

# Token refresh safety window — refresh when within 60s of expiry
_TOKEN_REFRESH_BUFFER_SEC = 60


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------
class DeribitAuthError(RuntimeError):
    """Raised when auth or token refresh fails."""


class DeribitSafetyError(RuntimeError):
    """Raised when the safety guard (DERIBIT_LIVE_CONFIRMED) is not satisfied."""


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _env_bool(name: str, default: bool = False) -> bool:
    """Parse a "YES"/"1"/"true" env var into a bool."""
    val = os.environ.get(name, "").strip().lower()
    if not val:
        return default
    return val in ("1", "yes", "y", "true", "on")


def _post_json(url: str, payload: dict, headers: Optional[dict] = None,
               timeout: int = DEFAULT_TIMEOUT) -> dict:
    """POST JSON to `url`. Returns the parsed response. Raises on non-2xx."""
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=data,
        method="POST",
        headers={
            "Content-Type": "application/json",
            "User-Agent": "crypto-options-bot/deribit-live",
            **(headers or {}),
        },
    )
    try:
        r = urllib.request.urlopen(req, timeout=timeout)
        body = r.read().decode("utf-8")
        return json.loads(body)
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="replace") if e.fp else ""
        raise RuntimeError(f"Deribit HTTP {e.code} POST {url}: {e.reason} | {body[:300]}")
    except urllib.error.URLError as e:
        raise RuntimeError(f"Deribit POST {url} URLError: {e.reason}")


def _get_json(url: str, headers: Optional[dict] = None,
              timeout: int = DEFAULT_TIMEOUT) -> dict:
    """GET JSON from `url`. Returns parsed response."""
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": "crypto-options-bot/deribit-live",
            **(headers or {}),
        },
    )
    try:
        r = urllib.request.urlopen(req, timeout=timeout)
        body = r.read().decode("utf-8")
        return json.loads(body)
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="replace") if e.fp else ""
        raise RuntimeError(f"Deribit HTTP {e.code} GET {url}: {e.reason} | {body[:300]}")
    except urllib.error.URLError as e:
        raise RuntimeError(f"Deribit GET {url} URLError: {e.reason}")


# ---------------------------------------------------------------------------
# Main class
# ---------------------------------------------------------------------------
class DeribitClient(BrokerClient):
    """Live Deribit broker — REST private endpoints only.

    Threading: a single RLock guards the OAuth token + pending order dict.
    Tick injection is supported (we synthesise ``Tick`` from the order/position
    stream) so the same tick pipeline that powers the paper client works
    unchanged here.
    """

    def __init__(
        self,
        env: str = "testnet",
        client_id: Optional[str] = None,
        client_secret: Optional[str] = None,
        live_confirmed: Optional[bool] = None,
        persist_path: str = "data_cache/live_state.json",
        timeout: int = DEFAULT_TIMEOUT,
    ):
        """
        Args:
            env: "testnet" or "prod"
            client_id: Deribit API key. Falls back to env DERIBIT_CLIENT_ID.
            client_secret: Deribit API secret. Falls back to env DERIBIT_CLIENT_SECRET.
            live_confirmed: safety guard. Falls back to env DERIBIT_LIVE_CONFIRMED.
            persist_path: optional JSON snapshot of recent order/position state.
            timeout: HTTP timeout in seconds.
        """
        if env == "testnet":
            self.base_url = TESTNET_BASE
        elif env == "prod":
            self.base_url = PROD_BASE
        else:
            raise ValueError(f"env must be 'testnet' or 'prod', got: {env}")
        self.env = env
        self.client_id = client_id or os.environ.get("DERIBIT_CLIENT_ID", "")
        self.client_secret = client_secret or os.environ.get("DERIBIT_CLIENT_SECRET", "")
        # Safety guard — refuse to start live trading without explicit consent.
        if live_confirmed is None:
            live_confirmed = _env_bool("DERIBIT_LIVE_CONFIRMED", default=False)
        self.live_confirmed = bool(live_confirmed)
        if not self.live_confirmed:
            raise DeribitSafetyError(
                "Refusing to start live DeribitClient without "
                "DERIBIT_LIVE_CONFIRMED=YES (env var or live_confirmed=True)"
            )
        if not self.client_id or not self.client_secret:
            raise DeribitAuthError(
                "Missing DERIBIT_CLIENT_ID / DERIBIT_CLIENT_SECRET"
            )

        self.persist_path = Path(persist_path)
        self.persist_path.parent.mkdir(parents=True, exist_ok=True)
        self.timeout = int(timeout)

        # OAuth token state
        self._access_token: str = ""
        self._token_expires_at: float = 0.0
        self._refresh_token: str = ""
        self._token_lock = threading.Lock()

        # In-memory shadow book (mirrors PaperClient's shape)
        self._lock = threading.RLock()
        self._orders: dict[str, Order] = {}
        self._positions: dict[str, Position] = {}
        self._ticks: dict[str, Tick] = {}
        self._tick_callbacks: list[Callable[[Tick], None]] = []
        self._connected = False

        logger.info(
            f"DeribitClient initialised (env={env}, base={self.base_url}, "
            f"client_id={self.client_id[:6]}***, live_confirmed=True)"
        )

    # ------------------------------------------------------------------
    # Auth
    # ------------------------------------------------------------------
    def _ensure_token(self, force: bool = False) -> None:
        """Refresh the OAuth token if missing or close to expiry."""
        with self._token_lock:
            now = time.time()
            if not force and self._access_token and now < (self._token_expires_at - _TOKEN_REFRESH_BUFFER_SEC):
                return
            url = f"{self.base_url}/public/auth"
            payload = {
                "jsonrpc": "2.0",
                "id": int(now * 1000) & 0x7FFFFFFF,
                "method": "public/auth",
                "params": {
                    "grant_type": "client_credentials",
                    "client_id": self.client_id,
                    "client_secret": self.client_secret,
                },
            }
            try:
                resp = _post_json(url, payload, timeout=self.timeout)
            except Exception as e:
                raise DeribitAuthError(f"auth POST failed: {e}") from e
            res = resp.get("result") or {}
            token = res.get("access_token", "")
            expires_in = float(res.get("expires_in", 900))
            if not token:
                raise DeribitAuthError(
                    f"auth response missing access_token: {json.dumps(resp)[:300]}"
                )
            self._access_token = token
            self._token_expires_at = now + expires_in
            self._refresh_token = res.get("refresh_token", "")
            logger.info(
                f"DeribitClient token refreshed (expires_in={expires_in:.0f}s)"
            )

    def _auth_headers(self) -> dict:
        """Return Authorization headers for private calls."""
        self._ensure_token()
        return {"Authorization": f"Bearer {self._access_token}"}

    # ------------------------------------------------------------------
    # Connection lifecycle
    # ------------------------------------------------------------------
    def connect(self) -> None:
        with self._lock:
            self._ensure_token()
            self._connected = True
            logger.info("DeribitClient connected (auth ok)")
            try:
                self._refresh_positions()
            except Exception as e:
                logger.warning(f"DeribitClient initial positions fetch failed: {e}")

    def disconnect(self) -> None:
        with self._lock:
            self._connected = False
            logger.info("DeribitClient disconnected")

    def is_connected(self) -> bool:
        with self._lock:
            return self._connected

    # ------------------------------------------------------------------
    # Order management
    # ------------------------------------------------------------------
    def place_order(self, order: Order) -> Order:
        with self._lock:
            if not self._connected:
                raise RuntimeError("DeribitClient not connected — call connect() first")
            endpoint = "/private/buy" if order.side == OrderSide.BUY else "/private/sell"
            payload = {
                "jsonrpc": "2.0",
                "id": int(time.time() * 1000) & 0x7FFFFFFF,
                "method": endpoint,
                "params": {
                    "instrument_name": order.symbol,
                    "amount": int(order.qty),
                    "type": order.order_type.value,
                },
            }
            if order.order_type == OrderType.LIMIT and order.price > 0:
                payload["params"]["price"] = float(order.price)
            if order.order_type in (OrderType.SL, OrderType.SL_M) and order.trigger_price > 0:
                payload["params"]["trigger_price"] = float(order.trigger_price)
            payload["params"]["label"] = order.tag or "crypto-options-bot"
            url = f"{self.base_url}{endpoint}"
            resp = _post_json(url, payload, headers=self._auth_headers(), timeout=self.timeout)
            result = resp.get("result") or {}
            order.order_id = str(result.get("order_id", ""))
            order.status = OrderStatus.OPEN
            order.placed_at = datetime.now(timezone.utc)
            avg = float(result.get("average_price", 0) or 0)
            filled = int(result.get("filled_amount", 0) or 0)
            if filled >= order.qty and avg > 0:
                order.avg_fill_price = avg
                order.filled_qty = filled
                order.status = OrderStatus.COMPLETE
                order.filled_at = datetime.now(timezone.utc)
            elif filled > 0:
                order.avg_fill_price = avg
                order.filled_qty = filled
            self._orders[order.order_id] = order
            logger.info(
                f"[DERIBIT] PLACE {order.order_id} {order.side.value} "
                f"{order.qty}x{order.symbol} {order.order_type.value} @ {order.price}"
            )
            return order

    def modify_order(self, order_id: str, **kwargs) -> Order:
        with self._lock:
            order = self._orders.get(order_id)
            if not order:
                raise KeyError(f"Order {order_id} not found")
            if order.status in (OrderStatus.COMPLETE, OrderStatus.CANCELLED, OrderStatus.REJECTED):
                raise ValueError(f"Cannot modify {order.status.value} order")
            # Deribit's modify endpoint is /private/edit
            payload = {
                "jsonrpc": "2.0",
                "id": int(time.time() * 1000) & 0x7FFFFFFF,
                "method": "/private/edit" if False else "private/edit",
                "params": {
                    "order_id": order_id,
                    "amount": kwargs.get("qty", order.qty),
                },
            }
            if "price" in kwargs and kwargs["price"] is not None:
                payload["params"]["price"] = float(kwargs["price"])
            url = f"{self.base_url}/private/edit"
            resp = _post_json(url, payload, headers=self._auth_headers(), timeout=self.timeout)
            for k, v in kwargs.items():
                if hasattr(order, k):
                    setattr(order, k, v)
            logger.info(f"[DERIBIT] MODIFY {order_id} {kwargs}")
            return order

    def cancel_order(self, order_id: str) -> Order:
        with self._lock:
            order = self._orders.get(order_id)
            if not order:
                raise KeyError(f"Order {order_id} not found")
            if order.status == OrderStatus.COMPLETE:
                raise ValueError("Cannot cancel filled order")
            payload = {
                "jsonrpc": "2.0",
                "id": int(time.time() * 1000) & 0x7FFFFFFF,
                "method": "private/cancel",
                "params": {"order_id": order_id},
            }
            url = f"{self.base_url}/private/cancel"
            _post_json(url, payload, headers=self._auth_headers(), timeout=self.timeout)
            order.status = OrderStatus.CANCELLED
            logger.info(f"[DERIBIT] CANCEL {order_id}")
            return order

    def get_order(self, order_id: str) -> Optional[Order]:
        with self._lock:
            cached = self._orders.get(order_id)
            if cached and cached.status not in (OrderStatus.OPEN, OrderStatus.PENDING):
                return cached
        # live refresh
        try:
            payload = {
                "jsonrpc": "2.0",
                "id": int(time.time() * 1000) & 0x7FFFFFFF,
                "method": "private/get_order_state",
                "params": {"order_id": order_id},
            }
            url = f"{self.base_url}/private/get_order_state"
            resp = _post_json(url, payload, headers=self._auth_headers(), timeout=self.timeout)
            return self._hydrate_order_from_state(resp.get("result") or {})
        except Exception as e:
            logger.warning(f"DeribitClient get_order({order_id}) failed: {e}")
            return self._orders.get(order_id)

    def get_positions(self) -> list[Position]:
        with self._lock:
            try:
                self._refresh_positions()
            except Exception as e:
                logger.warning(f"DeribitClient positions refresh failed: {e}")
            return list(self._positions.values())

    def get_holdings(self) -> list[Position]:
        # Deribit options are settled in underlying; "holdings" is the same
        # shape as positions.
        return self.get_positions()

    def get_margins(self) -> dict:
        with self._lock:
            try:
                payload = {
                    "jsonrpc": "2.0",
                    "id": int(time.time() * 1000) & 0x7FFFFFFF,
                    "method": "private/get_account_summary",
                    "params": {"currency": "BTC", "extended": "true"},
                }
                url = f"{self.base_url}/private/get_account_summary"
                resp = _post_json(url, payload, headers=self._auth_headers(), timeout=self.timeout)
                res = resp.get("result") or {}
                return {
                    "available": float(res.get("available_funds", 0) or 0),
                    "used": float(res.get("initial_margin", 0) or 0),
                    "total": float(res.get("equity", 0) or 0),
                    "realized_pnl": float(res.get("realized_pl", 0) or 0),
                    "unrealized_pnl": float(res.get("unrealized_pl", 0) or 0),
                    "margin_balance": float(res.get("margin_balance", 0) or 0),
                }
            except Exception as e:
                logger.warning(f"DeribitClient get_margins failed: {e}")
                return {
                    "available": 0.0, "used": 0.0, "total": 0.0,
                    "realized_pnl": 0.0, "unrealized_pnl": 0.0,
                    "margin_balance": 0.0,
                }

    def get_ltp(self, symbol: str, exchange: str = "DERIBIT") -> float:
        with self._lock:
            tick = self._ticks.get(symbol)
            return tick.ltp if tick else 0.0

    def subscribe(self, symbols: list[str], exchange: str = "DERIBIT") -> None:
        # Public market data is best driven from DeribitWebSocketFeed; this
        # method is a no-op here but the ABC requires it.
        logger.debug(f"DeribitClient.subscribe({len(symbols)} symbols) — no-op")

    def on_tick(self, callback: Callable[[Tick], None]) -> None:
        self._tick_callbacks.append(callback)

    def inject_tick(self, tick: Tick) -> None:
        """Accept a tick (from the WS feed) and mark positions to market."""
        with self._lock:
            self._ticks[tick.symbol] = tick
            pos = self._positions.get(tick.symbol)
            if pos:
                pos.ltp = tick.ltp
                pos.pnl = (pos.ltp - pos.avg_price) * pos.qty * pos.contract_size
        for cb in self._tick_callbacks:
            try:
                cb(tick)
            except Exception as e:
                logger.exception(f"DeribitClient tick callback error: {e}")

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------
    def _refresh_positions(self) -> None:
        """Pull positions from /private/get_positions and hydrate the cache."""
        payload = {
            "jsonrpc": "2.0",
            "id": int(time.time() * 1000) & 0x7FFFFFFF,
            "method": "private/get_positions",
            "params": {"currency": "BTC", "kind": "option"},
        }
        url = f"{self.base_url}/private/get_positions"
        resp = _post_json(url, payload, headers=self._auth_headers(), timeout=self.timeout)
        rows = resp.get("result") or []
        new_positions: dict[str, Position] = {}
        for r in rows:
            try:
                size = float(r.get("size", 0) or 0)
                if size == 0:
                    continue
                symbol = r.get("instrument_name", "")
                avg_price = float(r.get("average_price", 0) or 0)
                mark_price = float(r.get("mark_price", 0) or 0)
                pnl = float(r.get("floating_profit_loss", 0) or 0)
                strike = float(r.get("strike", 0) or 0)
                opt_type = r.get("option_type", "")
                expiry = str(r.get("expiration_timestamp", ""))[:10] or None
                pos = Position(
                    symbol=symbol,
                    qty=int(size),
                    avg_price=avg_price,
                    ltp=mark_price,
                    exchange="DERIBIT",
                    pnl=pnl,
                    strike=strike,
                    option_type=opt_type,
                    expiry=expiry,
                    underlying=r.get("underlying", ""),
                    contract_size=1.0,
                )
                new_positions[symbol] = pos
            except Exception as e:
                logger.debug(f"position parse error: {e}")
        # Also fetch ETH positions (the API requires currency-specific calls)
        for ccy in ("ETH",):
            try:
                payload["params"]["currency"] = ccy
                resp2 = _post_json(url, payload, headers=self._auth_headers(), timeout=self.timeout)
                for r in resp2.get("result") or []:
                    size = float(r.get("size", 0) or 0)
                    if size == 0:
                        continue
                    symbol = r.get("instrument_name", "")
                    avg_price = float(r.get("average_price", 0) or 0)
                    mark_price = float(r.get("mark_price", 0) or 0)
                    pnl = float(r.get("floating_profit_loss", 0) or 0)
                    new_positions[symbol] = Position(
                        symbol=symbol,
                        qty=int(size),
                        avg_price=avg_price,
                        ltp=mark_price,
                        exchange="DERIBIT",
                        pnl=pnl,
                        strike=float(r.get("strike", 0) or 0),
                        option_type=r.get("option_type", ""),
                        expiry=str(r.get("expiration_timestamp", ""))[:10] or None,
                        underlying=ccy,
                        contract_size=1.0,
                    )
            except Exception as e:
                logger.debug(f"DeribitClient ETH positions fetch failed: {e}")
        with self._lock:
            self._positions = new_positions

    def _hydrate_order_from_state(self, r: dict) -> Order:
        """Build/refresh an Order from a Deribit order_state payload."""
        order_id = str(r.get("order_id", ""))
        side = OrderSide.BUY if str(r.get("direction", "")).lower() == "buy" else OrderSide.SELL
        order_type_str = str(r.get("order_type", "limit")).upper().replace("-", "_")
        try:
            ot = OrderType(order_type_str)
        except ValueError:
            ot = OrderType.LIMIT
        amount = int(r.get("amount", 0) or 0)
        filled = int(r.get("filled_amount", 0) or 0)
        avg_price = float(r.get("average_price", 0) or 0)
        state = str(r.get("state", "open")).lower()
        if state in ("filled",):
            status = OrderStatus.COMPLETE
        elif state in ("cancelled", "canceled"):
            status = OrderStatus.CANCELLED
        elif state in ("rejected",):
            status = OrderStatus.REJECTED
        else:
            status = OrderStatus.OPEN
        order = Order(
            symbol=r.get("instrument_name", ""),
            side=side,
            qty=amount,
            order_type=ot,
            price=float(r.get("price", 0) or 0),
            trigger_price=float(r.get("trigger_price", 0) or 0),
            tag=str(r.get("label", "") or ""),
            order_id=order_id,
            status=status,
            filled_qty=filled,
            avg_fill_price=avg_price,
        )
        with self._lock:
            self._orders[order_id] = order
        return order

    # Optional convenience: list open orders (used by the dashboard).
    def get_open_orders(self) -> list[Order]:
        try:
            payload = {
                "jsonrpc": "2.0",
                "id": int(time.time() * 1000) & 0x7FFFFFFF,
                "method": "private/get_open_orders",
                "params": {"kind": "option"},
            }
            url = f"{self.base_url}/private/get_open_orders"
            resp = _post_json(url, payload, headers=self._auth_headers(), timeout=self.timeout)
            return [
                self._hydrate_order_from_state(r) for r in (resp.get("result") or [])
            ]
        except Exception as e:
            logger.warning(f"DeribitClient.get_open_orders failed: {e}")
            return []


__all__ = ["DeribitClient", "DeribitAuthError", "DeribitSafetyError", "TESTNET_BASE", "PROD_BASE"]

