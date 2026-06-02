"""Read-only web dashboard for monitoring the Kalshi portfolio.

See app.py for the entry point. This package must never place or cancel orders.
"""
from kalshi_trader.dashboard.app import app, create_app

__all__ = ["app", "create_app"]
