"""Dashboard package — read-only HTTP status page for the paper/live bot."""
from .server import DashboardServer, start_dashboard

__all__ = ["DashboardServer", "start_dashboard"]
