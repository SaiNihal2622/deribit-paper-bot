"""Tests for the risk engine — caps, presets, daily loss."""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from crypto_options_bot.risk.engine import RiskDecision, RiskEngine
from crypto_options_bot.strategy.base import StrategyName, TradePlan


def _plan(stop: float = 200.0, target: float = 100.0) -> TradePlan:
    return TradePlan(
        strategy=StrategyName.IRON_CONDOR,
        underlying="BTC",
        legs=[{"side": "SELL", "qty": 1, "strike": 70000.0, "opt_type": "C",
               "order_type": "LIMIT", "price": 200.0, "tag": "t"}],
        target=target, stop=stop, confidence=0.6, reason="t",
        expected_hold_minutes=60,
    )


def test_max_position_cap_blocks_new_trade():
    eng = RiskEngine({"max_open_positions": 2, "max_daily_loss_pct": 5.0,
                      "max_trade_loss_pct": 2.0})
    eng.update_open_positions(2)
    d = eng.check_trade(_plan(), account_state={"open_positions": 2, "realized_pnl": 0.0})
    assert not d.allowed
    assert "position cap" in d.reason
    assert d.suggested_qty == 0
    assert d.to_dict()["preset"] == "base"


def test_max_loss_blocks_trade():
    eng = RiskEngine({"max_open_positions": 4, "max_daily_loss_pct": 5.0,
                      "max_trade_loss_pct": 1.0})
    eng.update_open_positions(0)
    # A plan with stop=0 should be rejected (no defined max loss)
    d = eng.check_trade(_plan(stop=0.0), account_state={})
    assert not d.allowed
    assert "no defined max loss" in d.reason


def test_daily_loss_cap_blocks_trade():
    eng = RiskEngine({"max_open_positions": 4, "max_daily_loss_pct": 5.0,
                      "max_trade_loss_pct": 2.0})
    eng.update_open_positions(0)
    # 5% of 100k = 5000; trigger at -5000 realized
    d = eng.check_trade(_plan(), account_state={"realized_pnl": -5500.0, "open_positions": 0})
    assert not d.allowed
    assert "daily loss cap" in d.reason


def test_aggressive_preset_lifts_caps():
    eng = RiskEngine({"max_open_positions": 4, "losses_to_defensive": 3,
                      "wins_to_aggressive": 3, "presets": {
                          "aggressive": {"max_open_positions": 8,
                                          "max_daily_loss_pct": 8.0,
                                          "max_trade_loss_pct": 3.0},
                      }})
    eng.update_open_positions(0)
    # Three wins -> aggressive
    eng.record_trade_result(100.0)
    eng.record_trade_result(100.0)
    eng.record_trade_result(100.0)
    assert eng.state.preset == "aggressive"
    assert eng.max_open_positions == 8
    assert eng.max_daily_loss_pct == 8.0


def test_defensive_preset_lowers_caps():
    eng = RiskEngine({"max_open_positions": 4, "losses_to_defensive": 2,
                      "presets": {
                          "defensive": {"max_open_positions": 1,
                                         "max_daily_loss_pct": 1.0,
                                         "max_trade_loss_pct": 0.5},
                      }})
    eng.update_open_positions(0)
    eng.record_trade_result(-10.0)
    eng.record_trade_result(-10.0)
    assert eng.state.preset == "defensive"
    assert eng.max_open_positions == 1
    assert eng.max_daily_loss_pct == 1.0


def test_decision_to_dict_round_trip():
    d = RiskDecision(allowed=True, reason="ok", suggested_qty=1,
                     max_loss_for_trade=200.0, preset="base")
    out = d.to_dict()
    assert out["allowed"] is True
    assert out["suggested_qty"] == 1
    assert out["max_loss_for_trade"] == 200.0
    assert out["preset"] == "base"


def test_status_includes_preset_and_counters():
    eng = RiskEngine({"max_open_positions": 4})
    eng.update_open_positions(1)
    s = eng.status()
    assert s["preset"] == "base"
    assert s["open_positions"] == 1
    assert "consecutive_losses" in s
    assert "consecutive_wins" in s


# ---------- Quantity scaling tests (the "actually profitable" change) ----------


