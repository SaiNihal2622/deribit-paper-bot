"""Base strategy types — StrategyName, SignalContext, TradePlan, BaseStrategy.

Mirrors the Kotak Neo bot's surface but adapted for crypto:
  - underlying: "BTC" or "ETH" (instead of NIFTY/BANKNIFTY)
  - option_type: "C" or "P" (Deribit single-letter; tolerated "CE"/"PE" too)
  - minutes_to_event / dvol / iv_rank fields added (Deribit has DVOL)
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Optional


class StrategyName(str, Enum):
    DIRECTIONAL_DEBIT = "directional_debit"
    IRON_CONDOR = "iron_condor"
    SHORT_STRANGLE = "short_strangle"
    CALENDAR_SPREAD = "calendar_spread"
    LONG_STRADDLE = "long_straddle"


@dataclass
class SignalContext:
    """All info a strategy needs to make a decision.

    Mirrors the Kotak SignalContext but adds Deribit-specific fields (dvol,
    minutes_to_event) and uses crypto symbol names.
    """

    underlying: str           # "BTC" or "ETH"
    spot: float               # current spot price (e.g. 65000.0)
    dvol: float               # Deribit vol index (annualized %, 0 if unknown)
    iv_rank: float            # 0-100 percentile
    adx: float                # trend strength 0-100
    trend_strength: float     # -1..+1 (bear..bull)
    regime: str               # 'trending' | 'range' | 'volatile'
    timestamp: datetime
    # option chain
    strikes: list[float] = field(default_factory=list)
    option_ltps: dict = field(default_factory=dict)    # (strike, opt_type) -> ltp
    option_ivs: dict = field(default_factory=dict)     # (strike, opt_type) -> iv (decimal)
    # event context
    upcoming_event: Optional[str] = None
    minutes_to_event: Optional[int] = None


@dataclass
class TradePlan:
    """A concrete plan to place a trade. The execution layer turns this into orders."""

    strategy: StrategyName
    underlying: str
    legs: list[dict]  # each: {side, qty, strike, opt_type, order_type, price, tag}
    target: float      # expected profit at target
    stop: float        # max loss
    confidence: float  # 0..1
    reason: str
    expiry: str = ""   # ISO date
    expected_hold_minutes: int = 60

    @property
    def is_multi_leg(self) -> bool:
        return len(self.legs) > 1

    @property
    def max_loss(self) -> float:
        return abs(self.stop)

    @property
    def max_profit(self) -> float:
        return abs(self.target)


class BaseStrategy(ABC):
    """Abstract base for all strategies. Subclasses set `name` and implement
    `is_eligible` + `build_plan`."""

    name: StrategyName

    @abstractmethod
    def is_eligible(self, ctx: SignalContext, account_state: dict) -> tuple[bool, str]: ...

    @abstractmethod
    def build_plan(self, ctx: SignalContext, account_state: dict) -> Optional[TradePlan]: ...
