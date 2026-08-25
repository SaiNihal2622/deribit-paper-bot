"""Tests for the PaperClient broker — fills, positions, persistence.

Covers:
  - market order fills at LTP with slippage
  - limit order fills when within spread
  - limit order rejected outside band
  - force-fill in market_like mode with no tick
  - position averaging on add
  - realized P&L on close
  - JSON state persistence round-trip
"""
from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from crypto_options_bot.broker.base import (
    Order,
    OrderSide,
    OrderStatus,
    OrderType,
    Tick,
)
from crypto_options_bot.broker.paper_client import PaperClient


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _make_tick(symbol: str, ltp: float, bid: float = 0.0, ask: float = 0.0) -> Tick:
    return Tick(
        symbol=symbol,
        ltp=ltp,
        bid=bid,
        ask=ask,
        volume=0,
        oi=0,
        timestamp=datetime.now(timezone.utc),
        exchange="DERIBIT",
        strike=0.0,
        option_type="C",
        expiry="2025-12-26",
        underlying="BTC",
        iv=0.5,
    )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------
def test_market_order_fills_at_ltp_with_slippage(tmp_path):
    """A market BUY fills above LTP by `slippage_bps/10000`."""
    persist = tmp_path / "paper.json"
    broker = PaperClient(
        starting_capital=10_000.0,
        slippage_bps=10.0,  # 10 bps = 0.10%
        fill_mode="aggressive_limit",  # disable market_like force-fill
        persist_path=str(persist),
    )
    broker.connect()
    broker.inject_tick(_make_tick("BTC-26DEC25-100000-C", ltp=100.0, bid=99.5, ask=100.5))
    order = Order(
        symbol="BTC-26DEC25-100000-C",
        side=OrderSide.BUY,
        qty=1,
        order_type=OrderType.MARKET,
        strike=100000.0,
        option_type="C",
        underlying="BTC",
    )
    out = broker.place_order(order)
    assert out.status == OrderStatus.COMPLETE
    # Slippage on a buy at 10 bps: fill = 100.0 * (1 + 10/10000) = 100.10
    assert abs(out.avg_fill_price - 100.10) < 1e-3
    positions = broker.get_positions()
    assert len(positions) == 1
    assert positions[0].qty == 1
    assert abs(positions[0].avg_price - 100.10) < 1e-3


def test_limit_order_fills_when_within_spread(tmp_path):
    """A limit BUY above the synthetic ask should fill immediately at min(limit, ask)."""
    persist = tmp_path / "paper.json"
    broker = PaperClient(
        starting_capital=10_000.0,
        fill_mode="aggressive_limit",
        limit_fill_spread_pct=1.0,  # 1% spread -> synthetic ask = 101 on $100
        limit_fill_min_spread=0.01,
        persist_path=str(persist),
    )
    broker.connect()
    broker.inject_tick(_make_tick("BTC-26DEC25-100000-C", ltp=100.0, bid=99.0, ask=101.0))
    order = Order(
        symbol="BTC-26DEC25-100000-C",
        side=OrderSide.BUY,
        qty=1,
        order_type=OrderType.LIMIT,
        price=102.0,  # above synthetic ask
        strike=100000.0,
        option_type="C",
        underlying="BTC",
    )
    out = broker.place_order(order)
    assert out.status == OrderStatus.COMPLETE
    # fill = min(limit, ask) = 101.0
    assert abs(out.avg_fill_price - 101.0) < 1e-3


def test_limit_order_rejected_outside_band(tmp_path):
    """A limit BUY far below bid stays open and is never force-filled in
    aggressive_limit mode."""
    persist = tmp_path / "paper.json"
    broker = PaperClient(
        starting_capital=10_000.0,
        fill_mode="aggressive_limit",
        persist_path=str(persist),
    )
    broker.connect()
    broker.inject_tick(_make_tick("BTC-26DEC25-100000-C", ltp=100.0, bid=99.0, ask=101.0))
    order = Order(
        symbol="BTC-26DEC25-100000-C",
        side=OrderSide.BUY,
        qty=1,
        order_type=OrderType.LIMIT,
        price=50.0,  # way below the bid
        strike=100000.0,
        option_type="C",
        underlying="BTC",
    )
    out = broker.place_order(order)
    assert out.status == OrderStatus.OPEN
    # And aggressive_limit doesn't force-fill — confirm no position.
    assert broker.get_positions() == []


