"""Tests for ConditionalEventAgent and find_chain_violations."""
from __future__ import annotations

import json
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from kalshi_trader.agents.conditional_event_agent import (
    ConditionalEventAgent,
    find_chain_violations,
)
from kalshi_trader.models import SignalEstimate


# ---------------------------------------------------------------------------
# find_chain_violations unit tests
# ---------------------------------------------------------------------------


def _market(ticker: str, yes_ask: int, close_time: str) -> dict:
    return {"ticker": ticker, "yes_ask": yes_ask, "yes_bid": yes_ask - 2, "close_time": close_time}


def test_no_violations_monotonic_decreasing():
    """Correct chain: early price >= late price → no violations."""
    markets = [
        _market("GAME5", 70, "2026-06-05T20:00:00Z"),
        _market("SERIES", 55, "2026-06-10T20:00:00Z"),
        _market("CHAMP", 30, "2026-06-20T20:00:00Z"),
    ]
    assert find_chain_violations(markets) == []


def test_violation_detected_single_pair():
    """Later market priced higher than earlier one → one violation."""
    markets = [
        _market("GAME5", 40, "2026-06-05T20:00:00Z"),
        _market("CHAMP", 55, "2026-06-20T20:00:00Z"),
    ]
    violations = find_chain_violations(markets)
    assert len(violations) == 1
    v = violations[0]
    assert v["early_ticker"] == "GAME5"
    assert v["late_ticker"] == "CHAMP"
    assert v["early_price"] == 40
    assert v["late_price"] == 55
    assert v["price_gap_cents"] == 15


def test_violation_correct_gap_value():
    """price_gap_cents equals late_price - early_price exactly."""
    markets = [
        _market("A", 30, "2026-06-01T00:00:00Z"),
        _market("B", 37, "2026-06-02T00:00:00Z"),
    ]
    violations = find_chain_violations(markets)
    assert len(violations) == 1
    assert violations[0]["price_gap_cents"] == 7


def test_multiple_violations_three_markets():
    """All three pairs violate when each later market is higher."""
    markets = [
        _market("A", 20, "2026-06-01T00:00:00Z"),
        _market("B", 40, "2026-06-02T00:00:00Z"),
        _market("C", 60, "2026-06-03T00:00:00Z"),
    ]
    violations = find_chain_violations(markets)
    # (A,B), (A,C), (B,C) — all three pairs violate
    assert len(violations) == 3
    tickers = {(v["early_ticker"], v["late_ticker"]) for v in violations}
    assert ("A", "B") in tickers
    assert ("A", "C") in tickers
    assert ("B", "C") in tickers


def test_skips_zero_price_markets():
    """Markets with zero yes_ask are skipped entirely."""
    markets = [
        _market("A", 0, "2026-06-01T00:00:00Z"),
        _market("B", 50, "2026-06-02T00:00:00Z"),
        _market("C", 60, "2026-06-03T00:00:00Z"),
    ]
    violations = find_chain_violations(markets)
    # A is skipped; only (B, C) is a valid pair
    assert len(violations) == 1
    assert violations[0]["early_ticker"] == "B"
    assert violations[0]["late_ticker"] == "C"


def test_skips_none_price_markets():
    """Markets where yes_ask is None are skipped."""
    markets = [
        {"ticker": "A", "yes_ask": None, "close_time": "2026-06-01"},
        {"ticker": "B", "yes_ask": 50, "close_time": "2026-06-02"},
        {"ticker": "C", "yes_ask": 70, "close_time": "2026-06-03"},
    ]
    violations = find_chain_violations(markets)
    assert len(violations) == 1
    assert violations[0]["early_ticker"] == "B"


def test_equal_prices_not_a_violation():
    """Equal prices do not violate the chain (must be strictly greater)."""
    markets = [
        _market("A", 50, "2026-06-01T00:00:00Z"),
        _market("B", 50, "2026-06-02T00:00:00Z"),
    ]
    assert find_chain_violations(markets) == []


