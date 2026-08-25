"""Execution layer: order manager that turns plans into broker orders."""
from .order_manager import ManagedTrade, OrderManager

__all__ = ["ManagedTrade", "OrderManager"]
