"""Parsing helper for Kalshi fixed-point numeric strings.

Kalshi's API returns many numeric values (dollar amounts, signed contract
counts) as fixed-point STRINGS rather than JSON numbers. This single helper
normalizes them to ``float``. It lived in ``dashboard/portfolio_mapping.py``
until that read-only monitor was removed; it is kept here because several
scripts (``evaluate_portfolio``, ``exit_monitor``, ``night_execute``,
``place_order``) rely on it.
"""
from __future__ import annotations

from typing import Any


def parse_fixed_point(value: Any) -> float:
    """Parse a Kalshi fixed-point string (dollars or contract count) to float."""
    if value is None or value == "":
        return 0.0
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0
