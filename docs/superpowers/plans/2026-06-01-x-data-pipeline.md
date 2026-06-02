# X Social Signal Pipeline Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build `XClient`, four search strategy classes, and an `XAgent` with two Claude tools (`search_x_signal`, `override_x_strategies`) that pull live X social signal via the xAI/Grok API and return `SignalEstimate` lists compatible with the rest of the trading system.

**Architecture:** `XClient` wraps the xAI API with a single `live_search()` method. Four `BaseXStrategy` subclasses (Sentiment, News, ExpertOpinion, Buzz) each implement `build_query()` and map results to `SignalEstimate`. `XAgent` holds the two Claude tool schemas and handlers: it runs strategies in parallel (semaphore-gated), escalates to a Claude second-pass when Grok uncertainty exceeds 0.15, and returns serialised estimates. `BaseAgent` (already implemented) is not touched.

**Tech Stack:** Python 3.14, `aiohttp` (xAI HTTP), `anthropic` SDK (Claude second-pass), `pytest` + `pytest-asyncio` + `unittest.mock` (tests). `kalshi_trader.config` extended with seven new constants.

---

## File Map

| File | Action | Responsibility |
|------|--------|----------------|
| `kalshi_trader/config.py` | Modify | Add XAI_API_KEY, XAI_BASE_URL, XAI_MODEL, thresholds, weights |
| `kalshi_trader/external/x_client.py` | Create | `GrokSearchResult` TypedDict, `XClient.live_search()`, JSON parser, error fallback |
| `kalshi_trader/external/x_strategies.py` | Create | `BaseXStrategy`, four strategy classes, `CATEGORY_STRATEGIES` registry, `STRATEGY_NAME_MAP`, `FALLBACK_STRATEGIES` |
| `kalshi_trader/agents/x_agent.py` | Create | `XAgent` with `search_x_signal` and `override_x_strategies` tool handlers + Claude second-pass |
| `tests/test_x_client.py` | Create | XClient unit tests with mocked aiohttp |
| `tests/test_x_strategies.py` | Create | Strategy query builder + registry unit tests |
| `tests/test_x_agent.py` | Create | XAgent tool handler tests with mocked XClient + Anthropic |
| `tests/test_x_integration.py` | Create | Integration test (skipped unless `XAI_API_KEY` is set) |

---

## Task 1: Add XAI config constants

**Files:**
- Modify: `kalshi_trader/config.py`

- [ ] **Step 1: Append constants to config.py**

Add these lines at the end of `kalshi_trader/config.py`:

```python
XAI_API_KEY = os.environ.get("XAI_API_KEY", "")
XAI_BASE_URL = "https://api.x.ai/v1"
XAI_MODEL = "grok-3"
X_GROK_UNCERTAINTY_THRESHOLD = 0.15
X_MAX_CONCURRENT_SEARCHES = 3
X_GROK_SIGNAL_WEIGHT = 0.6
X_CLAUDE_SIGNAL_WEIGHT = 0.75
```

- [ ] **Step 2: Verify existing tests still pass**

```bash
cd /Users/scorley/code && .venv/bin/pytest tests/test_config.py -v
```

Expected: all tests pass.

- [ ] **Step 3: Commit**

```bash
git add kalshi_trader/config.py
git commit -m "feat: add XAI config constants for X data pipeline"
```

---

## Task 2: XClient — xAI API wrapper

**Files:**
- Create: `kalshi_trader/external/x_client.py`
- Create: `tests/test_x_client.py`

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_x_client.py
from __future__ import annotations
import json
import pytest
from unittest.mock import AsyncMock, MagicMock
from kalshi_trader.external.x_client import XClient, GrokSearchResult, _empty_result


class _MockResponse:
    def __init__(self, data: dict):
        self._data = data

    def raise_for_status(self) -> None:
        pass

    async def json(self) -> dict:
        return self._data

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_):
        pass


@pytest.mark.asyncio
async def test_returns_empty_result_when_api_key_missing(monkeypatch):
    monkeypatch.setattr("kalshi_trader.config.XAI_API_KEY", "")
    client = XClient()
    result = await client.live_search("test query", "test market")
    assert result["probability"] == 0.5
    assert result["uncertainty"] == 1.0


