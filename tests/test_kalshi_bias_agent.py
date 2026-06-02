from __future__ import annotations
import json
import pytest
from datetime import datetime, timezone
from unittest.mock import AsyncMock, patch

from kalshi_trader.agents.kalshi_bias_agent import (
    KalshiBiasAgent,
    compute_bias_adjustment,
    _is_political,
    _horizon_factor,
)


# ---------------------------------------------------------------------------
# _is_political
# ---------------------------------------------------------------------------

def test_political_category():
    assert _is_political("politics", "Will X win?") is True


def test_political_title_keywords():
    assert _is_political("mentions", "Will candidate win the election?") is True


def test_not_political():
    assert _is_political("weather", "Will it rain in NYC?") is False


def test_political_sports_not_political():
    assert _is_political("sports", "Will the Lakers win tonight?") is False


# ---------------------------------------------------------------------------
# _horizon_factor
# ---------------------------------------------------------------------------

def test_horizon_under_12h():
    assert _horizon_factor(6) == pytest.approx(0.30)


def test_horizon_12_to_48h():
    f = _horizon_factor(24)
    assert f == pytest.approx(0.60)


def test_horizon_over_48h():
    assert _horizon_factor(72) == pytest.approx(1.0)


# ---------------------------------------------------------------------------
# compute_bias_adjustment
# ---------------------------------------------------------------------------

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


# ---------------------------------------------------------------------------
# Tool handler: _apply_bias_corrections (tested directly on agent instance)
# ---------------------------------------------------------------------------

def _make_agent() -> KalshiBiasAgent:
    client = AsyncMock()
    return KalshiBiasAgent(client)


@pytest.mark.asyncio
async def test_apply_bias_corrections_longshot():
    agent = _make_agent()
    result = await agent._apply_bias_corrections("TICKER", price_cents=10.0, category="sports")
    assert result["corrections_applied"] == ["longshot_bias"]
    assert result["corrected_prob"] == pytest.approx(0.10 * 0.72, rel=1e-4)
    assert result["raw_prob"] == pytest.approx(0.10, rel=1e-4)
    assert result["delta_cents"] < 0  # downward correction


@pytest.mark.asyncio
async def test_apply_bias_corrections_no_correction_midrange():
    agent = _make_agent()
    result = await agent._apply_bias_corrections("TICKER", price_cents=50.0, category="sports")
    assert result["corrections_applied"] == []
    assert result["delta_cents"] == pytest.approx(0.0, abs=1e-6)


@pytest.mark.asyncio
async def test_apply_bias_corrections_political_leader():
    agent = _make_agent()
    result = await agent._apply_bias_corrections("TICKER", price_cents=65.0, category="politics")
    assert "political_underconfidence" in result["corrections_applied"]
    assert result["corrected_prob"] > result["raw_prob"]


@pytest.mark.asyncio
async def test_apply_bias_corrections_political_trailer():
    agent = _make_agent()
    result = await agent._apply_bias_corrections("TICKER", price_cents=35.0, category="politics")
    assert "political_underconfidence" in result["corrections_applied"]
    assert result["corrected_prob"] < result["raw_prob"]


@pytest.mark.asyncio
async def test_apply_bias_corrections_near_certainty():
    agent = _make_agent()
    result = await agent._apply_bias_corrections("TICKER", price_cents=90.0, category="crypto")
    assert "near_certainty" in result["corrections_applied"]
    assert result["corrected_prob"] > result["raw_prob"]


@pytest.mark.asyncio
async def test_apply_bias_corrections_political_midrange_no_correction():
    """Politics market between 45-55¢ should not trigger political underconfidence."""
    agent = _make_agent()
    result = await agent._apply_bias_corrections("TICKER", price_cents=50.0, category="politics")
    assert "political_underconfidence" not in result["corrections_applied"]


