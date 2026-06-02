"""Tests for kalshi_trader/external/market_scorer.py — TDD RED phase first."""
from datetime import datetime, timedelta, timezone

import pytest

from kalshi_trader.models import Market
from kalshi_trader.external.market_scorer import (
    score_market,
    should_take_profit,
    is_stale_thesis,
)


# ---------------------------------------------------------------------------
# Fixture helpers
# ---------------------------------------------------------------------------

def _make_market(hours_to_close=24, open_interest=2000, yes_bid=48, yes_ask=52):
    return Market(
        ticker="TEST-1",
        event_ticker="TEST",
        series_ticker="TEST",
        title="Will X happen?",
        yes_bid=float(yes_bid),
        yes_ask=float(yes_ask),
        last_price=50.0,
        volume_24h=5000,
        open_interest=open_interest,
        category="test",
        close_time=datetime.now(tz=timezone.utc) + timedelta(hours=hours_to_close),
        status="open",
    )


# ---------------------------------------------------------------------------
# score_market — filter tests
# ---------------------------------------------------------------------------

def test_score_market_returns_none_when_gap_below_7_cents():
    """Edge < 7 cents (0.07) → disqualified."""
    market = _make_market()
    # midpoint = (48 + 52) / 2 = 50 cents = 0.50 in probability
    # polymarket_prob close to midpoint → tiny gap
    polymarket_prob = 0.53  # gap = |0.53 - 0.50| = 0.03 < 0.07
    result = score_market(market, polymarket_prob)
    assert result is None


def test_score_market_returns_none_when_open_interest_below_500():
    """open_interest < 500 → disqualified (not enough depth)."""
    market = _make_market(open_interest=499)
    polymarket_prob = 0.65  # gap = |0.65 - 0.50| = 0.15 > 0.07
    result = score_market(market, polymarket_prob)
    assert result is None


def test_score_market_returns_none_when_closes_in_under_4_hours():
    """hours_to_resolution < 4 → too late to enter."""
    market = _make_market(hours_to_close=3)
    polymarket_prob = 0.65
    result = score_market(market, polymarket_prob)
    assert result is None


def test_score_market_returns_none_when_closes_in_over_168_hours():
    """hours_to_resolution > 168 (1 week) → too slow."""
    market = _make_market(hours_to_close=169)
    polymarket_prob = 0.65
    result = score_market(market, polymarket_prob)
    assert result is None


def test_score_market_returns_dict_for_qualifying_market():
    """All thresholds met → returns a non-None dict."""
    market = _make_market(hours_to_close=24, open_interest=2000)
    polymarket_prob = 0.65  # gap = 0.15 > 0.07; depth 2000 >= 500; hours ok
    result = score_market(market, polymarket_prob)
    assert result is not None
    assert isinstance(result, dict)


def test_score_market_dict_contains_gap_depth_hours_ev():
    """Returned dict must include 'market', 'gap', 'depth', 'hours', and 'ev' keys."""
    market = _make_market(hours_to_close=24, open_interest=2000)
    polymarket_prob = 0.65
    result = score_market(market, polymarket_prob)
    assert result is not None
    for key in ("market", "gap", "depth", "hours", "ev"):
        assert key in result, f"Missing key: {key}"


# ---------------------------------------------------------------------------
# should_take_profit — exit trigger tests
# ---------------------------------------------------------------------------

def test_take_profit_true_at_85_pct_of_gap():
    """Exactly at 85 % threshold → take profit."""
    entry_price = 0.50
    expected_gap = 0.20
    # target = entry + 0.85 * gap = 0.50 + 0.17 = 0.67
    current_price = 0.50 + 0.85 * 0.20  # == 0.67
    assert should_take_profit(entry_price, current_price, expected_gap) is True


def test_take_profit_false_below_85_pct_of_gap():
    """Price moved less than 85 % of expected gap → do NOT take profit."""
    entry_price = 0.50
    expected_gap = 0.20
    # 84 % of gap
    current_price = 0.50 + 0.84 * 0.20  # 0.668
    assert should_take_profit(entry_price, current_price, expected_gap) is False


def test_take_profit_true_above_85_pct_of_gap():
    """Price blew past target → definitely take profit."""
    entry_price = 0.50
    expected_gap = 0.20
    # 100 % of gap
    current_price = 0.50 + 1.00 * 0.20  # 0.70
    assert should_take_profit(entry_price, current_price, expected_gap) is True


# ---------------------------------------------------------------------------
# is_stale_thesis — exit trigger tests
# ---------------------------------------------------------------------------

def test_stale_thesis_true_after_24h_with_small_price_change():
    """More than 24 h elapsed and price barely moved → thesis is stale."""
    hours_since_entry = 25.0
    price_change_abs = 0.01  # < 0.02 threshold
    assert is_stale_thesis(hours_since_entry, price_change_abs) is True


def test_stale_thesis_false_under_24h():
    """Less than 24 h elapsed → not yet stale, regardless of price movement."""
    hours_since_entry = 23.9
    price_change_abs = 0.005
    assert is_stale_thesis(hours_since_entry, price_change_abs) is False


def test_stale_thesis_false_after_24h_with_large_price_change():
    """More than 24 h but price moved significantly → thesis still live."""
    hours_since_entry = 30.0
    price_change_abs = 0.05  # > 0.02 threshold
    assert is_stale_thesis(hours_since_entry, price_change_abs) is False