@pytest.mark.asyncio
async def test_parses_json_from_grok_response(monkeypatch):
    monkeypatch.setattr("kalshi_trader.config.XAI_API_KEY", "test-key")

    payload = {
        "probability": 0.72,
        "uncertainty": 0.09,
        "summary": "Bulls are favoured on X.",
        "key_quotes": ["Bulls will win", "Easy sweep"],
        "sentiment_breakdown": {"positive": 0.7, "negative": 0.1, "neutral": 0.2},
        "source_quality": {"high_follower": 0.5, "general": 0.5},
        "velocity": {"1h": 10, "6h": 40, "24h": 120},
        "key_entities": ["Bulls", "Heat"],
        "contrarian_signal": "",
        "issued_at": "2026-06-01T12:00:00",
    }
    api_response = {"choices": [{"message": {"content": json.dumps(payload)}}]}
    mock_resp = _MockResponse(api_response)

    mock_session = MagicMock()
    mock_session.post = MagicMock(return_value=mock_resp)

    client = XClient()
    client._session = mock_session

    result = await client.live_search("Bulls win series", "Will Bulls win the series?")
    assert result["probability"] == 0.72
    assert result["uncertainty"] == 0.09
    assert result["summary"] == "Bulls are favoured on X."


@pytest.mark.asyncio
async def test_parses_json_wrapped_in_markdown_code_block(monkeypatch):
    monkeypatch.setattr("kalshi_trader.config.XAI_API_KEY", "test-key")

    payload = {
        "probability": 0.5, "uncertainty": 0.2, "summary": "Mixed views.",
        "key_quotes": [], "sentiment_breakdown": {"positive": 0.5, "negative": 0.3, "neutral": 0.2},
        "source_quality": {"high_follower": 0.3, "general": 0.7},
        "velocity": {"1h": 1, "6h": 5, "24h": 20},
        "key_entities": [], "contrarian_signal": "", "issued_at": "2026-06-01T10:00:00",
    }
    wrapped = f"```json\n{json.dumps(payload)}\n```"
    api_response = {"choices": [{"message": {"content": wrapped}}]}
    mock_resp = _MockResponse(api_response)
    mock_session = MagicMock()
    mock_session.post = MagicMock(return_value=mock_resp)

    client = XClient()
    client._session = mock_resp  # will be replaced
    client._session = mock_session

    result = await client.live_search("query", "market")
    assert result["probability"] == 0.5


@pytest.mark.asyncio
async def test_returns_empty_result_on_network_error(monkeypatch):
    monkeypatch.setattr("kalshi_trader.config.XAI_API_KEY", "test-key")

    mock_session = MagicMock()
    mock_session.post = MagicMock(side_effect=Exception("Network error"))

    client = XClient()
    client._session = mock_session

    result = await client.live_search("query", "market")
    assert result["uncertainty"] == 1.0


@pytest.mark.asyncio
async def test_returns_empty_result_on_invalid_json(monkeypatch):
    monkeypatch.setattr("kalshi_trader.config.XAI_API_KEY", "test-key")

    api_response = {"choices": [{"message": {"content": "This is not JSON at all."}}]}
    mock_resp = _MockResponse(api_response)
    mock_session = MagicMock()
    mock_session.post = MagicMock(return_value=mock_resp)

    client = XClient()
    client._session = mock_session

    result = await client.live_search("query", "market")
    assert result["uncertainty"] == 1.0
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
cd /Users/scorley/code && .venv/bin/pytest tests/test_x_client.py -v
```

Expected: `ModuleNotFoundError: No module named 'kalshi_trader.external.x_client'`

- [ ] **Step 3: Implement x_client.py**

```python
# kalshi_trader/external/x_client.py
from __future__ import annotations
import json
from datetime import datetime
from typing import TypedDict
import aiohttp
from kalshi_trader import config


class GrokSearchResult(TypedDict):
    probability: float
    uncertainty: float
    summary: str
    key_quotes: list
    sentiment_breakdown: dict
    source_quality: dict
    velocity: dict
    key_entities: list
    contrarian_signal: str
    issued_at: str


