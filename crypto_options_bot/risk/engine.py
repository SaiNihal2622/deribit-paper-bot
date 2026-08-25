"""Risk engine — position cap, daily loss cap, DVOL-aware sizing, presets.

Caps enforced:
  - max_open_positions: hard cap on simultaneous open trades
  - max_daily_loss_pct: stop opening new trades once daily P&L drops below
    -this % of starting capital
  - max_trade_loss_pct: per-trade max loss as % of capital (sanity check)

Sizing adjustment:
  - DVOL / IV rank: if Deribit's DVOL > 80 or iv_rank > 75, halve the qty

Adaptive presets (aggressive / base / defensive) — selected based on DVOL
and the recent P&L run. Mirrors the Kotak bot's adaptive sizing:
  - aggressive: max_open_positions+1, max_daily_loss_pct+1, etc.
  - base: defaults
  - defensive: max_open_positions-1, max_daily_loss_pct/2, etc.

Public surface (unchanged for the paper loop):
  - ``RiskEngine(config).check_trade(plan, account_state)``
  - ``.status()`` returns a dict
  - ``RiskDecision.to_dict()`` is provided for the dashboard
"""
from __future__ import annotations

import copy
from dataclasses import dataclass, field
from datetime import date
from typing import Optional

from loguru import logger

from ..strategy.base import TradePlan


# ---------------------------------------------------------------------------
# Preset definitions
# ---------------------------------------------------------------------------
DEFAULT_PRESETS = {
    "aggressive": {
        "max_open_positions": 6,
        "max_daily_loss_pct": 6.0,
        "max_trade_loss_pct": 2.5,
        "high_dvol_threshold": 90.0,
        "high_iv_rank_threshold": 80.0,
    },
    "base": {
        "max_open_positions": 4,
        "max_daily_loss_pct": 5.0,
        "max_trade_loss_pct": 2.0,
        "high_dvol_threshold": 80.0,
        "high_iv_rank_threshold": 75.0,
    },
    "defensive": {
        "max_open_positions": 2,
        "max_daily_loss_pct": 2.5,
        "max_trade_loss_pct": 1.0,
        "high_dvol_threshold": 60.0,
        "high_iv_rank_threshold": 55.0,
    },
}


# ---------------------------------------------------------------------------
# Dataclasses
# ---------------------------------------------------------------------------
@dataclass
class RiskState:
    """Snapshot of the current account/portfolio for risk decisions."""

    capital: float
    daily_pnl: float = 0.0
    open_positions: int = 0
    paused: bool = False
    pause_reason: str = ""
    last_trade_pnl: float = 0.0
    dvol: float = 0.0  # Deribit volatility index (annualized %), 0 if unknown
    iv_rank: float = 50.0  # 0-100, default mid-range
    # Adaptive risk tracking
    preset: str = "base"
    consecutive_losses: int = 0
    consecutive_wins: int = 0
    last_preset_change: float = 0.0  # monotonic ts


@dataclass
class RiskDecision:
    """Result of a risk check."""

    allowed: bool
    reason: str
    suggested_qty: int = 0
    max_loss_for_trade: float = 0.0
    preset: str = "base"

    def to_dict(self) -> dict:
        return {
            "allowed": bool(self.allowed),
            "reason": self.reason,
            "suggested_qty": int(self.suggested_qty),
            "max_loss_for_trade": float(self.max_loss_for_trade),
            "preset": self.preset,
        }


