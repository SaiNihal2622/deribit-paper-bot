"""Tests for PaperRunner._recover_orphan — startup reconciliation guard.

Scenario: a previous bot restart lost broker in-memory state, but the trade
journal (data_cache/trades_state.json) still has open trades with COMPLETE
order fills. The next bot startup should detect the drift and either warn
(--no --recover-orphan) or auto-fix (--recover-orphan).

The recovery logic derives per-symbol net qty + VWAP fill price from the
journal and writes them back into broker._positions, then _save_state()
flushes paper_state.json.
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

# Repo root on path so we can import the package without installing
REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from crypto_options_bot.broker.paper_client import PaperClient  # noqa: E402
from crypto_options_bot.execution.order_manager import OrderManager  # noqa: E402


class _Shim:
    """Minimal shim — PaperRunner.__init__ has too many side effects
    (logger, dashboard, alerter, Trader init) to instantiate in tests.
    _recover_orphan only depends on self, so we skip the base class."""

    def _recover_orphan(self, broker, order_mgr):
        from crypto_options_bot.__main__ import PaperRunner
        return PaperRunner._recover_orphan(self, broker, order_mgr)


def _make_trade_state(
    trades_path: Path,
    trades: list[dict],
) -> None:
    """Write a trades_state.json with the given open trades."""
    payload = {
        "trades": {t["trade_id"]: t for t in trades},
        "symbol_to_trade": {o["symbol"]: t["trade_id"]
                            for t in trades for o in t["orders"]},
    }
    trades_path.write_text(
        json.dumps(payload, indent=2, default=str), encoding="utf-8"
    )


def _make_trade(
    tid: str,
    legs: list[dict],
    opened_at: str = "2026-09-22T16:00:00+00:00",
) -> dict:
    return {
        "trade_id": tid,
        "plan": None,
        "orders": legs,
        "opened_at": opened_at,
        "closed_at": None,
        "realized_pnl": 0.0,
        "target_hit": False,
        "stop_hit": False,
        "exit_reason": "",
        "status": "open",
        "underlying": legs[0].get("underlying", "ETH"),
        "leg_count": len(legs),
        "pnl": 0.0,
        "entry_time": opened_at,
    }


def _make_leg(symbol, side, qty, avg_price, **extra) -> dict:
    parts = symbol.split("-")  # e.g. "ETH-23SEP26-2500-C"
    underlying = parts[0]
    return {
        "symbol": symbol,
        "side": side,
        "qty": qty,
        "filled_qty": qty,
        "avg_fill_price": avg_price,
        "status": "complete",  # lowercase per OrderStatus enum
        "order_type": "LIMIT",
        "price": avg_price,
        "placed_at": "2026-09-22T16:00:00+00:00",
        "filled_at": "2026-09-22T16:00:01+00:00",
        "order_id": f"PAPER-{symbol}",
        "tag": "T-TEST",
        "exchange": "DERIBIT",
        "strike": float(parts[2]),
        "option_type": parts[3],
        "expiry": "2026-09-23",
        "underlying": underlying,
        **extra,
    }


def test_recovers_short_strangle_into_broker_positions():
    """A short-strangle = SELL put + SELL call. Both COMPLETE.
    Broker should hold 2 short positions with negative qty."""
    with tempfile.TemporaryDirectory() as tmp:
        d = Path(tmp)
        # Use absolute paths so Path(persist_path) inside OrderManager /
        # PaperClient doesn't accidentally resolve relative to cwd.
        paper_path = d / "paper_state.json"
        trades_path = d / "trades_state.json"

        # Empty broker state (orphan scenario — bot lost in-memory state)
        paper_path.write_text(
            json.dumps(
                {"cash": 100000.0, "realized_pnl": 0.0,
                 "orders": {}, "positions": {}},
                indent=2,
            ),
            encoding="utf-8",
        )

        # Journal has 1 open ETH strangle
        trade = _make_trade("T-AAA", [
            _make_leg("ETH-23SEP26-2400-P", "SELL", 1, 0.0007),
            _make_leg("ETH-23SEP26-2500-C", "SELL", 1, 0.0907),
        ])
        _make_trade_state(trades_path, [trade])

        # chdir so that the relative "data_cache/..." defaults inside
        # PaperClient/OrderManager fall inside the temp dir rather than the
        # live project's data_cache.
        old_cwd = os.getcwd()
        try:
            os.chdir(d)
            broker = PaperClient(persist_path="paper_state.json")
            broker.connect()
            assert len(broker._positions) == 0, "precondition: empty broker"

            order_mgr = OrderManager(broker, persist_path="trades_state.json")
            assert len(order_mgr.open_trades()) == 1, "precondition: 1 open trade"

            ok = _Shim()._recover_orphan(broker, order_mgr)
            assert ok is True, "_recover_orphan should return True"

            # Broker should now have 2 positions: -1 @ avg_price for each
            assert len(broker._positions) == 2, (
                f"expected 2 positions, got {len(broker._positions)}: "
                f"{list(broker._positions.keys())}"
            )

            put_pos = broker._positions["ETH-23SEP26-2400-P"]
            assert put_pos.qty == -1, f"put qty should be -1, got {put_pos.qty}"
            assert abs(put_pos.avg_price - 0.0007) < 1e-9

            call_pos = broker._positions["ETH-23SEP26-2500-C"]
            assert call_pos.qty == -1, f"call qty should be -1, got {call_pos.qty}"
            assert abs(call_pos.avg_price - 0.0907) < 1e-9

            # paper_state.json should have been persisted
            persisted = json.loads(paper_path.read_text(encoding="utf-8"))
            assert len(persisted["positions"]) == 2
        finally:
            os.chdir(old_cwd)


def test_skips_closed_trades_and_unfilled_orders():
    """Closed trades should be ignored entirely. OPEN-status orders within
    an open trade should also be ignored (not filled yet)."""
    with tempfile.TemporaryDirectory() as tmp:
        d = Path(tmp)
        paper_path = d / "paper_state.json"
        trades_path = d / "trades_state.json"
        paper_path.write_text(
            json.dumps(
                {"cash": 100000.0, "realized_pnl": 0.0,
                 "orders": {}, "positions": {}},
                indent=2,
            ),
            encoding="utf-8",
        )

        open_trade = _make_trade("T-OPEN1", [
            _make_leg("BTC-23SEP26-76000-P", "SELL", 1, 0.0003),
            # Second leg is OPEN (unfilled) — should be ignored
            {**_make_leg("BTC-23SEP26-78000-C", "SELL", 1, 0.0),
             "status": "open", "avg_fill_price": 0.0, "filled_qty": 0},
        ])
        closed_trade = _make_trade(
            "T-CLOSED1", [
                _make_leg("ETH-23SEP26-2500-C", "SELL", 1, 0.0907),
            ],
        )
        closed_trade["closed_at"] = "2026-09-22T16:30:00+00:00"
        closed_trade["status"] = "closed"

        _make_trade_state(trades_path, [open_trade, closed_trade])

        old_cwd = os.getcwd()
        try:
            os.chdir(d)
            broker = PaperClient(persist_path="paper_state.json")
            broker.connect()
            order_mgr = OrderManager(broker, persist_path="trades_state.json")

            ok = _Shim()._recover_orphan(broker, order_mgr)
            assert ok is True

            # Only 1 position (the COMPLETE put). Closed trade's call is ignored.
            # Unfilled call within open trade is ignored.
            assert len(broker._positions) == 1, (
                f"expected 1 position, got {len(broker._positions)}: "
                f"{list(broker._positions.keys())}"
            )
            assert "BTC-23SEP26-76000-P" in broker._positions
        finally:
            os.chdir(old_cwd)


def test_no_open_trades_returns_zero_positions():
    """If the journal has no open trades, the recovery is a no-op."""
    with tempfile.TemporaryDirectory() as tmp:
        d = Path(tmp)
        paper_path = d / "paper_state.json"
        trades_path = d / "trades_state.json"
        paper_path.write_text(
            json.dumps(
                {"cash": 100000.0, "realized_pnl": 0.0,
                 "orders": {}, "positions": {}},
                indent=2,
            ),
            encoding="utf-8",
        )

        # One closed trade, no opens
        closed = _make_trade("T-DONE", [
            _make_leg("ETH-23SEP26-2500-C", "SELL", 1, 0.0907),
        ])
        closed["closed_at"] = "2026-09-22T17:00:00+00:00"
        closed["status"] = "closed"
        _make_trade_state(trades_path, [closed])

        old_cwd = os.getcwd()
        try:
            os.chdir(d)
            broker = PaperClient(persist_path="paper_state.json")
            broker.connect()
            order_mgr = OrderManager(broker, persist_path="trades_state.json")

            ok = _Shim()._recover_orphan(broker, order_mgr)
            assert ok is True
            assert len(broker._positions) == 0
        finally:
            os.chdir(old_cwd)


def test_missing_journal_returns_false():
    """If trades_state.json doesn't exist, recovery can't proceed."""
    with tempfile.TemporaryDirectory() as tmp:
        d = Path(tmp)
        paper_path = d / "paper_state.json"
        paper_path.write_text(
            json.dumps(
                {"cash": 100000.0, "realized_pnl": 0.0,
                 "orders": {}, "positions": {}},
                indent=2,
            ),
            encoding="utf-8",
        )

        old_cwd = os.getcwd()
        try:
            os.chdir(d)
            broker = PaperClient(persist_path="paper_state.json")
            broker.connect()
            order_mgr = OrderManager(
                broker, persist_path="trades_state.json"
            )

            ok = _Shim()._recover_orphan(broker, order_mgr)
            assert ok is False, (
                "missing journal should return False (graceful skip)"
            )
        finally:
            os.chdir(old_cwd)


if __name__ == "__main__":
    import pytest
    sys.exit(pytest.main([__file__, "-v"]))
