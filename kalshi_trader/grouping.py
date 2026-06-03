"""Group and serialize scored markets by event.

Shared by the scoring CLI (scripts/score_markets.py), the market-scout agent
(kalshi_trader/agents/market_scout.py re-exports the serializers), and the
dashboard's /api/ideas endpoint — so all three present events identically.
"""
from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timezone

from kalshi_trader.actionability.scorer import MarketScorer
from kalshi_trader.agents.kalshi_bias_agent import build_bias_estimate
from kalshi_trader.agents.parsing import estimate_to_dict
from kalshi_trader.models import ScoredMarket
from kalshi_trader.signals.microstructure import estimate_from_signed
from kalshi_trader.web_links import kalshi_market_url


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


def coverage_fraction(scored_market: ScoredMarket) -> float:
    """Fraction (0.0-1.0) of total signal weight that actually contributed.

    A high score at low coverage rests on only a few signals — trust it less.
    """
    weights = MarketScorer.WEIGHTS
    scores = MarketScorer._scores_dict(scored_market)
    present_weight = sum(
        weight for signal_name, weight in weights.items()
        if scores.get(signal_name) is not None
    )
    return present_weight / sum(weights.values())


def serialize_event_group(
    average_score: float,
    market_count: int,
    best_market: ScoredMarket,
) -> dict:
    """Turn one ``group_by_event()`` entry into a JSON-able row.

    ``best_market`` is the highest-scoring market within the event; its prices,
    signals, and liquidity stand in for the event. Liquidity is read from the
    bid/ask spread (tighter = more liquid); a missing bid or ask marks a one-sided
    book that is hard to enter or exit.
    """
    market = best_market.market
    spread_cents = market.yes_ask - market.yes_bid
    event_ticker = market.event_ticker or market.ticker
    midpoint_cents = (market.yes_bid + market.yes_ask) / 2.0
    close_time = market.close_time
    if close_time.tzinfo is None:
        close_time = close_time.replace(tzinfo=timezone.utc)
    hours_to_close = max(
        0.0, (close_time - datetime.now(timezone.utc)).total_seconds() / 3600.0
    )

    # Deterministic signal estimates computed from data the scout already
    # fetched — emitted so the pipeline gets real signals with no extra calls.
    signal_estimates: list[dict] = []
    microstructure = estimate_from_signed(
        price_cents=midpoint_cents,
        momentum_cents=best_market.signed_momentum_cents,
        ofi=best_market.signed_ofi,
        skew=best_market.signed_orderbook_skew,
        position=best_market.range_position,
        ticker=market.ticker,
    )
    if microstructure is not None:
        signal_estimates.append(estimate_to_dict(microstructure))
    bias = build_bias_estimate(
        market.ticker, market.title, market.category, hours_to_close, midpoint_cents
    )
    if bias is not None:
        signal_estimates.append(estimate_to_dict(bias))

    return {
        "event_ticker": event_ticker,
        "best_market_ticker": market.ticker,
        "title": market.title,
        "category": market.category,
        "market_count": market_count,
        "average_score": round(average_score, 4),
        "best_score": round(best_market.composite_score, 4),
        "coverage_pct": round(coverage_fraction(best_market) * 100, 1),
        "yes_bid": market.yes_bid,
        "yes_ask": market.yes_ask,
        "spread_cents": round(spread_cents, 2),
        "one_sided": market.yes_bid == 0 or market.yes_ask == 0,
        "last_price": market.last_price,
        "open_interest": market.open_interest,
        "volume_24h": market.volume_24h,
        "signals": MarketScorer._scores_dict(best_market),
        "signal_estimates": signal_estimates,
        "close_time": market.close_time.isoformat(),
        "series_url": kalshi_market_url(event_ticker),
    }


def serialize_event_groups(
    grouped: list[tuple[float, int, ScoredMarket]],
) -> list[dict]:
    """Serialize every ``group_by_event()`` entry — no truncation, all events."""
    return [
        serialize_event_group(average_score, market_count, best_market)
        for average_score, market_count, best_market in grouped
    ]