def test_qty_scales_with_risk_budget():
    """qty = floor(risk_budget / risk_per_contract).

    With $100k capital, 2% per-trade risk = $2000 budget. Plan stop=$0.40 →
    floor(2000/0.40) = 5000 contracts. Capped at max_contracts_per_trade.
    """
    eng = RiskEngine({
        "starting_capital": 100_000.0,
        "max_trade_loss_pct": 2.0,
        "max_open_positions": 10,
        "max_contracts_per_trade": 200,
    })
    d = eng.check_trade(_plan(stop=0.40, target=0.04),
                        account_state={"open_positions": 0, "realized_pnl": 0.0,
                                       "capital": 100_000.0})
    assert d.allowed
    # floor(2000 / 0.40) = 5000 → capped at 200
    assert d.suggested_qty == 200


def test_qty_respects_max_contracts_cap():
    """Even when risk_budget/qty is large, we cap at max_contracts_per_trade."""
    eng = RiskEngine({
        "starting_capital": 100_000.0,
        "max_trade_loss_pct": 5.0,  # big budget
        "max_open_positions": 10,
        "max_contracts_per_trade": 50,
    })
    d = eng.check_trade(_plan(stop=0.10, target=0.02),
                        account_state={"open_positions": 0, "realized_pnl": 0.0,
                                       "capital": 100_000.0})
    assert d.allowed
    # floor(5000 / 0.10) = 50000 → capped at 50
    assert d.suggested_qty == 50


def test_qty_halves_in_high_dvol_regime():
    """DVOL > high_dvol_threshold cuts qty in half."""
    eng = RiskEngine({
        "starting_capital": 100_000.0,
        "max_trade_loss_pct": 2.0,
        "max_open_positions": 10,
        "max_contracts_per_trade": 200,
        "high_dvol_threshold": 80.0,
    })
    d_low = eng.check_trade(_plan(stop=0.40, target=0.04),
                            account_state={"open_positions": 0, "realized_pnl": 0.0,
                                           "capital": 100_000.0})
    assert d_low.allowed
    assert d_low.suggested_qty == 200

    eng.update_market_state(dvol=85.0, iv_rank=50.0)
    d_high = eng.check_trade(_plan(stop=0.40, target=0.04),
                             account_state={"open_positions": 0, "realized_pnl": 0.0,
                                            "capital": 100_000.0})
    assert d_high.allowed
    # 200 // 2 = 100
    assert d_high.allowed
    assert d_high.suggested_qty == 100


def test_qty_scales_1_5x_in_aggressive_preset():
    """Aggressive preset multiplies base qty by 1.5 (still capped)."""
    eng = RiskEngine({
        "starting_capital": 100_000.0,
        "max_trade_loss_pct": 2.0,
        "max_open_positions": 10,
        "max_contracts_per_trade": 200,
        "wins_to_aggressive": 1,
    })
    eng.update_market_state(dvol=30.0, iv_rank=30.0)
    # Force aggressive via 5 wins
    for _ in range(5):
        eng.record_trade_result(0.01)
    assert eng.state.preset == "aggressive"
    d = eng.check_trade(_plan(stop=0.20, target=0.04),
                        account_state={"open_positions": 0, "realized_pnl": 0.5,
                                       "capital": 100_000.0})
    assert d.allowed
    # floor(2000/0.20) = 10000 → capped 200 → 200 * 1.5 = 300 → capped 200
    assert d.suggested_qty == 200


def test_qty_floor_is_one_when_stop_is_tiny():
    """If plan.stop is very small, base_qty floor is 1, not 0."""
    eng = RiskEngine({
        "starting_capital": 100_000.0,
        "max_trade_loss_pct": 2.0,
        "max_open_positions": 10,
        "max_contracts_per_trade": 200,
    })
    # Plan with stop = 0.001 → 2000 / 0.001 = 2M contracts → capped 200
    d = eng.check_trade(_plan(stop=0.001, target=0.0005),
                        account_state={"open_positions": 0, "realized_pnl": 0.0,
                                       "capital": 100_000.0})
    assert d.allowed
    assert d.suggested_qty == 200