def _empty_result() -> GrokSearchResult:
    return GrokSearchResult(
        probability=0.5,
        uncertainty=1.0,
        summary="",
        key_quotes=[],
        sentiment_breakdown={"positive": 0.33, "negative": 0.33, "neutral": 0.34},
        source_quality={"high_follower": 0.0, "general": 1.0},
        velocity={"1h": 0, "6h": 0, "24h": 0},
        key_entities=[],
        contrarian_signal="",
        issued_at=datetime.utcnow().isoformat(),
    )


def _parse_grok_response(text: str) -> GrokSearchResult:
    text = text.strip()
    if "```" in text:
        parts = text.split("```")
        for part in parts:
            part = part.strip()
            if part.startswith("json"):
                part = part[4:].strip()
            if part.startswith("{"):
                text = part
                break
    try:
        data = json.loads(text)
        if not data.get("issued_at"):
            data["issued_at"] = datetime.utcnow().isoformat()
        return data  # type: ignore[return-value]
    except (json.JSONDecodeError, ValueError):
        return _empty_result()


_SEARCH_PROMPT = (
    "Search X (Twitter) for posts about: {query}\n\n"
    "You are helping analyse a Kalshi prediction market: \"{market_title}\"\n\n"
    "Return ONLY a JSON object with exactly these fields:\n"
    '{{\n'
    '  "probability": <float 0.0-1.0, your estimate of YES resolution probability>,\n'
    '  "uncertainty": <float 0.0-1.0, where 0.05=very confident, 0.4=very uncertain>,\n'
    '  "summary": "<2-3 sentence summary of what X posts are saying>",\n'
    '  "key_quotes": ["<post 1>", "<post 2>"],\n'
    '  "sentiment_breakdown": {{"positive": <float>, "negative": <float>, "neutral": <float>}},\n'
    '  "source_quality": {{"high_follower": <float>, "general": <float>}},\n'
    '  "velocity": {{"1h": <int>, "6h": <int>, "24h": <int>}},\n'
    '  "key_entities": ["<entity1>"],\n'
    '  "contrarian_signal": "<notable minority view, or empty string>",\n'
    '  "issued_at": "<ISO 8601 timestamp>"\n'
    '}}'
)


