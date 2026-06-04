"""Tests for kalshi_trader/weather_report.py — the pure ranking/render core of
the standard weather-scan deliverable. No network."""
from __future__ import annotations

from kalshi_trader.weather_report import (
    SUSPECT_EDGE_CENTS,
    SUSPECT_MIN_VOLUME_24H,
    ScoredWeatherMarket,
    is_suspect_edge,
    rank_and_render,
    rank_markets,
    render_table,
    score_sides,
)
from kalshi_trader.web_links import kalshi_market_url


def _row(ticker: str, edge_cents: float, *, side: str = "YES",
         yes_bid: float | None = 40.0, yes_ask: float | None = 50.0,
         forecast_point: str = "station:KLAX") -> ScoredWeatherMarket:
    return ScoredWeatherMarket(
        ticker=ticker, forecast_point=forecast_point, side=side,
        model_probability=0.55, fair_cents=55.0, yes_bid=yes_bid, yes_ask=yes_ask,
        edge_cents=edge_cents, volume_24h=1234,
    )


def _data_rows(table_markdown: str) -> list[str]:
    """The table's data rows: lines starting with '|' that begin with a rank digit."""
    return [
        line for line in table_markdown.splitlines()
        if line.startswith("|") and line.lstrip("| ")[:1].isdigit()
    ]


# ---------------------------------------------------------------------------
# score_sides
# ---------------------------------------------------------------------------

def test_score_sides_picks_yes_when_model_above_ask():
    # Model 70¢ fair, ask 50¢ → YES edge +20; bid 45¢ → NO edge -25. YES wins.
    side, edge_cents, fair_cents = score_sides(0.70, yes_bid=45.0, yes_ask=50.0)
    assert side == "YES"
    assert edge_cents == 20.0
    assert fair_cents == 70.0


def test_score_sides_picks_no_when_model_below_bid():
    # Model 30¢ fair, bid 45¢ → NO edge +15; ask 50¢ → YES edge -20. NO wins.
    side, edge_cents, fair_cents = score_sides(0.30, yes_bid=45.0, yes_ask=50.0)
    assert side == "NO"
    assert edge_cents == 15.0


def test_score_sides_missing_quote_makes_side_ineligible():
    # No ask → YES side ineligible; NO edge = bid - fair.
    side, edge_cents, _ = score_sides(0.20, yes_bid=45.0, yes_ask=None)
    assert side == "NO"
    assert edge_cents == 25.0


# ---------------------------------------------------------------------------
# rank_markets / rank_and_render
# ---------------------------------------------------------------------------

def test_rank_markets_sorts_by_edge_descending():
    rows = [_row("KXA-1", 3.0), _row("KXB-1", 9.0), _row("KXC-1", -2.0), _row("KXD-1", 5.0)]
    ranked = rank_markets(rows)
    assert [market.edge_cents for market in ranked] == [9.0, 5.0, 3.0, -2.0]


def test_rank_markets_takes_top_25_even_without_positive_edge():
    # 40 rows, ALL strictly-negative edge → still returns exactly 25, highest
    # (least negative) first.
    rows = [_row(f"KXNEG{index:02d}-1", -float(index) - 1.0) for index in range(40)]
    ranked = rank_markets(rows)
    assert len(ranked) == 25
    assert ranked[0].edge_cents == -1.0  # least-negative is the largest
    assert ranked[-1].edge_cents == -25.0


def test_rank_markets_returns_all_when_fewer_than_top_n():
    rows = [_row("KXA-1", 3.0), _row("KXB-1", 1.0)]
    assert len(rank_markets(rows)) == 2


def test_rank_and_render_shows_exactly_25_rows_when_more_inputs():
    rows = [_row(f"KXEDGE{index:02d}-1", float(index)) for index in range(40)]
    table = rank_and_render(rows)
    assert len(_data_rows(table)) == 25


def test_rank_and_render_shows_all_rows_when_fewer():
    rows = [_row(f"KXEDGE{index:02d}-1", float(index)) for index in range(5)]
    table = rank_and_render(rows)
    assert len(_data_rows(table)) == 5


def test_rank_and_render_includes_live_bid_ask():
    rows = [_row("KXHIGHLAX-26JUN04-T85", 12.0, yes_bid=41.0, yes_ask=48.0)]
    table = rank_and_render(rows)
    assert "41/48" in table  # live bid/ask present


def test_rank_and_render_renders_ticker_with_series_link():
    ticker = "KXHIGHLAX-26JUN04-T85"
    rows = [_row(ticker, 12.0)]
    table = rank_and_render(rows)
    # Backticked ticker wrapped in its safe series link (per CLAUDE.md).
    assert f"[`{ticker}`]({kalshi_market_url(ticker)})" in table
    assert "https://kalshi.com/markets/kxhighlax" in table


def test_rank_and_render_renders_missing_quote_as_dash():
    rows = [_row("KXHIGHLAX-26JUN04-T85", 5.0, yes_bid=None, yes_ask=48.0)]
    table = rank_and_render(rows)
    assert "—/48" in table


def test_render_table_marks_centroid_fallback():
    rows = [_row("KXTEMPNYCH-26JUN04", 4.0, forecast_point="centroid")]
    table = render_table(rank_markets(rows))
    assert "centroid" in table


# ---------------------------------------------------------------------------
# Suspect-edge guardrail: an outsized edge against a liquid market is a likely
# model artifact, held out of the ranked table (2026-06-04 weather-scan audit).
# ---------------------------------------------------------------------------

def test_is_suspect_edge_flags_large_edge_on_liquid_market():
    assert is_suspect_edge(98.0, 3000.0) is True
    # Both thresholds are inclusive boundaries.
    assert is_suspect_edge(SUSPECT_EDGE_CENTS, SUSPECT_MIN_VOLUME_24H) is True


def test_is_suspect_edge_allows_modest_or_illiquid_edges():
    assert is_suspect_edge(SUSPECT_EDGE_CENTS - 1.0, 5000.0) is False   # edge too small
    assert is_suspect_edge(98.0, SUSPECT_MIN_VOLUME_24H - 1.0) is False  # market too thin


def test_rank_and_render_suppresses_suspect_rows_from_main_table():
    # A +98¢ edge on a liquid market (_row defaults vol 1234) is an artifact; an 8¢
    # edge is a normal candidate. The artifact must be held out of the ranked table
    # and called out below the fold; the real edge stays in the main table.
    rows = [
        _row("KXARTIFACT-1", 98.0, yes_bid=99.0, yes_ask=100.0),
        _row("KXREAL-1", 8.0),
    ]
    table = rank_and_render(rows)
    assert "suppressed as suspect" in table.lower()
    main, marker, suppressed = table.partition("## ⚠️ Suppressed as suspect")
    assert marker  # the suppressed section is present
    assert "KXREAL-1" in main and "KXARTIFACT-1" not in main
    assert "KXARTIFACT-1" in suppressed


def test_rank_and_render_no_suspect_section_when_none_suspect():
    # All modest edges → no suppression note, no suppressed section.
    rows = [_row(f"KXEDGE{index:02d}-1", float(index)) for index in range(5)]
    table = rank_and_render(rows)
    assert "Suppressed as suspect" not in table
    assert "suppressed" not in table.lower()
