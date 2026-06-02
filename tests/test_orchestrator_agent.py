"""Tests for OrchestratorAgent — all tests mock BaseAgent.run."""
from __future__ import annotations

import json
from datetime import datetime, timezone
from unittest.mock import AsyncMock, patch

import pytest

from kalshi_trader.agents.orchestrator_agent import (
    OrchestratorAgent,
    _build_trade_idea,
    _estimate_to_input,
    _parse_trade_ideas,
)
from kalshi_trader.models import Market, OrderAction, Side, SignalEstimate, TradeIdea


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

def _make_signal(
    source: str = "order_flow",
    probability: float = 0.65,
    uncertainty: float = 0.08,
    weight: float = 0.80,
    narrative: str = "strong buy-side imbalance",
) -> SignalEstimate:
    return SignalEstimate(
        source=source,
        probability=probability,
        uncertainty=uncertainty,
        weight=weight,
        data_issued_at=datetime(2026, 6, 1, 12, 0, 0, tzinfo=timezone.utc),
        metadata={"narrative": narrative},
    )


def _make_market(
    ticker: str = "SPORTS-NFL-KC-2026",
    title: str = "Chiefs to win Super Bowl 2026?",
    yes_ask: float = 35.0,
    category: str = "sports",
    close_time: datetime | None = None,
) -> Market:
    if close_time is None:
        close_time = datetime(2027, 1, 1, 0, 0, 0, tzinfo=timezone.utc)
    return Market(
        ticker=ticker,
        event_ticker="NFL-2026",
        series_ticker="NFL",
        title=title,
        yes_bid=34.0,
        yes_ask=yes_ask,
        last_price=34.5,
        volume_24h=5000,
        open_interest=10000,
        category=category,
        close_time=close_time,
        status="open",
    )


def _trade_idea_json(
    ticker: str = "SPORTS-NFL-KC-2026",
    side: str = "yes",
    confidence: float = 0.72,
    market_price: float = 35.0,
    category: str = "sports",
    signal_sources: list[str] | None = None,
    suggested_size_dollars: float = 45.0,
) -> dict:
    return {
        "agent_id": "orchestrator",
        "ticker": ticker,
        "side": side,
        "action": "buy",
        "confidence": confidence,
        "market_price": market_price,
        "reasoning": "Two independent signals agree. Bear case checked. Survives challenge.",
        "signal_sources": signal_sources or ["order_flow", "polymarket_price"],
        "suggested_size_dollars": suggested_size_dollars,
        "category": category,
    }


def _fenced_json(data: object) -> str:
    return f"```json\n{json.dumps(data, indent=2)}\n```"


# ---------------------------------------------------------------------------
# Unit tests: _build_trade_idea
# ---------------------------------------------------------------------------

def test_build_trade_idea_structure():
    result = _build_trade_idea(
        ticker="SPORTS-NFL-KC-2026",
        side="yes",
        confidence=0.72,
        reasoning="Signal survives challenge.",
        signal_sources=["order_flow", "polymarket_price"],
        suggested_size_dollars=45.0,
        market_price=35.0,
        category="sports",
    )
    assert result["agent_id"] == "orchestrator"
    assert result["ticker"] == "SPORTS-NFL-KC-2026"
    assert result["side"] == "yes"
    assert result["action"] == "buy"
    assert result["confidence"] == 0.72
    assert result["market_price"] == 35.0
    assert result["reasoning"] == "Signal survives challenge."
    assert result["signal_sources"] == ["order_flow", "polymarket_price"]
    assert result["suggested_size_dollars"] == 45.0
    assert result["category"] == "sports"


def test_build_trade_idea_defaults():
    result = _build_trade_idea(
        ticker="TEST-TICKER",
        side="no",
        confidence=0.55,
        reasoning="reasoning",
        signal_sources=[],
        suggested_size_dollars=10.0,
    )
    assert result["market_price"] == 0.0
    assert result["category"] == ""
    assert result["action"] == "buy"


# ---------------------------------------------------------------------------
# Unit tests: _estimate_to_input
# ---------------------------------------------------------------------------

def test_estimate_to_input_serializes_correctly():
    sig = _make_signal(
        source="polymarket_price",
        probability=0.70,
        uncertainty=0.05,
        weight=0.85,
        narrative="8¢ premium on Polymarket",
    )
    result = _estimate_to_input(sig)
    assert result["source"] == "polymarket_price"
    assert result["probability"] == 0.70
    assert result["uncertainty"] == 0.05
    assert result["weight"] == 0.85
    assert result["narrative"] == "8¢ premium on Polymarket"


def test_estimate_to_input_empty_narrative():
    sig = SignalEstimate(
        source="kalshi_bias",
        probability=0.60,
        uncertainty=0.10,
        weight=0.70,
        data_issued_at=datetime(2026, 6, 1, tzinfo=timezone.utc),
        metadata={},
    )
    result = _estimate_to_input(sig)
    assert result["narrative"] == ""