class XClient:
    def __init__(self) -> None:
        self._session: aiohttp.ClientSession | None = None

    async def live_search(self, query: str, market_title: str = "") -> GrokSearchResult:
        if not config.XAI_API_KEY:
            return _empty_result()

        if self._session is None:
            self._session = aiohttp.ClientSession()

        payload = {
            "model": config.XAI_MODEL,
            "messages": [{
                "role": "user",
                "content": _SEARCH_PROMPT.format(query=query, market_title=market_title),
            }],
            "search_parameters": {"mode": "on", "sources": [{"type": "x"}]},
        }
        headers = {
            "Authorization": f"Bearer {config.XAI_API_KEY}",
            "Content-Type": "application/json",
        }

        try:
            async with self._session.post(
                f"{config.XAI_BASE_URL}/chat/completions",
                json=payload,
                headers=headers,
                timeout=aiohttp.ClientTimeout(total=30),
            ) as resp:
                resp.raise_for_status()
                data = await resp.json()
                text = data["choices"][0]["message"]["content"]
                return _parse_grok_response(text)
        except Exception:
            return _empty_result()

    async def close(self) -> None:
        if self._session:
            await self._session.close()
            self._session = None
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
cd /Users/scorley/code && .venv/bin/pytest tests/test_x_client.py -v
```

Expected: 5 passed.

- [ ] **Step 5: Commit**

```bash
git add kalshi_trader/external/x_client.py tests/test_x_client.py
git commit -m "feat: add XClient wrapping xAI live search API"
```

---

## Task 3: Strategy classes and category registry

**Files:**
- Create: `kalshi_trader/external/x_strategies.py`
- Create: `tests/test_x_strategies.py`

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_x_strategies.py
from datetime import datetime
import pytest
from unittest.mock import AsyncMock
from kalshi_trader.external.x_strategies import (
    SentimentStrategy, NewsDetectionStrategy, ExpertOpinionStrategy, BuzzStrategy,
    CATEGORY_STRATEGIES, STRATEGY_NAME_MAP, FALLBACK_STRATEGIES,
)
from kalshi_trader.external.x_client import GrokSearchResult


def _make_grok_result(probability: float = 0.65, uncertainty: float = 0.1) -> GrokSearchResult:
    return GrokSearchResult(
        probability=probability, uncertainty=uncertainty,
        summary="Test summary.", key_quotes=["q1"],
        sentiment_breakdown={"positive": 0.6, "negative": 0.2, "neutral": 0.2},
        source_quality={"high_follower": 0.4, "general": 0.6},
        velocity={"1h": 5, "6h": 20, "24h": 80},
        key_entities=["Entity1"], contrarian_signal="",
        issued_at="2026-06-01T12:00:00",
    )


def test_sentiment_query_contains_market_title():
    q = SentimentStrategy().build_query("Will Celtics win the championship?")
    assert "Celtics win the championship" in q


def test_news_query_contains_market_title():
    q = NewsDetectionStrategy().build_query("Will inflation exceed 3%?")
    assert "inflation" in q.lower() or "exceed 3%" in q


def test_expert_query_contains_market_title():
    q = ExpertOpinionStrategy().build_query("Will it rain in NYC tomorrow?")
    assert "NYC" in q or "rain" in q.lower()


def test_buzz_query_contains_market_title():
    q = BuzzStrategy().build_query("Will Lakers win tonight?")
    assert "Lakers" in q


def test_all_source_tags_are_unique():
    tags = [cls().source_tag for cls in [SentimentStrategy, NewsDetectionStrategy, ExpertOpinionStrategy, BuzzStrategy]]
    assert len(tags) == len(set(tags))


def test_all_source_tags_start_with_x_grok():
    for cls in [SentimentStrategy, NewsDetectionStrategy, ExpertOpinionStrategy, BuzzStrategy]:
        assert cls().source_tag.startswith("x_grok_")


def test_category_strategies_has_required_categories():
    for cat in ("weather", "mentions", "politics", "sports"):
        assert cat in CATEGORY_STRATEGIES
        assert len(CATEGORY_STRATEGIES[cat]) >= 1


def test_strategy_name_map_has_all_four_names():
    for name in ("sentiment", "news", "experts", "buzz"):
        assert name in STRATEGY_NAME_MAP


def test_fallback_strategies_is_nonempty():
    assert len(FALLBACK_STRATEGIES) >= 1


@pytest.mark.asyncio
async def test_to_signal_estimate_maps_fields():
    strategy = SentimentStrategy()
    result = _make_grok_result(probability=0.72, uncertainty=0.08)
    estimate = strategy.to_signal_estimate(result)
    assert estimate.source == "x_grok_sentiment"
    assert estimate.probability == 0.72
    assert estimate.uncertainty == 0.08
    assert estimate.metadata["summary"] == "Test summary."
    assert "key_quotes" in estimate.metadata
    assert "velocity" in estimate.metadata


@pytest.mark.asyncio
async def test_run_delegates_to_client():
    strategy = NewsDetectionStrategy()
    mock_client = AsyncMock()
    mock_client.live_search = AsyncMock(return_value=_make_grok_result())
    await strategy.run("Will there be a recession?", mock_client)
    mock_client.live_search.assert_called_once()
    call_args = mock_client.live_search.call_args
    assert "recession" in call_args[0][0].lower() or "recession" in str(call_args)
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
cd /Users/scorley/code && .venv/bin/pytest tests/test_x_strategies.py -v
```

Expected: `ModuleNotFoundError: No module named 'kalshi_trader.external.x_strategies'`

- [ ] **Step 3: Implement x_strategies.py**

