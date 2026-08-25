"""Abstract broker interface and shared data models.

The crypto options bot uses the same ABC shape as the Kotak Neo bot but with
Deribit semantics:
  - exchange = "DERIBIT"
  - symbol format = "BTC-26DEC25-100000-C" (Deribit native, NOT NSE-style)
  - option_type = "C" or "P" (Deribit single-letter; PaperClient tolerates "CE"/"PE" too)
  - underlying = "BTC" | "ETH"
  - contract_size = 1.0 (Deribit settles inverse; 1 contract = 1 unit)

Both the in-memory paper client and (future) the live Deribit REST client
implement `BrokerClient`. The paper client injects synthetic ticks into the
order book and marks positions to market on every tick.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Callable, Optional


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------
class OrderType(str, Enum):
    """Supported order types. SL/SL_M are accepted by the paper client for
    API parity but rarely used in options strategies."""

    MARKET = "MARKET"
    LIMIT = "LIMIT"
    SL = "SL"
    SL_M = "SL-M"


class OrderSide(str, Enum):
    """Buy or sell."""

    BUY = "BUY"
    SELL = "SELL"


class OrderStatus(str, Enum):
    """Lifecycle: PENDING -> OPEN -> COMPLETE / CANCELLED / REJECTED."""

    PENDING = "pending"
    OPEN = "open"
    COMPLETE = "complete"
    CANCELLED = "cancelled"
    REJECTED = "rejected"


# ---------------------------------------------------------------------------
# Data models
# ---------------------------------------------------------------------------
@dataclass
class Order:
    """A single order leg. Symbol is the Deribit-format name (e.g. BTC-26DEC25-100000-C)."""

    symbol: str
    side: OrderSide
    qty: int
    order_type: OrderType
    price: float = 0.0
    trigger_price: float = 0.0
    tag: str = ""
    exchange: str = "DERIBIT"
    strike: float = 0.0
    option_type: Optional[str] = None  # "C" / "P" (paper also tolerates "CE"/"PE")
    expiry: Optional[str] = None  # ISO date "YYYY-MM-DD" (set when known)
    underlying: Optional[str] = None  # "BTC" / "ETH"

    # filled by broker
    order_id: Optional[str] = None
    status: OrderStatus = OrderStatus.PENDING
    filled_qty: int = 0
    avg_fill_price: float = 0.0
    placed_at: Optional[datetime] = None
    filled_at: Optional[datetime] = None
    rejection_reason: str = ""

    # paper-specific: the price we expect to fill at, used by the fill simulator
    expected_fill_price: float = 0.0


@dataclass
class Position:
    """Open position. qty is signed: +long, -short."""

    symbol: str
    qty: int
    avg_price: float
    ltp: float
    exchange: str = "DERIBIT"
    pnl: float = 0.0
    strike: float = 0.0
    option_type: Optional[str] = None  # "C" / "P"
    expiry: Optional[str] = None
    underlying: Optional[str] = None
    contract_size: float = 1.0  # Deribit inverse = 1
    entry_time: Optional[datetime] = None

    @property
    def is_option(self) -> bool:
        return self.option_type in ("C", "P", "CE", "PE")

    @property
    def notional(self) -> float:
        return abs(self.qty) * self.avg_price * self.contract_size


@dataclass
class Tick:
    """Market-data snapshot for a single instrument."""

    symbol: str
    ltp: float
    bid: float = 0.0
    ask: float = 0.0
    volume: int = 0
    oi: int = 0
    timestamp: datetime = field(default_factory=datetime.utcnow)
    exchange: str = "DERIBIT"
    strike: float = 0.0
    option_type: Optional[str] = None  # "C" / "P" for options, None for spot
    expiry: Optional[str] = None
    underlying: Optional[str] = None
    # Implied volatility as a decimal (0.65 = 65%). 0.0 = unknown (use bisection).
    # Populated by feeds with a direct IV source (Deribit mark_iv).
    iv: float = 0.0


# ---------------------------------------------------------------------------
# Broker ABC
# ---------------------------------------------------------------------------
class BrokerClient(ABC):
    """Abstract broker — same API for paper and live."""

    @abstractmethod
    def connect(self) -> None: ...

    @abstractmethod
    def disconnect(self) -> None: ...

    @abstractmethod
    def is_connected(self) -> bool: ...

    @abstractmethod
    def place_order(self, order: Order) -> Order: ...

    @abstractmethod
    def modify_order(self, order_id: str, **kwargs) -> Order: ...

    @abstractmethod
    def cancel_order(self, order_id: str) -> Order: ...

    @abstractmethod
    def get_order(self, order_id: str) -> Optional[Order]: ...

    @abstractmethod
    def get_positions(self) -> list[Position]: ...

    @abstractmethod
    def get_holdings(self) -> list[Position]: ...

    @abstractmethod
    def get_margins(self) -> dict: ...

    @abstractmethod
    def get_ltp(self, symbol: str, exchange: str = "DERIBIT") -> float: ...

    @abstractmethod
    def subscribe(self, symbols: list[str], exchange: str = "DERIBIT") -> None: ...

    @abstractmethod
    def on_tick(self, callback: Callable[[Tick], None]) -> None: ...

    # Paper-only API: feed ticks into the book from an external data source.
    @abstractmethod
    def inject_tick(self, tick: Tick) -> None: ...