# ---------------------------------------------------------------------------
# Engine
# ---------------------------------------------------------------------------
class RiskEngine:
    """Minimal, opinionated risk engine for the crypto options paper bot."""

    def __init__(self, config: Optional[dict] = None):
        """
        Args:
            config: dict with keys {r, max_open_positions, max_daily_loss_pct,
                   max_trade_loss_pct, high_dvol_threshold, high_iv_rank_threshold,
                   starting_capital, presets: {aggressive, base, defensive}}
        """
        cfg = config or {}
        self.config = cfg
        self.r = float(cfg.get("r", 0.045))
        # Active caps (mutable; switched when preset changes)
        self.max_open_positions = int(cfg.get("max_open_positions", 4))
        self.max_daily_loss_pct = float(cfg.get("max_daily_loss_pct", 5.0))
        self.max_trade_loss_pct = float(cfg.get("max_trade_loss_pct", 2.0))
        self.high_dvol_threshold = float(cfg.get("high_dvol_threshold", 80.0))
        self.high_iv_rank_threshold = float(cfg.get("high_iv_rank_threshold", 75.0))

        starting_capital = float(cfg.get("starting_capital", 100_000.0))
        self.state = RiskState(capital=starting_capital)
        self._day = date.today()
        # Consecutive-loss adaptive switching
        self._losses_to_defensive = int(cfg.get("losses_to_defensive", 3))
        self._wins_to_aggressive = int(cfg.get("wins_to_aggressive", 5))
        # Preset definitions (override defaults with config)
        self._presets: dict[str, dict] = copy.deepcopy(DEFAULT_PRESETS)
        if isinstance(cfg.get("presets"), dict):
            for name, vals in cfg["presets"].items():
                if not isinstance(vals, dict):
                    continue
                if name in self._presets:
                    self._presets[name].update(vals)
                else:
                    self._presets[name] = dict(vals)

    # ------------------------------------------------------------------
    # State updates
    # ------------------------------------------------------------------
    def update_capital(self, capital: float) -> None:
        self.state.capital = float(capital)

    def update_market_state(self, dvol: float = 0.0, iv_rank: float = 50.0) -> None:
        """Update the cached market state (called every poll cycle)."""
        self.state.dvol = float(dvol)
        self.state.iv_rank = float(iv_rank)

    def update_open_positions(self, n: int) -> None:
        self.state.open_positions = int(n)

    def update_daily_pnl(self, pnl: float) -> None:
        self._roll_period()
        self.state.daily_pnl = float(pnl)

    def record_trade_result(self, pnl: float) -> None:
        """Track a closed trade's P&L for adaptive preset selection.

        Positive P&L increments ``consecutive_wins`` and resets losses.
        Negative P&L increments ``consecutive_losses`` and resets wins.
        """
        if pnl >= 0:
            self.state.consecutive_wins += 1
            self.state.consecutive_losses = 0
        else:
            self.state.consecutive_losses += 1
            self.state.consecutive_wins = 0
        self.state.last_trade_pnl = float(pnl)
        self._maybe_switch_preset()

    def _maybe_switch_preset(self) -> None:
        """Adaptive: switch to defensive after N losses, aggressive after N wins."""
        old = self.state.preset
        if self.state.consecutive_losses >= self._losses_to_defensive:
            new = "defensive"
        elif self.state.consecutive_wins >= self._wins_to_aggressive:
            new = "aggressive"
        else:
            new = old
        # DVOL override — if market is very volatile, force defensive
        if self.state.dvol and self.state.dvol > 85.0:
            new = "defensive"
        if new != old:
            self._apply_preset(new)
            logger.info(
                f"RiskEngine preset: {old} -> {new} "
                f"(consec_losses={self.state.consecutive_losses}, "
                f"consec_wins={self.state.consecutive_wins}, dvol={self.state.dvol:.0f})"
            )

    def _apply_preset(self, name: str) -> None:
        """Apply a preset by name. Falls back to 'base' if unknown."""
        if name not in self._presets:
            name = "base"
        p = self._presets[name]
        self.max_open_positions = int(p.get("max_open_positions", self.max_open_positions))
        self.max_daily_loss_pct = float(p.get("max_daily_loss_pct", self.max_daily_loss_pct))
        self.max_trade_loss_pct = float(p.get("max_trade_loss_pct", self.max_trade_loss_pct))
        self.high_dvol_threshold = float(p.get("high_dvol_threshold", self.high_dvol_threshold))
        self.high_iv_rank_threshold = float(
            p.get("high_iv_rank_threshold", self.high_iv_rank_threshold)
        )
        self.state.preset = name
        import time as _t
        self.state.last_preset_change = _t.time()

    def force_preset(self, name: str) -> None:
        """Public hook to force a preset (used by tests / supervisor)."""
        self._apply_preset(name)

    # ------------------------------------------------------------------
    # Decision
    # ------------------------------------------------------------------
    def _roll_period(self) -> None:
        """Reset daily P&L at the day boundary."""
        today = date.today()
        if today != self._day:
            self.state.daily_pnl = 0.0
            self._day = today

    def _max_trade_loss_capital(self) -> float:
        return self.state.capital * (self.max_trade_loss_pct / 100.0)

    def _max_daily_loss_capital(self) -> float:
        return self.state.capital * (self.max_daily_loss_pct / 100.0)

    def check_trade(
        self, plan: TradePlan, account_state: Optional[dict] = None
    ) -> RiskDecision:
        """Decide whether ``plan`` is allowed and what qty to use."""
        self._roll_period()

        if self.state.paused:
            return RiskDecision(
                allowed=False,
                reason=f"paused: {self.state.pause_reason}",
                suggested_qty=0,
                preset=self.state.preset,
            )

        if account_state:
            self.state.daily_pnl = float(account_state.get("realized_pnl", self.state.daily_pnl))
            self.state.open_positions = int(
                account_state.get("open_positions", self.state.open_positions)
            )
            if "capital" in account_state:
                self.state.capital = float(account_state["capital"])

        # 1) daily loss cap
        daily_loss_cap = self._max_daily_loss_capital()
        if -self.state.daily_pnl >= daily_loss_cap:
            return RiskDecision(
                allowed=False,
                reason=f"daily loss cap hit: P&L=${self.state.daily_pnl:,.2f} <= -${daily_loss_cap:,.2f}",
                suggested_qty=0,
                preset=self.state.preset,
            )

        # 2) position cap
        if self.state.open_positions >= self.max_open_positions:
            return RiskDecision(
                allowed=False,
                reason=f"position cap: open={self.state.open_positions} >= max={self.max_open_positions}",
                suggested_qty=0,
                preset=self.state.preset,
            )

        # 3) per-trade loss cap
        max_loss_for_trade = min(self._max_trade_loss_capital(), abs(plan.stop or 0))
        if max_loss_for_trade <= 0:
            return RiskDecision(
                allowed=False,
                reason=f"plan has no defined max loss: stop={plan.stop}",
                suggested_qty=0,
                preset=self.state.preset,
            )

        # 4) qty sizing — start at 1, then trim if DVOL / IV rank are extreme
        base_qty = 1
        if self.state.dvol > self.high_dvol_threshold:
            base_qty = 0
        if self.state.iv_rank > self.high_iv_rank_threshold:
            base_qty = max(0, base_qty - 1)

        if base_qty <= 0:
            return RiskDecision(
                allowed=False,
                reason=(
                    f"sizing=0: dvol={self.state.dvol:.0f} (>{self.high_dvol_threshold:.0f}) "
                    f"or iv_rank={self.state.iv_rank:.0f} (>{self.high_iv_rank_threshold:.0f})"
                ),
                suggested_qty=0,
                preset=self.state.preset,
            )

        logger.debug(
            f"RiskEngine ALLOW {plan.strategy.value} {plan.underlying} "
            f"qty={base_qty} max_loss={max_loss_for_trade:.2f} "
            f"open={self.state.open_positions}/{self.max_open_positions} preset={self.state.preset}"
        )
        return RiskDecision(
            allowed=True,
            reason="ok",
            suggested_qty=base_qty,
            max_loss_for_trade=max_loss_for_trade,
            preset=self.state.preset,
        )

    def pause(self, reason: str = "") -> None:
        self.state.paused = True
        self.state.pause_reason = reason
        logger.warning(f"RiskEngine PAUSED: {reason}")

    def resume(self) -> None:
        self.state.paused = False
        self.state.pause_reason = ""
        logger.info("RiskEngine RESUMED")

    def status(self) -> dict:
        return {
            "paused": self.state.paused,
            "pause_reason": self.state.pause_reason,
            "capital": self.state.capital,
            "daily_pnl": self.state.daily_pnl,
            "open_positions": self.state.open_positions,
            "max_open_positions": self.max_open_positions,
            "max_daily_loss_pct": self.max_daily_loss_pct,
            "max_trade_loss_pct": self.max_trade_loss_pct,
            "dvol": self.state.dvol,
            "iv_rank": self.state.iv_rank,
            "preset": self.state.preset,
            "consecutive_losses": self.state.consecutive_losses,
            "consecutive_wins": self.state.consecutive_wins,
            "last_trade_pnl": self.state.last_trade_pnl,
        }
