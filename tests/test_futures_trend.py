"""test_futures_trend.py — verify FuturesTrendStrategy.

The futures trend strategy is the leveraged directional play:
  - Long or short BTC/ETH perp on momentum signals
  - ATR-based stops (defined risk per contract)
  - 3:1 R:R target (defined reward per contract)
  - Win rate needed: >25%
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from crypto_options_bot.strategy.base import SignalContext, StrategyName
from crypto_options_bot.strategy.futures_trend import FuturesTrendStrategy


def _ctx(spot=83000.0, momentum=0.01, adx=25.0, regime="trending",
         underlying="BTC") -> SignalContext:
    return SignalContext(
        underlying=underlying,
        spot=spot,
        dvol=50.0,
        iv_rank=40.0,
        adx=adx,
        trend_strength=momentum * 50.0,
        regime=regime,
        timestamp=None,
        strikes=[],
        option_ltps={},
    )


def test_long_signal_on_positive_momentum():
    """Positive momentum + trending → long futures plan."""
    s = FuturesTrendStrategy()
    ctx = _ctx(momentum=0.01, adx=25.0)
    plan = s.build_plan(ctx, account_state={"momentum": 0.01})
    assert plan is not None
    assert plan.strategy == StrategyName.FUTURES_TREND
    assert plan.underlying == "BTC"
    assert len(plan.legs) == 1
    leg = plan.legs[0]
    assert leg["side"] == "BUY"     # long
    assert leg["symbol"] == "BTC-PERP"
    assert leg["strike"] == 0.0      # no strike
    assert leg["opt_type"] is None   # no option type
    # Defined risk: stop > 0
    assert plan.stop > 0
    # Defined reward: target > stop (3:1 R:R)
    assert plan.target > plan.stop


def test_short_signal_on_negative_momentum():
    """Negative momentum → short futures plan."""
    s = FuturesTrendStrategy()
    ctx = _ctx(momentum=-0.01, adx=25.0)
    plan = s.build_plan(ctx, account_state={"momentum": -0.01})
    assert plan is not None
    leg = plan.legs[0]
    assert leg["side"] == "SELL"    # short
    assert leg["symbol"] == "BTC-PERP"


def test_no_trade_when_momentum_below_threshold():
    """Tiny momentum → skip."""
    s = FuturesTrendStrategy({"min_momentum": 0.01})
    ctx = _ctx(momentum=0.005)
    plan = s.build_plan(ctx, account_state={"momentum": 0.005})
    assert plan is None


def test_no_trade_when_adx_below_threshold():
    """Weak trend (ADX < 20) → skip (no clear directional bias)."""
    s = FuturesTrendStrategy({"min_momentum": 0.005, "min_adx": 25.0})
    ctx = _ctx(momentum=0.01, adx=15.0)
    plan = s.build_plan(ctx, account_state={"momentum": 0.01})
    assert plan is None


def test_eth_uses_correct_contract_size():
    """ETH uses 0.01 ETH per contract (vs 0.001 for BTC)."""
    s = FuturesTrendStrategy()
    ctx = _ctx(spot=2700.0, momentum=0.01, underlying="ETH")
    plan = s.build_plan(ctx, account_state={"momentum": 0.01})
    assert plan is not None
    leg = plan.legs[0]
    assert leg["contract_size"] == 0.01   # ETH-PERP contract size
    assert leg["symbol"] == "ETH-PERP"


def test_btc_uses_correct_contract_size():
    """BTC uses 0.001 BTC per contract."""
    s = FuturesTrendStrategy()
    ctx = _ctx(momentum=0.01)
    plan = s.build_plan(ctx, account_state={"momentum": 0.01})
    assert plan is not None
    assert plan.legs[0]["contract_size"] == 0.001


def test_risk_reward_is_3_to_1():
    """Target should be 3x the stop (R:R = 3:1)."""
    s = FuturesTrendStrategy({"atr_stop_mult": 2.0, "atr_target_mult": 6.0})
    ctx = _ctx(momentum=0.01)
    plan = s.build_plan(ctx, account_state={"momentum": 0.01})
    assert plan is not None
    # Target/stop should equal atr_target_mult / atr_stop_mult = 3.0
    assert abs(plan.target / plan.stop - 3.0) < 0.01


def test_no_trade_when_spot_zero():
    """Spot = 0 → skip (would divide by zero)."""
    s = FuturesTrendStrategy()
    ctx = _ctx(spot=0.0, momentum=0.01)
    plan = s.build_plan(ctx, account_state={"momentum": 0.01})
    assert plan is None


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