# ---------------------------------------------------------------------------
# Unit tests: _parse_trade_ideas
# ---------------------------------------------------------------------------

def test_parse_trade_ideas_valid():
    raw = _fenced_json([_trade_idea_json()])
    ideas = _parse_trade_ideas(raw)
    assert len(ideas) == 1
    idea = ideas[0]
    assert isinstance(idea, TradeIdea)
    assert idea.ticker == "SPORTS-NFL-KC-2026"
    assert idea.side == Side.YES
    assert idea.action == OrderAction.BUY
    assert idea.confidence == 0.72
    assert idea.market_price == 35.0
    assert idea.signal_sources == ["order_flow", "polymarket_price"]
    assert idea.suggested_size_dollars == 45.0
    assert idea.category == "sports"
    assert idea.agent_id == "orchestrator"


def test_parse_trade_ideas_multiple():
    raw = _fenced_json([
        _trade_idea_json(ticker="MKT-A", confidence=0.80),
        _trade_idea_json(ticker="MKT-B", confidence=0.65),
    ])
    ideas = _parse_trade_ideas(raw)
    assert len(ideas) == 2
    assert ideas[0].ticker == "MKT-A"
    assert ideas[1].ticker == "MKT-B"


def test_parse_trade_ideas_no_fenced_block():
    raw = "No JSON here at all."
    assert _parse_trade_ideas(raw) == []


def test_parse_trade_ideas_malformed_json():
    raw = "```json\n{not valid json\n```"
    assert _parse_trade_ideas(raw) == []


def test_parse_trade_ideas_not_a_list():
    raw = _fenced_json({"ticker": "SINGLE"})
    assert _parse_trade_ideas(raw) == []


def test_parse_trade_ideas_empty_list():
    raw = _fenced_json([])
    assert _parse_trade_ideas(raw) == []


def test_parse_trade_ideas_skips_malformed_items():
    raw = _fenced_json([
        _trade_idea_json(ticker="GOOD-MKT"),
        {"bad": "item_missing_required_fields"},
        {"ticker": "INCOMPLETE", "side": "yes"},  # missing confidence
    ])
    ideas = _parse_trade_ideas(raw)
    assert len(ideas) == 1
    assert ideas[0].ticker == "GOOD-MKT"


def test_parse_trade_ideas_side_no():
    raw = _fenced_json([_trade_idea_json(side="no")])
    ideas = _parse_trade_ideas(raw)
    assert ideas[0].side == Side.NO


# ---------------------------------------------------------------------------
# OrchestratorAgent integration tests (mock BaseAgent.run)
# ---------------------------------------------------------------------------

def _make_orchestrator() -> OrchestratorAgent:
    """Create OrchestratorAgent without touching the real Anthropic client."""
    with patch("kalshi_trader.agents.orchestrator_agent.BaseAgent"):
        agent = OrchestratorAgent()
    return agent


@pytest.mark.asyncio
async def test_run_returns_empty_when_no_markets():
    agent = _make_orchestrator()
    result = await agent.run(markets=[], signals={})
    assert result == []


@pytest.mark.asyncio
async def test_run_returns_empty_when_no_signals():
    agent = _make_orchestrator()
    market = _make_market()
    result = await agent.run(markets=[market], signals={})
    assert result == []


@pytest.mark.asyncio
async def test_run_returns_empty_when_signals_dict_empty_lists():
    agent = _make_orchestrator()
    market = _make_market()
    result = await agent.run(markets=[market], signals={market.ticker: []})
    assert result == []


@pytest.mark.asyncio
async def test_run_passes_only_markets_with_signals():
    """Markets without signals should not appear in the prompt sent to Claude."""
    agent = _make_orchestrator()

    market_with_signal = _make_market(ticker="MKT-WITH-SIGNAL", title="Has signal")
    market_no_signal = _make_market(ticker="MKT-NO-SIGNAL", title="No signal")
    signals = {market_with_signal.ticker: [_make_signal()]}

    captured_prompt: list[str] = []

    async def fake_run(prompt: str) -> str:
        captured_prompt.append(prompt)
        return _fenced_json([])

    agent._agent.run = AsyncMock(side_effect=fake_run)

    await agent.run(
        markets=[market_with_signal, market_no_signal],
        signals=signals,
    )

    assert len(captured_prompt) == 1
    assert "MKT-WITH-SIGNAL" in captured_prompt[0]
    assert "MKT-NO-SIGNAL" not in captured_prompt[0]


