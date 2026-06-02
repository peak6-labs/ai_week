from __future__ import annotations
import pytest
import asyncio
from unittest.mock import AsyncMock, patch

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


# --- MarketMakerAgent.run ---

@pytest.mark.asyncio
async def test_run_returns_empty_on_client_error():
    client = AsyncMock()
    client.get_orderbook = AsyncMock(side_effect=Exception("timeout"))
    agent = MarketMakerAgent(client, snapshot_count=1, snapshot_delay=0)
    result = await agent.run("TICKER", "Some market")
    assert result == []


@pytest.mark.asyncio
async def test_run_returns_empty_for_normal_spread():
    client = AsyncMock()
    client.get_orderbook = AsyncMock(return_value=_ob(
        yes_levels=[[50, 100]], no_levels=[[54, 100]]
    ))
    agent = MarketMakerAgent(client, snapshot_count=2, snapshot_delay=0)
    result = await agent.run("TICKER", "Some market")
    assert result == []


@pytest.mark.asyncio
async def test_run_returns_signal_on_widening_with_imbalance():
    responses = [
        _ob(yes_levels=[[50, 500]], no_levels=[[54, 100]]),  # spread=6, high bid imbalance
        _ob(yes_levels=[[50, 500]], no_levels=[[58, 100]]),  # spread=10, still high imbalance
        _ob(yes_levels=[[50, 500]], no_levels=[[62, 100]]),  # spread=14, growing
    ]
    client = AsyncMock()
    client.get_orderbook = AsyncMock(side_effect=responses)
    agent = MarketMakerAgent(client, snapshot_count=3, snapshot_delay=0)
    result = await agent.run("TICKER", "Some market")
    assert isinstance(result, list)
    if result:
        assert result[0].source == "kalshi_mm_spread"
        assert "spread_cents" in result[0].metadata
        assert result[0].weight in (0.55, 0.65)


@pytest.mark.asyncio
async def test_run_skips_withdrawal_signal():
    """Maker withdrawal alone (no directional imbalance) → return empty."""
    client = AsyncMock()
    client.get_orderbook = AsyncMock(return_value=_ob(
        yes_levels=[[50, 100]], no_levels=[[66, 100]]  # spread=16, balanced
    ))
    agent = MarketMakerAgent(client, snapshot_count=1, snapshot_delay=0)
    result = await agent.run("TICKER", "Some market")
    assert result == []


@pytest.mark.asyncio
async def test_run_metadata_fields():
    responses = [
        _ob(yes_levels=[[50, 800]], no_levels=[[54, 100]]),
        _ob(yes_levels=[[50, 800]], no_levels=[[58, 100]]),
        _ob(yes_levels=[[50, 800]], no_levels=[[62, 100]]),
    ]
    client = AsyncMock()
    client.get_orderbook = AsyncMock(side_effect=responses)
    agent = MarketMakerAgent(client, snapshot_count=3, snapshot_delay=0)
    result = await agent.run("TICKER", "Some market")
    if result:
        md = result[0].metadata
        assert "ticker" in md
        assert "narrative" in md
        assert "data_quality" in md
        assert md["data_quality"] == "fresh"
