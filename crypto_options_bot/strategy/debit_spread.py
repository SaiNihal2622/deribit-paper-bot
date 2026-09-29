"""Long call/put spread — defined risk, defined reward, asymmetric R:R.

The textbook "low risk high profit" options structure:

  Bull call spread (long call + short higher-strike call):
    - Max loss  = net debit paid
    - Max profit = (short_strike - long_strike) - net debit
    - Typical R:R = 3:1 to 5:1 (depending on spread width)
    - Wins when underlying rallies through long strike + debit
    - Win rate ~30-40% in moderate uptrends

  Bear put spread (mirror):
    - Same structure, profits on down moves.

Why this beats naked short premium:
  - Defined risk: you can't lose more than the debit paid
  - Asymmetric payoff: typically 3:1 to 5:1
  - Wins only need to hit ~25-35% of the time to be profitable
  - No tail-event blowup risk (vs naked short strangle's 8:1 R:R requiring >89% WR)

Sizing:
  - Risk per trade = debit × qty
  - Win per trade = (spread_width - debit) × qty
  - Position sized so that risk ≤ max_trade_loss_pct of capital
"""
from __future__ import annotations

from typing import Optional

from loguru import logger

from .base import BaseStrategy, SignalContext, StrategyName, TradePlan
from ._helpers import snap_strike


class DebitSpreadStrategy(BaseStrategy):
    """Long call spread (bullish) or long put spread (bearish) on momentum."""

    name = StrategyName.DEBIT_SPREAD

    def __init__(self, config: dict | None = None):
        cfg = config or {}
        # Trigger: |momentum| above this fires the spread (0.005 = 0.5%)
        self.min_momentum = float(cfg.get("min_momentum", 0.003))
        # Spread width as % of spot (e.g. 0.05 = 5% wide spread)
        self.spread_width_pct = float(cfg.get("spread_width_pct", 0.05))
        # Min IV rank to pay debit (avoid buying into compressed vol)
        self.min_iv_rank = float(cfg.get("min_iv_rank", 15.0))
        # Max IV rank — refuse to chase when options are very expensive
        self.max_iv_rank = float(cfg.get("max_iv_rank", 85.0))
        # DTE range
        self.min_dte = int(cfg.get("min_dte", 5))
        self.max_dte = int(cfg.get("max_dte", 21))
        # Take profit at this multiple of debit (1.5 = 150% of debit)
        self.target_multiple = float(cfg.get("target_multiple", 2.5))
        # Stop at this multiple of debit (0.5 = half of debit)
        self.stop_multiple = float(cfg.get("stop_multiple", 0.5))
        # Confidence
        self.confidence = float(cfg.get("confidence", 0.55))

    def is_eligible(self, ctx: SignalContext, account_state: dict) -> tuple[bool, str]:
        momentum = float(account_state.get("momentum", 0.0) or 0.0)
        if abs(momentum) < self.min_momentum:
            return False, f"|momentum|={abs(momentum):.4f} < {self.min_momentum}"
        if ctx.iv_rank < self.min_iv_rank:
            return False, f"iv_rank={ctx.iv_rank:.0f} < {self.min_iv_rank}"
        if ctx.iv_rank > self.max_iv_rank:
            return False, f"iv_rank={ctx.iv_rank:.0f} > {self.max_iv_rank}"
        if not ctx.strikes or len(ctx.strikes) < 5:
            return False, f"insufficient strikes: {len(ctx.strikes or [])}"
        return True, "eligible"

    def build_plan(self, ctx: SignalContext, account_state: dict) -> Optional[TradePlan]:
        eligible, reason = self.is_eligible(ctx, account_state)
        if not eligible:
            return None

        momentum = float(account_state.get("momentum", 0.0) or 0.0)
        if momentum == 0.0:
            momentum = float(ctx.trend_strength) / 50.0
        direction = "C" if momentum > 0 else "P"  # C = call spread (bull), P = put spread (bear)

        spot = float(ctx.spot)
        atm = min(ctx.strikes, key=lambda k: abs(k - spot))
        long_strike = snap_strike(atm, ctx.strikes) or atm

        # Spread width: pick the strike ~spread_width_pct away from long strike
        target_short = long_strike + (long_strike * self.spread_width_pct if direction == "C"
                                       else -long_strike * self.spread_width_pct)
        short_strike = snap_strike(target_short, ctx.strikes)
        if short_strike is None or short_strike == long_strike:
            # Try the next strike in the right direction
            sorted_strikes = sorted(ctx.strikes)
            try:
                idx = sorted_strikes.index(long_strike)
                if direction == "C" and idx + 1 < len(sorted_strikes):
                    short_strike = sorted_strikes[idx + 1]
                elif direction == "B" and idx - 1 >= 0:
                    short_strike = sorted_strikes[idx - 1]
                else:
                    return None
            except ValueError:
                return None

        long_ltp = float(ctx.option_ltps.get((long_strike, direction), 0.0))
        short_ltp = float(ctx.option_ltps.get((short_strike, direction), 0.0))
        if long_ltp <= 0 or short_ltp <= 0:
            logger.debug(
                f"debit_spread: missing LTPs long={long_ltp}({long_strike}{direction}) "
                f"short={short_ltp}({short_strike}{direction}) — skipping"
            )
            return None

        # Net debit = what we pay
        debit = long_ltp - short_ltp
        if debit <= 0:
            # Inverted chain — long is cheaper than short. Skip rather than
            # create a free trade (likely illiquid or crossed market).
            return None

        spread_width = abs(short_strike - long_strike)
        max_profit = spread_width - debit
        if max_profit <= 0:
            return None

        # Defined risk: max loss = debit (premium paid)
        target = debit * self.target_multiple
        stop = debit * self.stop_multiple
        # NOTE: target is bounded by max_profit since the spread caps gains
        target = min(target, max_profit * 0.9)  # take profit before expiry

        return TradePlan(
            strategy=self.name,
            underlying=ctx.underlying,
            legs=[
                {
                    "side": "BUY", "qty": 1, "strike": long_strike, "opt_type": direction,
                    "order_type": "LIMIT", "price": round(long_ltp, 4),
                    "tag": f"ds_{ctx.underlying}_{'BULL' if direction == 'C' else 'BEAR'}_long",
                },
                {
                    "side": "SELL", "qty": 1, "strike": short_strike, "opt_type": direction,
                    "order_type": "LIMIT", "price": round(short_ltp, 4),
                    "tag": f"ds_{ctx.underlying}_{'BULL' if direction == 'C' else 'BEAR'}_short",
                },
            ],
            target=round(target, 4),
            stop=round(stop, 4),
            confidence=self.confidence,
            reason=(
                f"debit spread {('BULL' if direction == 'C' else 'BEAR')}: "
                f"regime={ctx.regime}, momentum={momentum:+.4f}, "
                f"iv_rank={ctx.iv_rank:.0f}, long@{long_strike} short@{short_strike} "
                f"debit={debit:.4f} max_profit={max_profit:.4f} R:R={max_profit/debit:.1f}:1"
            ),
            expected_hold_minutes=240,  # longer hold for spread to play out
        )
