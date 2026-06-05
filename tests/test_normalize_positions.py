"""Tests for the metadata + opened-at join performed by normalize_positions.

These focus on the enrichment that turns raw closed/open dicts into trade rows
with market_type, days_to_settlement, and holding_days — including the graceful
fallthrough when metadata or opened_at is missing.
"""
from datetime import datetime, timezone

import pytest

from kalshi_trader.ui.pnl_analytics import bucket_days_to_settlement, normalize_positions

NOW = datetime(2026, 6, 5, 0, 0, 0, tzinfo=timezone.utc)


def _closed(ticker, opened_at="2026-06-01T00:00:00Z", closed_at="2026-06-03T00:00:00Z"):
    return {
        "ticker": ticker,
        "side": "YES",
        "contracts": 10,
        "entry_price_cents": 30.0,
        "exit_price_cents": 60.0,
        "opened_at": opened_at,
        "closed_at": closed_at,
        "realized_pnl_dollars": 3.0,
        "gross_realized_pnl_dollars": 3.3,
    }


def _open(ticker):
    return {
        "ticker": ticker,
        "side": "YES",
        "quantity": 10,
        "avg_price_dollars": 0.40,
        "current_price_dollars": 0.55,
        "unrealized_pnl_dollars": 1.5,
        "gross_unrealized_pnl_dollars": 1.6,
        "realized_pnl_dollars": 0.0,
    }


def test_market_type_attached_from_metadata():
    metadata = {"FOO-1": {"market_type": "weather", "close_time": "2026-06-03T00:00:00Z"}}
    rows = normalize_positions([_closed("FOO-1")], [], metadata, {}, NOW)
    assert rows[0]["market_type"] == "weather"


def test_market_type_unknown_when_metadata_missing():
    rows = normalize_positions([_closed("FOO-1")], [], {}, {}, NOW)
    assert rows[0]["market_type"] == "unknown"


def test_days_to_settlement_for_closed_uses_close_time_minus_opened_at():
    # opened 06-01, close 06-03 → 2 days
    metadata = {"FOO-1": {"market_type": "x", "close_time": "2026-06-03T00:00:00Z"}}
    rows = normalize_positions([_closed("FOO-1", opened_at="2026-06-01T00:00:00Z")], [], metadata, {}, NOW)
    assert rows[0]["days_to_settlement"] == 2
    assert bucket_days_to_settlement(rows[0]["days_to_settlement"]) == "1–3d"


def test_days_to_settlement_for_open_uses_opened_at_lookup():
    # open ticker has no opened_at natively; comes from the lookup. opened 06-01, close 06-09 → 8 days
    metadata = {"BAR-1": {"market_type": "x", "close_time": "2026-06-09T00:00:00Z"}}
    rows = normalize_positions([], [_open("BAR-1")], metadata, {"BAR-1": "2026-06-01T00:00:00Z"}, NOW)
    assert rows[0]["days_to_settlement"] == 8
    assert bucket_days_to_settlement(rows[0]["days_to_settlement"]) == "1–2wk"


def test_missing_close_time_gives_none_days_and_unknown_bucket():
    metadata = {"FOO-1": {"market_type": "weather", "close_time": None}}
    rows = normalize_positions([_closed("FOO-1")], [], metadata, {}, NOW)
    assert rows[0]["days_to_settlement"] is None
    assert bucket_days_to_settlement(rows[0]["days_to_settlement"]) == "unknown"


def test_open_missing_opened_at_handled_gracefully():
    # No opened_at_lookup entry for the open ticker.
    metadata = {"BAR-1": {"market_type": "x", "close_time": "2026-06-09T00:00:00Z"}}
    rows = normalize_positions([], [_open("BAR-1")], metadata, {}, NOW)
    assert rows[0]["opened_at"] is None
    assert rows[0]["days_to_settlement"] is None
    assert rows[0]["holding_days"] is None
