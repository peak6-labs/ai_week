from __future__ import annotations
import pytest
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch

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


# --- tool handler unit tests ---

@pytest.mark.asyncio
async def test_fetch_and_compute_metrics_returns_all_keys():
    client = AsyncMock()
    trades = [_trade("yes") for _ in range(10)] + [_trade("no") for _ in range(5)]
    client.get_trades = AsyncMock(return_value={"trades": trades})
    agent = OrderFlowAgent.__new__(OrderFlowAgent)
    agent._client = client
    result = await agent._fetch_and_compute_metrics("TICKER")
    assert "vpin_score" in result
    assert "high_informed_trading" in result
    assert "ofi_score" in result
    assert "direction" in result
    assert "buying_fraction" in result
    assert "recent_ofi_trades" in result
    assert "recent_trade_count" in result
    assert result["total_trades"] == 15
    # 10 buys vs 5 sells → OFI should be positive
    assert result["ofi_score"] > 0


@pytest.mark.asyncio
async def test_fetch_and_compute_metrics_empty_market():
    client = AsyncMock()
    client.get_trades = AsyncMock(return_value={"trades": []})
    agent = OrderFlowAgent.__new__(OrderFlowAgent)
    agent._client = client
    result = await agent._fetch_and_compute_metrics("TICKER")
    assert result["vpin_score"] == 0.0
    assert result["ofi_score"] == 0.0
    assert result["total_trades"] == 0


@pytest.mark.asyncio
async def test_get_market_trades_dict_response():
    client = AsyncMock()
    trades = [_trade("yes") for _ in range(5)]
    client.get_trades = AsyncMock(return_value={"trades": trades})
    agent = OrderFlowAgent.__new__(OrderFlowAgent)
    agent._client = client
    result = await agent._get_market_trades("TICKER", limit=200)
    assert result == trades


@pytest.mark.asyncio
async def test_get_market_trades_list_response():
    client = AsyncMock()
    trades = [_trade("yes") for _ in range(5)]
    client.get_trades = AsyncMock(return_value=trades)
    agent = OrderFlowAgent.__new__(OrderFlowAgent)
    agent._client = client
    result = await agent._get_market_trades("TICKER", limit=200)
    assert result == trades


@pytest.mark.asyncio
async def test_compute_vpin_handler_all_buys():
    agent = OrderFlowAgent.__new__(OrderFlowAgent)
    trades = [_trade("yes", count=1000) for _ in range(20)]
    result = await agent._compute_vpin(trades, n_buckets=10)
    assert "vpin_score" in result
    assert "high_informed_trading" in result
    assert result["vpin_score"] > 0.4
    assert result["high_informed_trading"] is True


@pytest.mark.asyncio
async def test_compute_vpin_handler_balanced():
    agent = OrderFlowAgent.__new__(OrderFlowAgent)
    trades = [_trade("yes", count=100), _trade("no", count=100)] * 20
    result = await agent._compute_vpin(trades, n_buckets=10)
    assert result["vpin_score"] < 0.4
    assert result["high_informed_trading"] is False


@pytest.mark.asyncio
async def test_compute_vpin_handler_empty():
    agent = OrderFlowAgent.__new__(OrderFlowAgent)
    result = await agent._compute_vpin([], n_buckets=10)
    assert result["vpin_score"] == 0.0
    assert result["high_informed_trading"] is False


@pytest.mark.asyncio
async def test_compute_ofi_handler_all_buys():
    agent = OrderFlowAgent.__new__(OrderFlowAgent)
    trades = [_trade("yes") for _ in range(10)]
    result = await agent._compute_ofi(trades)
    assert result["ofi_score"] == pytest.approx(1.0)
    assert result["direction"] == "YES"
    assert result["buying_fraction"] == pytest.approx(1.0)
    assert result["recent_ofi_trades"] == 10


@pytest.mark.asyncio
async def test_compute_ofi_handler_all_sells():
    agent = OrderFlowAgent.__new__(OrderFlowAgent)
    trades = [_trade("no") for _ in range(10)]
    result = await agent._compute_ofi(trades)
    assert result["ofi_score"] == pytest.approx(-1.0)
    assert result["direction"] == "NO"
    assert result["buying_fraction"] == pytest.approx(0.0)
    assert result["recent_ofi_trades"] == 10


