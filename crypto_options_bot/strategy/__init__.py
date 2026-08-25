"""Strategy layer: signal context, trade plans, concrete strategies.

Five strategies are exported:
  - IronCondorStrategy: 4-leg range-bound premium seller (defined risk)
  - ShortStrangleStrategy: 2-leg undefined-risk premium seller
  - DirectionalDebitStrategy: 1-leg long option on momentum
  - CalendarSpreadStrategy: time-decay play (sell near-DTE, buy far-DTE)
  - LongStraddleStrategy: 2-leg vol expansion (buy ATM call + put)
"""
from .base import BaseStrategy, SignalContext, StrategyName, TradePlan
from .iron_condor import IronCondorStrategy
from .short_strangle import ShortStrangleStrategy
from .directional_debit import DirectionalDebitStrategy
from .calendar_spread import CalendarSpreadStrategy
from .long_straddle import LongStraddleStrategy

__all__ = [
    "BaseStrategy",
    "SignalContext",
    "StrategyName",
    "TradePlan",
    "IronCondorStrategy",
    "ShortStrangleStrategy",
    "DirectionalDebitStrategy",
    "CalendarSpreadStrategy",
    "LongStraddleStrategy",
]
