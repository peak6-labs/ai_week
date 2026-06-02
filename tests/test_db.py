"""Tests for kalshi_trader/db.py.

All tests mock the Supabase client — no live connection required.
Tests focus on:
  - Field mapping correctness (right values go to right columns)
  - URL safety validation (rejects wrong project)
  - _prepare_polymarket_row edge cases (string JSON, missing fields)
  - resolve_market brier score computation
"""
from __future__ import annotations

import pytest
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

from kalshi_trader.models import (
    OrderAction,
    OrderResult,
    RiskDecision,
    Side,
    SignalEstimate,
    TradeIdea,
)
from kalshi_trader.db import _prepare_polymarket_row


# ---------------------------------------------------------------------------
# _prepare_polymarket_row — pure function, no mocking needed
# ---------------------------------------------------------------------------

def _base_market() -> dict:
    return {
        "conditionId": "0xabc",
        "question": "Will X happen?",
        "outcomePrices": "[0.45, 0.55]",
        "active": True,
        "closed": False,
        "volume24hr": "12345.67",
        "slug": "will-x-happen",
        "bestBid": "0.44",
        "bestAsk": "0.46",
        "endDate": "2026-06-05T00:00:00Z",
        "negRisk": False,
    }


def test_prepare_row_basic_fields():
    row = _prepare_polymarket_row(_base_market())
    assert row["condition_id"] == "0xabc"
    assert row["question"] == "Will X happen?"
    assert row["yes_price"] == pytest.approx(0.45)
    assert row["active"] is True
    assert row["closed"] is False
    assert row["volume_24h"] == pytest.approx(12345.67)
    assert row["slug"] == "will-x-happen"
    assert row["neg_risk"] is False


def test_prepare_row_outcome_prices_parsed_from_string():
    row = _prepare_polymarket_row(_base_market())
    assert row["outcome_prices"] == [0.45, 0.55]


def test_prepare_row_outcome_prices_already_list():
    m = _base_market()
    m["outcomePrices"] = [0.60, 0.40]
    row = _prepare_polymarket_row(m)
    assert row["outcome_prices"] == [0.60, 0.40]
    assert row["yes_price"] == pytest.approx(0.60)


def test_prepare_row_outcome_prices_invalid_string():
    m = _base_market()
    m["outcomePrices"] = "not-json"
    row = _prepare_polymarket_row(m)
    assert row["outcome_prices"] is None
    assert row["yes_price"] is None


def test_prepare_row_end_date_normalised():
    row = _prepare_polymarket_row(_base_market())
    assert row["end_date"] == "2026-06-05T00:00:00+00:00"


def test_prepare_row_missing_volume():
    m = _base_market()
    del m["volume24hr"]
    row = _prepare_polymarket_row(m)
    assert row["volume_24h"] is None


def test_prepare_row_best_bid_ask():
    row = _prepare_polymarket_row(_base_market())
    assert row["best_bid"] == pytest.approx(0.44)
    assert row["best_ask"] == pytest.approx(0.46)


def test_prepare_row_missing_bid_ask():
    m = _base_market()
    del m["bestBid"]
    del m["bestAsk"]
    row = _prepare_polymarket_row(m)
    assert row["best_bid"] is None
    assert row["best_ask"] is None


# ---------------------------------------------------------------------------
# URL safety check
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_get_client_rejects_wrong_project():
    """_get_client must refuse to connect if URL doesn't contain expected ref."""
    import kalshi_trader.db as db_module
    db_module._client = None  # reset singleton

    with patch.object(db_module.config, "SUPABASE_URL", "https://wrongproject.supabase.co"), \
         patch.object(db_module.config, "SUPABASE_SERVICE_KEY", "some-key"):
        with pytest.raises(RuntimeError, match="ai_week project"):
            await db_module._get_client()


@pytest.mark.asyncio
async def test_get_client_rejects_missing_url():
    import kalshi_trader.db as db_module
    db_module._client = None

    with patch.object(db_module.config, "SUPABASE_URL", ""), \
         patch.object(db_module.config, "SUPABASE_SERVICE_KEY", "some-key"):
        with pytest.raises(RuntimeError, match="must be set"):
            await db_module._get_client()