# ---------------------------------------------------------------------------
# ConditionalEventAgent._get_event_markets sorts by close_time
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_event_markets_sorted_by_close_time():
    client = AsyncMock()
    client.get = AsyncMock(return_value={
        "markets": [
            {"ticker": "B", "title": "B", "yes_bid": 30, "yes_ask": 32, "close_time": "2026-06-10"},
            {"ticker": "A", "title": "A", "yes_bid": 50, "yes_ask": 52, "close_time": "2026-06-05"},
            {"ticker": "C", "title": "C", "yes_bid": 10, "yes_ask": 12, "close_time": "2026-06-20"},
        ]
    })

    with patch("kalshi_trader.agents.conditional_event_agent.BaseAgent"):
        agent = ConditionalEventAgent.__new__(ConditionalEventAgent)
        agent._client = client

    result = await agent._get_event_markets("TESTEVENT")
    assert [r["ticker"] for r in result] == ["A", "B", "C"]


@pytest.mark.asyncio
async def test_get_event_markets_handles_client_exception():
    client = AsyncMock()
    client.get = AsyncMock(side_effect=Exception("network error"))

    with patch("kalshi_trader.agents.conditional_event_agent.BaseAgent"):
        agent = ConditionalEventAgent.__new__(ConditionalEventAgent)
        agent._client = client

    result = await agent._get_event_markets("TESTEVENT")
    assert result == []


# ---------------------------------------------------------------------------
# ConditionalEventAgent._build_conditional_signal returns correct structure
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_build_conditional_signal_structure():
    with patch("kalshi_trader.agents.conditional_event_agent.BaseAgent"):
        agent = ConditionalEventAgent.__new__(ConditionalEventAgent)

    violation = {
        "early_ticker": "GAME5",
        "early_price": 40,
        "late_ticker": "CHAMP",
        "late_price": 55,
        "price_gap_cents": 15,
    }
    result = await agent._build_conditional_signal("GAME5", violation)

    assert result["source"] == "conditional_event"
    assert result["weight"] == 0.80
    assert 0.0 < result["probability"] <= 0.95
    assert result["uncertainty"] > 0.0
    assert "data_issued_at" in result
    assert result["metadata"]["ticker"] == "GAME5"
    assert result["metadata"]["price_gap_cents"] == 15
    assert result["metadata"]["late_ticker"] == "CHAMP"
    assert result["metadata"]["early_price"] == 40
    assert result["metadata"]["late_price"] == 55
    assert result["metadata"]["data_quality"] == "fresh"
    assert "narrative" in result["metadata"]


@pytest.mark.asyncio
async def test_signal_source_is_conditional_event():
    """Source field must be 'conditional_event'."""
    with patch("kalshi_trader.agents.conditional_event_agent.BaseAgent"):
        agent = ConditionalEventAgent.__new__(ConditionalEventAgent)

    violation = {
        "early_ticker": "X",
        "early_price": 30,
        "late_ticker": "Y",
        "late_price": 50,
        "price_gap_cents": 20,
    }
    result = await agent._build_conditional_signal("X", violation)
    assert result["source"] == "conditional_event"


@pytest.mark.asyncio
async def test_signal_weight_is_0_80():
    """Weight must be 0.80."""
    with patch("kalshi_trader.agents.conditional_event_agent.BaseAgent"):
        agent = ConditionalEventAgent.__new__(ConditionalEventAgent)

    violation = {
        "early_ticker": "X",
        "early_price": 30,
        "late_ticker": "Y",
        "late_price": 50,
        "price_gap_cents": 20,
    }
    result = await agent._build_conditional_signal("X", violation)
    assert result["weight"] == 0.80


