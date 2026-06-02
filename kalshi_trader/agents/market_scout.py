"""Serialize scored Kalshi markets into JSON-able rows for the market-scout agent.

The market-scout agent (``.claude/agents/market-scout.md``) consumes this via
``scripts/score_markets.py --json``. Each event becomes one row carrying every
signal, its coverage, a bid/ask-spread liquidity read, and a series web link —
the full picture the agent reasons over, not just the five signals the human
table prints.

The implementation now lives in ``kalshi_trader.grouping`` so the dashboard's
/api/ideas endpoint and this agent present events identically. These names are
re-exported here to keep the agent-facing import path stable.
"""
from __future__ import annotations

from kalshi_trader.grouping import (
    coverage_fraction,
    serialize_event_group,
    serialize_event_groups,
)

__all__ = ["coverage_fraction", "serialize_event_group", "serialize_event_groups"]
