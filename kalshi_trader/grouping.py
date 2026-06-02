"""Group scored markets by event for display.

Shared by the scoring CLI (scripts/score_markets.py) and the dashboard's
background scoring loop so the two stay in lockstep.
"""
from __future__ import annotations

from collections import defaultdict

from kalshi_trader.models import ScoredMarket


def group_by_event(ranked: list[ScoredMarket]) -> list[tuple[float, int, ScoredMarket]]:
    """Group markets by event_ticker and average their scores.

    Returns a list of (average_composite_score, market_count, best_market) tuples,
    sorted by average score descending. ``best_market`` is the highest-scoring
    individual market within the event. Markets with no event_ticker are grouped
    under their own ticker.
    """
    groups: dict[str, list[ScoredMarket]] = defaultdict(list)
    for scored_market in ranked:
        event_key = scored_market.market.event_ticker or scored_market.market.ticker
        groups[event_key].append(scored_market)
    result: list[tuple[float, int, ScoredMarket]] = []
    for event_markets in groups.values():
        average_score = sum(
            scored_market.composite_score for scored_market in event_markets
        ) / len(event_markets)
        best_market = max(event_markets, key=lambda scored_market: scored_market.composite_score)
        result.append((average_score, len(event_markets), best_market))
    result.sort(key=lambda grouped_event: grouped_event[0], reverse=True)
    return result
