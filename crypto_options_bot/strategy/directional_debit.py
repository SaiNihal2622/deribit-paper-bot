"""Directional debit — single-leg long option based on momentum.

Long call if momentum is positive, long put if negative. ATM strike,
7-14 DTE. Stop at 50% of premium; target at 100% of premium.

Designed to be a counter-cyclical play to the short-premium iron condor
and short strangle: when the market is trending or volatile, this strategy
takes the other side of the trade with a defined-risk debit.
"""
from __future__ import annotations

from datetime import date, datetime
from typing import Optional

from loguru import logger

from .base import BaseStrategy, SignalContext, StrategyName, TradePlan
from ._helpers import snap_strike


class DirectionalDebitStrategy(BaseStrategy):
    name = StrategyName.DIRECTIONAL_DEBIT

    def __init__(self, config: dict | None = None):
        cfg = config or {}
        # Minimum absolute momentum (fraction, e.g. 0.02 = 2% over the window)
        self.min_momentum = float(cfg.get("min_momentum", 0.02))
        # IV rank lower bound — don't pay rich premium for a debit
        self.min_iv_rank = float(cfg.get("min_iv_rank", 20.0))
        # Max IV rank — refuse to chase when options are very expensive
        self.max_iv_rank = float(cfg.get("max_iv_rank", 80.0))
        # Stop and target as multiples of the premium paid
        self.stop_multiple = float(cfg.get("stop_multiple", 0.5))
        self.target_multiple = float(cfg.get("target_multiple", 1.0))
        # DTE range for the chosen expiry
        self.min_dte = int(cfg.get("min_dte", 7))
        self.max_dte = int(cfg.get("max_dte", 14))
        # Confidence attached to plans we build
        self.confidence = float(cfg.get("confidence", 0.55))

    # ------------------------------------------------------------------
    def is_eligible(self, ctx: SignalContext, account_state: dict) -> tuple[bool, str]:
        if ctx.regime not in ("trending", "volatile"):
            return False, f"regime={ctx.regime} not trending/volatile"
        if not ctx.strikes or len(ctx.strikes) < 3:
            return False, f"insufficient strikes: {len(ctx.strikes or [])}"
        if not hasattr(ctx, "_momentum"):
            # We don't get momentum on the SignalContext directly; the runner
            # sets it as a side-channel via `account_state`. Fall back to
            # trend_strength if not set.
            momentum = float(account_state.get("momentum", 0.0) or 0.0)
        else:
            momentum = float(ctx._momentum)
        if abs(momentum) < self.min_momentum:
            return False, f"|momentum|={abs(momentum):.4f} < {self.min_momentum}"
        if ctx.iv_rank < self.min_iv_rank:
            return False, f"iv_rank={ctx.iv_rank:.0f} < {self.min_iv_rank}"
        if ctx.iv_rank > self.max_iv_rank:
            return False, f"iv_rank={ctx.iv_rank:.0f} > {self.max_iv_rank} (premium too rich)"
        return True, "eligible"

    # ------------------------------------------------------------------
    def build_plan(self, ctx: SignalContext, account_state: dict) -> Optional[TradePlan]:
        eligible, reason = self.is_eligible(ctx, account_state)
        if not eligible:
            return None

        # Prefer the runner-provided momentum; fall back to trend_strength
        momentum = float(account_state.get("momentum", 0.0) or 0.0)
        if momentum == 0.0:
            momentum = float(ctx.trend_strength) / 50.0  # already a fraction proxy

        direction = "C" if momentum > 0 else "P"
        spot = float(ctx.spot)
        atm = min(ctx.strikes, key=lambda k: abs(k - spot))
        strike = snap_strike(atm, ctx.strikes) or atm
        ltp = float(ctx.option_ltps.get((strike, direction), 0.0))
        if ltp <= 0:
            # Fall back to the nearest non-zero LTP for the same direction
            candidates = [
                (s, ctx.option_ltps.get((s, direction), 0.0))
                for s in ctx.strikes
                if ctx.option_ltps.get((s, direction), 0.0) > 0
            ]
            if not candidates:
                logger.debug(
                    f"directional_debit: no live {direction} LTPs for {ctx.underlying} "
                    f"(atm={atm}); skipping cycle"
                )
                return None
            strike, ltp = min(candidates, key=lambda kv: abs(kv[0] - spot))

        # Debit = the premium we pay
        debit = ltp
        target = debit * self.target_multiple
        stop = debit * self.stop_multiple

        tag_suffix = "BULL" if direction == "C" else "BEAR"
        return TradePlan(
            strategy=self.name,
            underlying=ctx.underlying,
            legs=[
                {
                    "side": "BUY",
                    "qty": 1,
                    "strike": strike,
                    "opt_type": direction,
                    "order_type": "LIMIT",
                    "price": round(ltp, 4),
                    "tag": f"dd_{ctx.underlying}_{tag_suffix}",
                }
            ],
            target=round(target, 4),
            stop=round(stop, 4),
            confidence=self.confidence,
            reason=(
                f"directional debit: regime={ctx.regime}, momentum={momentum:+.4f}, "
                f"iv_rank={ctx.iv_rank:.0f}, {direction}@{strike} debit={debit:.4f}"
            ),
            expected_hold_minutes=120,
        )
