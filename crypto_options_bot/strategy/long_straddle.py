"""Long straddle — buy ATM call + put, same strike, same expiry.

Vol-expansion play. Buy both sides of the ATM straddle, profit if the
underlying moves far enough in either direction to cover the combined
premium. Requires elevated IV rank (so that subsequent expansion is
likely) but does NOT want a strongly trending regime (because we want
the IV to be under-priced, not already paid for).

Stop at 50% of combined premium, target at 100% of combined premium.
"""
from __future__ import annotations

from typing import Optional

from loguru import logger

from .base import BaseStrategy, SignalContext, StrategyName, TradePlan
from ._helpers import snap_strike


class LongStraddleStrategy(BaseStrategy):
    name = StrategyName.LONG_STRADDLE

    def __init__(self, config: dict | None = None):
        cfg = config or {}
        self.min_iv_rank = float(cfg.get("min_iv_rank", 30.0))
        self.max_iv_rank = float(cfg.get("max_iv_rank", 90.0))
        self.stop_multiple = float(cfg.get("stop_multiple", 0.5))
        self.target_multiple = float(cfg.get("target_multiple", 1.0))
        self.confidence = float(cfg.get("confidence", 0.5))

    # ------------------------------------------------------------------
    def is_eligible(self, ctx: SignalContext, account_state: dict) -> tuple[bool, str]:
        if ctx.regime == "trending":
            return False, f"regime={ctx.regime} (trending already pricing the move)"
        if ctx.iv_rank < self.min_iv_rank:
            return False, f"iv_rank={ctx.iv_rank:.0f} < {self.min_iv_rank}"
        if ctx.iv_rank > self.max_iv_rank:
            return False, f"iv_rank={ctx.iv_rank:.0f} > {self.max_iv_rank} (premium too rich)"
        if not ctx.strikes or len(ctx.strikes) < 3:
            return False, f"insufficient strikes: {len(ctx.strikes or [])}"
        return True, "eligible"

    # ------------------------------------------------------------------
    def build_plan(self, ctx: SignalContext, account_state: dict) -> Optional[TradePlan]:
        eligible, reason = self.is_eligible(ctx, account_state)
        if not eligible:
            return None

        spot = float(ctx.spot)
        atm = min(ctx.strikes, key=lambda k: abs(k - spot))
        strike = snap_strike(atm, ctx.strikes) or atm
        ce = float(ctx.option_ltps.get((strike, "C"), 0.0))
        pe = float(ctx.option_ltps.get((strike, "P"), 0.0))
        if min(ce, pe) <= 0:
            logger.debug(
                f"long_straddle: missing ATM LTP ce={ce} pe={pe} (K={strike}); skipping"
            )
            return None
        premium = ce + pe
        target = premium * self.target_multiple
        stop = premium * self.stop_multiple
        return TradePlan(
            strategy=self.name,
            underlying=ctx.underlying,
            legs=[
                {
                    "side": "BUY",
                    "qty": 1,
                    "strike": strike,
                    "opt_type": "C",
                    "order_type": "LIMIT",
                    "price": round(ce, 4),
                    "tag": f"ls_{ctx.underlying}_C",
                },
                {
                    "side": "BUY",
                    "qty": 1,
                    "strike": strike,
                    "opt_type": "P",
                    "order_type": "LIMIT",
                    "price": round(pe, 4),
                    "tag": f"ls_{ctx.underlying}_P",
                },
            ],
            target=round(target, 4),
            stop=round(stop, 4),
            confidence=self.confidence,
            reason=(
                f"long straddle: regime={ctx.regime}, iv_rank={ctx.iv_rank:.0f}, "
                f"ATM={strike}, premium={premium:.4f}"
            ),
            expected_hold_minutes=300,
        )
