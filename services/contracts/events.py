"""services/contracts/events.py — typed event schemas for the bus.

Every event crossing service boundaries conforms to one of these schemas. Adding a new
event type means: (1) add a class here, (2) version-bump the schema, (3) update consumers.

Schemas use Pydantic v2 for validation. Every event carries:
  - schema_version: str  — bump when fields change
  - ts_event_ms: int    — when the event happened (venue time, not bus time)
  - ts_bus_ms: int      — when the bus got it (set by the publisher, not the producer)
  - trace_id: str       — OTel-compatible, propagates through the pipeline
  - source: str         — who produced this ("ingest", "feature", etc.)
"""
from __future__ import annotations

from typing import Literal, Optional
from pydantic import BaseModel, Field


SCHEMA_VERSION = "1.0"


class BaseEvent(BaseModel):
    """Common envelope for every event on the bus."""

    schema_version: str = SCHEMA_VERSION
    ts_event_ms: int = Field(..., description="Wall-clock ms when the event happened at the source")
    ts_bus_ms: int = Field(default=0, description="Wall-clock ms when the bus received the event")
    trace_id: str = Field(default="", description="OTel trace ID for end-to-end correlation")
    source: str = Field(..., description="Service that produced this event")


# ===========================================================================
# Ingestion events (Deribit WS feed → bus)
# ===========================================================================

class TickEvent(BaseEvent):
    """Single price tick from any venue."""

    venue: Literal["deribit", "binance", "okx", "coinbase", "kraken"]
    instrument: str = Field(..., description="Venue symbol, e.g. BTC-PERP, BTC-29SEP26-100000-C")
    underlying: str = Field(..., description="BTC / ETH / SOL")
    kind: Literal["perp", "future", "option"]
    bid: float = 0.0
    ask: float = 0.0
    ltp: float = 0.0
    mark: float = 0.0
    iv: float = 0.0
    size: float = 0.0
    strike: float = 0.0
    option_type: Optional[Literal["C", "P"]] = None
    expiry: Optional[str] = None


class SpotTickEvent(BaseEvent):
    """Spot index tick — used for futures mark, signal computation, regime detection."""

    venue: Literal["deribit"]
    underlying: str  # BTC, ETH, ...
    ltp: float


# ===========================================================================
# Feature events (compute → strategy)
# ===========================================================================

class FeaturesEvent(BaseEvent):
    """Computed features for a symbol. Strategy consumes these."""

    underlying: str  # BTC, ETH
    spot: float
    dvol: float          # annualised %
    iv_rank: float       # 0-100
    momentum_20: float   # fractional change over last 20 ticks
    momentum_100: float
    regime: Literal["trending", "range", "volatile"]
    adx: float
    trend_strength: float  # -1 .. +1
    minutes_to_event: Optional[int] = None
    upcoming_event: Optional[str] = None
    # Microstructure
    bid_ask_spread_pct: float = 0.0
    order_book_imbalance: float = 0.0  # -1 (sell pressure) to +1 (buy pressure)
    flow_toxicity: float = 0.0        # 0 = benign flow, 1 = toxic
    # Sentiment (LLM-derived, optional)
    sentiment_score: float = 0.0       # -1 (bearish) to +1 (bullish)
    sentiment_confidence: float = 0.0


# ===========================================================================
# Strategy events (strategy → risk)
# ===========================================================================

class StrategyPlan(BaseEvent):
    """A concrete plan emitted by a strategy. Risk evaluates it."""

    strategy_id: str  # e.g. "short_strangle:monthly_tight"
    strategy_version: str
    underlying: str
    legs: list[dict]  # [{"side": "BUY"/"SELL", "qty": int, "instrument": str, "price": float}]
    target: float      # expected per-contract profit
    stop: float        # expected per-contract loss
    confidence: float  # 0..1
    reason: str = ""
    hypothesis: str = ""  # why this strategy thinks this works
    expected_hold_minutes: int = 60


# ===========================================================================
# Risk events (risk → exec)
# ===========================================================================

class RiskDecision(BaseEvent):
    """Risk verdict on a plan."""

    plan_id: str  # ties back to StrategyPlan
    strategy_id: str
    allowed: bool
    reason: str
    qty: int          # 0 = blocked; >0 = approved size
    max_loss_for_trade: float
    preset: str       # "aggressive" | "base" | "defensive"


class RiskBreakerEvent(BaseEvent):
    """Circuit breaker state change (drawdown, daily loss, vol spike)."""

    breaker: Literal["drawdown", "daily_loss", "dvol_spike", "kill_switch"]
    triggered: bool
    reason: str
    threshold: float
    current_value: float


# ===========================================================================
# Execution events (exec → bus)
# ===========================================================================

class OrderPlaced(BaseEvent):
    """Order submitted to a venue."""

    venue: str
    order_id: str
    plan_id: str
    strategy_id: str
    instrument: str
    side: Literal["BUY", "SELL"]
    qty: int
    order_type: Literal["LIMIT", "MARKET", "SL", "SL_M"]
    price: float = 0.0


class FillEvent(BaseEvent):
    """Order fill (full or partial)."""

    venue: str
    order_id: str
    plan_id: str
    instrument: str
    side: Literal["BUY", "SELL"]
    qty: int
    price: float
    fee: float = 0.0
    liquidity: Literal["MAKER", "TAKER"] = "TAKER"


class PositionSnapshot(BaseEvent):
    """Periodic snapshot of every open position. Used for reconciliation + state."""

    venue: str
    positions: list[dict]  # [{symbol, qty, avg_price, mark_price, pnl, ...}]


# ===========================================================================
# Audit + control
# ===========================================================================

class AuditEvent(BaseEvent):
    """Anything that must be persisted for compliance / debugging."""

    kind: str  # "trade", "config_change", "strategy_added", "kill_switch", ...
    payload: dict


class ControlEvent(BaseEvent):
    """Operator → system commands (pause, resume, kill switch, parameter mutation)."""

    command: Literal["pause", "resume", "kill_switch", "set_param", "mutate_strategy"]
    payload: dict = {}


# ===========================================================================
# Bus subject conventions
# ===========================================================================

def subject_market_tick(venue: str, instrument: str) -> str:
    return f"market.{venue}.{instrument}.tick"


def subject_spot(underlying: str) -> str:
    return f"market.spot.{underlying}"


def subject_features(underlying: str) -> str:
    return f"signal.features.{underlying}"


def subject_strategy_plan(strategy_id: str) -> str:
    return f"strategy.{strategy_id}.plan"


def subject_risk_decision(strategy_id: str) -> str:
    return f"risk.{strategy_id}.decision"


def subject_exec_order(venue: str) -> str:
    return f"exec.{venue}.order"


def subject_exec_fill(venue: str) -> str:
    return f"exec.{venue}.fill"


def subject_position_snapshot(venue: str) -> str:
    return f"position.{venue}.snapshot"


def subject_audit() -> str:
    return "audit"


def subject_control() -> str:
    return "control"


__all__ = [
    "SCHEMA_VERSION",
    "BaseEvent",
    "TickEvent",
    "SpotTickEvent",
    "FeaturesEvent",
    "StrategyPlan",
    "RiskDecision",
    "RiskBreakerEvent",
    "OrderPlaced",
    "FillEvent",
    "PositionSnapshot",
    "AuditEvent",
    "ControlEvent",
    "subject_market_tick",
    "subject_spot",
    "subject_features",
    "subject_strategy_plan",
    "subject_risk_decision",
    "subject_exec_order",
    "subject_exec_fill",
    "subject_position_snapshot",
    "subject_audit",
    "subject_control",
]
