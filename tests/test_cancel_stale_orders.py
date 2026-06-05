"""Tests for scripts/cancel_stale_orders.py."""
from __future__ import annotations

import importlib
import sys
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

sys.path.insert(0, str(__file__).rsplit("/tests", 1)[0])
import kalshi_trader.config  # noqa: F401 — loads .env

cancel_stale_orders = importlib.import_module("scripts.cancel_stale_orders")


def _order(
    order_id: str = "ord-1",
    ticker: str = "KXTEST-1",
    action: str = "buy",
    side: str = "yes",
    yes_price_dollars: str = "0.3500",
    remaining_count_fp: str = "10.00",
    age_minutes: float = 15.0,
) -> dict:
    created = (datetime.now(timezone.utc) - timedelta(minutes=age_minutes)).strftime(
        "%Y-%m-%dT%H:%M:%S.%f"
    ) + "Z"
    return {
        "order_id": order_id,
        "ticker": ticker,
        "action": action,
        "side": side,
        "yes_price_dollars": yes_price_dollars,
        "remaining_count_fp": remaining_count_fp,
        "created_time": created,
        "status": "resting",
    }


def _make_client(orders: list[dict] | None = None) -> MagicMock:
    client = MagicMock()
    client.__aenter__ = AsyncMock(return_value=client)
    client.__aexit__ = AsyncMock(return_value=None)
    client.get_orders = AsyncMock(return_value={"orders": orders or []})
    client.cancel_order = AsyncMock(return_value={})
    return client


# ---------------------------------------------------------------------------
# dry_run=True — no real API calls, cancelled flag still set
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_dry_run_marks_cancelled_without_api_call():
    client = _make_client(orders=[_order(age_minutes=15)])
    with patch("scripts.cancel_stale_orders.KalshiClient", return_value=client):
        results = await cancel_stale_orders.run(stale_minutes=10, dry_run=True)

    assert len(results) == 1
    assert results[0]["cancelled"] is True
    client.cancel_order.assert_not_awaited()


@pytest.mark.asyncio
async def test_dry_run_returns_correct_fields():
    client = _make_client(orders=[_order(
        order_id="ord-abc", ticker="KXFOO-1", action="sell", side="no",
        yes_price_dollars="0.6500", remaining_count_fp="5.00", age_minutes=20,
    )])
    with patch("scripts.cancel_stale_orders.KalshiClient", return_value=client):
        results = await cancel_stale_orders.run(stale_minutes=10, dry_run=True)

    record = results[0]
    assert record["order_id"] == "ord-abc"
    assert record["ticker"] == "KXFOO-1"
    assert record["action"] == "sell"
    assert record["side"] == "no"
    assert record["yes_price_dollars"] == "0.6500"
    assert record["remaining_count_fp"] == "5.00"
    assert record["cancelled"] is True


# ---------------------------------------------------------------------------
# Age filtering — only stale orders are included
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_fresh_order_not_included():
    client = _make_client(orders=[_order(age_minutes=5)])
    with patch("scripts.cancel_stale_orders.KalshiClient", return_value=client):
        results = await cancel_stale_orders.run(stale_minutes=10, dry_run=True)

    assert results == []


@pytest.mark.asyncio
async def test_just_under_threshold_not_included():
    """An order a few seconds under the threshold is not yet stale."""
    client = _make_client(orders=[_order(age_minutes=9.9)])
    with patch("scripts.cancel_stale_orders.KalshiClient", return_value=client):
        results = await cancel_stale_orders.run(stale_minutes=10, dry_run=True)

    assert results == []


@pytest.mark.asyncio
async def test_mixed_ages_only_stale_returned():
    client = _make_client(orders=[
        _order(order_id="fresh", age_minutes=3),
        _order(order_id="stale", age_minutes=25),
    ])
    with patch("scripts.cancel_stale_orders.KalshiClient", return_value=client):
        results = await cancel_stale_orders.run(stale_minutes=10, dry_run=True)

    assert len(results) == 1
    assert results[0]["order_id"] == "stale"


@pytest.mark.asyncio
async def test_custom_minutes_threshold():
    client = _make_client(orders=[
        _order(order_id="under-15", age_minutes=12),
        _order(order_id="over-15", age_minutes=18),
    ])
    with patch("scripts.cancel_stale_orders.KalshiClient", return_value=client):
        results = await cancel_stale_orders.run(stale_minutes=15, dry_run=True)

    assert len(results) == 1
    assert results[0]["order_id"] == "over-15"


# ---------------------------------------------------------------------------
# Live mode — cancel_order is called, cancelled flag reflects outcome
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_live_mode_calls_cancel_order():
    client = _make_client(orders=[_order(order_id="ord-live", age_minutes=12)])
    with patch("scripts.cancel_stale_orders.KalshiClient", return_value=client):
        results = await cancel_stale_orders.run(stale_minutes=10, dry_run=False)

    assert results[0]["cancelled"] is True
    client.cancel_order.assert_awaited_once_with("ord-live")


@pytest.mark.asyncio
async def test_live_mode_cancel_failure_sets_cancelled_false():
    client = _make_client(orders=[_order(order_id="ord-fail", age_minutes=12)])
    client.cancel_order = AsyncMock(side_effect=Exception("network error"))
    with patch("scripts.cancel_stale_orders.KalshiClient", return_value=client):
        results = await cancel_stale_orders.run(stale_minutes=10, dry_run=False)

    assert results[0]["cancelled"] is False


@pytest.mark.asyncio
async def test_live_mode_cancels_multiple_orders():
    client = _make_client(orders=[
        _order(order_id="ord-1", age_minutes=15),
        _order(order_id="ord-2", age_minutes=20),
    ])
    with patch("scripts.cancel_stale_orders.KalshiClient", return_value=client):
        results = await cancel_stale_orders.run(stale_minutes=10, dry_run=False)

    assert len(results) == 2
    assert all(r["cancelled"] for r in results)
    assert client.cancel_order.await_count == 2


# ---------------------------------------------------------------------------
# Empty order book
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_no_resting_orders_returns_empty():
    client = _make_client(orders=[])
    with patch("scripts.cancel_stale_orders.KalshiClient", return_value=client):
        results = await cancel_stale_orders.run(stale_minutes=10, dry_run=True)

    assert results == []


@pytest.mark.asyncio
async def test_missing_orders_key_returns_empty():
    client = _make_client()
    client.get_orders = AsyncMock(return_value={})
    with patch("scripts.cancel_stale_orders.KalshiClient", return_value=client):
        results = await cancel_stale_orders.run(stale_minutes=10, dry_run=True)

    assert results == []


# ---------------------------------------------------------------------------
# action field is preserved correctly for buy vs sell
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_buy_action_preserved():
    client = _make_client(orders=[_order(action="buy", age_minutes=15)])
    with patch("scripts.cancel_stale_orders.KalshiClient", return_value=client):
        results = await cancel_stale_orders.run(stale_minutes=10, dry_run=True)

    assert results[0]["action"] == "buy"


@pytest.mark.asyncio
async def test_sell_action_preserved():
    client = _make_client(orders=[_order(action="sell", age_minutes=15)])
    with patch("scripts.cancel_stale_orders.KalshiClient", return_value=client):
        results = await cancel_stale_orders.run(stale_minutes=10, dry_run=True)

    assert results[0]["action"] == "sell"
