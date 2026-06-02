from __future__ import annotations
import json
import pytest
from unittest.mock import AsyncMock, MagicMock
from kalshi_trader.agents.x_agent import XAgent, X_AGENT_TOOLS
from kalshi_trader.external.x_client import GrokSearchResult


def _make_grok_result(probability: float = 0.65, uncertainty: float = 0.08) -> GrokSearchResult:
    return GrokSearchResult(
        probability=probability, uncertainty=uncertainty,
        summary="Crowd leans YES.", key_quotes=["Post 1"],
        sentiment_breakdown={"positive": 0.6, "negative": 0.2, "neutral": 0.2},
        source_quality={"high_follower": 0.4, "general": 0.6},
        velocity={"1h": 5, "6h": 20, "24h": 80},
        key_entities=["Team A"], contrarian_signal="",
        issued_at="2026-06-01T12:00:00",
    )


@pytest.mark.asyncio
async def test_search_x_signal_returns_list_of_estimates():
    agent = XAgent()
    agent._client.live_search = AsyncMock(return_value=_make_grok_result())

    results = await agent.search_x_signal("NBA-CELTICS", "sports", "Will Celtics win?")

    assert isinstance(results, list)
    assert len(results) >= 1
    for r in results:
        assert "probability" in r
        assert "source" in r
        assert "uncertainty" in r
        assert "metadata" in r


@pytest.mark.asyncio
async def test_search_x_signal_runs_strategies_for_category():
    agent = XAgent()
    agent._client.live_search = AsyncMock(return_value=_make_grok_result())

    results = await agent.search_x_signal("POL-VOTE", "politics", "Will candidate win?")

    sources = [r["source"] for r in results]
    assert any("sentiment" in s for s in sources)
    assert any("news" in s for s in sources)


@pytest.mark.asyncio
async def test_search_x_signal_falls_back_for_unknown_category():
    agent = XAgent()
    agent._client.live_search = AsyncMock(return_value=_make_grok_result())

    results = await agent.search_x_signal("CRYPTO-BTC", "crypto", "Will BTC hit 100k?")

    assert len(results) >= 1


@pytest.mark.asyncio
async def test_escalates_to_claude_when_uncertainty_exceeds_threshold():
    agent = XAgent()
    agent._client.live_search = AsyncMock(return_value=_make_grok_result(uncertainty=0.30))

    claude_text = json.dumps({"probability": 0.55, "uncertainty": 0.18, "reasoning": "hard call"})
    content_block = MagicMock()
    content_block.text = claude_text
    claude_resp = MagicMock()
    claude_resp.content = [content_block]
    mock_anthropic = MagicMock()
    mock_anthropic.messages.create = AsyncMock(return_value=claude_resp)
    agent._anthropic = mock_anthropic

    results = await agent.search_x_signal("POL-VOTE", "politics", "Will candidate win?")

    sources = [r["source"] for r in results]
    assert any("x_claude_" in s for s in sources)


@pytest.mark.asyncio
async def test_no_claude_escalation_when_uncertainty_below_threshold():
    agent = XAgent()
    agent._client.live_search = AsyncMock(return_value=_make_grok_result(uncertainty=0.05))

    results = await agent.search_x_signal("NBA-CELTICS", "sports", "Will Celtics win?")

    sources = [r["source"] for r in results]
    assert not any("x_claude_" in s for s in sources)


@pytest.mark.asyncio
async def test_override_x_strategies_uses_named_strategies():
    agent = XAgent()
    agent._client.live_search = AsyncMock(return_value=_make_grok_result())

    results = await agent.override_x_strategies(
        "NBA-CELTICS", "Will Celtics win?", ["sentiment"]
    )

    assert len(results) >= 1
    grok_sources = [r["source"] for r in results if "grok" in r["source"]]
    assert all("sentiment" in s for s in grok_sources)


@pytest.mark.asyncio
async def test_override_falls_back_on_all_unknown_strategy_names():
    agent = XAgent()
    agent._client.live_search = AsyncMock(return_value=_make_grok_result())

    results = await agent.override_x_strategies(
        "NBA-CELTICS", "Will Celtics win?", ["totally_fake_strategy"]
    )

    assert len(results) >= 1


def test_x_agent_tools_has_two_schemas():
    assert len(X_AGENT_TOOLS) == 2
    names = {t["name"] for t in X_AGENT_TOOLS}
    assert "search_x_signal" in names
    assert "override_x_strategies" in names
