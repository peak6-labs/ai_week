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


# ---------------------------------------------------------------------------
# Ideas History reader — row normalizers + recommendations_with_marks
# ---------------------------------------------------------------------------

def test_normalize_recommendation_row_remaps_id_and_created_at():
    from kalshi_trader.db import _normalize_recommendation_row
    row = {
        "id": "uuid-1", "created_at": "2026-06-03T04:00:00+00:00",
        "ticker": "KXTEST-A", "side": "no", "entry_price_cents": 54.0,
        "disposition": "worth_trading", "paper_only": True, "sources": ["microstructure"],
    }
    normalized = _normalize_recommendation_row(row)
    assert normalized["rec_id"] == "uuid-1"
    assert normalized["recorded_at"] == "2026-06-03T04:00:00+00:00"
    assert "id" not in normalized and "created_at" not in normalized
    # mode flag and other fields pass through unchanged (real-trading invariant)
    assert normalized["paper_only"] is True
    assert normalized["disposition"] == "worth_trading"
    assert normalized["ticker"] == "KXTEST-A"


def test_normalize_mark_row_remaps_recommendation_id():
    from kalshi_trader.db import _normalize_mark_row
    row = {
        "recommendation_id": "uuid-1", "checked_at": "2026-06-03T05:00:00+00:00",
        "current_value_cents": 60.0, "pnl_cents": 6.0, "would_profit": True, "resolved": False,
    }
    normalized = _normalize_mark_row(row)
    assert normalized["rec_id"] == "uuid-1"
    assert "recommendation_id" not in normalized
    assert normalized["checked_at"] == "2026-06-03T05:00:00+00:00"
    assert normalized["pnl_cents"] == 6.0


@pytest.mark.asyncio
async def test_recommendations_with_marks_joins_and_remaps():
    import kalshi_trader.db as db_module
    db_module._client = None

    rec_rows = [
        {"id": "uuid-a", "created_at": "2026-06-03T04:00:00+00:00", "ticker": "KXTEST-A",
         "side": "no", "entry_price_cents": 54.0, "disposition": "worth_trading", "paper_only": True},
        {"id": "uuid-b", "created_at": "2026-06-03T05:00:00+00:00", "ticker": "KXTEST-B",
         "side": "yes", "entry_price_cents": 30.0, "disposition": "approved", "paper_only": True},
    ]
    mark_rows = [
        {"recommendation_id": "uuid-a", "checked_at": "2026-06-03T11:00:00+00:00",
         "current_value_cents": 60.0, "pnl_cents": 6.0, "would_profit": True, "resolved": False},
        {"recommendation_id": "uuid-a", "checked_at": "2026-06-03T04:00:00+00:00",
         "current_value_cents": 54.0, "pnl_cents": 0.0, "would_profit": False, "resolved": False},
    ]

    def table(name):
        builder = MagicMock()
        data = rec_rows if name == "recommendations" else mark_rows
        builder.select.return_value.execute = AsyncMock(return_value=MagicMock(data=data))
        return builder

    mock_client = MagicMock()
    mock_client.table.side_effect = table

    with patch.object(db_module, "_get_client", AsyncMock(return_value=mock_client)):
        ideas = await db_module.recommendations_with_marks()

    # newest first by recorded_at
    assert [idea["rec_id"] for idea in ideas] == ["uuid-b", "uuid-a"]
    rec_a = next(idea for idea in ideas if idea["rec_id"] == "uuid-a")
    assert rec_a["recorded_at"] == "2026-06-03T04:00:00+00:00"
    assert rec_a["paper_only"] is True
    # marks joined, oldest first, elapsed computed
    assert len(rec_a["marks"]) == 2
    assert rec_a["marks"][0]["elapsed_seconds"] == pytest.approx(0.0)
    assert rec_a["marks"][1]["elapsed_seconds"] == pytest.approx(7 * 3600)


@pytest.mark.asyncio
async def test_insert_recommendation_writes_recorded_at_as_created_at():
    import kalshi_trader.db as db_module
    db_module._client = None
    mock_client = MagicMock()
    mock_client.table.return_value.upsert.return_value.execute = AsyncMock(
        return_value=MagicMock(data=[{"id": "rec-1"}]))
    rec = {"rec_id": "rec-1", "recorded_at": "2026-06-03T04:00:00+00:00", "cycle_ts": "c1",
           "ticker": "KXTEST-A", "side": "no", "entry_price_cents": 54.0}
    with patch.object(db_module, "_get_client", AsyncMock(return_value=mock_client)):
        await db_module.insert_recommendation(rec)
    upserted = mock_client.table.return_value.upsert.call_args[0][0]
    assert upserted["created_at"] == "2026-06-03T04:00:00+00:00"


