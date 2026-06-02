from __future__ import annotations
import json
import pytest
from unittest.mock import AsyncMock, patch, MagicMock
from datetime import datetime, timezone

from kalshi_trader.agents.market_maker_agent import (
    MarketMakerAgent, _parse_orderbook, analyze_spread_dynamics
)


def _ob(yes_levels=None, no_levels=None) -> dict:
    """Build a mock orderbook REST response."""
    return {
        "orderbook": {
            "yes": yes_levels or [],
            "no": no_levels or [],
        }
    }


# --- _parse_orderbook ---

def test_parse_normal_book():
    raw = _ob(yes_levels=[[52, 100], [50, 200]], no_levels=[[51, 150]])
    result = _parse_orderbook(raw)
    assert result["best_bid"] == 52
    assert result["best_ask"] == 49  # 100 - 51
    assert result["spread_cents"] == -3  # 49 - 52 (inverted = crossed book)


def test_parse_spread_calculation():
    raw = _ob(yes_levels=[[45, 100]], no_levels=[[40, 100]])
    result = _parse_orderbook(raw)
    assert result["best_bid"] == 45
    assert result["best_ask"] == 60  # 100 - 40
    assert result["spread_cents"] == 15


def test_parse_empty_book():
    result = _parse_orderbook(_ob())
    assert result["best_bid"] is None
    assert result["best_ask"] is None
    assert result["spread_cents"] is None
    assert result["imbalance"] == 0.0


def test_parse_imbalance_all_bids():
    raw = _ob(yes_levels=[[50, 500]], no_levels=[])
    result = _parse_orderbook(raw)
    assert result["imbalance"] == pytest.approx(1.0)


def test_parse_imbalance_balanced():
    raw = _ob(yes_levels=[[50, 100]], no_levels=[[50, 100]])
    result = _parse_orderbook(raw)
    assert result["imbalance"] == pytest.approx(0.0)


def test_parse_handles_top_level_keys():
    """Response may omit 'orderbook' wrapper."""
    raw = {"yes": [[50, 100]], "no": [[55, 100]]}
    result = _parse_orderbook(raw)
    assert result["best_bid"] == 50


# --- analyze_spread_dynamics ---

def _snap(spread, imbalance=0.0):
    return {"spread_cents": spread, "imbalance": imbalance, "best_bid": 50, "best_ask": 50 + spread}


def test_withdrawal_when_spread_over_15():
    snaps = [_snap(5), _snap(8), _snap(18)]
    result = analyze_spread_dynamics(snaps)
    assert result["signal"] == "withdrawal"


def test_directional_when_widening_and_high_imbalance():
    snaps = [_snap(4, imbalance=0.7), _snap(5, imbalance=0.7), _snap(6, imbalance=0.72)]
    result = analyze_spread_dynamics(snaps)
    assert result["signal"] in ("directional", "widening")


def test_normal_when_stable_narrow():
    snaps = [_snap(4), _snap(4), _snap(5)]
    result = analyze_spread_dynamics(snaps)
    assert result["signal"] == "normal"


def test_empty_snapshots():
    result = analyze_spread_dynamics([])
    assert result["signal"] == "normal"


def test_widening_trend_detection():
    snaps = [_snap(4), _snap(6), _snap(8)]
    result = analyze_spread_dynamics(snaps)
    assert result["spread_trend"] > 0


# --- Tool handler unit tests ---

@pytest.mark.asyncio
async def test_get_orderbook_handler_returns_expected_keys():
    client = AsyncMock()
    client.get_orderbook = AsyncMock(return_value=_ob(
        yes_levels=[[50, 200]], no_levels=[[45, 100]]
    ))
    agent = MarketMakerAgent(client)
    result = await agent._get_orderbook("TICKER-X")
    assert "yes_bid" in result
    assert "yes_ask" in result
    assert "spread_cents" in result
    assert "bid_depth" in result
    assert "ask_depth" in result
    assert "timestamp" in result
    assert result["yes_bid"] == 50
    assert result["yes_ask"] == 55  # 100 - 45


@pytest.mark.asyncio
async def test_get_orderbook_handler_empty_book():
    client = AsyncMock()
    client.get_orderbook = AsyncMock(return_value=_ob())
    agent = MarketMakerAgent(client)
    result = await agent._get_orderbook("TICKER-X")
    assert result["yes_bid"] is None
    assert result["yes_ask"] is None
    assert result["spread_cents"] is None