```python
# kalshi_trader/external/x_strategies.py
from __future__ import annotations
from abc import ABC, abstractmethod
from datetime import datetime
from kalshi_trader.models import SignalEstimate
from kalshi_trader.external.x_client import XClient, GrokSearchResult
from kalshi_trader import config


def _parse_issued_at(issued_at: str) -> datetime:
    try:
        return datetime.fromisoformat(issued_at.replace("Z", "+00:00")).replace(tzinfo=None)
    except (ValueError, AttributeError):
        return datetime.utcnow()


class BaseXStrategy(ABC):
    source_tag: str

    @abstractmethod
    def build_query(self, market_title: str) -> str: ...

    async def run(self, market_title: str, client: XClient) -> GrokSearchResult:
        query = self.build_query(market_title)
        return await client.live_search(query, market_title)

    def to_signal_estimate(self, result: GrokSearchResult) -> SignalEstimate:
        return SignalEstimate(
            source=self.source_tag,
            probability=result["probability"],
            uncertainty=result["uncertainty"],
            weight=config.X_GROK_SIGNAL_WEIGHT,
            data_issued_at=_parse_issued_at(result["issued_at"]),
            metadata={
                "summary": result["summary"],
                "key_quotes": result["key_quotes"],
                "sentiment_breakdown": result["sentiment_breakdown"],
                "source_quality": result["source_quality"],
                "velocity": result["velocity"],
                "key_entities": result["key_entities"],
                "contrarian_signal": result["contrarian_signal"],
            },
        )


class SentimentStrategy(BaseXStrategy):
    source_tag = "x_grok_sentiment"

    def build_query(self, market_title: str) -> str:
        return f"{market_title} prediction odds probability sentiment"


class NewsDetectionStrategy(BaseXStrategy):
    source_tag = "x_grok_news"

    def build_query(self, market_title: str) -> str:
        return f"{market_title} breaking news latest update"


class ExpertOpinionStrategy(BaseXStrategy):
    source_tag = "x_grok_experts"

    def build_query(self, market_title: str) -> str:
        return f"{market_title} expert forecast analysis opinion"


class BuzzStrategy(BaseXStrategy):
    source_tag = "x_grok_buzz"

    def build_query(self, market_title: str) -> str:
        return market_title


CATEGORY_STRATEGIES: dict[str, list[type[BaseXStrategy]]] = {
    "weather":  [ExpertOpinionStrategy, NewsDetectionStrategy],
    "mentions": [BuzzStrategy, SentimentStrategy],
    "politics": [SentimentStrategy, NewsDetectionStrategy],
    "sports":   [NewsDetectionStrategy, SentimentStrategy],
}

STRATEGY_NAME_MAP: dict[str, type[BaseXStrategy]] = {
    "sentiment": SentimentStrategy,
    "news":      NewsDetectionStrategy,
    "experts":   ExpertOpinionStrategy,
    "buzz":      BuzzStrategy,
}

FALLBACK_STRATEGIES: list[type[BaseXStrategy]] = [BuzzStrategy, SentimentStrategy]
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
cd /Users/scorley/code && .venv/bin/pytest tests/test_x_strategies.py -v
```

Expected: 11 passed.

- [ ] **Step 5: Commit**

```bash
git add kalshi_trader/external/x_strategies.py tests/test_x_strategies.py
git commit -m "feat: add X signal strategy classes and category registry"
```

---

## Task 4: XAgent — search_x_signal tool

**Files:**
- Create: `kalshi_trader/agents/x_agent.py`
- Create: `tests/test_x_agent.py`

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_x_agent.py
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
    # Override _anthropic directly so we control the response without hitting the real API
    mock_anthropic = MagicMock()
    mock_anthropic.messages.create = AsyncMock(return_value=claude_resp)
    agent._anthropic = mock_anthropic

    results = await agent.search_x_signal("POL-VOTE", "politics", "Will candidate win?")

    sources = [r["source"] for r in results]
    assert any("x_claude_" in s for s in sources)


@pytest.mark.asyncio
async def test_no_claude_escalation_when_uncertainty_below_threshold():
    agent = XAgent()
    # uncertainty=0.05 < X_GROK_UNCERTAINTY_THRESHOLD (0.15) — no escalation triggered
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
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
cd /Users/scorley/code && .venv/bin/pytest tests/test_x_agent.py -v
```

Expected: `ModuleNotFoundError: No module named 'kalshi_trader.agents.x_agent'`

- [ ] **Step 3: Implement x_agent.py**

```python
# kalshi_trader/agents/x_agent.py
from __future__ import annotations
import asyncio
import json
from datetime import datetime
import anthropic
from kalshi_trader import config
from kalshi_trader.models import SignalEstimate
from kalshi_trader.external.x_client import XClient
from kalshi_trader.external.x_strategies import (
    CATEGORY_STRATEGIES,
    STRATEGY_NAME_MAP,
    FALLBACK_STRATEGIES,
    BaseXStrategy,
)


