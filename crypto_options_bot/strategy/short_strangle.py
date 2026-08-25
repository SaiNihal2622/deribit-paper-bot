"""Short strangle: 2-leg undefined-risk premium selling.

SELL OTM CE + SELL OTM PE, no hedge. Higher IV rank required than the
iron condor because there's no protective wing. Use only when the
strategy is highly confident the market will stay range-bound.
"""
from __future__ import annotations

from typing import Optional

from loguru import logger

from .base import BaseStrategy, SignalContext, StrategyName, TradePlan
from ._helpers import snap_strike


def _snap_strike(target: float, valid_strikes: list[float]) -> Optional[float]:
    """Snap `target` to the nearest strike in `valid_strikes`. Returns None if list empty."""
    if not valid_strikes:
        return None
    return min(valid_strikes, key=lambda k: abs(k - target))


class ShortStrangleStrategy(BaseStrategy):
    name = StrategyName.SHORT_STRANGLE

    def __init__(self, config: dict | None = None):
        """
        Args:
            config: optional dict with {short_delta, profit_target_pct, stop_loss_multiplier,
                   min_iv_rank, max_dvol, wing_atm_mult}
        """
        cfg = config or {}
        self.short_delta = float(cfg.get("short_delta", 0.20))
        self.profit_target_pct = float(cfg.get("profit_target_pct", 50))
        self.stop_loss_multiplier = float(cfg.get("stop_loss_multiplier", 2.0))
        self.wing_atm_mult = float(cfg.get("wing_atm_mult", 0.07))  # 7% of spot
        # Configurable IV rank and DVOL thresholds (was hard-coded 50 / 80)
        self.min_iv_rank = float(cfg.get("min_iv_rank", 50.0))
        self.max_dvol = float(cfg.get("max_dvol", 80.0))

    def is_eligible(self, ctx: SignalContext, account_state: dict) -> tuple[bool, str]:
        if ctx.regime != "range":
            return False, f"regime={ctx.regime} not range"
        if ctx.iv_rank < self.min_iv_rank:
            return False, f"iv_rank={ctx.iv_rank:.0f} < {self.min_iv_rank} (need higher premium)"
        if ctx.dvol and ctx.dvol > self.max_dvol:
            return False, f"dvol={ctx.dvol:.0f} > {self.max_dvol} (too volatile for undefined risk)"
        return True, "eligible"

    def build_plan(self, ctx: SignalContext, account_state: dict) -> Optional[TradePlan]:
        eligible, reason = self.is_eligible(ctx, account_state)
        if not eligible:
            return None
        if not ctx.strikes:
            return None

        spot = float(ctx.spot)
        atm = min(ctx.strikes, key=lambda k: abs(k - spot))
        wing = max(50, int(round(spot * self.wing_atm_mult)))
        sc_strike = _snap_strike(atm + wing, ctx.strikes)
        sp_strike = _snap_strike(atm - wing, ctx.strikes)
        if not (sc_strike and sp_strike):
            return None

        sc = ctx.option_ltps.get((sc_strike, "C"), 0.0)
        sp = ctx.option_ltps.get((sp_strike, "P"), 0.0)
        if min(sc, sp) <= 0:
            logger.debug(
                f"short_strangle: missing option LTP sc={sc} (K={sc_strike}) sp={sp} (K={sp_strike})"
            )
            return None

        net_credit = sc + sp
        stop = net_credit * 2.0 * self.stop_loss_multiplier

        return TradePlan(
            strategy=self.name,
            underlying=ctx.underlying,
            legs=[
                {"side": "SELL", "qty": 1, "strike": sc_strike, "opt_type": "C",
                 "order_type": "LIMIT", "price": sc, "tag": f"ss_{ctx.underlying}_sc"},
                {"side": "SELL", "qty": 1, "strike": sp_strike, "opt_type": "P",
                 "order_type": "LIMIT", "price": sp, "tag": f"ss_{ctx.underlying}_sp"},
            ],
            target=net_credit * (self.profit_target_pct / 100.0),
            stop=stop,
            confidence=0.5,
            reason=(
                f"short strangle: range + high IV (iv_rank={ctx.iv_rank:.0f}), "
                f"credit={net_credit:.4f}"
            ),
            expected_hold_minutes=180,
        )
