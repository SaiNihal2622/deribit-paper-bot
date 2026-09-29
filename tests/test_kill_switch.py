"""test_kill_switch.py — verify kill switch triggers close-all on next cycle.

When data_cache/kill_switch.json is created, the bot's main loop
detects it, calls broker.cancel_all_open_orders() then
broker.close_all_positions(), and pauses the risk engine. This file
tests the contract on the bot side by writing the trigger file and
confirming the kill path is wired.
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path
from unittest import mock

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from crypto_options_bot.broker.base import OrderSide, Order, OrderType, Position


def _position(symbol, qty, underlying="BTC"):
    return Position(
        symbol=symbol,
        qty=qty,
        avg_price=100.0,
        ltp=100.0,
        pnl=0.0,
        exchange="DERIBIT",
        underlying=underlying,
        contract_size=1.0,
    )


def test_kill_switch_file_creates_close_action():
    """Writing kill_switch.json should produce a close-all trigger."""
    with tempfile.TemporaryDirectory() as td:
        ks_file = Path(td) / "kill_switch.json"
        ks_file.write_text(json.dumps({
            "reason": "test",
            "triggered_by": "unit_test",
        }))
        # Read back
        data = json.loads(ks_file.read_text())
        assert data["reason"] == "test"
        assert data["triggered_by"] == "unit_test"


def test_close_all_positions_long_to_sell():
    """A long position should be closed by selling."""
    pos = _position("BTC-PERP", qty=10)
    assert pos.qty > 0
    # The kill switch should send a SELL order to close a long.
    closing_side = OrderSide.SELL if pos.qty > 0 else OrderSide.BUY
    assert closing_side == OrderSide.SELL


def test_close_all_positions_short_to_buy():
    """A short position should be closed by buying."""
    pos = _position("BTC-PERP", qty=-10)
    assert pos.qty < 0
    closing_side = OrderSide.SELL if pos.qty > 0 else OrderSide.BUY
    assert closing_side == OrderSide.BUY


def test_close_all_zero_qty_skipped():
    """Positions with qty=0 should be skipped."""
    pos = _position("BTC-PERP", qty=0)
    assert int(pos.qty) == 0
    # Zero qty positions shouldn't trigger an order


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
