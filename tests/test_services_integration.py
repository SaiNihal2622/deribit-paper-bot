"""test_services_integration.py — verify the new event-driven architecture.

Tests the integration of:
  - InProcessBus
  - FeaturesEvent / TickEvent flow
  - Strategy service publishing StrategyPlan
  - Sample momentum strategy
"""
from __future__ import annotations

import asyncio
import sys
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from services.contracts.events import (
    TickEvent, FeaturesEvent, StrategyPlan,
    subject_features,
)
from services.contracts.bus import InProcessBus
from services.strategy.service import StrategyService

pytestmark = pytest.mark.asyncio


def _make_features(underlying="BTC", momentum=0.005, iv_rank=40):
    """Build a FeaturesEvent for tests."""
    return FeaturesEvent(
        ts_event_ms=int(time.time() * 1000),
        source="test",
        underlying=underlying,
        spot=83000.0 if underlying == "BTC" else 2700.0,
        dvol=50.0,
        iv_rank=iv_rank,
        momentum_20=momentum,
        momentum_100=momentum,
        regime="trending" if abs(momentum) > 0.005 else "range",
        adx=25.0,
        trend_strength=momentum * 50,
    )


async def test_strategy_service_loads_sample():
    """StrategyService should auto-discover the momentum_trend strategy."""
    svc = StrategyService(strategies_dir=str(ROOT / "services" / "strategy" / "strategies"))
    await svc.start()
    try:
        assert len(svc.strategies) >= 1
        names = [s.__class__.__name__ for s in svc.strategies]
        assert "MomentumTrendStrategy" in names
    finally:
        await svc.stop()


async def test_end_to_end_features_to_plan():
    """FeaturesEvent in → StrategyPlan out via the bus."""
    svc = StrategyService(strategies_dir=str(ROOT / "services" / "strategy" / "strategies"))
    await svc.start()
    try:
        plans_received = []
        async def plan_handler(event):
            plans_received.append(event)
        # Subscribe to the strategy's plan subject
        await svc.bus.subscribe(
            "strategy.momentum_trend_v1.plan",
            "test-collector",
            plan_handler,
        )

        # Feed features that should trigger the strategy
        features = _make_features(momentum=0.01, iv_rank=40)
        await svc.bus.publish(subject_features("BTC"), features)
        # Give the async dispatch a moment
        await asyncio.sleep(0.1)

        assert len(plans_received) >= 1
        plan = plans_received[0]
        assert isinstance(plan, StrategyPlan)
        assert plan.strategy_id == "momentum_trend_v1"
        assert plan.underlying == "BTC"
        assert len(plan.legs) == 1
        assert plan.legs[0]["side"] == "BUY"
        assert plan.target > 0
        assert plan.stop > 0
        assert plan.target / plan.stop >= 2.0  # should be ~3:1 R:R
    finally:
        await svc.stop()


async def test_strategy_does_not_emit_when_ineligible():
    """If momentum < threshold, strategy should not emit a plan."""
    svc = StrategyService(strategies_dir=str(ROOT / "services" / "strategy" / "strategies"))
    await svc.start()
    try:
        plans_received = []
        await svc.bus.subscribe(
            "strategy.momentum_trend_v1.plan",
            "test",
            lambda e: plans_received.append(e),
        )
        # Momentum too low
        await svc.bus.publish(
            subject_features("BTC"),
            _make_features(momentum=0.0001, iv_rank=40),
        )
        await asyncio.sleep(0.1)
        assert len(plans_received) == 0
    finally:
        await svc.stop()


async def test_strategy_eligibility_low_iv_rank():
    """If iv_rank is too low, strategy refuses."""
    from services.strategy.strategies.momentum_trend import MomentumTrendStrategy
    s = MomentumTrendStrategy()
    features = _make_features(momentum=0.01, iv_rank=10)  # below min
    eligible, reason = s.eligible(features)
    assert eligible is False
    assert "iv_rank" in reason


async def test_strategy_eligibility_bullish_emits_call():
    """Bullish momentum → buy call (long call)."""
    from services.strategy.strategies.momentum_trend import MomentumTrendStrategy
    s = MomentumTrendStrategy()
    features = _make_features(momentum=0.01, iv_rank=40)
    plan = s.plan(features)
    assert plan is not None
    assert plan["legs"][0]["option_type"] == "C"


async def test_strategy_eligibility_bearish_emits_put():
    """Bearish momentum → buy put."""
    from services.strategy.strategies.momentum_trend import MomentumTrendStrategy
    s = MomentumTrendStrategy()
    features = _make_features(momentum=-0.01, iv_rank=40)
    plan = s.plan(features)
    assert plan is not None
    assert plan["legs"][0]["option_type"] == "P"


async def test_strategy_metadata_exposed():
    """Strategies must expose STRATEGY_ID, STRATEGY_VERSION, HYPOTHESIS for the evolver."""
    from services.strategy.strategies.momentum_trend import MomentumTrendStrategy
    assert hasattr(MomentumTrendStrategy, "STRATEGY_ID")
    assert hasattr(MomentumTrendStrategy, "STRATEGY_VERSION")
    assert hasattr(MomentumTrendStrategy, "HYPOTHESIS")
    assert len(MomentumTrendStrategy.HYPOTHESIS) > 30  # not a placeholder


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
