"""Tests for kalshi_trader/agents/market_scout — the JSON serializer the
market-scout agent consumes via `scripts/score_markets.py --json`."""
from __future__ import annotations

from datetime import datetime

from kalshi_trader.agents.market_scout import (
    coverage_fraction,
    serialize_event_group,
    serialize_event_groups,
)
from kalshi_trader.models import Market, ScoredMarket


def _market(
    ticker: str = "KXMARTINDNCOUT-26MAY-YES",
    event_ticker: str = "KXMARTINDNCOUT-26MAY",
    yes_bid: float = 60.0,
    yes_ask: float = 62.0,
) -> Market:
    return Market(
        ticker=ticker, event_ticker=event_ticker, series_ticker="KXMARTINDNCOUT",
        title="Ken Martin out as DNC chair?", yes_bid=yes_bid, yes_ask=yes_ask,
        last_price=61.0, volume_24h=5000, open_interest=12000,
        category="politics", close_time=datetime(2026, 5, 31, 12, 0, 0),
        status="open",
    )


def _scored(
    market: Market | None = None,
    composite_score: float = 0.85,
    full_coverage: bool = True,
) -> ScoredMarket:
    """A ScoredMarket. full_coverage=True fills all 9 signals; otherwise only
    the two that need no candle history (volume_oi_ratio is required)."""
    market = market or _market()
    if full_coverage:
        return ScoredMarket(
            market=market, composite_score=composite_score,
            volume_oi_ratio_score=0.5, relative_historical_volume_score=0.87,
            volume_spike_short_term_score=0.95, oi_change_score=0.7,
            momentum_score=0.68, intraday_hl_score=0.9, weekly_hl_score=0.8,
            ofi_score=1.0, orderbook_skew_score=0.3,
        )
    # Only volume_oi_ratio present (weight 0.10 of total 1.00)
    return ScoredMarket(
        market=market, composite_score=composite_score,
        volume_oi_ratio_score=0.5,
    )


def test_coverage_fraction_full():
    assert coverage_fraction(_scored(full_coverage=True)) == 1.0


def test_coverage_fraction_partial():
    # Only volume_oi_ratio (weight 0.10) is present out of total weight 1.00.
    assert coverage_fraction(_scored(full_coverage=False)) == 0.10


def test_serialize_event_group_shape_and_values():
    row = serialize_event_group(0.852, 3, _scored())
    assert row["event_ticker"] == "KXMARTINDNCOUT-26MAY"
    assert row["best_market_ticker"] == "KXMARTINDNCOUT-26MAY-YES"
    assert row["title"] == "Ken Martin out as DNC chair?"
    assert row["market_count"] == 3
    assert row["average_score"] == 0.852
    assert row["best_score"] == 0.85
    assert row["coverage_pct"] == 100.0
    # Liquidity read from the spread (cents).
    assert row["spread_cents"] == 2.0
    assert row["one_sided"] is False
    assert row["open_interest"] == 12000
    assert row["volume_24h"] == 5000
    # All nine signals carried through, keyed by canonical name.
    assert set(row["signals"]) == {
        "volume_oi_ratio", "relative_historical_volume", "volume_spike_short_term",
        "oi_change", "price_momentum", "intraday_hl", "weekly_hl", "ofi",
        "orderbook_skew",
    }
    assert row["close_time"] == "2026-05-31T12:00:00"
    # Series link is built from the safe helper — series prefix, lowercased.
    assert row["series_url"] == "https://kalshi.com/markets/kxmartindncout"


def test_serialize_event_group_one_sided_book():
    # No bid (yes_bid == 0) marks a one-sided, hard-to-trade book.
    row = serialize_event_group(0.5, 1, _scored(market=_market(yes_bid=0.0, yes_ask=80.0)))
    assert row["one_sided"] is True
    assert row["spread_cents"] == 80.0


def test_serialize_event_group_missing_signals_are_null():
    row = serialize_event_group(0.3, 1, _scored(full_coverage=False))
    assert row["signals"]["volume_oi_ratio"] == 0.5
    assert row["signals"]["ofi"] is None
    assert row["coverage_pct"] == 10.0


def test_serialize_event_groups_returns_all_rows_no_truncation():
    grouped = [
        (0.9 - index * 0.01, 1, _scored(composite_score=0.9 - index * 0.01))
        for index in range(50)
    ]
    rows = serialize_event_groups(grouped)
    assert len(rows) == 50
    # Order is preserved from the (already sorted) input.
    assert rows[0]["average_score"] >= rows[-1]["average_score"]
