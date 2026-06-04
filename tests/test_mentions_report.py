"""Tests for the pure mentions-scan renderer (kalshi_trader.mentions_report)."""
from __future__ import annotations

from kalshi_trader.mentions_report import rank_and_render


def _row(ticker: str, edge_cents: float, **overrides) -> dict:
    row = {
        "ticker": ticker,
        "speaker": "Powell",
        "word": "recession",
        "window": "2026-06-18",
        "model_probability": 0.6,
        "fair_cents": 60.0,
        "yes_bid": 40,
        "yes_ask": 42,
        "side": "YES",
        "edge_cents": edge_cents,
        "volume_24h": 123,
        "quality": "gdelt-only",
    }
    row.update(overrides)
    return row


def _data_rows(markdown: str) -> list[str]:
    """Table body rows (skip the 2 header lines)."""
    return [line for line in markdown.splitlines() if line.startswith("| ") and "---" not in line][1:]


def test_sorted_by_edge_descending():
    rows = [_row("KXA-1", 2.0), _row("KXB-2", 9.0), _row("KXC-3", 5.0)]
    body = _data_rows(rank_and_render(rows))
    # Highest edge first.
    assert "KXB-2" in body[0]
    assert "KXC-3" in body[1]
    assert "KXA-1" in body[2]


def test_caps_at_top_n_when_more_inputs():
    rows = [_row(f"KX-{index}", float(index)) for index in range(30)]
    body = _data_rows(rank_and_render(rows, top_n=25))
    assert len(body) == 25


def test_shows_all_rows_when_fewer_than_top_n():
    rows = [_row("KX-1", 1.0), _row("KX-2", 2.0), _row("KX-3", 3.0)]
    body = _data_rows(rank_and_render(rows, top_n=25))
    assert len(body) == 3


def test_includes_negative_edge_rows_to_fill_table():
    rows = [_row("KX-POS", 4.0), _row("KX-NEG", -8.0)]
    body = _data_rows(rank_and_render(rows, top_n=25))
    assert len(body) == 2  # negative-edge row still shown
    assert "-8.0" in body[1]


def test_live_bid_ask_present():
    markdown = rank_and_render([_row("KXFOO-1", 5.0, yes_bid=33, yes_ask=37)])
    assert "33/37" in markdown


def test_ticker_rendered_with_series_link():
    markdown = rank_and_render([_row("KXFEDMENTION-26JUN-RECE", 5.0)])
    assert "`KXFEDMENTION-26JUN-RECE`" in markdown
    # backtick label wrapped in a kalshi.com series link
    assert "](https://kalshi.com/markets/" in markdown


def test_quality_flag_shown():
    markdown = rank_and_render([_row("KX-1", 5.0, quality="suppressed")])
    assert "suppressed" in markdown
