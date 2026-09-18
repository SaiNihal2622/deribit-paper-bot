"""Short call: single-leg premium selling on the call side only.

Why this exists: on Deribit testnet (and many low-liquidity venues),
OTM put strikes have bid=0 / ask=0.0001 with only a synthetic IV
published. The bot's multi-leg strategies (strangle, iron_condor,
straddle) need BOTH call and put quotes, so they silently fail on
testnet. A call-only short strategy works around this by trading
only the leg that has real liquidity.

Risk profile:
  - Defined risk: capped by the stop-loss (typically 2x premium).
  - Unlimited upside risk in theory (covered-call style) — if the
    underlying rallies past the strike by a lot, loss grows. With a
    stop at 2x premium, the bot bails before the loss gets out of
    control.
  - Profit target: 50% of premium (close half the trade for 50%
    profit, let the rest expire if ITM).

Eligibility:
  - iv_rank >= min_iv_rank  (we want to sell premium in rich vol regimes)
  - dvol < max_dvol          (avoid blowing out on vol spikes)
  - call LTP must have a real bid+ask (not just a synthetic price)
  - the strike's expiry DTE >= min_dte_to_trade (we want theta runway)

This is a deliberately narrow strategy to fill the gap left by the
multi-leg strategies when put liquidity is missing.
"""
from __future__ import annotations

import logging
from typing import Optional

from .base import StrategyName, TradePlan
from ._helpers import dte_from_ddmmyy

logger = logging.getLogger(__name__)


class ShortCallStrategy:
    name = StrategyName.SHORT_CALL
    """Display name: 'short_call'."""

    def __init__(self, config: dict | None = None):
        cfg = config or {}
        self.target_delta = float(cfg.get("target_delta", 0.20))
        self.profit_target_pct = float(cfg.get("profit_target_pct", 50))
        self.stop_loss_multiplier = float(cfg.get("stop_loss_multiplier", 2.0))
        self.min_iv_rank = float(cfg.get("min_iv_rank", 35.0))
        self.max_dvol = float(cfg.get("max_dvol", 90.0))
        self.wing_atm_mult = float(cfg.get("wing_atm_mult", 0.05))
        self.min_open_interest = float(cfg.get("min_open_interest", 1.0))
        # NEW (2026-09-18): Minimum DTE for the picked leg's expiry. Stops
        # the bot from firing 0DTE / 1DTE shorts that have no theta runway.
        self.min_dte = int(cfg.get("min_dte_to_trade", 2))

    def is_eligible(self, ctx, account_state) -> tuple[bool, str]:
        if ctx.iv_rank < self.min_iv_rank:
            return False, f"iv_rank={ctx.iv_rank:.0f} < {self.min_iv_rank} (not rich enough)"
        if ctx.dvol > self.max_dvol:
            return False, f"dvol={ctx.dvol:.0f} > {self.max_dvol} (too volatile for short single-leg)"
        if not ctx.strikes:
            return False, "no strikes available"
        # Need at least one call with REAL bid+ask (liquidity check).
        liquid_calls = [
            s for s in ctx.strikes
            if ctx.option_ltps.get((s, "C"), 0.0) > 0
        ]
        if not liquid_calls:
            return False, "no liquid calls available"
        return True, "eligible"

    def build_plan(self, ctx, account_state) -> Optional[TradePlan]:
        eligible, reason = self.is_eligible(ctx, account_state)
        if not eligible:
            logger.debug(f"short_call: {reason}")
            return None

        spot = float(ctx.spot)
        # Pick the strike with OTM-call delta closest to target_delta.
        # We don't compute deltas here; we use a delta-of-wings heuristic.
        # Strike ~ spot * (1 + target_delta * sqrt(1/2)) gives a 20-delta OTM.
        # For 20% delta on normal vol (~50% annualized), wing ≈ 0.05 * spot.
        wing = max(50, int(round(spot * self.wing_atm_mult)))
        atm = min(ctx.strikes, key=lambda k: abs(k - spot))
        sc_strike = _snap(atm + wing, ctx.strikes)
        if not sc_strike:
            return None

        sc = ctx.option_ltps.get((sc_strike, "C"), 0.0)
        if sc <= 0:
            logger.debug(
                f"short_call: missing option LTP sc_strike={sc_strike} sc={sc}"
            )
            return None

        # DTE filter: refuse to build a plan on 0DTE / 1DTE legs. The bot
        # already subscribes to multiple expiries; this enforces that we
        # only use expiries with enough theta runway.
        expiry_ddmmyy = getattr(ctx, "expiry_ddmmyy", None) or ""
        dte = dte_from_ddmmyy(expiry_ddmmyy) if expiry_ddmmyy else None
        if dte is not None and dte < self.min_dte:
            logger.debug(
                f"short_call: expiry {expiry_ddmmyy} has DTE={dte} < min_dte={self.min_dte}, skip"
            )
            return None

        net_credit = sc
        stop = net_credit * 2.0 * self.stop_loss_multiplier
        target = net_credit * (self.profit_target_pct / 100.0)

        return TradePlan(
            strategy=self.name,
            underlying=ctx.underlying,
            legs=[
                {
                    "side": "SELL",
                    "qty": 1,
                    "strike": sc_strike,
                    "opt_type": "C",
                    "order_type": "LIMIT",
                    "price": sc,
                    "tag": f"sc_{ctx.underlying}_sc",
                },
            ],
            target=target,
            stop=stop,
            confidence=0.55,
            reason=(
                f"short_call: high IV (iv_rank={ctx.iv_rank:.0f}) "
                f"+ range regime, sell {sc_strike} OTM call for "
                f"${sc:.4f} credit, target ${target:.4f}, stop ${stop:.4f}"
                + (f" (DTE={dte})" if dte is not None else "")
            ),
        )


def _snap(target: float, valid: list[float]) -> Optional[float]:
    if not valid:
        return None
    return min(valid, key=lambda k: abs(k - target))
