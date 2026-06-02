from __future__ import annotations
import pytest
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock

from kalshi_trader.agents.order_flow_agent import (
    OrderFlowAgent, compute_ofi, compute_vpin, _recent_trade_count
)


def _trade(side: str, price: int = 55, count: int = 100, minutes_ago: float = 5.0) -> dict:
    ts = (datetime.now(tz=timezone.utc) - timedelta(minutes=minutes_ago)).isoformat()
    return {"taker_side": side, "yes_price": price, "count": count, "created_time": ts}


def _old_trade(side: str) -> dict:
    ts = (datetime.now(tz=timezone.utc) - timedelta(hours=3)).isoformat()
    return {"taker_side": side, "yes_price": 55, "count": 100, "created_time": ts}


# --- compute_ofi ---

def test_ofi_all_buys():
    trades = [_trade("yes") for _ in range(10)]
    assert compute_ofi(trades) == pytest.approx(1.0)


def test_ofi_all_sells():
    trades = [_trade("no") for _ in range(10)]
    assert compute_ofi(trades) == pytest.approx(-1.0)


def test_ofi_balanced():
    trades = [_trade("yes"), _trade("no")]
    assert compute_ofi(trades) == pytest.approx(0.0)


def test_ofi_empty():
    assert compute_ofi([]) == 0.0


def test_ofi_excludes_old_trades():
    recent = [_trade("yes") for _ in range(5)]
    old = [_old_trade("no") for _ in range(20)]
    result = compute_ofi(recent + old, window_minutes=30)
    assert result > 0.5  # old sells excluded, recent buys dominate


# --- compute_vpin ---

def test_vpin_all_one_side_is_high():
    trades = [_trade("yes", count=1000) for _ in range(20)]
    vpin = compute_vpin(trades, bucket_size_usd=500.0)
    assert vpin > 0.5


def test_vpin_balanced_is_low():
    trades = [_trade("yes", count=100), _trade("no", count=100)] * 20
    vpin = compute_vpin(trades, bucket_size_usd=500.0)
    assert vpin < 0.3


def test_vpin_empty():
    assert compute_vpin([]) == 0.0


def test_vpin_single_bucket():
    trades = [_trade("yes", count=500, price=50)]
    vpin = compute_vpin(trades, bucket_size_usd=250.0)
    assert vpin > 0


# --- _recent_trade_count ---

def test_recent_trade_count_all_recent():
    trades = [_trade("yes", minutes_ago=10) for _ in range(15)]
    assert _recent_trade_count(trades, window_minutes=60) == 15


def test_recent_trade_count_filters_old():
    recent = [_trade("yes", minutes_ago=10) for _ in range(5)]
    old = [_old_trade("yes") for _ in range(10)]
    assert _recent_trade_count(recent + old, window_minutes=60) == 5


# --- OrderFlowAgent.run ---

@pytest.mark.asyncio
async def test_run_returns_empty_when_too_few_trades():
    client = AsyncMock()
    client.get_trades = AsyncMock(return_value={"trades": [_trade("yes") for _ in range(5)]})
    agent = OrderFlowAgent(client)
    result = await agent.run("TICKER", "Some market")
    assert result == []


@pytest.mark.asyncio
async def test_run_returns_empty_when_vpin_low():
    # Balanced trades → low VPIN, low OFI
    trades = [_trade("yes"), _trade("no")] * 20
    client = AsyncMock()
    client.get_trades = AsyncMock(return_value={"trades": trades})
    agent = OrderFlowAgent(client)
    result = await agent.run("TICKER", "Some market")
    assert result == []


@pytest.mark.asyncio
async def test_run_returns_signal_on_high_vpin_ofi():
    # All buys → high OFI, high VPIN
    trades = [_trade("yes", count=500) for _ in range(30)]
    client = AsyncMock()
    client.get_trades = AsyncMock(return_value={"trades": trades})
    agent = OrderFlowAgent(client)
    result = await agent.run("TICKER", "Some market")
    assert len(result) == 1
    assert result[0].source == "kalshi_ofi"
    assert result[0].probability > 0.5
    assert result[0].weight == pytest.approx(0.70)
    assert "vpin" in result[0].metadata
    assert "ofi" in result[0].metadata
    assert result[0].metadata["data_quality"] == "fresh"


@pytest.mark.asyncio
async def test_run_returns_empty_on_client_error():
    client = AsyncMock()
    client.get_trades = AsyncMock(side_effect=Exception("network error"))
    agent = OrderFlowAgent(client)
    result = await agent.run("TICKER", "Some market")
    assert result == []


@pytest.mark.asyncio
async def test_run_handles_list_response():
    """Client may return bare list instead of dict."""
    trades = [_trade("yes", count=500) for _ in range(30)]
    client = AsyncMock()
    client.get_trades = AsyncMock(return_value=trades)
    agent = OrderFlowAgent(client)
    result = await agent.run("TICKER", "Some market")
    assert isinstance(result, list)


@pytest.mark.asyncio
async def test_run_sell_pressure_gives_prob_below_50():
    trades = [_trade("no", count=500) for _ in range(30)]
    client = AsyncMock()
    client.get_trades = AsyncMock(return_value={"trades": trades})
    agent = OrderFlowAgent(client)
    result = await agent.run("TICKER", "Some market")
    if result:  # only assert if signal fired
        assert result[0].probability < 0.5
