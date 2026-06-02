from __future__ import annotations
import pytest
from unittest.mock import AsyncMock

from kalshi_trader.agents.kalshi_bias_agent import (
    KalshiBiasAgent,
    compute_bias_adjustment,
    _is_political,
    _horizon_factor,
)


# --- _is_political ---

def test_political_category():
    assert _is_political("politics", "Will X win?") is True


def test_political_title_keywords():
    assert _is_political("mentions", "Will candidate win the election?") is True


def test_not_political():
    assert _is_political("weather", "Will it rain in NYC?") is False


def test_political_sports_not_political():
    assert _is_political("sports", "Will the Lakers win tonight?") is False


# --- _horizon_factor ---

def test_horizon_under_12h():
    assert _horizon_factor(6) == pytest.approx(0.30)


def test_horizon_12_to_48h():
    f = _horizon_factor(24)
    assert f == pytest.approx(0.60)


def test_horizon_over_48h():
    assert _horizon_factor(72) == pytest.approx(1.0)


# --- compute_bias_adjustment ---

def test_longshot_adjusted_down():
    adjusted = compute_bias_adjustment(0.08, is_political=False, hours_to_resolution=72)
    assert adjusted < 0.08  # longshot overpriced → adjusted down


def test_favorite_adjusted_up():
    adjusted = compute_bias_adjustment(0.92, is_political=False, hours_to_resolution=72)
    assert adjusted > 0.92  # favorite underpriced → adjusted up


def test_midrange_no_adjustment():
    adjusted = compute_bias_adjustment(0.50, is_political=False, hours_to_resolution=72)
    assert adjusted == pytest.approx(0.50)


def test_political_leader_adjusted_up():
    adjusted = compute_bias_adjustment(0.65, is_political=True, hours_to_resolution=72)
    assert adjusted > 0.65


def test_political_trailer_adjusted_down():
    adjusted = compute_bias_adjustment(0.35, is_political=True, hours_to_resolution=72)
    assert adjusted < 0.35


def test_horizon_reduces_adjustment():
    far = compute_bias_adjustment(0.08, is_political=False, hours_to_resolution=96)
    near = compute_bias_adjustment(0.08, is_political=False, hours_to_resolution=6)
    assert abs(far - 0.08) > abs(near - 0.08)


def test_adjustment_clamps_to_valid_range():
    result = compute_bias_adjustment(0.01, is_political=False, hours_to_resolution=72)
    assert 0.01 <= result <= 0.99


# --- KalshiBiasAgent.run ---

def _mock_market(bid: int, ask: int) -> dict:
    return {"market": {"yes_bid": bid, "yes_ask": ask, "ticker": "TEST"}}


@pytest.mark.asyncio
async def test_run_returns_empty_for_midrange_price():
    client = AsyncMock()
    client.get_market = AsyncMock(return_value=_mock_market(bid=45, ask=55))
    agent = KalshiBiasAgent(client)
    result = await agent.run("TICKER", "Some sports market", category="sports", hours_to_resolution=72)
    assert result == []


@pytest.mark.asyncio
async def test_run_fires_for_longshot():
    client = AsyncMock()
    client.get_market = AsyncMock(return_value=_mock_market(bid=5, ask=9))
    agent = KalshiBiasAgent(client)
    result = await agent.run("TICKER", "Longshot market", category="sports", hours_to_resolution=72)
    assert len(result) == 1
    assert result[0].source == "kalshi_bias"
    assert result[0].probability < 0.08  # adjusted down from ~7¢
    assert result[0].metadata["bias_type"] == "longshot_bias"


@pytest.mark.asyncio
async def test_run_fires_for_political_leader():
    client = AsyncMock()
    client.get_market = AsyncMock(return_value=_mock_market(bid=63, ask=67))
    agent = KalshiBiasAgent(client)
    result = await agent.run(
        "TICKER", "Will candidate win the election?",
        category="politics", hours_to_resolution=72
    )
    assert len(result) == 1
    assert result[0].probability > 0.65
    assert result[0].metadata["is_political"] is True


@pytest.mark.asyncio
async def test_run_returns_empty_on_client_error():
    client = AsyncMock()
    client.get_market = AsyncMock(side_effect=Exception("network error"))
    agent = KalshiBiasAgent(client)
    result = await agent.run("TICKER", "Some market")
    assert result == []


@pytest.mark.asyncio
async def test_run_returns_empty_when_no_quotes():
    client = AsyncMock()
    client.get_market = AsyncMock(return_value=_mock_market(bid=0, ask=0))
    agent = KalshiBiasAgent(client)
    result = await agent.run("TICKER", "Some market")
    assert result == []


@pytest.mark.asyncio
async def test_run_metadata_fields():
    client = AsyncMock()
    client.get_market = AsyncMock(return_value=_mock_market(bid=7, ask=9))
    agent = KalshiBiasAgent(client)
    result = await agent.run("TICKER", "Longshot", category="crypto", hours_to_resolution=72)
    if result:
        md = result[0].metadata
        assert "ticker" in md
        assert "narrative" in md
        assert "data_quality" in md
        assert "market_price_cents" in md
        assert md["data_quality"] == "fresh"


@pytest.mark.asyncio
async def test_run_weight_is_correct():
    client = AsyncMock()
    client.get_market = AsyncMock(return_value=_mock_market(bid=5, ask=9))
    agent = KalshiBiasAgent(client)
    result = await agent.run("TICKER", "Longshot", category="sports", hours_to_resolution=72)
    if result:
        assert result[0].weight == pytest.approx(0.55)
