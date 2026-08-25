"""Calendar spread — sell near-DTE, buy same-strike far-DTE.

A time-decay play that profits when the near option decays faster than the
far option. Best in range/trending markets with IV not too low. The classic
structure is SELL 7-DTE + BUY 28-DTE at the same strike (and side). Net
debit (you pay); the trader captures the theta differential.

Risk: a big move in either direction makes the back-month protective
component expensive (you'd be looking at an additional debit or a stop).
Stop at 2x the debit. Target at 50% of the debit (close before theta flips).
"""
from __future__ import annotations

from typing import Optional

from loguru import logger

from .base import BaseStrategy, SignalContext, StrategyName, TradePlan
from ._helpers import snap_strike


class CalendarSpreadStrategy(BaseStrategy):
    name = StrategyName.CALENDAR_SPREAD

    def __init__(self, config: dict | None = None):
        cfg = config or {}
        self.min_iv_rank = float(cfg.get("min_iv_rank", 20.0))
        self.max_iv_rank = float(cfg.get("max_iv_rank", 50.0))
        self.near_dte = int(cfg.get("near_dte", 7))
        self.far_dte = int(cfg.get("far_dte", 28))
        self.stop_multiple = float(cfg.get("stop_multiple", 2.0))
        self.target_multiple = float(cfg.get("target_multiple", 0.5))
        self.confidence = float(cfg.get("confidence", 0.5))

    # ------------------------------------------------------------------
    def is_eligible(self, ctx: SignalContext, account_state: dict) -> tuple[bool, str]:
        if ctx.regime not in ("range", "trending"):
            return False, f"regime={ctx.regime} not range/trending"
        if ctx.iv_rank < self.min_iv_rank:
            return False, f"iv_rank={ctx.iv_rank:.0f} < {self.min_iv_rank}"
        if ctx.iv_rank > self.max_iv_rank:
            return False, f"iv_rank={ctx.iv_rank:.0f} > {self.max_iv_rank} (rich, theta risk)"
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
        # Trade the same side as the dominant trend (default to call side
        # for a range market; if trend is strong bear, use put side).
        direction = "C" if float(ctx.trend_strength) >= 0 else "P"

        # The actual near/far selection happens in the runner (it has the
        # expiry picker). We expose ``near_iso`` and ``far_iso`` via the
        # account_state so the runner can stamp them on the plan. Here we
        # just record the desired DTE and the runner fills them in.
        near_iso = str(account_state.get("near_expiry_iso", "") or "")
        far_iso = str(account_state.get("far_expiry_iso", "") or "")
        if not near_iso or not far_iso or near_iso == far_iso:
            # Runner didn't give us two distinct expiries; skip this cycle.
            logger.debug("calendar_spread: missing near/far expiry; skipping")
            return None

        near_ltp = float(ctx.option_ltps.get((strike, direction), 0.0))
        if near_ltp <= 0:
            logger.debug(
                f"calendar_spread: no live {direction} LTP for {ctx.underlying} "
                f"K={strike}; skipping"
            )
            return None
        # For the far expiry we don't have a separate option_ltps entry in
        # the SignalContext (it's only the near chain). The runner will
        # look up the far-leg price from the feed at execution time. So
        # we just estimate debit using the IV term-structure proxy: far
        # options are worth ~ sqrt(T_far/T_near) * near. If we cannot
        # verify the actual far-leg price at execution the runner will
        # skip the plan, so this is just a planning estimate.
        try:
            from datetime import date
            n_dt = date.fromisoformat(near_iso)
            f_dt = date.fromisoformat(far_iso)
            t_near = max(1, (n_dt - date.today()).days)
            t_far = max(2, (f_dt - date.today()).days)
            term_ratio = (t_far / t_near) ** 0.5
        except Exception:
            term_ratio = 2.0
        far_ltp_est = round(near_ltp * term_ratio, 4)
        # Net debit (you pay the difference, since far > near)
        debit = far_ltp_est - near_ltp
        if debit <= 0:
            logger.debug("calendar_spread: debit<=0 from term-structure proxy; skip")
            return None
        target = debit * self.target_multiple
        stop = debit * self.stop_multiple

        return TradePlan(
            strategy=self.name,
            underlying=ctx.underlying,
            legs=[
                {
                    "side": "SELL",
                    "qty": 1,
                    "strike": strike,
                    "opt_type": direction,
                    "order_type": "LIMIT",
                    "price": round(near_ltp, 4),
                    "tag": f"cal_{ctx.underlying}_{direction}_near",
                    "expiry": near_iso,
                },
                {
                    "side": "BUY",
                    "qty": 1,
                    "strike": strike,
                    "opt_type": direction,
                    "order_type": "LIMIT",
                    "price": round(far_ltp_est, 4),
                    "tag": f"cal_{ctx.underlying}_{direction}_far",
                    "expiry": far_iso,
                },
            ],
            target=round(target, 4),
            stop=round(stop, 4),
            confidence=self.confidence,
            reason=(
                f"calendar: regime={ctx.regime}, iv_rank={ctx.iv_rank:.0f}, "
                f"{direction}@{strike} debit~{debit:.4f} "
                f"(near={near_iso}, far={far_iso})"
            ),
            expected_hold_minutes=720,
        )
