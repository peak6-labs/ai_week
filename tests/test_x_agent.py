from __future__ import annotations
import pytest
from unittest.mock import AsyncMock, MagicMock
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
    from kalshi_trader.agents.x_agent import XAgent
    agent = XAgent.__new__(XAgent)
    import asyncio
    from kalshi_trader import config
    from kalshi_trader.external.x_client import XClient
    agent._client = MagicMock(spec=XClient)
    agent._client.live_search = AsyncMock(return_value=_make_grok_result())
    agent._semaphore = asyncio.Semaphore(config.X_MAX_CONCURRENT_SEARCHES)

    results = await agent._search_x_signal("NBA-CELTICS", "sports", "Will Celtics win?")

    assert isinstance(results, list)
    assert len(results) >= 1
    for r in results:
        assert "probability" in r
        assert "source" in r
        assert "uncertainty" in r


@pytest.mark.asyncio
async def test_search_x_signal_runs_strategies_for_category():
    from kalshi_trader.agents.x_agent import XAgent
    agent = XAgent.__new__(XAgent)
    import asyncio
    from kalshi_trader import config
    from kalshi_trader.external.x_client import XClient
    agent._client = MagicMock(spec=XClient)
    agent._client.live_search = AsyncMock(return_value=_make_grok_result())
    agent._semaphore = asyncio.Semaphore(config.X_MAX_CONCURRENT_SEARCHES)

    results = await agent._search_x_signal("POL-VOTE", "politics", "Will candidate win?")

    sources = [r["source"] for r in results]
    assert any("sentiment" in s for s in sources)
    assert any("news" in s for s in sources)


@pytest.mark.asyncio
async def test_search_x_signal_falls_back_for_unknown_category():
    from kalshi_trader.agents.x_agent import XAgent
    agent = XAgent.__new__(XAgent)
    import asyncio
    from kalshi_trader import config
    from kalshi_trader.external.x_client import XClient
    agent._client = MagicMock(spec=XClient)
    agent._client.live_search = AsyncMock(return_value=_make_grok_result())
    agent._semaphore = asyncio.Semaphore(config.X_MAX_CONCURRENT_SEARCHES)

    results = await agent._search_x_signal("CRYPTO-BTC", "crypto", "Will BTC hit 100k?")

    assert len(results) >= 1


@pytest.mark.asyncio
async def test_override_x_strategies_uses_named_strategies():
    from kalshi_trader.agents.x_agent import XAgent
    agent = XAgent.__new__(XAgent)
    import asyncio
    from kalshi_trader import config
    from kalshi_trader.external.x_client import XClient
    agent._client = MagicMock(spec=XClient)
    agent._client.live_search = AsyncMock(return_value=_make_grok_result())
    agent._semaphore = asyncio.Semaphore(config.X_MAX_CONCURRENT_SEARCHES)

    results = await agent._override_x_strategies(
        "NBA-CELTICS", "Will Celtics win?", ["sentiment"]
    )

    assert len(results) >= 1
    grok_sources = [r["source"] for r in results if "grok" in r["source"]]
    assert all("sentiment" in s for s in grok_sources)


@pytest.mark.asyncio
async def test_override_falls_back_on_all_unknown_strategy_names():
    from kalshi_trader.agents.x_agent import XAgent
    agent = XAgent.__new__(XAgent)
    import asyncio
    from kalshi_trader import config
    from kalshi_trader.external.x_client import XClient
    agent._client = MagicMock(spec=XClient)
    agent._client.live_search = AsyncMock(return_value=_make_grok_result())
    agent._semaphore = asyncio.Semaphore(config.X_MAX_CONCURRENT_SEARCHES)

    results = await agent._override_x_strategies(
        "NBA-CELTICS", "Will Celtics win?", ["totally_fake_strategy"]
    )

    assert len(results) >= 1


def test_x_agent_parse_estimates_valid():
    from kalshi_trader.agents.x_agent import XAgent
    agent = XAgent.__new__(XAgent)
    raw = '''```json
[
  {
    "source": "x_sentiment",
    "probability": 0.62,
    "uncertainty": 0.14,
    "weight": 0.55,
    "data_issued_at": "2026-06-02T12:00:00+00:00",
    "metadata": {"ticker": "NBA-CELTICS", "sentiment_direction": "bullish",
                 "sentiment_reasoning": "Analysts favor YES.", "post_count": 17,
                 "strategies_used": "sentiment,buzz", "data_quality": "fresh",
                 "narrative": "Bullish."}
  }
]
```'''
    from kalshi_trader.models import SignalEstimate
    results = agent._parse_estimates(raw)
    assert len(results) == 1
    assert isinstance(results[0], SignalEstimate)
    assert results[0].metadata["sentiment_direction"] == "bullish"


def test_x_agent_parse_estimates_empty():
    from kalshi_trader.agents.x_agent import XAgent
    agent = XAgent.__new__(XAgent)
    assert agent._parse_estimates("```json\n[]\n```") == []