@pytest.mark.asyncio
async def test_insert_recommendation_omits_created_at_when_absent():
    import kalshi_trader.db as db_module
    db_module._client = None
    mock_client = MagicMock()
    mock_client.table.return_value.upsert.return_value.execute = AsyncMock(
        return_value=MagicMock(data=[{"id": "rec-1"}]))
    rec = {"rec_id": "rec-1", "ticker": "KXTEST-A", "side": "no", "entry_price_cents": 54.0}
    with patch.object(db_module, "_get_client", AsyncMock(return_value=mock_client)):
        await db_module.insert_recommendation(rec)
    upserted = mock_client.table.return_value.upsert.call_args[0][0]
    # no created_at sent → DB default now() stands (never send null into NOT NULL)
    assert "created_at" not in upserted


@pytest.mark.asyncio
async def test_insert_recommendation_mark_writes_checked_at():
    import kalshi_trader.db as db_module
    db_module._client = None
    mock_client = MagicMock()
    mock_client.table.return_value.insert.return_value.execute = AsyncMock(
        return_value=MagicMock(data=[{"id": "mark-1"}]))
    mark = {"checked_at": "2026-06-03T05:00:00+00:00", "current_value_cents": 60.0,
            "pnl_cents": 6.0, "would_profit": True, "resolved": False}
    with patch.object(db_module, "_get_client", AsyncMock(return_value=mock_client)):
        await db_module.insert_recommendation_mark("rec-1", mark)
    inserted = mock_client.table.return_value.insert.call_args[0][0]
    assert inserted["checked_at"] == "2026-06-03T05:00:00+00:00"


@pytest.mark.asyncio
async def test_fetch_open_recommendations_normalizes_and_filters_status():
    import kalshi_trader.db as db_module
    db_module._client = None
    rows = [
        {"id": "uuid-a", "created_at": "2026-06-03T04:00:00+00:00", "ticker": "KXTEST-A",
         "side": "no", "entry_price_cents": 54.0, "status": "open", "disposition": "approved"},
    ]
    builder = MagicMock()
    builder.select.return_value.eq.return_value.execute = AsyncMock(
        return_value=MagicMock(data=rows))
    mock_client = MagicMock()
    mock_client.table.return_value = builder

    with patch.object(db_module, "_get_client", AsyncMock(return_value=mock_client)):
        recs = await db_module.fetch_open_recommendations()

    # filtered to status='open' via the builder
    builder.select.return_value.eq.assert_called_once_with("status", "open")
    assert len(recs) == 1
    assert recs[0]["rec_id"] == "uuid-a"           # normalized id -> rec_id
    assert recs[0]["recorded_at"] == "2026-06-03T04:00:00+00:00"
    assert recs[0]["ticker"] == "KXTEST-A"


@pytest.mark.asyncio
async def test_fetch_open_recommendations_applies_max_age_minutes():
    import kalshi_trader.db as db_module
    from datetime import datetime, timezone, timedelta
    db_module._client = None
    now = datetime(2026, 6, 3, 12, 0, 0, tzinfo=timezone.utc)
    rows = [
        {"id": "young", "created_at": (now - timedelta(minutes=20)).isoformat(),
         "ticker": "KX-Y", "side": "yes", "entry_price_cents": 40.0, "status": "open"},
        {"id": "old", "created_at": (now - timedelta(minutes=300)).isoformat(),
         "ticker": "KX-O", "side": "yes", "entry_price_cents": 40.0, "status": "open"},
    ]
    builder = MagicMock()
    builder.select.return_value.eq.return_value.execute = AsyncMock(
        return_value=MagicMock(data=rows))
    mock_client = MagicMock()
    mock_client.table.return_value = builder

    with patch.object(db_module, "_get_client", AsyncMock(return_value=mock_client)):
        recs = await db_module.fetch_open_recommendations(max_age_minutes=130, now=now)

    assert [r["rec_id"] for r in recs] == ["young"]
