"""Order manager — turns a TradePlan (multi-leg) into broker orders.

Mirrors the Kotak Neo bot's OrderManager but adapted for Deribit:
  - exchange = "DERIBIT"
  - symbol format = "BTC-26DEC25-100000-C" (Deribit native, not NSE-style)
  - lot sizes: Deribit doesn't use NSE-style lots; we treat each contract as
    1 unit. `contract_sizes` is a no-op here (kept for API parity).
  - No bracket orders (Deribit doesn't have server-side brackets in paper).

State is persisted to JSON so a bot restart doesn't lose the trade book.
"""
from __future__ import annotations

import json
import os
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from threading import RLock
from typing import Callable, Optional

from loguru import logger

from ..broker.base import (
    BrokerClient,
    Order,
    OrderSide,
    OrderStatus,
    OrderType,
)
from ..strategy.base import StrategyName, TradePlan


@dataclass
class ManagedTrade:
    """A multi-leg trade tracked from open to close."""

    trade_id: str = ""
    plan: Optional[TradePlan] = None
    orders: list[Order] = field(default_factory=list)
    opened_at: Optional[datetime] = None
    closed_at: Optional[datetime] = None
    realized_pnl: float = 0.0
    target_hit: bool = False
    stop_hit: bool = False
    exit_reason: str = ""
    # Derived top-level fields for fast queries
    status: str = "open"  # "open" | "closed"
    underlying: str = ""
    leg_count: int = 0
    pnl: float = 0.0
    entry_time: Optional[datetime] = None