@pytest.mark.asyncio
async def test_analyze_spread_dynamics_handler_anomaly():
    client = AsyncMock()
    agent = MarketMakerAgent(client)
    orderbook = {
        "yes_bid": 45,
        "yes_ask": 55,
        "spread_cents": 10,
        "bid_depth": 600.0,
        "ask_depth": 100.0,
        "timestamp": "2026-06-02T00:00:00+00:00",
    }
    result = await agent._analyze_spread_dynamics("TICKER-X", orderbook)
    assert result["spread_anomaly"] is True  # spread > 8
    assert "depth_imbalance" in result
    assert "direction" in result
    assert "maker_withdrawal_score" in result
    assert result["direction"] == "YES"  # bid-heavy


@pytest.mark.asyncio
async def test_analyze_spread_dynamics_handler_no_anomaly():
    client = AsyncMock()
    agent = MarketMakerAgent(client)
    orderbook = {
        "yes_bid": 48,
        "yes_ask": 52,
        "spread_cents": 4,
        "bid_depth": 100.0,
        "ask_depth": 100.0,
        "timestamp": "2026-06-02T00:00:00+00:00",
    }
    result = await agent._analyze_spread_dynamics("TICKER-X", orderbook)
    assert result["spread_anomaly"] is False
    assert result["direction"] == "neutral"


@pytest.mark.asyncio
async def test_build_market_maker_signal_with_anomaly():
    client = AsyncMock()
    agent = MarketMakerAgent(client)
    analysis = {
        "spread_cents": 12.0,
        "spread_anomaly": True,
        "depth_imbalance": 0.52,
        "direction": "YES",
        "maker_withdrawal_score": 0.3,
    }
    result = await agent._build_market_maker_signal("TICKER-X", analysis)
    assert result.get("signal") is None  # signal key not set when estimate is built
    assert result["source"] == "market_maker"
    assert "probability" in result
    assert "metadata" in result
    assert result["metadata"]["data_quality"] == "fresh"
    assert result["metadata"]["direction"] == "YES"


@pytest.mark.asyncio
async def test_build_market_maker_signal_no_anomaly_returns_null():
    client = AsyncMock()
    agent = MarketMakerAgent(client)
    analysis = {
        "spread_cents": 4.0,
        "spread_anomaly": False,
        "depth_imbalance": 0.1,
        "direction": "neutral",
        "maker_withdrawal_score": 0.0,
    }
    result = await agent._build_market_maker_signal("TICKER-X", analysis)
    assert result == {"signal": None}


@pytest.mark.asyncio
async def test_build_market_maker_signal_high_imbalance_no_spread_anomaly():
    client = AsyncMock()
    agent = MarketMakerAgent(client)
    analysis = {
        "spread_cents": 5.0,
        "spread_anomaly": False,
        "depth_imbalance": 0.6,
        "direction": "YES",
        "maker_withdrawal_score": 0.0,
    }
    result = await agent._build_market_maker_signal("TICKER-X", analysis)
    # abs(0.6) > 0.4, so signal should be built
    assert "source" in result
    assert result["source"] == "market_maker"


# --- MarketMakerAgent.run (mocking BaseAgent) ---

@pytest.mark.asyncio
async def test_run_returns_empty_list_on_empty_agent_response():
    client = AsyncMock()
    agent = MarketMakerAgent(client)
    with patch.object(agent._agent, "run", new=AsyncMock(return_value="```json\n[]\n```")):
        result = await agent.run("TICKER", "Some market")
    assert result == []


@pytest.mark.asyncio
async def test_run_parses_signal_from_agent_response():
    client = AsyncMock()
    agent = MarketMakerAgent(client)
    signal_json = json.dumps([{
        "source": "market_maker",
        "probability": 0.64,
        "uncertainty": 0.14,
        "weight": 0.65,
        "data_issued_at": "2026-06-02T13:05:00+00:00",
        "metadata": {
            "ticker": "TICKER",
            "narrative": "Spread widened to 12¢.",
            "data_quality": "fresh",
            "spread_cents": 12.0,
            "depth_imbalance": 0.52,
            "direction": "YES",
            "maker_withdrawal_score": 0.3,
        },
    }])
    raw_response = f"```json\n{signal_json}\n```"
    with patch.object(agent._agent, "run", new=AsyncMock(return_value=raw_response)):
        result = await agent.run("TICKER", "Some market")
    assert len(result) == 1
    assert result[0].source == "market_maker"
    assert result[0].probability == pytest.approx(0.64)
    assert result[0].weight == pytest.approx(0.65)
    assert result[0].metadata["data_quality"] == "fresh"


@pytest.mark.asyncio
async def test_run_returns_empty_on_malformed_response():
    client = AsyncMock()
    agent = MarketMakerAgent(client)
    with patch.object(agent._agent, "run", new=AsyncMock(return_value="No anomaly detected.")):
        result = await agent.run("TICKER", "Some market")
    assert result == []
