"""Tests for group_by_event (shared by the scoring CLI and the dashboard)."""
from datetime import datetime, timezone

import pytest

from kalshi_trader.grouping import group_by_event
from kalshi_trader.models import Market, ScoredMarket


def _scored(ticker, event_ticker, composite):
    market = Market(
        ticker=ticker, event_ticker=event_ticker, series_ticker=ticker.split("-")[0],
        title=ticker, yes_bid=40, yes_ask=42, last_price=41, volume_24h=10,
        open_interest=100, category="politics",
        close_time=datetime(2026, 6, 3, tzinfo=timezone.utc), status="open",
    )
    return ScoredMarket(market=market, composite_score=composite, volume_oi_ratio_score=0.1)


def test_groups_by_event_averages_and_picks_best():
    ranked = [
        _scored("EVT-A-1", "EVT-A", 0.80),
        _scored("EVT-A-2", "EVT-A", 0.40),   # same event -> avg 0.60, best is the 0.80
        _scored("EVT-B-1", "EVT-B", 0.70),
    ]
    grouped = group_by_event(ranked)
    assert len(grouped) == 2
    # Sorted by average score descending: EVT-B (0.70) before EVT-A (0.60)
    assert grouped[0][0] == pytest.approx(0.70) and grouped[0][1] == 1
    assert grouped[1][0] == pytest.approx(0.60) and grouped[1][1] == 2
    assert grouped[1][2].market.ticker == "EVT-A-1"   # best market within EVT-A


def test_missing_event_ticker_groups_under_own_ticker():
    ranked = [_scored("LONELY-1", "", 0.5)]
    grouped = group_by_event(ranked)
    assert len(grouped) == 1
    assert grouped[0][1] == 1


def test_empty_input():
    assert group_by_event([]) == []