def test_force_fill_market_like_with_no_tick(tmp_path):
    """market_like mode force-fills even with no tick (using the $1 fallback
    or the underlying-derived ref)."""
    persist = tmp_path / "paper.json"
    broker = PaperClient(
        starting_capital=10_000.0,
        fill_mode="market_like",
        persist_path=str(persist),
    )
    broker.connect()
    # Inject a spot tick so the underlying-derived ref works
    broker.inject_tick(_make_tick("BTC", ltp=60_000.0))
    order = Order(
        symbol="BTC-26DEC25-100000-C",
        side=OrderSide.BUY,
        qty=1,
        order_type=OrderType.LIMIT,
        price=0.0,  # not set — fall back to spot-derived ref
        strike=100000.0,
        option_type="C",
        underlying="BTC",
    )
    out = broker.place_order(order)
    assert out.status == OrderStatus.COMPLETE
    # Spot 60000 * 0.5% = 300.0 ref
    assert out.avg_fill_price > 0
    assert abs(out.avg_fill_price - 300.0) < 1.0


def test_position_avg_price_on_add(tmp_path):
    """Adding to a LONG position averages up the price."""
    persist = tmp_path / "paper.json"
    broker = PaperClient(starting_capital=10_000.0, persist_path=str(persist))
    broker.connect()
    broker.inject_tick(_make_tick("BTC-26DEC25-100000-C", ltp=100.0, bid=99.0, ask=101.0))
    broker.place_order(Order(
        symbol="BTC-26DEC25-100000-C", side=OrderSide.BUY, qty=1,
        order_type=OrderType.MARKET, strike=100000.0, option_type="C", underlying="BTC",
    ))
    # Move the market up, add another
    broker.inject_tick(_make_tick("BTC-26DEC25-100000-C", ltp=110.0, bid=109.0, ask=111.0))
    broker.place_order(Order(
        symbol="BTC-26DEC25-100000-C", side=OrderSide.BUY, qty=2,
        order_type=OrderType.MARKET, strike=100000.0, option_type="C", underlying="BTC",
    ))
    positions = broker.get_positions()
    assert len(positions) == 1
    pos = positions[0]
    # avg = (100*1 + 110*2) / 3 = 106.67 (with slippage in the mix; just
    # check it's between 100 and 110)
    assert 100.0 < pos.avg_price < 110.0


def test_realized_pnl_on_close(tmp_path):
    """SELLing a long position books the realized P&L."""
    persist = tmp_path / "paper.json"
    broker = PaperClient(starting_capital=10_000.0, persist_path=str(persist))
    broker.connect()
    broker.inject_tick(_make_tick("BTC-26DEC25-100000-C", ltp=100.0, bid=99.0, ask=101.0))
    broker.place_order(Order(
        symbol="BTC-26DEC25-100000-C", side=OrderSide.BUY, qty=1,
        order_type=OrderType.MARKET, strike=100000.0, option_type="C", underlying="BTC",
    ))
    # Move the market up 10%
    broker.inject_tick(_make_tick("BTC-26DEC25-100000-C", ltp=110.0, bid=109.0, ask=111.0))
    # Sell to close
    broker.place_order(Order(
        symbol="BTC-26DEC25-100000-C", side=OrderSide.SELL, qty=1,
        order_type=OrderType.MARKET, strike=100000.0, option_type="C", underlying="BTC",
    ))
    # No more positions
    assert broker.get_positions() == []
    # Realized P&L = (sell - buy) * qty ≈ 10
    margins = broker.get_margins()
    pnl = margins["realized_pnl"]
    assert 8.0 < pnl < 12.0, f"P&L {pnl} not in (8, 12)"


def test_state_persistence_round_trip(tmp_path):
    """Save and reload state — orders, positions, cash are preserved."""
    persist = tmp_path / "paper.json"
    # Round 1: place an order, save, disconnect
    b1 = PaperClient(starting_capital=10_000.0, persist_path=str(persist))
    b1.connect()
    b1.inject_tick(_make_tick("BTC-26DEC25-100000-C", ltp=100.0, bid=99.0, ask=101.0))
    b1.place_order(Order(
        symbol="BTC-26DEC25-100000-C", side=OrderSide.BUY, qty=2,
        order_type=OrderType.MARKET, strike=100000.0, option_type="C", underlying="BTC",
    ))
    b1.disconnect()
    assert persist.exists()
    # Round 2: reload
    b2 = PaperClient(starting_capital=10_000.0, persist_path=str(persist))
    b2.connect()
    positions = b2.get_positions()
    assert len(positions) == 1
    assert positions[0].qty == 2
    assert positions[0].symbol == "BTC-26DEC25-100000-C"
    margins = b2.get_margins()
    # Cash should be < starting capital (we bought something)
    assert margins["available"] < 10_000.0