class OrderManager:
    """Translates TradePlans into broker orders, tracks the resulting positions.

    Persistence: when `persist_path` is set, the entire _trades dict (open and
    closed) is written to JSON after every open/close event and reloaded on
    construction. This survives bot restarts.
    """

    def __init__(
        self,
        broker: BrokerClient,
        persist_path: str = "data_cache/trades_state.json",
    ):
        self.broker = broker
        self.persist_path = Path(persist_path)
        self.persist_path.parent.mkdir(parents=True, exist_ok=True)
        self._trades: dict[str, ManagedTrade] = {}
        self._symbol_to_trade: dict[str, str] = {}
        self._on_trade_event: Optional[Callable] = None
        self._lock = RLock()
        self._load_state()

    def set_event_callback(self, cb: Callable) -> None:
        self._on_trade_event = cb

    def _save_state(self) -> None:
        """Persist the entire _trades dict to JSON. Atomic write + retry."""
        with self._lock:
            try:
                for t in self._trades.values():
                    self._refresh_derived(t)
                state = {
                    "trades": {
                        tid: self._trade_to_dict(t) for tid, t in self._trades.items()
                    },
                    "symbol_to_trade": dict(self._symbol_to_trade),
                }
                tmp = self.persist_path.with_suffix(".tmp")
                tmp.write_text(
                    json.dumps(state, indent=2, default=str, ensure_ascii=False),
                    encoding="utf-8",
                )
                os.replace(tmp, self.persist_path)
            except Exception as e:
                logger.warning(f"OrderManager state save failed: {e}")

    @staticmethod
    def _refresh_derived(t: ManagedTrade) -> None:
        t.status = "closed" if t.closed_at is not None else "open"
        t.underlying = (t.plan.underlying if t.plan and getattr(t.plan, "underlying", None) else "")
        t.leg_count = len(t.orders)
        t.entry_time = t.opened_at
        t.pnl = float(t.realized_pnl or 0.0)

    def _trade_to_dict(self, t: ManagedTrade) -> dict:
        return {
            "trade_id": t.trade_id,
            "plan": self._plan_to_dict(t.plan) if t.plan else None,
            "orders": [self._order_to_dict(o) for o in t.orders],
            "opened_at": t.opened_at.isoformat() if t.opened_at else None,
            "closed_at": t.closed_at.isoformat() if t.closed_at else None,
            "realized_pnl": t.realized_pnl,
            "target_hit": t.target_hit,
            "stop_hit": t.stop_hit,
            "exit_reason": t.exit_reason,
            "status": t.status,
            "underlying": t.underlying,
            "leg_count": t.leg_count,
            "pnl": t.pnl,
            "entry_time": t.entry_time.isoformat() if t.entry_time else None,
        }

    @staticmethod
    def _plan_to_dict(plan) -> dict:
        if not plan:
            return {}
        return {
            "strategy": plan.strategy.value,
            "underlying": plan.underlying,
            "legs": list(plan.legs),
            "target": plan.target,
            "stop": plan.stop,
            "confidence": plan.confidence,
            "reason": plan.reason,
            "expiry": plan.expiry,
            "expected_hold_minutes": plan.expected_hold_minutes,
        }

    @staticmethod
    def _order_to_dict(o: Order) -> dict:
        d = o.__dict__.copy()
        for k, v in list(d.items()):
            if isinstance(v, datetime):
                d[k] = v.isoformat()
            elif hasattr(v, "value"):  # Enum
                d[k] = v.value
        return d

    def _dict_to_order(self, d: dict) -> Order:
        for k in ("side", "order_type"):
            if k in d and isinstance(d[k], str):
                if k == "side":
                    d[k] = OrderSide(d[k])
                elif k == "order_type":
                    d[k] = OrderType(d[k])
        for ts in ("placed_at", "filled_at"):
            if d.get(ts) and isinstance(d[ts], str):
                d[ts] = datetime.fromisoformat(d[ts])
        if d.get("status") and isinstance(d["status"], str):
            d["status"] = OrderStatus(d["status"])
        return Order(**d)

    def _load_state(self) -> None:
        if not self.persist_path.exists():
            return
        try:
            state = json.loads(self.persist_path.read_text(encoding="utf-8"))
            for tid, td in state.get("trades", {}).items():
                plan = None
                if td.get("plan"):
                    p = td["plan"]
                    plan = TradePlan(
                        strategy=StrategyName(p["strategy"]),
                        underlying=p.get("underlying", ""),
                        legs=p.get("legs", []),
                        target=p.get("target", 0.0),
                        stop=p.get("stop", 0.0),
                        confidence=p.get("confidence", 0.0),
                        reason=p.get("reason", ""),
                        expiry=p.get("expiry", ""),
                        expected_hold_minutes=p.get("expected_hold_minutes", 60),
                    )
                orders = [self._dict_to_order(od) for od in td.get("orders", [])]
                opened_at = (
                    datetime.fromisoformat(td["opened_at"]) if td.get("opened_at") else None
                )
                closed_at = (
                    datetime.fromisoformat(td["closed_at"]) if td.get("closed_at") else None
                )
                trade = ManagedTrade(
                    trade_id=td.get("trade_id", tid),
                    plan=plan,
                    orders=orders,
                    opened_at=opened_at,
                    closed_at=closed_at,
                    realized_pnl=td.get("realized_pnl", 0.0),
                    target_hit=td.get("target_hit", False),
                    stop_hit=td.get("stop_hit", False),
                    exit_reason=td.get("exit_reason", ""),
                    status=td.get("status") or ("closed" if closed_at is not None else "open"),
                    underlying=td.get("underlying")
                    or (plan.underlying if plan else ""),
                    leg_count=td.get("leg_count", len(orders)),
                    pnl=td.get("pnl", td.get("realized_pnl", 0.0)),
                    entry_time=(
                        datetime.fromisoformat(td["entry_time"])
                        if td.get("entry_time")
                        else opened_at
                    ),
                )
                self._trades[tid] = trade
            self._symbol_to_trade = state.get("symbol_to_trade", {})
            logger.info(
                f"OrderManager loaded state: {len(self._trades)} trades "
                f"({len([t for t in self._trades.values() if t.closed_at is None])} open)"
            )
        except Exception as e:
            logger.warning(f"OrderManager state load failed: {e}")

    # ------- public API -------
    def execute_plan(
        self,
        plan: TradePlan,
        qty: int = 1,
        expiry: str = "",
    ) -> ManagedTrade:
        """Place all legs of a plan via the broker.

        For crypto options: no lot size (each contract = 1 unit), no bracket
        orders. The `expiry` is an ISO date string ("2025-12-26") which we
        format into Deribit's DDMMMYY form.
        """
        with self._lock:
            trade_id = f"T-{uuid.uuid4().hex[:10].upper()}"
            trade = ManagedTrade(
                trade_id=trade_id,
                plan=plan,
                opened_at=datetime.now(timezone.utc),
            )
            for leg in plan.legs:
                strike = leg.get("strike", 0)
                opt_type = leg.get("opt_type", "C")
                symbol = self._format_symbol(plan.underlying, expiry, strike, opt_type)
                order = Order(
                    symbol=symbol,
                    side=OrderSide(leg["side"]),
                    qty=leg.get("qty", 1) * qty,
                    order_type=OrderType(leg.get("order_type", "LIMIT")),
                    price=leg.get("price", 0),
                    tag=leg.get("tag", trade_id),
                    exchange="DERIBIT",
                    strike=strike,
                    option_type=opt_type,
                    expiry=expiry,
                    underlying=plan.underlying,
                )
                placed = self.broker.place_order(order)
                trade.orders.append(placed)
            self._trades[trade_id] = trade
            for o in trade.orders:
                if o.symbol and o.symbol not in self._symbol_to_trade:
                    self._symbol_to_trade[o.symbol] = trade_id
            logger.info(
                f"Executed plan {trade_id}: {plan.strategy.value} "
                f"{plan.underlying} {len(plan.legs)} legs"
            )
            if self._on_trade_event:
                try:
                    self._on_trade_event("opened", trade)
                except Exception as e:
                    logger.exception(f"trade event cb: {e}")
            self._save_state()
            return trade

    def close_trade(self, trade_id: str, reason: str = "manual") -> ManagedTrade:
        with self._lock:
            trade = self._trades.get(trade_id)
            if not trade:
                raise KeyError(trade_id)
            for order in trade.orders:
                if order.status != OrderStatus.COMPLETE:
                    continue
                close_side = OrderSide.SELL if order.side == OrderSide.BUY else OrderSide.BUY
                close_order = Order(
                    symbol=order.symbol,
                    side=close_side,
                    qty=order.filled_qty,
                    order_type=OrderType.MARKET,
                    tag=f"close_{order.order_id}",
                    exchange=order.exchange,
                    strike=order.strike,
                    option_type=order.option_type,
                    expiry=order.expiry,
                    underlying=order.underlying,
                )
                self.broker.place_order(close_order)
            for s, tid in list(self._symbol_to_trade.items()):
                if tid == trade_id:
                    del self._symbol_to_trade[s]
            trade.closed_at = datetime.now(timezone.utc)
            trade.exit_reason = reason
            if self._on_trade_event:
                try:
                    self._on_trade_event("closed", trade)
                except Exception as e:
                    logger.exception(f"trade event cb: {e}")
            self._save_state()
            return trade

    def square_off_all(self, reason: str = "eod") -> int:
        closed = 0
        for tid in list(self._trades.keys()):
            trade = self._trades[tid]
            if trade.closed_at is None:
                self.close_trade(tid, reason=reason)
                closed += 1
        return closed

    def open_trades(self) -> list[ManagedTrade]:
        with self._lock:
            return [t for t in self._trades.values() if t.closed_at is None]

    def get_trade_by_symbol(self, symbol: str) -> Optional[ManagedTrade]:
        tid = self._symbol_to_trade.get(symbol)
        if not tid:
            return None
        return self._trades.get(tid)

    @staticmethod
    def _format_symbol(underlying: str, expiry: str, strike: float, opt_type: str) -> str:
        """Format a Deribit instrument name from parts.

        Example: _format_symbol("BTC", "2025-12-26", 100000, "C")
            → "BTC-26DEC25-100000-C"
        """
        if not expiry:
            # Without an expiry, fall back to bare symbol. Strategies should
            # always pass an expiry so this branch should not be hit normally.
            return f"{underlying}-{int(strike)}-{opt_type[0].upper()}"
        try:
            dt = datetime.strptime(expiry, "%Y-%m-%d")
            ddmmyy = dt.strftime("%d%b%y").upper()
        except Exception:
            # caller may have passed a DDMMMYY already
            ddmmyy = expiry
        cp = "C" if opt_type.upper().startswith("C") else "P"
        return f"{underlying}-{ddmmyy}-{int(strike)}-{cp}"

