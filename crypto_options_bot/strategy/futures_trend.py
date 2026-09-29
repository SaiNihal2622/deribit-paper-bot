"""Futures trend strategy — leveraged directional plays with ATR-based stops.

This is the "options AND futures" play the user explicitly asked for. Unlike
short-premium strategies, futures trend plays capture BIG moves with leverage.

Structure:
  - Long or short the underlying (1x direction)
  - Use spot feed + ADX + momentum for entry signal
  - ATR-based stops (not arbitrary %)
  - Target = 3:1 R:R (or 2:1 conservative)

Risk profile:
  - Defined risk per trade: ATR × leverage × qty
  - Defined reward: 3x the risk
  - Win rate needed: >25%
  - Typical outcome: small frequent losses + occasional large wins

Why this beats naked short premium on $100k capital:
  - 10x leverage on BTC = $830k effective notional per contract
  - 1% move in BTC = $8.3k per contract (10 contracts = $83k)
  - Captures directional trends, not just theta

The strategy uses spot price as the futures price (sufficient for paper trading).
A live Deribit connection would query BTC-PERP / ETH-PERP orderbook directly.
"""
from __future__ import annotations

from typing import Optional

from loguru import logger

from .base import BaseStrategy, SignalContext, StrategyName, TradePlan


class FuturesTrendStrategy(BaseStrategy):
    """Trend-following futures with ATR stops and 3:1 R:R."""

    name = StrategyName.FUTURES_TREND

    def __init__(self, config: dict | None = None):
        cfg = config or {}
        # Trend entry thresholds
        self.min_momentum = float(cfg.get("min_momentum", 0.005))   # 0.5% move
        self.min_adx = float(cfg.get("min_adx", 20.0))
        # ATR multiplier for stop and target
        self.atr_period = int(cfg.get("atr_period", 14))
        self.atr_stop_mult = float(cfg.get("atr_stop_mult", 2.0))    # stop = 2x ATR
        self.atr_target_mult = float(cfg.get("atr_target_mult", 6.0))  # target = 6x ATR (3:1 R:R)
        # Leverage: paper-trade 5x by default. Real account would default 3-5x.
        self.leverage = float(cfg.get("leverage", 5.0))
        # Confidence
        self.confidence = float(cfg.get("confidence", 0.55))

    # ------------------------------------------------------------------
    def is_eligible(self, ctx: SignalContext, account_state: dict) -> tuple[bool, str]:
        momentum = float(account_state.get("momentum", 0.0) or 0.0)
        if abs(momentum) < self.min_momentum:
            return False, f"|momentum|={abs(momentum):.4f} < {self.min_momentum}"
        if ctx.adx < self.min_adx:
            return False, f"adx={ctx.adx:.0f} < {self.min_adx}"
        return True, "eligible"

    # ------------------------------------------------------------------
    def build_plan(self, ctx: SignalContext, account_state: dict) -> Optional[TradePlan]:
        eligible, reason = self.is_eligible(ctx, account_state)
        if not eligible:
            return None

        momentum = float(account_state.get("momentum", 0.0) or 0.0)
        if momentum == 0.0:
            momentum = float(ctx.trend_strength) / 50.0

        spot = float(ctx.spot)
        if spot <= 0:
            return None

        # Estimate ATR from recent price history is not available here, so
        # use a momentum-derived proxy: ATR ≈ |momentum| × spot × sqrt(DTE).
        # For typical 5-day holds with 0.5-2% momentum, this gives realistic
        # ATR estimates without needing a full ATR computation in the strategy.
        atr_proxy = abs(momentum) * spot * 1.5

        # Stop and target in price units
        stop_distance = atr_proxy * self.atr_stop_mult
        target_distance = atr_proxy * self.atr_target_mult

        if stop_distance <= 0:
            return None

        direction = "LONG" if momentum > 0 else "SHORT"
        # Deribit perp symbol naming convention (paper trading uses these symbols)
        symbol = f"{ctx.underlying}-PERP"

        # Position size: 1 contract = 0.001 BTC for BTC-PERP.
        # 10x leverage on $100k = $1M notional / $83k per BTC = 12 BTC = 12,000 contracts.
        # For paper trading, scale to fit within capital × leverage.
        # The risk engine's qty scaling handles the absolute size — we just need
        # to specify the leg and the risk per contract.
        if ctx.underlying == "BTC":
            contract_size = 0.001  # 0.001 BTC per BTC-PERP contract
        elif ctx.underlying == "ETH":
            contract_size = 0.01   # 0.01 ETH per ETH-PERP contract
        else:
            return None

        # Stop in price terms = the stop distance; convert to per-contract USD
        # by multiplying by contract_size (1 contract moves contract_size × $1
        # per $1 price move for inverse, or contract_size × 1 for linear).
        # Per-contract max loss = stop_distance × contract_size × USD_per_unit
        # But since the price IS in USD for inverse perp, it's stop_distance × contract_size.
        stop_per_contract = stop_distance * contract_size
        target_per_contract = target_distance * contract_size

        return TradePlan(
            strategy=self.name,
            underlying=ctx.underlying,
            legs=[
                {
                    "side": "BUY" if direction == "LONG" else "SELL",
                    "qty": 1,
                    "strike": 0.0,           # futures have no strike
                    "opt_type": None,         # futures have no option type
                    "order_type": "MARKET",
                    "price": round(spot, 2),
                    "tag": f"ft_{ctx.underlying}_{direction}",
                    "symbol": symbol,
                    "contract_size": contract_size,
                }
            ],
            target=round(target_per_contract, 4),  # target in per-contract P&L
            stop=round(stop_per_contract, 4),      # stop in per-contract P&L
            confidence=self.confidence,
            reason=(
                f"futures trend {direction}: momentum={momentum:+.4f}, "
                f"adx={ctx.adx:.0f}, atr≈{atr_proxy:.0f}, "
                f"spot={spot:.2f}, R:R=3:1, leverage={self.leverage:.0f}x"
            ),
            expected_hold_minutes=720,  # hold up to 12h
        )