# ---------------------------------------------------------------------------
# insert_signal — field mapping
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_insert_signal_field_mapping():
    import kalshi_trader.db as db_module
    db_module._client = None

    issued = datetime(2026, 6, 2, 12, 0, 0, tzinfo=timezone.utc)
    signal = SignalEstimate(
        source="noaa_gfs",
        probability=0.73,
        uncertainty=0.08,
        weight=0.85,
        data_issued_at=issued,
        metadata={"ticker": "WEATHER-NYC", "data_quality": "fresh"},
    )

    # Supabase uses a synchronous builder chain — only .execute() is async.
    mock_client = MagicMock()
    mock_client.table.return_value.insert.return_value.execute = AsyncMock(
        return_value=MagicMock(data=[{"id": "uuid-123"}])
    )

    with patch.object(db_module, "_get_client", AsyncMock(return_value=mock_client)):
        result_id = await db_module.insert_signal(signal, ticker="WEATHER-NYC", trade_id=None)

    assert result_id == "uuid-123"
    inserted = mock_client.table.return_value.insert.call_args[0][0]
    assert inserted["source"] == "noaa_gfs"
    assert inserted["probability"] == 0.73
    assert inserted["uncertainty"] == 0.08
    assert inserted["weight"] == 0.85
    assert inserted["ticker"] == "WEATHER-NYC"
    assert inserted["trade_id"] is None
    assert inserted["data_issued_at"] == issued.isoformat()


# ---------------------------------------------------------------------------
# insert_trade — field mapping
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_insert_trade_field_mapping():
    import kalshi_trader.db as db_module
    db_module._client = None

    idea = TradeIdea(
        agent_id="weather",
        ticker="WEATHER-NYC",
        side=Side.YES,
        action=OrderAction.BUY,
        confidence=0.73,
        market_price=18.0,
        reasoning="NOAA shows 73% precip vs 18¢ market.",
        signal_sources=["noaa_gfs"],
        suggested_size_dollars=20.0,
        category="weather",
    )
    result = OrderResult(
        order_id="kalshi-order-001",
        ticker="WEATHER-NYC",
        side=Side.YES,
        action=OrderAction.BUY,
        size_dollars=19.8,
        fill_price=18.5,
        status="filled",
        created_at=datetime(2026, 6, 2, 13, 0, tzinfo=timezone.utc),
    )
    decision = RiskDecision(approved=True, approved_size_dollars=20.0)

    mock_client = MagicMock()
    mock_client.table.return_value.insert.return_value.execute = AsyncMock(
        return_value=MagicMock(data=[{"id": "trade-uuid-456"}])
    )

    with patch.object(db_module, "_get_client", AsyncMock(return_value=mock_client)):
        trade_id = await db_module.insert_trade(idea, result, decision, contracts=5)

    assert trade_id == "trade-uuid-456"
    inserted = mock_client.table.return_value.insert.call_args[0][0]
    assert inserted["ticker"] == "WEATHER-NYC"
    assert inserted["side"] == "yes"
    assert inserted["action"] == "buy"
    assert inserted["contracts"] == 5
    assert inserted["entry_price_cents"] == 18.0
    assert inserted["fill_price_cents"] == 18.5
    assert inserted["size_dollars"] == 19.8
    assert inserted["suggested_size_dollars"] == 20.0
    assert inserted["kalshi_order_id"] == "kalshi-order-001"
    assert inserted["agent_id"] == "weather"
    assert inserted["confidence"] == 0.73
    assert inserted["category"] == "weather"


# ---------------------------------------------------------------------------
# resolve_market — brier score
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_resolve_market_brier_score():
    import kalshi_trader.db as db_module
    db_module._client = None

    mock_client = MagicMock()
    # Simulate two signals for the ticker
    mock_client.table.return_value.select.return_value.eq.return_value.is_.return_value.execute = AsyncMock(
        return_value=MagicMock(data=[
            {"id": "sig-1", "probability": 0.73},
            {"id": "sig-2", "probability": 0.40},
        ])
    )
    mock_client.table.return_value.update.return_value.eq.return_value.execute = AsyncMock(
        return_value=MagicMock(data=[{}])
    )

    with patch.object(db_module, "_get_client", AsyncMock(return_value=mock_client)):
        await db_module.resolve_market("WEATHER-NYC", resolved_yes=True)

    # Check brier scores: (prob - 1.0)^2
    update_calls = mock_client.table.return_value.update.call_args_list
    assert len(update_calls) == 2
    scores = {call[0][0]["brier_score"] for call in update_calls}
    expected = {round((0.73 - 1.0) ** 2, 6), round((0.40 - 1.0) ** 2, 6)}
    assert scores == expected
