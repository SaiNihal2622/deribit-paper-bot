"""test_debit_spread.py — verify DebitSpreadStrategy.

The long call/put spread is the "low risk, high profit" options structure:
  - Max loss = debit paid (defined)
  - Max profit = (spread_width - debit) × qty (defined, capped)
  - Typical R:R = 3:1 to 5:1
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from crypto_options_bot.strategy.base import SignalContext, StrategyName
from crypto_options_bot.strategy.debit_spread import DebitSpreadStrategy


def _ctx(spot=83000.0, momentum=0.005, iv_rank=40.0, regime="range",
         strikes=None, ltps=None) -> SignalContext:
    return SignalContext(
        underlying="BTC",
        spot=spot,
        dvol=50.0,
        iv_rank=iv_rank,
        adx=20.0,
        trend_strength=momentum * 50.0,
        regime=regime,
        timestamp=None,
        strikes=strikes or [80000, 81000, 82000, 83000, 84000, 85000, 86000],
        option_ltps=ltps or {
            # Calls: deeper OTM strikes (higher) → LESS premium
            (83000, "C"): 0.05,
            (84000, "C"): 0.03,
            (85000, "C"): 0.02,
            (86000, "C"): 0.01,
            (82000, "C"): 0.08,
            (81000, "C"): 0.12,
            (80000, "C"): 0.16,
            # Puts: deeper OTM strikes (lower) → LESS premium
            (83000, "P"): 0.05,
            (82000, "P"): 0.03,
            (81000, "P"): 0.015,
            (80000, "P"): 0.005,
        },
    )


def test_bull_call_spread_defined_risk():
    """Bullish momentum → buy ATM call, sell OTM call."""
    s = DebitSpreadStrategy({"min_momentum": 0.003})
    ctx = _ctx(spot=83000, momentum=0.005)
    plan = s.build_plan(ctx, account_state={"momentum": 0.005})
    assert plan is not None
    assert plan.strategy == StrategyName.DEBIT_SPREAD
    assert plan.underlying == "BTC"
    assert len(plan.legs) == 2
    # Long leg = BUY (debit)
    long_leg = next(l for l in plan.legs if l["side"] == "BUY")
    short_leg = next(l for l in plan.legs if l["side"] == "SELL")
    assert long_leg["opt_type"] == "C"
    assert short_leg["opt_type"] == "C"
    assert long_leg["strike"] < short_leg["strike"]   # long ITM-side, short OTM
    # Debit = long - short
    debit = long_leg["price"] - short_leg["price"]
    assert debit > 0
    # Defined risk: max loss = debit × qty, target capped by spread width
    assert plan.stop == round(debit * 0.5, 4)
    # R:R is the structure's whole point
    spread_width = short_leg["strike"] - long_leg["strike"]
    max_profit = spread_width - debit
    assert max_profit > 0
    # Target multiple 2.5x means we take profit before max_profit
    target_capped = min(debit * 2.5, max_profit * 0.9)
    assert plan.target == round(target_capped, 4)


def test_bear_put_spread_on_downward_momentum():
    """Negative momentum → buy ATM put, sell OTM put."""
    s = DebitSpreadStrategy({"min_momentum": 0.003})
    ctx = _ctx(spot=83000, momentum=-0.005)
    plan = s.build_plan(ctx, account_state={"momentum": -0.005})
    assert plan is not None
    long_leg = next(l for l in plan.legs if l["side"] == "BUY")
    short_leg = next(l for l in plan.legs if l["side"] == "SELL")
    assert long_leg["opt_type"] == "P"
    assert short_leg["opt_type"] == "P"
    # Bear put spread: long strike > short strike (closer to money)
    assert long_leg["strike"] > short_leg["strike"]
    debit = long_leg["price"] - short_leg["price"]
    assert debit > 0


def test_no_trade_when_momentum_below_threshold():
    """Tiny momentum → skip."""
    s = DebitSpreadStrategy({"min_momentum": 0.01})
    ctx = _ctx(momentum=0.005)
    plan = s.build_plan(ctx, account_state={"momentum": 0.005})
    assert plan is None


def test_no_trade_when_missing_liquid_quotes():
    """No LTP for either strike → skip (don't illiquid-trade)."""
    s = DebitSpreadStrategy({"min_momentum": 0.003})
    ctx = _ctx(
        momentum=0.005,
        ltps={(83000, "C"): 0.0, (84000, "C"): 0.0},
    )
    plan = s.build_plan(ctx, account_state={"momentum": 0.005})
    assert plan is None


def test_no_trade_when_iv_rank_out_of_range():
    """Too-rich IV → skip (don't pay 30%+ premium for a spread)."""
    s = DebitSpreadStrategy({"min_momentum": 0.003, "max_iv_rank": 60.0})
    ctx = _ctx(momentum=0.005, iv_rank=70.0)
    plan = s.build_plan(ctx, account_state={"momentum": 0.005})
    assert plan is None


def test_no_trade_when_iv_rank_too_low():
    """Compressed vol → skip (debit spreads need vol to be at least medium)."""
    s = DebitSpreadStrategy({"min_momentum": 0.003, "min_iv_rank": 20.0})
    ctx = _ctx(momentum=0.005, iv_rank=10.0)
    plan = s.build_plan(ctx, account_state={"momentum": 0.005})
    assert plan is None


def test_defined_risk_always_loss_capped_by_debit():
    """The trade-plan's stop is ALWAYS <= debit (max loss = debit)."""
    s = DebitSpreadStrategy({"min_momentum": 0.003, "stop_multiple": 0.5})
    ctx = _ctx(spot=83000, momentum=0.005)
    plan = s.build_plan(ctx, account_state={"momentum": 0.005})
    assert plan is not None
    long_leg = next(l for l in plan.legs if l["side"] == "BUY")
    short_leg = next(l for l in plan.legs if l["side"] == "SELL")
    debit = long_leg["price"] - short_leg["price"]
    # stop = debit × stop_multiple (0.5 by default)
    # The stop is a "take action at this loss" level — it's well below debit,
    # so max realistic loss is the debit itself (if option goes to 0).
    assert plan.stop <= debit


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