@pytest.mark.asyncio
async def test_compute_ofi_handler_neutral():
    agent = OrderFlowAgent.__new__(OrderFlowAgent)
    trades = [_trade("yes"), _trade("no")]
    result = await agent._compute_ofi(trades)
    assert result["ofi_score"] == pytest.approx(0.0)
    assert result["direction"] == "neutral"


@pytest.mark.asyncio
async def test_compute_ofi_stale_trades_give_neutral_buying_fraction():
    """buying_fraction must use the same window as ofi_score — stale trades excluded from both."""
    agent = OrderFlowAgent.__new__(OrderFlowAgent)
    # All trades are 3 hours old — outside the 30-min OFI window
    stale_buys = [_old_trade("yes") for _ in range(20)]
    result = await agent._compute_ofi(stale_buys)
    # OFI window has no trades → both ofi_score and buying_fraction are neutral
    assert result["ofi_score"] == pytest.approx(0.0)
    assert result["buying_fraction"] == pytest.approx(0.5)
    assert result["recent_ofi_trades"] == 0


@pytest.mark.asyncio
async def test_build_order_flow_signal_yes_direction():
    agent = OrderFlowAgent.__new__(OrderFlowAgent)
    vpin_result = {"vpin_score": 0.55, "high_informed_trading": True}
    ofi_result = {"ofi_score": 0.60, "direction": "YES", "buying_fraction": 0.80}
    result = await agent._build_order_flow_signal("TICKER", vpin_result, ofi_result)
    assert result["source"] == "order_flow"
    assert result["probability"] > 0.5
    assert result["weight"] == pytest.approx(0.70)
    assert result["metadata"]["ofi_direction"] == "YES"
    assert result["metadata"]["data_quality"] == "fresh"
    assert "data_issued_at" in result


@pytest.mark.asyncio
async def test_build_order_flow_signal_no_direction():
    agent = OrderFlowAgent.__new__(OrderFlowAgent)
    vpin_result = {"vpin_score": 0.55, "high_informed_trading": True}
    ofi_result = {"ofi_score": -0.50, "direction": "NO", "buying_fraction": 0.20}
    result = await agent._build_order_flow_signal("TICKER", vpin_result, ofi_result)
    assert result["probability"] < 0.5
    assert result["metadata"]["ofi_direction"] == "NO"


# --- OrderFlowAgent.run (mocks BaseAgent.run) ---

@pytest.mark.asyncio
async def test_run_returns_empty_on_no_signal():
    client = AsyncMock()
    with patch("kalshi_trader.agents.order_flow_agent.BaseAgent") as MockBaseAgent:
        mock_agent_instance = AsyncMock()
        mock_agent_instance.run = AsyncMock(return_value="```json\n[]\n```")
        MockBaseAgent.return_value = mock_agent_instance

        agent = OrderFlowAgent(client)
        result = await agent.run("TICKER", "Some market")
        assert result == []


@pytest.mark.asyncio
async def test_run_returns_signal_estimates():
    client = AsyncMock()
    signal_json = """```json
[
  {
    "source": "order_flow",
    "probability": 0.68,
    "uncertainty": 0.12,
    "weight": 0.70,
    "data_issued_at": "2026-06-02T13:00:00+00:00",
    "metadata": {
      "ticker": "TICKER",
      "narrative": "VPIN of 0.52 indicates active informed trading.",
      "data_quality": "fresh",
      "vpin_score": 0.52,
      "ofi_score": 0.42,
      "ofi_direction": "YES"
    }
  }
]
```"""
    with patch("kalshi_trader.agents.order_flow_agent.BaseAgent") as MockBaseAgent:
        mock_agent_instance = AsyncMock()
        mock_agent_instance.run = AsyncMock(return_value=signal_json)
        MockBaseAgent.return_value = mock_agent_instance

        agent = OrderFlowAgent(client)
        result = await agent.run("TICKER", "Some market")
        assert len(result) == 1
        assert result[0].source == "order_flow"
        assert result[0].probability == pytest.approx(0.68)
        assert result[0].weight == pytest.approx(0.70)


@pytest.mark.asyncio
async def test_run_returns_empty_on_malformed_output():
    client = AsyncMock()
    with patch("kalshi_trader.agents.order_flow_agent.BaseAgent") as MockBaseAgent:
        mock_agent_instance = AsyncMock()
        mock_agent_instance.run = AsyncMock(return_value="No JSON here")
        MockBaseAgent.return_value = mock_agent_instance

        agent = OrderFlowAgent(client)
        result = await agent.run("TICKER", "Some market")
        assert result == []
