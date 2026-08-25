"""Broker layer: paper client + base models + live Deribit client."""
from .base import (
    BrokerClient,
    Order,
    OrderSide,
    OrderStatus,
    OrderType,
    Position,
    Tick,
)
from .paper_client import PaperClient

__all__ = [
    "BrokerClient",
    "Order",
    "OrderSide",
    "OrderStatus",
    "OrderType",
    "Position",
    "Tick",
    "PaperClient",
]

# Live client is optional — only importable if the module is on disk.
# Importing the module does NOT auto-connect; it only exposes the class.
try:
    from .deribit_client import (  # noqa: F401
        DeribitClient,
        DeribitAuthError,
        DeribitSafetyError,
    )
    __all__ += ["DeribitClient", "DeribitAuthError", "DeribitSafetyError"]
except Exception:
    pass
