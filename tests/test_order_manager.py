"""Tests for the OrderManager — plan execution, close, square-off, persistence."""
from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from crypto_options_bot.broker.base import OrderSide, OrderType, Tick
from crypto_options_bot.broker.paper_client import PaperClient
from crypto_options_bot.execution.order_manager import OrderManager
from crypto_options_bot.strategy.base import StrategyName, TradePlan


def _make_tick(symbol: str, ltp: float, bid: float = 0.0, ask: float = 0.0) -> Tick:
    return Tick(
        symbol=symbol, ltp=ltp, bid=bid, ask=ask,
        volume=0, oi=0, timestamp=datetime.now(timezone.utc), exchange="DERIBIT",
        strike=0.0, option_type="C", expiry="2025-12-26", underlying="BTC",
        iv=0.5,
    )


def _iron_condor_plan() -> TradePlan:
    return TradePlan(
        strategy=StrategyName.IRON_CONDOR,
        underlying="BTC",
        legs=[
            {"side": "SELL", "qty": 1, "strike": 70000.0, "opt_type": "C",
             "order_type": "LIMIT", "price": 200.0, "tag": "ic_sc"},
            {"side": "BUY",  "qty": 1, "strike": 72000.0, "opt_type": "C",
             "order_type": "LIMIT", "price": 100.0, "tag": "ic_lc"},
            {"side": "SELL", "qty": 1, "strike": 60000.0, "opt_type": "P",
             "order_type": "LIMIT", "price": 200.0, "tag": "ic_sp"},
            {"side": "BUY",  "qty": 1, "strike": 58000.0, "opt_type": "P",
             "order_type": "LIMIT", "price": 100.0, "tag": "ic_lp"},
        ],
        target=100.0, stop=1000.0, confidence=0.6, reason="test",
        expected_hold_minutes=60,
    )


def _seed_broker(broker: PaperClient) -> None:
    """Inject ticks for every leg so the force-fill works."""
    for sym in ("BTC-26DEC25-70000-C", "BTC-26DEC25-72000-C",
                "BTC-26DEC25-60000-P", "BTC-26DEC25-58000-P"):
        broker.inject_tick(_make_tick(sym, ltp=150.0, bid=149.0, ask=151.0))


def test_execute_plan_places_all_legs(tmp_path):
    broker = PaperClient(starting_capital=100_000.0, persist_path=str(tmp_path / "p.json"))
    broker.connect()
    _seed_broker(broker)
    om = OrderManager(broker, persist_path=str(tmp_path / "t.json"))
    plan = _iron_condor_plan()
    trade = om.execute_plan(plan, qty=1, expiry="2025-12-26")
    assert trade.trade_id
    assert len(trade.orders) == 4
    # All orders are either COMPLETE or OPEN; check they have order_ids.
    for o in trade.orders:
        assert o.order_id
        assert o.symbol.startswith("BTC-26DEC25-")


def test_close_trade_closes_all_legs(tmp_path):
    broker = PaperClient(starting_capital=100_000.0, persist_path=str(tmp_path / "p.json"))
    broker.connect()
    _seed_broker(broker)
    om = OrderManager(broker, persist_path=str(tmp_path / "t.json"))
    trade = om.execute_plan(_iron_condor_plan(), qty=1, expiry="2025-12-26")
    om.close_trade(trade.trade_id, reason="manual")
    # After close, the trade has closed_at set
    assert trade.closed_at is not None
    assert trade.exit_reason == "manual"


def test_square_off_all_closes_everything(tmp_path):
    broker = PaperClient(starting_capital=100_000.0, persist_path=str(tmp_path / "p.json"))
    broker.connect()
    _seed_broker(broker)
    om = OrderManager(broker, persist_path=str(tmp_path / "t.json"))
    om.execute_plan(_iron_condor_plan(), qty=1, expiry="2025-12-26")
    om.execute_plan(_iron_condor_plan(), qty=1, expiry="2025-12-26")
    assert len(om.open_trades()) == 2
    n_closed = om.square_off_all(reason="eod")
    assert n_closed == 2
    assert om.open_trades() == []


def test_persistence_round_trip(tmp_path):
    p = tmp_path / "p.json"
    t = tmp_path / "t.json"
    broker1 = PaperClient(starting_capital=100_000.0, persist_path=str(p))
    broker1.connect()
    _seed_broker(broker1)
    om1 = OrderManager(broker1, persist_path=str(t))
    trade1 = om1.execute_plan(_iron_condor_plan(), qty=1, expiry="2025-12-26")
    broker1.disconnect()
    # Re-open
    broker2 = PaperClient(starting_capital=100_000.0, persist_path=str(p))
    broker2.connect()
    om2 = OrderManager(broker2, persist_path=str(t))
    open_t = om2.open_trades()
    assert len(open_t) == 1
    assert open_t[0].trade_id == trade1.trade_id
    assert len(open_t[0].orders) == 4