_SEARCH_X_SIGNAL_SCHEMA = {
    "name": "search_x_signal",
    "description": (
        "Search X for social signal on a Kalshi market. "
        "Returns a list of SignalEstimate dicts from all default strategies for the market category."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "ticker":       {"type": "string", "description": "Kalshi market ticker"},
            "category":     {"type": "string", "description": "e.g. weather, politics, sports, mentions"},
            "market_title": {"type": "string", "description": "Full market question title"},
        },
        "required": ["ticker", "category", "market_title"],
    },
}

_OVERRIDE_X_STRATEGIES_SCHEMA = {
    "name": "override_x_strategies",
    "description": (
        "Run specific X search strategies instead of the category defaults. "
        "Use when the market is ambiguous or you want additional signal types."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "ticker":       {"type": "string"},
            "market_title": {"type": "string"},
            "strategies": {
                "type": "array",
                "items": {"type": "string", "enum": ["sentiment", "news", "experts", "buzz"]},
                "description": "Strategy names to run",
            },
        },
        "required": ["ticker", "market_title", "strategies"],
    },
}

X_AGENT_TOOLS = [_SEARCH_X_SIGNAL_SCHEMA, _OVERRIDE_X_STRATEGIES_SCHEMA]


def _estimate_to_dict(e: SignalEstimate) -> dict:
    return {
        "source": e.source,
        "probability": e.probability,
        "uncertainty": e.uncertainty,
        "weight": e.weight,
        "data_issued_at": e.data_issued_at.isoformat(),
        "metadata": e.metadata,
    }


class XAgent:
    def __init__(self) -> None:
        self._client = XClient()
        self._semaphore = asyncio.Semaphore(config.X_MAX_CONCURRENT_SEARCHES)
        self._anthropic: anthropic.AsyncAnthropic | None = (
            anthropic.AsyncAnthropic(api_key=config.ANTHROPIC_API_KEY)
            if config.ANTHROPIC_API_KEY
            else None
        )

    async def _run_one(
        self, strategy: BaseXStrategy, market_title: str
    ) -> list[SignalEstimate]:
        async with self._semaphore:
            result = await strategy.run(market_title, self._client)

        estimates = [strategy.to_signal_estimate(result)]

        if result["uncertainty"] > config.X_GROK_UNCERTAINTY_THRESHOLD:
            second = await self._claude_second_pass(
                summary=result["summary"],
                market_title=market_title,
                source_tag=strategy.source_tag,
                issued_at=estimates[0].data_issued_at,
            )
            if second is not None:
                estimates.append(second)

        return estimates

    async def _claude_second_pass(
        self,
        summary: str,
        market_title: str,
        source_tag: str,
        issued_at: datetime,
    ) -> SignalEstimate | None:
        if not summary or self._anthropic is None:
            return None
        try:
            resp = await self._anthropic.messages.create(
                model=config.SPECIALIST_MODEL,
                max_tokens=256,
                messages=[{
                    "role": "user",
                    "content": (
                        f"Market: {market_title}\n\n"
                        f"X social signal summary: {summary}\n\n"
                        "Based on this X posts summary, estimate the probability this market resolves YES. "
                        'Return only a JSON object: {"probability": float, "uncertainty": float, "reasoning": string}'
                    ),
                }],
            )
            data = json.loads(resp.content[0].text)
            return SignalEstimate(
                source=f"x_claude_{source_tag.split('_')[-1]}",
                probability=float(data["probability"]),
                uncertainty=float(data["uncertainty"]),
                weight=config.X_CLAUDE_SIGNAL_WEIGHT,
                data_issued_at=issued_at,
                metadata={"reasoning": data.get("reasoning", "")},
            )
        except Exception:
            return None

    async def search_x_signal(
        self, ticker: str, category: str, market_title: str
    ) -> list[dict]:
        strategy_classes = CATEGORY_STRATEGIES.get(category, FALLBACK_STRATEGIES)
        strategies = [cls() for cls in strategy_classes]
        all_estimates = await asyncio.gather(*[self._run_one(s, market_title) for s in strategies])
        flat = [e for group in all_estimates for e in group]
        return [_estimate_to_dict(e) for e in flat]

    async def override_x_strategies(
        self, ticker: str, market_title: str, strategies: list[str]
    ) -> list[dict]:
        classes = [STRATEGY_NAME_MAP[n] for n in strategies if n in STRATEGY_NAME_MAP]
        if not classes:
            classes = list(FALLBACK_STRATEGIES)
        strats = [cls() for cls in classes]
        all_estimates = await asyncio.gather(*[self._run_one(s, market_title) for s in strats])
        flat = [e for group in all_estimates for e in group]
        return [_estimate_to_dict(e) for e in flat]

    async def close(self) -> None:
        await self._client.close()
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
cd /Users/scorley/code && .venv/bin/pytest tests/test_x_agent.py -v
```

Expected: 9 passed.

- [ ] **Step 5: Run the full test suite to check for regressions**

```bash
cd /Users/scorley/code && .venv/bin/pytest tests/ -v
```

Expected: all previously-passing tests still pass, total count increased.

- [ ] **Step 6: Commit**

```bash
git add kalshi_trader/agents/x_agent.py tests/test_x_agent.py
git commit -m "feat: add XAgent with search_x_signal and override_x_strategies tools"
```

---

## Task 5: Integration test

**Files:**
- Create: `tests/test_x_integration.py`

- [ ] **Step 1: Write the integration test**

```python
# tests/test_x_integration.py
"""Integration test — requires XAI_API_KEY in environment. Skipped in CI."""
import os
import pytest
from kalshi_trader.external.x_client import XClient


