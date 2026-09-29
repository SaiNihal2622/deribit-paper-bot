"""services/strategy/strategies/momentum_trend.py — sample momentum strategy.

This is a TEMPLATE strategy showing the new event-driven strategy interface.

Strategy interface (for the strategy service):
  - eligible(features_event) -> (bool, reason)
  - plan(features_event) -> dict | None

The strategy uses features computed by the feature engine (which subscribes to
the ingest service's tick stream). It produces a StrategyPlan that's published
to the bus and consumed by the risk service.

To add a new strategy: drop a .py file in this directory. The strategy service
will auto-discover and load it.
"""
from __future__ import annotations

from typing import Optional


class MomentumTrendStrategy:
    """Simple directional long-option strategy."""

    # Metadata exposed via class attributes — the evolver reads these.
    STRATEGY_ID = "momentum_trend_v1"
    STRATEGY_VERSION = "1.0"
    HYPOTHESIS = (
        "Short-term momentum predicts returns at the 5-min horizon when IV-rank > 30. "
        "Buy ATM calls on bullish momentum, ATM puts on bearish. "
        "Defined risk = premium paid; win rate needed ~35%."
    )

    def __init__(self):
        self.min_momentum = 0.003      # 0.3% move
        self.min_iv_rank = 25.0
        self.max_iv_rank = 80.0
        self.min_dte = 5
        self.max_dte = 21

    def eligible(self, features) -> tuple[bool, str]:
        if abs(features.momentum_20) < self.min_momentum:
            return False, f"|momentum|={abs(features.momentum_20):.4f} < {self.min_momentum}"
        if features.iv_rank < self.min_iv_rank:
            return False, f"iv_rank={features.iv_rank:.0f} < {self.min_iv_rank}"
        if features.iv_rank > self.max_iv_rank:
            return False, f"iv_rank={features.iv_rank:.0f} > {self.max_iv_rank}"
        return True, "eligible"

    def plan(self, features) -> Optional[dict]:
        eligible, _ = self.eligible(features)
        if not eligible:
            return None

        # Direction: long on bullish, short on bearish
        direction = "C" if features.momentum_20 > 0 else "P"
        # Compute strike from spot
        spot = features.spot
        strike = round(spot / 1000) * 1000  # round to nearest 1000
        # Pretend ATM long option premium estimate (paper-trading friendly)
        est_premium = max(spot * 0.001, 50.0)
        # Risk management: 1.5x debit target, 0.5x debit stop (R:R = 3:1)
        target = est_premium * 1.5
        stop = est_premium * 0.5
        return {
            "legs": [
                {
                    "side": "BUY",
                    "qty": 1,
                    "instrument": f"{features.underlying}-PERP",
                    "price": est_premium,
                    "strike": strike,
                    "option_type": direction,
                }
            ],
            "target": target,
            "stop": stop,
            "confidence": 0.5,
            "reason": (
                f"momentum_trend: momentum={features.momentum_20:+.4f}, "
                f"iv_rank={features.iv_rank:.0f}, {direction}@{strike}"
            ),
            "expected_hold_minutes": 240,
        }
