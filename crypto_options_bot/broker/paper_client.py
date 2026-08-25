"""Paper trading client (in-memory, no real exchange).

Synthesises fills from injected ticks. Does NOT call any real broker.
All orders are intercepted, logged, and filled against a synthetic book.
This is the safe default for paper trading when no live account is provisioned.

Adapted from the Kotak Neo bot's PaperClient for Deribit semantics:
  - exchange = "DERIBIT"
  - symbol format = "BTC-26DEC25-100000-C" (Deribit native)
  - fallback chain for force-fill uses 0.5% of spot (ATM-ish estimate)
  - FIX 2026-XX-YY: shallow-copy order/position __dict__ before mutating for
    JSON serialisation (mirrors the Kotak fix from 2026-08-11).
"""
from __future__ import annotations

import copy
import json
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from threading import RLock
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


class PaperClient(BrokerClient):
    """In-process paper trading simulator.

    - Tracks a virtual book of orders and positions
    - Fills market orders at the most recent tick LTP (with simulated slippage)
    - Fills limit orders when the tick crosses the price
    - Persists state to a JSON file for crash recovery
    """

    def __init__(
        self,
        starting_capital: float = 100_000.0,
        slippage_bps: float = 5.0,
        limit_fill_spread_pct: float = 0.1,
        limit_fill_min_spread: float = 0.01,
        limit_fill_near_ltp_pct: float = 0.5,
        fill_mode: str = "market_like",
        persist_path: str = "data_cache/paper_state.json",
    ):
        """
        Args:
            starting_capital: virtual cash in USD
            slippage_bps: simulated slippage (5 bps = 0.05%) on market orders
            limit_fill_spread_pct: synthetic spread (%) used to simulate bid/ask
            limit_fill_min_spread: min spread in absolute units
            limit_fill_near_ltp_pct: also fill LIMIT orders within this % of LTP
            fill_mode: 'market_like' (force fill) | 'aggressive_limit' | 'realistic_limit'
            persist_path: where to save state JSON
        """
        self.starting_capital = float(starting_capital)
        self.slippage_bps = float(slippage_bps)
        self.limit_fill_spread_pct = float(limit_fill_spread_pct)
        self.limit_fill_min_spread = float(limit_fill_min_spread)
        self.limit_fill_near_ltp_pct = float(limit_fill_near_ltp_pct)
        self.fill_mode = fill_mode
        self.persist_path = Path(persist_path)
        self.persist_path.parent.mkdir(parents=True, exist_ok=True)

        self._lock = RLock()
        self._orders: dict[str, Order] = {}
        self._positions: dict[str, Position] = {}
        self._ticks: dict[str, Tick] = {}
        self._tick_callbacks: list[Callable[[Tick], None]] = []
        self._cash = self.starting_capital
        self._realized_pnl = 0.0
        self._connected = False

        # load state if exists
        self._load_state()

    # ------- connection (no-op) -------
    def connect(self) -> None:
        with self._lock:
            self._connected = True
            logger.info(
                f"PaperClient connected | capital=${self._cash:,.2f} | "
                f"orders={len(self._orders)} | positions={len(self._positions)}"
            )

    def disconnect(self) -> None:
        with self._lock:
            self._connected = False
            self._save_state()
            logger.info("PaperClient disconnected")

    def is_connected(self) -> bool:
        return self._connected

    # ------- order management -------
    def place_order(self, order: Order) -> Order:
        """Place an order. `bracket` argument from the Kotak API is intentionally
        omitted here — crypto options paper trading doesn't use server-side brackets."""
        with self._lock:
            if not self._connected:
                raise RuntimeError("PaperClient not connected — call connect() first")
            order.order_id = f"PAPER-{uuid.uuid4().hex[:10].upper()}"
            order.placed_at = datetime.now(timezone.utc)
            order.status = OrderStatus.OPEN
            self._orders[order.order_id] = order
            logger.info(
                f"[PAPER] PLACE {order.order_id} {order.side.value} {order.qty}x{order.symbol} "
                f"{order.order_type.value} @ {order.price} (tag={order.tag})"
            )
            # attempt immediate fill against the cached tick
            self._try_fill(order)
            # market_like mode: if still open, force fill so the strategy
            # loop can validate the math end-to-end.
            if order.status == OrderStatus.OPEN and self.fill_mode == "market_like":
                self._force_fill_market_like(order)
            self._save_state()
            return order

    def _force_fill_market_like(self, order: Order) -> None:
        """Force-fill a still-open order in market_like mode.

        Fallback chain (in order):
          1. Cached tick for the option symbol
          2. Order's limit price (if set)
          3. Order's expected_fill_price (if set)
          4. Underlying's last-known spot * 0.5% (ATM-ish estimate for crypto options)
          5. $1.00 (last resort — never skip a fill in paper mode)
        """
        tick = self._ticks.get(order.symbol)
        if tick is not None and tick.ltp > 0:
            ref_price = tick.ltp
        elif order.price and order.price > 0:
            ref_price = order.price
        elif order.expected_fill_price and order.expected_fill_price > 0:
            ref_price = order.expected_fill_price
        else:
            # Fallback 4: ATM-ish estimate from underlying's spot.
            underlying = (order.underlying or "").upper()
            underlying_ltp = 0.0
            if underlying:
                # Spot ticks use the bare symbol ("BTC" / "ETH")
                spot_tick = self._ticks.get(underlying)
                if spot_tick and spot_tick.ltp > 0:
                    underlying_ltp = spot_tick.ltp
            if underlying_ltp > 0:
                ref_price = round(underlying_ltp * 0.005, 4)  # ~0.5% of spot
                logger.debug(
                    f"[PAPER] FORCE_FILL underlying-derived ref for {order.order_id} "
                    f"{order.symbol}: underlying={underlying} ltp={underlying_ltp} -> ref={ref_price}"
                )
            else:
                # Fallback 5: last-resort synthetic $1.00. Never skip a fill in paper mode.
                ref_price = 1.0
                logger.warning(
                    f"[PAPER] FORCE_FILL last-resort ref for {order.order_id} {order.symbol}: "
                    f"no tick, no price, no expected_fill_price, no underlying — using $1.00 "
                    f"(check that {order.underlying} spot feed is alive)"
                )
        slip = ref_price * (self.slippage_bps / 10_000)
        fill_price = ref_price + (slip if order.side == OrderSide.BUY else -slip)
        fill_price = round(fill_price, 4)
        order.expected_fill_price = ref_price
        order.avg_fill_price = fill_price
        order.filled_qty = order.qty
        order.status = OrderStatus.COMPLETE
        order.filled_at = datetime.now(timezone.utc)
        self._apply_fill(order)
        logger.info(
            f"[PAPER] FORCE_FILL (market_like) {order.order_id} {order.qty}x{order.symbol} "
            f"@ {order.avg_fill_price} (ref={ref_price}, mode=market_like)"
        )

    def modify_order(self, order_id: str, **kwargs) -> Order:
        with self._lock:
            order = self._orders.get(order_id)
            if not order:
                raise KeyError(f"Order {order_id} not found")
            if order.status in (OrderStatus.COMPLETE, OrderStatus.CANCELLED, OrderStatus.REJECTED):
                raise ValueError(f"Cannot modify {order.status.value} order")
            for k, v in kwargs.items():
                if hasattr(order, k):
                    setattr(order, k, v)
            logger.info(f"[PAPER] MODIFY {order_id} {kwargs}")
            self._try_fill(order)
            self._save_state()
            return order

    def cancel_order(self, order_id: str) -> Order:
        with self._lock:
            order = self._orders.get(order_id)
            if not order:
                raise KeyError(f"Order {order_id} not found")
            if order.status == OrderStatus.COMPLETE:
                raise ValueError("Cannot cancel filled order")
            order.status = OrderStatus.CANCELLED
            logger.info(f"[PAPER] CANCEL {order_id}")
            self._save_state()
            return order

    def get_order(self, order_id: str) -> Optional[Order]:
        with self._lock:
            return self._orders.get(order_id)

    def get_positions(self) -> list[Position]:
        with self._lock:
            for pos in self._positions.values():
                tick = self._ticks.get(pos.symbol)
                if tick:
                    pos.ltp = tick.ltp
                    pos.pnl = (pos.ltp - pos.avg_price) * pos.qty * pos.contract_size
            return list(self._positions.values())

    def get_holdings(self) -> list[Position]:
        return []  # paper has no delivery holdings

    def get_margins(self) -> dict:
        with self._lock:
            used = sum(abs(p.qty) * p.ltp * p.contract_size for p in self._positions.values())
            return {
                "available": self._cash - used,
                "used": used,
                "total": self._cash,
                "realized_pnl": self._realized_pnl,
                "unrealized_pnl": sum(p.pnl for p in self._positions.values()),
            }

    def get_ltp(self, symbol: str, exchange: str = "DERIBIT") -> float:
        with self._lock:
            tick = self._ticks.get(symbol)
            return tick.ltp if tick else 0.0

    def subscribe(self, symbols: list[str], exchange: str = "DERIBIT") -> None:
        logger.info(f"[PAPER] subscribe {len(symbols)} symbols")

    def on_tick(self, callback: Callable[[Tick], None]) -> None:
        self._tick_callbacks.append(callback)

    def inject_tick(self, tick: Tick) -> None:
        """Feed a real tick into the paper book. Public for the data feed."""
        with self._lock:
            self._ticks[tick.symbol] = tick
            pos = self._positions.get(tick.symbol)
            if pos:
                pos.ltp = tick.ltp
                pos.pnl = (pos.ltp - pos.avg_price) * pos.qty * pos.contract_size
            for order in self._orders.values():
                if order.status == OrderStatus.OPEN and order.symbol == tick.symbol:
                    self._try_fill(order)
            # market_like mode: force-fill any still-open order on EVERY tick.
            if self.fill_mode == "market_like":
                for order in list(self._orders.values()):
                    if order.status == OrderStatus.OPEN:
                        self._force_fill_market_like(order)
            self._save_state()
        for cb in self._tick_callbacks:
            try:
                cb(tick)
            except Exception as e:
                logger.exception(f"tick callback error: {e}")

    # ------- internals -------
    def _try_fill(self, order: Order) -> None:
        tick = self._ticks.get(order.symbol)
        if not tick:
            return
        order.expected_fill_price = tick.ltp
        fill_price = 0.0
        if order.order_type == OrderType.MARKET:
            slip = tick.ltp * (self.slippage_bps / 10_000)
            fill_price = tick.ltp + (slip if order.side == OrderSide.BUY else -slip)
        elif order.order_type == OrderType.LIMIT:
            spread = max(
                self.limit_fill_min_spread,
                tick.ltp * (self.limit_fill_spread_pct / 100.0),
            )
            synthetic_bid = tick.ltp - spread
            synthetic_ask = tick.ltp + spread
            if order.side == OrderSide.BUY:
                if order.price >= synthetic_ask:
                    fill_price = min(order.price, synthetic_ask)
                elif abs(order.price - tick.ltp) / tick.ltp < (self.limit_fill_near_ltp_pct / 100.0):
                    fill_price = order.price
            elif order.side == OrderSide.SELL:
                if order.price <= synthetic_bid:
                    fill_price = max(order.price, synthetic_bid)
                elif abs(order.price - tick.ltp) / tick.ltp < (self.limit_fill_near_ltp_pct / 100.0):
                    fill_price = order.price
        elif order.order_type == OrderType.SL:
            if (order.side == OrderSide.BUY and tick.ltp >= order.trigger_price) or \
               (order.side == OrderSide.SELL and tick.ltp <= order.trigger_price):
                fill_price = order.price if order.price > 0 else tick.ltp
        elif order.order_type == OrderType.SL_M:
            if (order.side == OrderSide.BUY and tick.ltp >= order.trigger_price) or \
               (order.side == OrderSide.SELL and tick.ltp <= order.trigger_price):
                fill_price = tick.ltp
        if fill_price > 0:
            order.avg_fill_price = round(fill_price, 4)
            order.filled_qty = order.qty
            order.status = OrderStatus.COMPLETE
            order.filled_at = datetime.now(timezone.utc)
            self._apply_fill(order)
            logger.info(
                f"[PAPER] FILL {order.order_id} {order.qty}x{order.symbol} @ {order.avg_fill_price} "
                f"(tick={tick.ltp})"
            )

    def _apply_fill(self, order: Order) -> None:
        pos = self._positions.get(order.symbol)
        fill_value = order.filled_qty * order.avg_fill_price * 1.0  # contract_size default 1
        if order.side == OrderSide.BUY:
            self._cash -= fill_value
            if pos:
                if pos.qty > 0:
                    # adding to a LONG (average up)
                    total_qty = pos.qty + order.filled_qty
                    pos.avg_price = (
                        pos.avg_price * pos.qty + order.avg_fill_price * order.filled_qty
                    ) / total_qty
                    pos.qty = total_qty
                    pos.ltp = order.avg_fill_price
                else:
                    # reducing or closing a SHORT
                    short_close = min(abs(pos.qty), order.filled_qty)
                    self._realized_pnl += (pos.avg_price - order.avg_fill_price) * short_close
                    pos.qty += order.filled_qty
                    if pos.qty == 0:
                        del self._positions[order.symbol]
                    elif pos.qty > 0:
                        pos.avg_price = order.avg_fill_price
            else:
                self._positions[order.symbol] = Position(
                    symbol=order.symbol,
                    qty=order.filled_qty,
                    avg_price=order.avg_fill_price,
                    ltp=order.avg_fill_price,
                    exchange=order.exchange,
                    strike=order.strike,
                    option_type=order.option_type,
                    expiry=order.expiry,
                    underlying=order.underlying,
                    contract_size=1.0,
                    entry_time=datetime.now(timezone.utc),
                )
        else:  # SELL
            self._cash += fill_value
            if pos:
                if pos.qty > 0:
                    close_qty = min(pos.qty, order.filled_qty)
                    self._realized_pnl += (order.avg_fill_price - pos.avg_price) * close_qty
                    pos.qty -= order.filled_qty
                    if pos.qty == 0:
                        del self._positions[order.symbol]
                    elif pos.qty < 0:
                        pos.avg_price = order.avg_fill_price
                else:
                    # adding to or closing a SHORT
                    short_close = min(abs(pos.qty), order.filled_qty)
                    self._realized_pnl += (pos.avg_price - order.avg_fill_price) * short_close
                    pos.qty -= order.filled_qty
                    if pos.qty == 0:
                        del self._positions[order.symbol]
                    elif pos.qty > 0:
                        pos.avg_price = order.avg_fill_price
            else:
                # BUG FIX 2026-XX-YY (mirrors Kotak 2026-08-10): SELL into nothing
                # must OPEN a SHORT position. Previously the SHORT was never
                # reflected in self._positions, so the close-BUY later created a
                # phantom LONG and broke reconciliation.
                self._positions[order.symbol] = Position(
                    symbol=order.symbol,
                    qty=-order.filled_qty,  # negative = short
                    avg_price=order.avg_fill_price,
                    ltp=order.avg_fill_price,
                    exchange=order.exchange,
                    strike=order.strike,
                    option_type=order.option_type,
                    expiry=order.expiry,
                    underlying=order.underlying,
                    contract_size=1.0,
                    entry_time=datetime.now(timezone.utc),
                )

    # ------- persistence -------
    def _save_state(self) -> None:
        """Persist the order book and positions to JSON. Atomic write + retry.

        FIX 2026-XX-YY (mirrors Kotak 2026-08-11): shallow-copy each object's
        __dict__ before mutating for serialization. `o.__dict__` returns a
        REFERENCE to the instance namespace, so the previous code was mutating
        the live Order/Position enums to strings every save. That broke any
        downstream code that did `if order.side == OrderSide.BUY`.
        """
        try:
            state = {
                "cash": self._cash,
                "realized_pnl": self._realized_pnl,
                "orders": {oid: copy.copy(o.__dict__) for oid, o in self._orders.items()},
                "positions": {s: copy.copy(p.__dict__) for s, p in self._positions.items()},
            }
            for oid, od in state["orders"].items():
                for k, v in list(od.items()):
                    if isinstance(v, datetime):
                        od[k] = v.isoformat()
                    elif isinstance(v, OrderStatus):
                        od[k] = v.value
                    elif isinstance(v, (OrderSide, OrderType)):
                        od[k] = v.value
            for s, pd in state["positions"].items():
                for k, v in list(pd.items()):
                    if isinstance(v, datetime):
                        pd[k] = v.isoformat()
            tmp = self.persist_path.with_suffix(".tmp")
            json_text = json.dumps(state, indent=2, default=str, ensure_ascii=False)
            for attempt in range(3):
                try:
                    tmp.write_text(json_text, encoding="utf-8")
                    if self.persist_path.exists():
                        import os
                        os.replace(tmp, self.persist_path)
                    else:
                        tmp.replace(self.persist_path)
                    return
                except (PermissionError, OSError):
                    if attempt < 2:
                        time.sleep(0.05 * (attempt + 1))
                    else:
                        try:
                            self.persist_path.write_text(json_text, encoding="utf-8")
                            return
                        except Exception:
                            raise
        except Exception as e:
            logger.warning(f"PaperClient state save failed: {e}")

    def _load_state(self) -> None:
        if not self.persist_path.exists():
            return
        try:
            state = json.loads(self.persist_path.read_text(encoding="utf-8"))
            self._cash = state.get("cash", self.starting_capital)
            self._realized_pnl = state.get("realized_pnl", 0.0)
            for oid, od in state.get("orders", {}).items():
                if "placed_at" in od and od["placed_at"]:
                    od["placed_at"] = datetime.fromisoformat(od["placed_at"])
                if "filled_at" in od and od["filled_at"]:
                    od["filled_at"] = datetime.fromisoformat(od["filled_at"])
                if "status" in od and isinstance(od["status"], str):
                    od["status"] = OrderStatus(od["status"])
                for k in ("side", "order_type"):
                    if k in od and isinstance(od[k], str):
                        if k == "side":
                            od[k] = OrderSide(od[k])
                        elif k == "order_type":
                            od[k] = OrderType(od[k])
                # ZOMBIE order cleanup: historical OPEN orders from previous days
                # were saved with price=0 (multi-leg orders that never got a real
                # limit). Cancel them on load so the bot starts clean.
                if od.get("status") == OrderStatus.OPEN and (od.get("price", 0) or 0) <= 0:
                    od["status"] = OrderStatus.CANCELLED
                    logger.info(
                        f"[PAPER] ZOMBIE_CLEAN cancelled {oid} {od.get('symbol')} "
                        f"(loaded with price=0)"
                    )
                self._orders[oid] = Order(**od)
            for s, pd in state.get("positions", {}).items():
                if "entry_time" in pd and pd["entry_time"]:
                    pd["entry_time"] = datetime.fromisoformat(pd["entry_time"])
                self._positions[s] = Position(**pd)
            logger.info(
                f"PaperClient loaded state: {len(self._orders)} orders, "
                f"{len(self._positions)} positions"
            )
        except Exception as e:
            logger.warning(f"PaperClient state load failed: {e}")

    def reset(self) -> None:
        """Wipe paper state and start fresh."""
        with self._lock:
            self._orders.clear()
            self._positions.clear()
            self._ticks.clear()
            self._cash = self.starting_capital
            self._realized_pnl = 0.0
            if self.persist_path.exists():
                self.persist_path.unlink()
            logger.info("PaperClient reset")

