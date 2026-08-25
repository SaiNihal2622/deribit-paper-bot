"""Tests for the target/stop monitor (in __main__.PaperRunner._monitor_targets_stops).

We exercise the monitor indirectly by constructing a PaperRunner, opening a
trade, manually manipulating the position P&L, and then calling
_monitor_targets_stops to check the close happens (or doesn't).
"""
from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from crypto_options_bot.broker.base import OrderSide, OrderType, Tick
from crypto_options_bot.broker.paper_client import PaperClient
from crypto_options_bot.execution.order_manager import OrderManager
from crypto_options_bot.strategy.base import StrategyName, TradePlan
from crypto_options_bot.__main__ import PaperRunner


def _make_tick(symbol: str, ltp: float, bid: float = 0.0, ask: float = 0.0) -> Tick:
    return Tick(
        symbol=symbol, ltp=ltp, bid=bid, ask=ask,
        volume=0, oi=0, timestamp=datetime.now(timezone.utc), exchange="DERIBIT",
        strike=0.0, option_type="C", expiry="2025-12-26", underlying="BTC",
        iv=0.5,
    )


def _plan(target: float, stop: float) -> TradePlan:
    return TradePlan(
        strategy=StrategyName.LONG_STRADDLE,
        underlying="BTC",
        legs=[
            {"side": "BUY", "qty": 1, "strike": 65000.0, "opt_type": "C",
             "order_type": "MARKET", "price": 500.0, "tag": "test_c"},
            {"side": "BUY", "qty": 1, "strike": 65000.0, "opt_type": "P",
             "order_type": "MARKET", "price": 500.0, "tag": "test_p"},
        ],
        target=target, stop=stop, confidence=0.6, reason="t",
        expected_hold_minutes=60,
    )


def _make_runner(tmp_path) -> tuple:
    """Build a minimal PaperRunner without starting a feed."""
    cfg = {
        "broker": {"paper_capital": 100_000.0, "fill_mode": "market_like",
                   "persist_path": str(tmp_path / "p.json")},
        "data": {"mark_price_proxy": True, "min_iv_rank_to_trade": 30.0},
        "strategy": {"cooldown_sec": 0},
        "risk": {"max_open_positions": 5, "max_daily_loss_pct": 5.0, "max_trade_loss_pct": 5.0},
    }
    runner = PaperRunner(cfg, mode="paper", verbose=False)
    broker = PaperClient(starting_capital=100_000.0, persist_path=str(tmp_path / "p.json"))
    broker.connect()
    om = OrderManager(broker, persist_path=str(tmp_path / "t.json"))
    return runner, broker, om


def test_target_hit_closes_trade(tmp_path):
    runner, broker, om = _make_runner(tmp_path)
    trade = om.execute_plan(_plan(target=50.0, stop=1000.0), qty=1, expiry="2025-12-26")
    # Force positive P&L by injecting a tick with a high LTP (we own a long)
    broker.inject_tick(_make_tick("BTC-26DEC25-65000-C", ltp=600.0, bid=599.0, ask=601.0))
    broker.inject_tick(_make_tick("BTC-26DEC25-65000-P", ltp=550.0, bid=549.0, ask=551.0))
    # Now the leg PnL is (600-500) + (550-500) = 100 + 50 = 150. Target=50 → hit.
    runner._monitor_targets_stops(broker, om)
    assert trade.closed_at is not None
    assert trade.exit_reason == "target_hit"


def test_stop_hit_closes_trade(tmp_path):
    runner, broker, om = _make_runner(tmp_path)
    trade = om.execute_plan(_plan(target=10_000.0, stop=200.0), qty=1, expiry="2025-12-26")
    # Force big negative P&L — the leg dropped a lot
    broker.inject_tick(_make_tick("BTC-26DEC25-65000-C", ltp=100.0, bid=99.0, ask=101.0))
    broker.inject_tick(_make_tick("BTC-26DEC25-65000-P", ltp=50.0, bid=49.0, ask=51.0))
    # PnL = (100-500) + (50-500) = -400 - 450 = -850. Stop=200 → hit (pnl < -stop).
    runner._monitor_targets_stops(broker, om)
    assert trade.closed_at is not None
    assert trade.exit_reason == "stop_hit"


def test_partial_pnl_does_not_close(tmp_path):
    runner, broker, om = _make_runner(tmp_path)
    trade = om.execute_plan(_plan(target=10_000.0, stop=10_000.0), qty=1, expiry="2025-12-26")
    # Tiny move, well inside the band
    broker.inject_tick(_make_tick("BTC-26DEC25-65000-C", ltp=520.0, bid=519.0, ask=521.0))
    broker.inject_tick(_make_tick("BTC-26DEC25-65000-P", ltp=510.0, bid=509.0, ask=511.0))
    # PnL ≈ 30. Target 10000, stop 10000 — neither hit.
    runner._monitor_targets_stops(broker, om)
    assert trade.closed_at is None
    # Trade is still open
    assert om.open_trades()

