"""Iron condor: 4-leg range-bound premium selling.

SELL OTM CE + BUY further OTM CE + SELL OTM PE + BUY further OTM PE,
all same expiry. Defined risk = wing_width - net_credit. Best for
range-bound markets with elevated IV (sell premium while it's expensive).

Sparse-testnet tolerant: if a leg has no LTP we skip the strategy for
that cycle (returning None). With ``mark_price_proxy=True`` (set in
``config.settings.data.mark_price_proxy``) the limit-fill simulator will
fall back to mark-price-as-bid/ask so the trade can still fill end-to-end.
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


class IronCondorStrategy(BaseStrategy):
    name = StrategyName.IRON_CONDOR

    def __init__(self, config: dict | None = None):
        """
        Args:
            config: optional dict with keys {wing_width_atm_mult, short_delta,
                   profit_target_pct, stop_loss_multiplier, min_iv_rank}
        """
        cfg = config or {}
        self.wing_width_atm_mult = float(cfg.get("wing_width_atm_mult", 0.05))  # 5% of spot
        self.short_delta = float(cfg.get("short_delta", 0.16))
        self.profit_target_pct = float(cfg.get("profit_target_pct", 50))
        self.stop_loss_multiplier = float(cfg.get("stop_loss_multiplier", 2.0))
        # New: minimum IV rank from config (default 30 to play with mark-price proxy)
        self.min_iv_rank = float(cfg.get("min_iv_rank", 30.0))
        self.max_dvol = float(cfg.get("max_dvol", 90.0))

    def is_eligible(self, ctx: SignalContext, account_state: dict) -> tuple[bool, str]:
        if ctx.regime not in ("range",):
            return False, f"regime={ctx.regime} not range"
        if ctx.iv_rank < self.min_iv_rank:
            return False, f"iv_rank={ctx.iv_rank:.0f} < {self.min_iv_rank} (premiums too low)"
        if ctx.dvol and ctx.dvol > self.max_dvol:
            return False, f"dvol={ctx.dvol:.0f} > {self.max_dvol} (too volatile)"
        return True, "eligible"

    def build_plan(self, ctx: SignalContext, account_state: dict) -> Optional[TradePlan]:
        eligible, reason = self.is_eligible(ctx, account_state)
        if not eligible:
            return None
        if not ctx.strikes or len(ctx.strikes) < 5:
            return None

        spot = float(ctx.spot)
        atm = min(ctx.strikes, key=lambda k: abs(k - spot))
        wing = max(50, int(round(spot * self.wing_width_atm_mult)))
        short_ce = _snap_strike(atm + wing, ctx.strikes)
        short_pe = _snap_strike(atm - wing, ctx.strikes)
        long_ce = _snap_strike(short_ce + wing if short_ce else 0, ctx.strikes)
        long_pe = _snap_strike(short_pe - wing if short_pe else 0, ctx.strikes)
        if not (short_ce and short_pe and long_ce and long_pe):
            return None
        if not (long_ce > short_ce and long_pe < short_pe):
            return None

        sc = ctx.option_ltps.get((short_ce, "C"), 0.0)
        sp = ctx.option_ltps.get((short_pe, "P"), 0.0)
        lc = ctx.option_ltps.get((long_ce, "C"), 0.0)
        lp = ctx.option_ltps.get((long_pe, "P"), 0.0)
        if min(sc, sp, lc, lp) <= 0:
            logger.debug(
                f"iron_condor: missing option LTP sc={sc} sp={sp} lc={lc} lp={lp} "
                f"(short_ce={short_ce} long_ce={long_ce} short_pe={short_pe} long_pe={long_pe})"
            )
            return None

        effective_wing = long_ce - short_ce
        net_credit = (sc + sp) - (lc + lp)
        if net_credit <= 0:
            return None
        max_loss = effective_wing - net_credit
        if max_loss <= 0:
            return None

        return TradePlan(
            strategy=self.name,
            underlying=ctx.underlying,
            legs=[
                {"side": "SELL", "qty": 1, "strike": short_ce, "opt_type": "C",
                 "order_type": "LIMIT", "price": sc, "tag": f"ic_{ctx.underlying}_sc"},
                {"side": "BUY",  "qty": 1, "strike": long_ce,  "opt_type": "C",
                 "order_type": "LIMIT", "price": lc, "tag": f"ic_{ctx.underlying}_lc"},
                {"side": "SELL", "qty": 1, "strike": short_pe, "opt_type": "P",
                 "order_type": "LIMIT", "price": sp, "tag": f"ic_{ctx.underlying}_sp"},
                {"side": "BUY",  "qty": 1, "strike": long_pe,  "opt_type": "P",
                 "order_type": "LIMIT", "price": lp, "tag": f"ic_{ctx.underlying}_lp"},
            ],
            target=net_credit * (self.profit_target_pct / 100.0),
            stop=max_loss * self.stop_loss_multiplier,
            confidence=0.6,
            reason=(
                f"iron condor: range regime, iv_rank={ctx.iv_rank:.0f}, "
                f"credit={net_credit:.4f}, wing={effective_wing:.0f}, max_loss={max_loss:.4f}"
            ),
            expected_hold_minutes=240,
        )
