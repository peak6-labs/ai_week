"""Market scoring and exit trigger logic.

Adapts the LunarResearcher Polymarket strategy for Kalshi markets.
All functions are stateless — no class needed.
"""
from __future__ import annotations

from datetime import datetime, timezone

from kalshi_trader.models import Market
from kalshi_trader.ui.config_manager import cfg


def score_market(market: Market, polymarket_prob: float) -> dict | None:
    """Score a Kalshi market against a Polymarket probability estimate.

    Returns None if the market fails any filter threshold, otherwise returns
    a dict with keys: market, gap, depth, hours, ev.

    Filters (mirrors LunarResearcher strategy):
    - gap < 0.07  → edge too thin
    - open_interest < 500  → can't fill
    - hours_to_close < 4  → too late
    - hours_to_close > 168  → too slow (>1 week)

    Args:
        market: A Kalshi Market dataclass.
        polymarket_prob: Polymarket yes-probability in [0.0, 1.0].
    """
    # Convert Kalshi midpoint from cents (0–100) to probability (0.0–1.0)
    midpoint_cents = (market.yes_bid + market.yes_ask) / 2.0
    midpoint_prob = midpoint_cents / 100.0

    gap = abs(polymarket_prob - midpoint_prob)
    depth = market.open_interest

    now = datetime.now(tz=timezone.utc)
    close_time = market.close_time
    # Ensure close_time is timezone-aware for comparison
    if close_time.tzinfo is None:
        close_time = close_time.replace(tzinfo=timezone.utc)
    hours_left = (close_time - now).total_seconds() / 3600.0

    # --- Hard filters ---
    if gap < cfg.get("poly_min_gap_cents") / 100.0:
        return None
    if depth < cfg.get("filter_min_open_interest"):
        return None
    if hours_left < cfg.get("filter_min_hours_to_close"):
        return None
    if hours_left > cfg.get("filter_max_hours_to_close"):
        return None

    # Expected value: gap * (1 - midpoint_prob) gives rough EV for a YES bet
    # when polymarket is higher than Kalshi price; keep simple.
    ev = gap * (1.0 - midpoint_prob)

    return {
        "market": market,
        "gap": gap,
        "depth": depth,
        "hours": hours_left,
        "ev": ev,
    }


def should_take_profit(
    entry_price: float,
    current_price: float,
    expected_gap: float,
) -> bool:
    """Return True when price has moved at least 85 % of the expected gap.

    Args:
        entry_price: Price at time of entry (probability, 0.0–1.0).
        current_price: Current market price (probability, 0.0–1.0).
        expected_gap: The edge gap identified at entry time.
    """
    target = entry_price + expected_gap * cfg.get("exit_take_profit_threshold")
    return current_price >= target


def is_stale_thesis(hours_since_entry: float, price_change_abs: float) -> bool:
    """Return True when a position's thesis has gone stale.

    Staleness criteria: more than 24 hours since entry AND the absolute
    price change is less than 2 cents (0.02), indicating the market has
    not moved toward our thesis.

    Args:
        hours_since_entry: How many hours since the position was opened.
        price_change_abs: Absolute price change since entry (probability units).
    """
    return hours_since_entry > cfg.get("exit_stale_thesis_hours") and price_change_abs < cfg.get("exit_stale_thesis_min_move")
