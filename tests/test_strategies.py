"""Tests for the 5 strategies — eligibility + plan construction."""
from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from crypto_options_bot.strategy.base import SignalContext, StrategyName
from crypto_options_bot.strategy.iron_condor import IronCondorStrategy
from crypto_options_bot.strategy.short_strangle import ShortStrangleStrategy
from crypto_options_bot.strategy.directional_debit import DirectionalDebitStrategy
from crypto_options_bot.strategy.calendar_spread import CalendarSpreadStrategy
from crypto_options_bot.strategy.long_straddle import LongStraddleStrategy


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _ctx(
    *,
    underlying: str = "BTC",
    spot: float = 65_000.0,
    regime: str = "range",
    iv_rank: float = 60.0,
    dvol: float = 50.0,
    trend_strength: float = 0.0,
    strikes: list = None,
    option_ltps: dict = None,
    option_ivs: dict = None,
) -> SignalContext:
    strikes = strikes or [60_000.0, 62_000.0, 64_000.0, 65_000.0, 66_000.0, 68_000.0, 70_000.0]
    option_ltps = option_ltps or {
        (60_000.0, "C"): 5500.0, (60_000.0, "P"): 500.0,
        (62_000.0, "C"): 3500.0, (62_000.0, "P"): 800.0,
        (64_000.0, "C"): 1500.0, (64_000.0, "P"): 1200.0,
        (65_000.0, "C"): 800.0, (65_000.0, "P"): 850.0,
        (66_000.0, "C"): 400.0, (66_000.0, "P"): 1300.0,
        (68_000.0, "C"): 200.0, (68_000.0, "P"): 2000.0,
        (70_000.0, "C"): 80.0, (70_000.0, "P"): 3000.0,
    }
    option_ivs = option_ivs or {k: 0.65 for k in option_ltps}
    return SignalContext(
        underlying=underlying,
        spot=spot,
        dvol=dvol,
        iv_rank=iv_rank,
        adx=20.0,
        trend_strength=trend_strength,
        regime=regime,
        timestamp=datetime.now(timezone.utc),
        strikes=strikes,
        option_ltps=option_ltps,
        option_ivs=option_ivs,
    )


# ---------------------------------------------------------------------------
# Iron condor
# ---------------------------------------------------------------------------
def test_iron_condor_eligibility_range_regime():
    s = IronCondorStrategy({"min_iv_rank": 30, "max_dvol": 90})
    eligible, _ = s.is_eligible(_ctx(regime="range", iv_rank=60.0), {})
    assert eligible is True
    eligible, _ = s.is_eligible(_ctx(regime="trending", iv_rank=60.0), {})
    assert eligible is False


def test_iron_condor_eligibility_low_iv_rank():
    s = IronCondorStrategy({"min_iv_rank": 30})
    eligible, reason = s.is_eligible(_ctx(regime="range", iv_rank=10.0), {})
    assert eligible is False
    assert "iv_rank" in reason


def test_iron_condor_builds_4_legs_in_range():
    s = IronCondorStrategy({"min_iv_rank": 30, "wing_width_atm_mult": 0.05})
    ctx = _ctx(regime="range", iv_rank=70.0)
    plan = s.build_plan(ctx, {})
    assert plan is not None
    assert plan.strategy == StrategyName.IRON_CONDOR
    assert len(plan.legs) == 4
    sides = sorted([leg["side"] for leg in plan.legs])
    assert sides.count("BUY") == 2 and sides.count("SELL") == 2


# ---------------------------------------------------------------------------
# Short strangle
# ---------------------------------------------------------------------------
def test_short_strangle_eligibility():
    s = ShortStrangleStrategy({"min_iv_rank": 50, "max_dvol": 80})
    eligible, _ = s.is_eligible(_ctx(regime="range", iv_rank=60.0, dvol=50.0), {})
    assert eligible is True
    eligible, _ = s.is_eligible(_ctx(regime="trending", iv_rank=60.0), {})
    assert eligible is False
    eligible, _ = s.is_eligible(_ctx(regime="range", iv_rank=30.0, dvol=50.0), {})
    assert eligible is False
    eligible, _ = s.is_eligible(_ctx(regime="range", iv_rank=60.0, dvol=90.0), {})
    assert eligible is False


def test_short_strangle_builds_2_sells():
    s = ShortStrangleStrategy({"min_iv_rank": 50, "wing_atm_mult": 0.05})
    plan = s.build_plan(_ctx(regime="range", iv_rank=60.0), {})
    assert plan is not None
    assert len(plan.legs) == 2
    assert all(leg["side"] == "SELL" for leg in plan.legs)


# ---------------------------------------------------------------------------
# Directional debit
# ---------------------------------------------------------------------------
def test_directional_debit_picks_call_or_put():
    s = DirectionalDebitStrategy({"min_momentum": 0.02})
    ctx = _ctx(regime="trending", trend_strength=0.0)
    ctx._momentum = 0.05
    plan = s.build_plan(ctx, {"momentum": 0.05})
    assert plan is not None
    assert plan.legs[0]["opt_type"] == "C"
    # Negative momentum -> put
    plan2 = s.build_plan(ctx, {"momentum": -0.05})
    assert plan2 is not None
    assert plan2.legs[0]["opt_type"] == "P"


def test_directional_debit_rejects_range_regime():
    s = DirectionalDebitStrategy()
    ctx = _ctx(regime="range")
    eligible, _ = s.is_eligible(ctx, {"momentum": 0.05})
    assert eligible is False


# ---------------------------------------------------------------------------
# Calendar spread
# ---------------------------------------------------------------------------
def test_calendar_spread_picks_near_and_far_expiry():
    s = CalendarSpreadStrategy({"near_dte": 7, "far_dte": 28})
    ctx = _ctx(regime="range", iv_rank=30.0, trend_strength=0.0)
    plan = s.build_plan(ctx, {"near_expiry_iso": "2025-12-26",
                               "far_expiry_iso": "2026-01-30"})
    assert plan is not None
    assert len(plan.legs) == 2
    sides = sorted([leg["side"] for leg in plan.legs])
    assert sides == ["BUY", "SELL"]
    # Without near/far from the runner, the strategy should refuse
    plan2 = s.build_plan(ctx, {})
    assert plan2 is None


def test_calendar_spread_rejects_high_iv_rank():
    s = CalendarSpreadStrategy({"max_iv_rank": 50.0})
    ctx = _ctx(regime="range", iv_rank=80.0)
    eligible, _ = s.is_eligible(ctx, {})
    assert eligible is False


# ---------------------------------------------------------------------------
# Long straddle
# ---------------------------------------------------------------------------
def test_long_straddle_builds_atm_call_and_put():
    s = LongStraddleStrategy({"min_iv_rank": 30, "max_iv_rank": 90})
    ctx = _ctx(regime="range", iv_rank=50.0)
    plan = s.build_plan(ctx, {})
    assert plan is not None
    assert len(plan.legs) == 2
    opt_types = sorted([leg["opt_type"] for leg in plan.legs])
    assert opt_types == ["C", "P"]
    assert all(leg["side"] == "BUY" for leg in plan.legs)


def test_long_straddle_rejects_trending():
    s = LongStraddleStrategy()
    ctx = _ctx(regime="trending", iv_rank=50.0)
    eligible, _ = s.is_eligible(ctx, {})
    assert eligible is False