# ---------------------------------------------------------------------------
# ConditionalEventAgent.run — no event_ticker and client fails → []
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_run_returns_empty_when_no_event_ticker_and_client_fails():
    client = AsyncMock()
    client.get_market = AsyncMock(side_effect=Exception("API error"))

    with patch("kalshi_trader.agents.conditional_event_agent.BaseAgent"):
        agent = ConditionalEventAgent.__new__(ConditionalEventAgent)
        agent._client = client
        agent._agent = AsyncMock()

    result = await agent.run("TICKER-X", "Some market title")
    assert result == []


@pytest.mark.asyncio
async def test_run_returns_empty_when_event_ticker_empty_after_lookup():
    client = AsyncMock()
    # get_market returns a market dict with no event_ticker
    client.get_market = AsyncMock(return_value={"market": {"ticker": "X", "event_ticker": ""}})

    with patch("kalshi_trader.agents.conditional_event_agent.BaseAgent"):
        agent = ConditionalEventAgent.__new__(ConditionalEventAgent)
        agent._client = client
        agent._agent = AsyncMock()

    result = await agent.run("X", "Some title")
    assert result == []


# ---------------------------------------------------------------------------
# ConditionalEventAgent.run — get_event_markets returns empty → agent gets []
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_run_returns_empty_when_get_event_markets_empty():
    """When agent returns empty JSON array, run() returns []."""
    client = AsyncMock()

    mock_base_agent = AsyncMock()
    mock_base_agent.run = AsyncMock(return_value="```json\n[]\n```")

    with patch("kalshi_trader.agents.conditional_event_agent.BaseAgent", return_value=mock_base_agent):
        agent = ConditionalEventAgent(client)

    result = await agent.run("TICKER-A", "Some title", event_ticker="TESTEVENT")
    assert result == []


# ---------------------------------------------------------------------------
# ConditionalEventAgent.run — calls BaseAgent with correct prompt
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_run_calls_base_agent_with_correct_prompt():
    client = AsyncMock()

    mock_base_agent = AsyncMock()
    mock_base_agent.run = AsyncMock(return_value="```json\n[]\n```")

    with patch("kalshi_trader.agents.conditional_event_agent.BaseAgent", return_value=mock_base_agent):
        agent = ConditionalEventAgent(client)

    await agent.run("NBA-CELTICS-R2-G5", "Celtics win Game 5?", event_ticker="NBA-CELTICS-R2")

    mock_base_agent.run.assert_called_once()
    call_args = mock_base_agent.run.call_args[0][0]
    assert "NBA-CELTICS-R2-G5" in call_args
    assert "NBA-CELTICS-R2" in call_args
    assert "Celtics win Game 5?" in call_args


# ---------------------------------------------------------------------------
# ConditionalEventAgent.run — parses valid signal from agent output
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_run_parses_valid_signal_from_agent():
    """When agent returns a valid signal block, run() returns SignalEstimate list."""
    client = AsyncMock()

    signal_json = json.dumps([{
        "source": "conditional_event",
        "probability": 0.65,
        "uncertainty": 0.10,
        "weight": 0.80,
        "data_issued_at": "2026-06-01T12:00:00+00:00",
        "metadata": {
            "ticker": "GAME5",
            "narrative": "Chain violation detected.",
            "data_quality": "fresh",
            "price_gap_cents": 10,
            "late_ticker": "CHAMP",
            "early_price": 40,
            "late_price": 50,
        },
    }])

    mock_base_agent = AsyncMock()
    mock_base_agent.run = AsyncMock(return_value=f"```json\n{signal_json}\n```")

    with patch("kalshi_trader.agents.conditional_event_agent.BaseAgent", return_value=mock_base_agent):
        agent = ConditionalEventAgent(client)

    results = await agent.run("GAME5", "Win Game 5?", event_ticker="NBA-EVENT")
    assert len(results) == 1
    sig = results[0]
    assert isinstance(sig, SignalEstimate)
    assert sig.source == "conditional_event"
    assert sig.probability == 0.65
    assert sig.weight == 0.80
    assert sig.uncertainty == 0.10