@pytest.mark.asyncio
async def test_run_prompt_contains_market_count():
    agent = _make_orchestrator()
    market = _make_market()
    signals = {market.ticker: [_make_signal()]}

    async def fake_run(prompt: str) -> str:
        return _fenced_json([])

    agent._agent.run = AsyncMock(side_effect=fake_run)
    await agent.run(markets=[market], signals=signals)

    call_args = agent._agent.run.call_args
    prompt = call_args[0][0]
    assert "1 Kalshi" in prompt


@pytest.mark.asyncio
async def test_run_parses_trade_ideas_from_agent_output():
    agent = _make_orchestrator()
    market = _make_market(ticker="SPORTS-NFL-KC-2026", yes_ask=35.0, category="sports")
    signals = {market.ticker: [_make_signal()]}

    async def fake_run(prompt: str) -> str:
        return _fenced_json([_trade_idea_json()])

    agent._agent.run = AsyncMock(side_effect=fake_run)

    ideas = await agent.run(markets=[market], signals=signals)

    assert len(ideas) == 1
    assert isinstance(ideas[0], TradeIdea)
    assert ideas[0].ticker == "SPORTS-NFL-KC-2026"


@pytest.mark.asyncio
async def test_run_returns_empty_on_agent_empty_output():
    agent = _make_orchestrator()
    market = _make_market()
    signals = {market.ticker: [_make_signal()]}

    agent._agent.run = AsyncMock(return_value=_fenced_json([]))

    ideas = await agent.run(markets=[market], signals=signals)
    assert ideas == []


@pytest.mark.asyncio
async def test_run_returns_empty_on_agent_no_json():
    agent = _make_orchestrator()
    market = _make_market()
    signals = {market.ticker: [_make_signal()]}

    agent._agent.run = AsyncMock(return_value="No valid JSON here.")

    ideas = await agent.run(markets=[market], signals=signals)
    assert ideas == []


# ---------------------------------------------------------------------------
# OrchestratorAgent._get_market_signals
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_get_market_signals_returns_correct_dicts():
    agent = _make_orchestrator()
    market = _make_market(ticker="TEST-MKT", yes_ask=42.0)
    sig = _make_signal(source="weather", probability=0.75, narrative="cold snap incoming")

    agent._signals_by_ticker = {"TEST-MKT": [sig]}
    agent._markets_by_ticker = {"TEST-MKT": market}

    result = await agent._get_market_signals("TEST-MKT")

    assert len(result) == 1
    item = result[0]
    assert item["source"] == "weather"
    assert item["probability"] == 0.75
    assert item["uncertainty"] == sig.uncertainty
    assert item["weight"] == sig.weight
    assert item["narrative"] == "cold snap incoming"
    assert item["market_yes_ask"] == 42.0


@pytest.mark.asyncio
async def test_get_market_signals_unknown_ticker():
    agent = _make_orchestrator()
    agent._signals_by_ticker = {}
    agent._markets_by_ticker = {}

    result = await agent._get_market_signals("UNKNOWN-TICKER")
    assert result == []


@pytest.mark.asyncio
async def test_get_market_signals_no_market_context():
    """market_yes_ask should not appear if market is not in _markets_by_ticker."""
    agent = _make_orchestrator()
    sig = _make_signal()
    agent._signals_by_ticker = {"ORPHAN": [sig]}
    agent._markets_by_ticker = {}

    result = await agent._get_market_signals("ORPHAN")
    assert len(result) == 1
    assert "market_yes_ask" not in result[0]


# ---------------------------------------------------------------------------
# OrchestratorAgent._build_trade_idea_handler
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_build_trade_idea_handler_fills_market_price_from_market():
    agent = _make_orchestrator()
    market = _make_market(ticker="FILL-PRICE", yes_ask=55.0, category="finance")
    agent._markets_by_ticker = {"FILL-PRICE": market}

    result = await agent._build_trade_idea_handler(
        ticker="FILL-PRICE",
        side="yes",
        confidence=0.68,
        reasoning="test",
        signal_sources=["order_flow"],
        suggested_size_dollars=20.0,
        market_price=0.0,  # should be filled from market
        category="",       # should be filled from market
    )

    assert result["market_price"] == 55.0
    assert result["category"] == "finance"


@pytest.mark.asyncio
async def test_build_trade_idea_handler_respects_explicit_values():
    agent = _make_orchestrator()
    market = _make_market(ticker="EXPLICIT", yes_ask=55.0, category="finance")
    agent._markets_by_ticker = {"EXPLICIT": market}

    result = await agent._build_trade_idea_handler(
        ticker="EXPLICIT",
        side="no",
        confidence=0.60,
        reasoning="explicit override",
        signal_sources=["kalshi_bias"],
        suggested_size_dollars=15.0,
        market_price=50.0,   # explicit — should not be overridden
        category="politics", # explicit — should not be overridden
    )

    assert result["market_price"] == 50.0
    assert result["category"] == "politics"