@pytest.mark.asyncio
@pytest.mark.skipif(
    not os.environ.get("XAI_API_KEY"),
    reason="XAI_API_KEY not set — integration test skipped",
)
async def test_live_search_returns_valid_structure():
    client = XClient()
    try:
        result = await client.live_search(
            "Celtics win NBA championship prediction",
            "Will the Celtics win the 2026 NBA championship?",
        )
        assert 0.0 <= result["probability"] <= 1.0
        assert 0.0 <= result["uncertainty"] <= 1.0
        assert isinstance(result["summary"], str)
        assert isinstance(result["key_quotes"], list)
        assert isinstance(result["velocity"], dict)
        assert "1h" in result["velocity"]
        assert isinstance(result["issued_at"], str)
    finally:
        await client.close()
```

- [ ] **Step 2: Verify it is skipped in normal test runs**

```bash
cd /Users/scorley/code && .venv/bin/pytest tests/test_x_integration.py -v
```

Expected: `1 skipped` (unless `XAI_API_KEY` is set in your env).

- [ ] **Step 3: Run full suite one final time**

```bash
cd /Users/scorley/code && .venv/bin/pytest tests/ -v
```

Expected: all tests pass, `test_x_integration.py` skipped.

- [ ] **Step 4: Commit**

```bash
git add tests/test_x_integration.py
git commit -m "test: add X pipeline integration test (skipped without XAI_API_KEY)"
```

---

## Spec Coverage Checklist

| Spec section | Covered by |
|---|---|
| XAI config constants | Task 1 |
| `GrokSearchResult` TypedDict | Task 2 |
| `XClient.live_search()` | Task 2 |
| Error fallback to `_empty_result()` | Task 2 tests |
| `BaseXStrategy` + 4 strategy classes | Task 3 |
| `CATEGORY_STRATEGIES` registry | Task 3 |
| `STRATEGY_NAME_MAP` + `FALLBACK_STRATEGIES` | Task 3 |
| `to_signal_estimate()` mapping | Task 3 tests |
| `XAgent.search_x_signal` tool | Task 4 |
| `XAgent.override_x_strategies` tool | Task 4 |
| Parallel execution with semaphore | Task 4 (`asyncio.gather` + `Semaphore`) |
| Claude second-pass when uncertainty > 0.15 | Task 4 |
| `X_AGENT_TOOLS` schemas | Task 4 |
| Integration test | Task 5 |