# ---------------------------------------------------------------------------
# Tool handler: _build_bias_signal (tested directly on agent instance)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_build_bias_signal_structure():
    agent = _make_agent()
    correction = {
        "corrected_prob": 0.072,
        "raw_prob": 0.10,
        "corrections_applied": ["longshot_bias"],
        "delta_cents": -2.8,
    }
    result = await agent._build_bias_signal("LONGSHOT-MARKET", correction)
    assert result["source"] == "kalshi_bias"
    assert result["probability"] == pytest.approx(0.072, rel=1e-3)
    assert result["weight"] == pytest.approx(0.55)
    assert result["uncertainty"] == pytest.approx(0.02)
    assert "data_issued_at" in result
    assert result["metadata"]["ticker"] == "LONGSHOT-MARKET"
    assert result["metadata"]["data_quality"] == "fresh"
    assert result["metadata"]["bias_type"] == "longshot_bias"
    assert result["metadata"]["is_political"] is False


@pytest.mark.asyncio
async def test_build_bias_signal_political():
    agent = _make_agent()
    correction = {
        "corrected_prob": 0.702,
        "raw_prob": 0.65,
        "corrections_applied": ["political_underconfidence"],
        "delta_cents": 5.2,
    }
    result = await agent._build_bias_signal("ELECTION-TICKER", correction)
    assert result["metadata"]["is_political"] is True
    assert result["metadata"]["bias_type"] == "political_underconfidence"


# ---------------------------------------------------------------------------
# KalshiBiasAgent.run — mock BaseAgent.run
# ---------------------------------------------------------------------------

def _mock_market(bid: int, ask: int) -> dict:
    return {"market": {"yes_bid": bid, "yes_ask": ask, "ticker": "TEST"}}


def _make_signal_json(probability: float, is_political: bool = False, bias_type: str = "longshot_bias") -> str:
    """Build a fenced JSON block as BaseAgent.run would return."""
    data = [
        {
            "source": "kalshi_bias",
            "probability": probability,
            "uncertainty": 0.02,
            "weight": 0.55,
            "data_issued_at": datetime.now(tz=timezone.utc).isoformat(),
            "metadata": {
                "ticker": "TICKER",
                "narrative": "Test narrative.",
                "data_quality": "fresh",
                "raw_prob": 0.10,
                "corrected_prob": probability,
                "delta_cents": -2.8,
                "corrections_applied": [bias_type],
                "market_price_cents": 10.0,
                "adjusted_prob": probability,
                "bias_type": bias_type,
                "is_political": is_political,
            },
        }
    ]
    return f"```json\n{json.dumps(data)}\n```"


@pytest.mark.asyncio
async def test_run_returns_empty_for_midrange_price():
    """Midrange price (45/55) — BaseAgent returns [] when delta < 3¢."""
    client = AsyncMock()
    client.get_market = AsyncMock(return_value=_mock_market(bid=45, ask=55))
    agent = KalshiBiasAgent(client)
    with patch.object(agent._agent, "run", new=AsyncMock(return_value="```json\n[]\n```")):
        result = await agent.run("TICKER", "Some sports market", category="sports", hours_to_resolution=72)
    assert result == []


@pytest.mark.asyncio
async def test_run_fires_for_longshot():
    client = AsyncMock()
    client.get_market = AsyncMock(return_value=_mock_market(bid=5, ask=9))
    agent = KalshiBiasAgent(client)
    agent_response = _make_signal_json(0.051, is_political=False, bias_type="longshot_bias")
    with patch.object(agent._agent, "run", new=AsyncMock(return_value=agent_response)):
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
    agent_response = _make_signal_json(0.702, is_political=True, bias_type="political_underconfidence")
    with patch.object(agent._agent, "run", new=AsyncMock(return_value=agent_response)):
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
    agent_response = _make_signal_json(0.057, is_political=False, bias_type="longshot_bias")
    with patch.object(agent._agent, "run", new=AsyncMock(return_value=agent_response)):
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
    agent_response = _make_signal_json(0.051, is_political=False, bias_type="longshot_bias")
    with patch.object(agent._agent, "run", new=AsyncMock(return_value=agent_response)):
        result = await agent.run("TICKER", "Longshot", category="sports", hours_to_resolution=72)
    if result:
        assert result[0].weight == pytest.approx(0.55)
