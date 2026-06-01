# Weather Agent Tools Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build Python tools callable via Claude API `tool_use` that give a Claude agent everything it needs to find edge on Kalshi weather markets — NOAA data fetching, market title parsing, probability estimation, and signal combining — with all math in Python, not Claude.

**Architecture:** A `NOAAClient` fetches NWS forecast data, `weather_parser` extracts structured questions from market titles, seven tool functions expose clean typed outputs to a Claude agent via JSON schemas, and `WeatherAgent` wires them together into a `run(markets?) -> list[TradeIdea]` interface. A `BaseAgent` class provides the reusable tool-use loop for all future specialist agents.

**Tech Stack:** Python 3.11+, `aiohttp` (NWS HTTP), `scipy` (Gaussian probability), `anthropic` SDK (tool-use loop), `pytest` + `pytest-asyncio` (tests)

---

## File Map

| File | Action | Responsibility |
|------|--------|----------------|
| `kalshi_trader/models.py` | Modify | Add `SignalEstimate` dataclass |
| `kalshi_trader/external/noaa.py` | Create | NWS HTTP client |
| `kalshi_trader/external/weather_parser.py` | Create | Market title parser + discussion parser |
| `kalshi_trader/agents/base.py` | Create | Reusable Claude tool-use loop |
| `kalshi_trader/agents/weather_agent.py` | Create | 7 tool schemas + handlers + WeatherAgent class |
| `requirements.txt` | Modify | Add `scipy` |
| `tests/test_signal_estimate.py` | Create | SignalEstimate tests |
| `tests/test_noaa.py` | Create | NOAAClient tests (mocked HTTP) |
| `tests/test_weather_parser.py` | Create | Parser unit tests |
| `tests/test_base_agent.py` | Create | BaseAgent tests (mocked Anthropic) |
| `tests/test_weather_agent.py` | Create | WeatherAgent tool handler tests |

---

## Task 1: Add SignalEstimate to models.py

**Files:**
- Modify: `kalshi_trader/models.py`
- Create: `tests/test_signal_estimate.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_signal_estimate.py
from datetime import datetime, timedelta
from kalshi_trader.models import SignalEstimate


def test_staleness_minutes_is_dynamic():
    issued = datetime.utcnow() - timedelta(minutes=30)
    est = SignalEstimate(
        source="noaa_gfs",
        probability=0.65,
        uncertainty=0.08,
        weight=0.85,
        data_issued_at=issued,
        metadata={},
    )
    assert 29 < est.staleness_minutes < 31


def test_staleness_increases_over_time():
    issued = datetime.utcnow() - timedelta(minutes=60)
    est = SignalEstimate(
        source="noaa_gfs",
        probability=0.65,
        uncertainty=0.08,
        weight=0.85,
        data_issued_at=issued,
        metadata={},
    )
    assert est.staleness_minutes > 59
```

- [ ] **Step 2: Run to verify it fails**

```bash
cd /Users/scorley/code && .venv/bin/pytest tests/test_signal_estimate.py -v
```

Expected: `ImportError: cannot import name 'SignalEstimate'`

- [ ] **Step 3: Add SignalEstimate to models.py**

Open `kalshi_trader/models.py` and add after the `RankedSlate` dataclass (at the bottom of the file):

```python
@dataclass
class SignalEstimate:
    source: str             # e.g. "noaa_gfs", "nws_discussion", "polymarket"
    probability: float      # 0.0–1.0
    uncertainty: float      # ± band in probability units, e.g. 0.08 = ±8pp
    weight: float           # source trustworthiness, 0.0–1.0
    data_issued_at: datetime  # from API response, NOT fetch time
    metadata: dict = field(default_factory=dict)

    @property
    def staleness_minutes(self) -> float:
        return (datetime.utcnow() - self.data_issued_at).total_seconds() / 60
```

- [ ] **Step 4: Run to verify it passes**

```bash
cd /Users/scorley/code && .venv/bin/pytest tests/test_signal_estimate.py -v
```

Expected: 2 tests PASS

- [ ] **Step 5: Commit**

```bash
git add kalshi_trader/models.py tests/test_signal_estimate.py
git commit -m "feat: add SignalEstimate data model with dynamic staleness"
```

---

## Task 2: Add scipy + build NOAAClient

**Files:**
- Modify: `requirements.txt`
- Create: `kalshi_trader/external/noaa.py`
- Create: `tests/test_noaa.py`

- [ ] **Step 1: Add scipy to requirements.txt**

Add `scipy>=1.13` to `requirements.txt`, then install:

```bash
cd /Users/scorley/code && .venv/bin/pip install scipy
```

- [ ] **Step 2: Write failing tests**

```python
# tests/test_noaa.py
import pytest
from datetime import date, datetime, timezone
from unittest.mock import AsyncMock, patch, MagicMock
from kalshi_trader.external.noaa import NOAAClient, _parse_wind_mph


def test_parse_wind_mph_single():
    assert _parse_wind_mph("10 mph") == 10.0


def test_parse_wind_mph_range():
    assert _parse_wind_mph("10 to 15 mph") == 12.5


def test_parse_wind_mph_empty():
    assert _parse_wind_mph("") == 0.0


@pytest.mark.asyncio
async def test_get_forecast_returns_structured_data():
    points_response = {
        "properties": {
            "forecast": "https://api.weather.gov/gridpoints/OKX/33,37/forecast",
            "forecastHourly": "https://api.weather.gov/gridpoints/OKX/33,37/forecast/hourly",
            "cwa": "OKX",
        }
    }
    forecast_response = {
        "properties": {
            "generatedAt": "2026-06-01T12:00:00Z",
            "periods": [
                {
                    "isDaytime": True,
                    "startTime": "2026-06-03T06:00:00-05:00",
                    "temperature": 82,
                    "temperatureUnit": "F",
                    "windSpeed": "10 mph",
                    "shortForecast": "Sunny",
                    "probabilityOfPrecipitation": {"value": 20},
                },
                {
                    "isDaytime": False,
                    "startTime": "2026-06-03T18:00:00-05:00",
                    "temperature": 65,
                    "temperatureUnit": "F",
                    "windSpeed": "5 mph",
                    "shortForecast": "Clear",
                    "probabilityOfPrecipitation": {"value": 10},
                },
            ],
        }
    }

    client = NOAAClient()
    with patch.object(client, "_get", new=AsyncMock(side_effect=[points_response, forecast_response])):
        result = await client.get_forecast(40.7128, -74.0060, date(2026, 6, 3))

    assert result["temp_high"] == 82
    assert result["temp_low"] == 65
    assert result["precip_pct"] == 20
    assert result["wind_mph"] == 10.0
    assert result["short_forecast"] == "Sunny"
    assert isinstance(result["generated_at"], datetime)
    await client.close()


@pytest.mark.asyncio
async def test_get_discussion_returns_text_and_time():
    points_response = {
        "properties": {
            "forecast": "https://api.weather.gov/gridpoints/OKX/33,37/forecast",
            "forecastHourly": "https://api.weather.gov/gridpoints/OKX/33,37/forecast/hourly",
            "cwa": "OKX",
        }
    }
    products_response = {
        "@graph": [{"@id": "https://api.weather.gov/products/abc123"}]
    }
    product_response = {
        "productText": "High confidence in the forecast. Temperatures well-defined.",
        "issuanceTime": "2026-06-01T06:00:00Z",
    }
    client = NOAAClient()
    with patch.object(client, "_get", new=AsyncMock(side_effect=[points_response, products_response, product_response])):
        result = await client.get_discussion(40.7128, -74.0060)

    assert "confidence" in result["text"].lower()
    assert isinstance(result["issuance_time"], datetime)
    await client.close()
```

- [ ] **Step 3: Run to verify tests fail**

```bash
cd /Users/scorley/code && .venv/bin/pytest tests/test_noaa.py -v
```

Expected: `ModuleNotFoundError: No module named 'kalshi_trader.external.noaa'`

- [ ] **Step 4: Implement noaa.py**

```python
# kalshi_trader/external/noaa.py
from __future__ import annotations
import re
from datetime import date, datetime
import aiohttp

NWS_BASE = "https://api.weather.gov"
_HEADERS = {"User-Agent": "kalshi-trader/1.0 scorley@peak6.com", "Accept": "application/geo+json"}


def _parse_wind_mph(wind_str: str) -> float:
    nums = re.findall(r"\d+", wind_str)
    if not nums:
        return 0.0
    return sum(float(n) for n in nums) / len(nums)


class NOAAClient:
    def __init__(self) -> None:
        self._session: aiohttp.ClientSession | None = None

    async def _get(self, url: str) -> dict:
        if self._session is None:
            self._session = aiohttp.ClientSession(headers=_HEADERS)
        async with self._session.get(url, timeout=aiohttp.ClientTimeout(total=10)) as resp:
            resp.raise_for_status()
            return await resp.json()

    async def _grid(self, lat: float, lon: float) -> dict:
        data = await self._get(f"{NWS_BASE}/points/{lat:.4f},{lon:.4f}")
        props = data["properties"]
        return {
            "forecast_url": props["forecast"],
            "hourly_url": props["forecastHourly"],
            "wfo": props["cwa"],
        }

    async def get_forecast(self, lat: float, lon: float, target_date: date) -> dict:
        grid = await self._grid(lat, lon)
        data = await self._get(grid["forecast_url"])
        props = data["properties"]
        generated_at = datetime.fromisoformat(props["generatedAt"].replace("Z", "+00:00")).replace(tzinfo=None)

        temp_high: float | None = None
        temp_low: float | None = None
        precip_pct = 0
        wind_mph = 0.0
        short_forecast = ""

        for period in props["periods"]:
            start = datetime.fromisoformat(period["startTime"])
            if start.date() != target_date:
                continue
            precip = (period.get("probabilityOfPrecipitation") or {}).get("value") or 0
            wind = _parse_wind_mph(period.get("windSpeed", ""))
            if period["isDaytime"]:
                temp_high = float(period["temperature"])
                precip_pct = int(precip)
                wind_mph = wind
                short_forecast = period.get("shortForecast", "")
            else:
                temp_low = float(period["temperature"])

        return {
            "temp_high": temp_high,
            "temp_low": temp_low,
            "precip_pct": precip_pct,
            "wind_mph": wind_mph,
            "short_forecast": short_forecast,
            "generated_at": generated_at,
        }

    async def get_discussion(self, lat: float, lon: float) -> dict:
        grid = await self._grid(lat, lon)
        products = await self._get(f"{NWS_BASE}/products?type=AFD&location={grid['wfo']}")
        graph = products.get("@graph", [])
        if not graph:
            return {"text": "", "issuance_time": datetime.utcnow()}
        product = await self._get(graph[0]["@id"])
        issuance_time = datetime.fromisoformat(
            product["issuanceTime"].replace("Z", "+00:00")
        ).replace(tzinfo=None)
        return {"text": product.get("productText", ""), "issuance_time": issuance_time}

    async def close(self) -> None:
        if self._session:
            await self._session.close()
            self._session = None
```

- [ ] **Step 5: Run tests**

```bash
cd /Users/scorley/code && .venv/bin/pytest tests/test_noaa.py -v
```

Expected: 5 tests PASS

- [ ] **Step 6: Commit**

```bash
git add requirements.txt kalshi_trader/external/noaa.py tests/test_noaa.py
git commit -m "feat: add NOAAClient with forecast and discussion endpoints"
```

---

## Task 3: Build weather_parser.py

**Files:**
- Create: `kalshi_trader/external/weather_parser.py`
- Create: `tests/test_weather_parser.py`

- [ ] **Step 1: Write failing tests**

```python
# tests/test_weather_parser.py
from datetime import date
from kalshi_trader.external.weather_parser import parse_title, parse_discussion


def test_parse_title_temp_above():
    result = parse_title("WEATHER-NYC-HIGH-JUNE3", "NYC high temp June 3: above 80°F?")
    assert result is not None
    assert result["city"] == "nyc"
    assert result["metric"] == "temp_high"
    assert result["threshold"] == 80.0
    assert result["operator"] == "above"
    assert result["target_date"] == "2026-06-03"
    assert result["lat"] == pytest.approx(40.7128)


def test_parse_title_rain():
    result = parse_title("WEATHER-NYC-RAIN-JUNE3", "Will it rain in Chicago on June 4?")
    assert result is not None
    assert result["metric"] == "precipitation"
    assert result["city"] == "chicago"


def test_parse_title_below():
    result = parse_title("TICKER", "Will Denver high temp be below 90°F on June 5?")
    assert result is not None
    assert result["operator"] == "below"
    assert result["threshold"] == 90.0


def test_parse_title_unknown_city_returns_none():
    result = parse_title("TICKER", "Will it rain in Timbuktu on June 3?")
    assert result is None


def test_parse_title_no_threshold_returns_none():
    result = parse_title("TICKER", "NYC high temp June 3")
    assert result is None


def test_parse_discussion_high_confidence():
    text = "High confidence in the forecast. Temperatures well-defined for the period."
    result = parse_discussion(text)
    assert result["confidence"] == "high"
    assert isinstance(result["key_points"], list)


def test_parse_discussion_low_confidence():
    text = (
        "Uncertain timing on the cold front. Possible rain Thursday. "
        "Confidence is low with potential for significant uncertainty. "
        "The system may shift north or south. Could bring heavy rain. "
        "Uncertain about wind speeds."
    )
    result = parse_discussion(text)
    assert result["confidence"] == "low"
    assert len(result["key_points"]) > 0


def test_parse_discussion_medium_confidence():
    text = "Some uncertainty remains about exact amounts. Mostly clear skies expected."
    result = parse_discussion(text)
    assert result["confidence"] == "medium"
```

Add `import pytest` at the top of the test file.

- [ ] **Step 2: Run to verify tests fail**

```bash
cd /Users/scorley/code && .venv/bin/pytest tests/test_weather_parser.py -v
```

Expected: `ModuleNotFoundError`

- [ ] **Step 3: Implement weather_parser.py**

```python
# kalshi_trader/external/weather_parser.py
from __future__ import annotations
import re
from datetime import date, datetime

CITY_COORDS: dict[str, tuple[float, float]] = {
    "new york city": (40.7128, -74.0060),
    "new york": (40.7128, -74.0060),
    "nyc": (40.7128, -74.0060),
    "los angeles": (34.0522, -118.2437),
    "chicago": (41.8781, -87.6298),
    "houston": (29.7604, -95.3698),
    "phoenix": (33.4484, -112.0740),
    "philadelphia": (39.9526, -75.1652),
    "san antonio": (29.4241, -98.4936),
    "san diego": (32.7157, -117.1611),
    "dallas": (32.7767, -96.7970),
    "miami": (25.7617, -80.1918),
    "seattle": (47.6062, -122.3321),
    "boston": (42.3601, -71.0589),
    "denver": (39.7392, -104.9903),
    "atlanta": (33.7490, -84.3880),
    "minneapolis": (44.9778, -93.2650),
    "las vegas": (36.1699, -115.1398),
    "portland": (45.5051, -122.6750),
    "nashville": (36.1627, -86.7816),
}

_MONTH_MAP = {
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
    "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12,
    "january": 1, "february": 2, "march": 3, "april": 4, "june": 6,
    "july": 7, "august": 8, "september": 9, "october": 10,
    "november": 11, "december": 12,
}

_UNCERTAINTY_KW = ["uncertain", "unsettled", "possible", "potential", "could", "may ", "confidence"]
_HIGH_CONFIDENCE_KW = ["high confidence", "confidence is high", "well-defined", "clear skies"]


def parse_title(ticker: str, title: str) -> dict | None:
    """Parse Kalshi weather market title → structured question. Returns None on no match."""
    t = title.lower()

    # City — try longer names first to avoid partial matches
    city_name = lat = lon = None
    for name in sorted(CITY_COORDS, key=len, reverse=True):
        if name in t:
            city_name = name
            lat, lon = CITY_COORDS[name]
            break
    if lat is None:
        return None

    # Metric
    if "high temp" in t or "high temperature" in t:
        metric = "temp_high"
    elif "low temp" in t or "low temperature" in t:
        metric = "temp_low"
    elif "rain" in t or "precipitation" in t or "precip" in t:
        metric = "precipitation"
    elif "wind" in t:
        metric = "wind"
    else:
        return None

    # Operator
    if any(kw in t for kw in ["above", "exceed", "or more", "at least", "over"]):
        operator = "above"
    elif any(kw in t for kw in ["below", "under", "less than"]):
        operator = "below"
    else:
        operator = "above"

    # Threshold (temperature °F or wind mph)
    threshold = None
    m = re.search(r"(\d+)\s*°?\s*f\b", t)
    if m:
        threshold = float(m.group(1))
    elif metric == "wind":
        m = re.search(r"(\d+)\s*mph", t)
        if m:
            threshold = float(m.group(1))

    if threshold is None and metric not in ("precipitation",):
        return None

    # Date
    target_date = None
    m = re.search(
        r"(jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|jun(?:e)?|"
        r"jul(?:y)?|aug(?:ust)?|sep(?:tember)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?)"
        r"[\s.]*(\d{1,2})",
        t,
    )
    if m:
        month = _MONTH_MAP.get(m.group(1)[:3])
        day = int(m.group(2))
        year = datetime.utcnow().year
        if month:
            target_date = date(year, month, day)

    if target_date is None:
        return None

    return {
        "city": city_name,
        "lat": lat,
        "lon": lon,
        "metric": metric,
        "threshold": threshold,
        "operator": operator,
        "target_date": target_date.isoformat(),
    }


def parse_discussion(text: str) -> dict:
    """Parse NWS AFD text → {confidence: str, key_points: list[str]}."""
    tl = text.lower()
    sentences = [s.strip() for s in re.split(r"[.!?\n]", text) if s.strip()]
    key_points = [s for s in sentences if any(kw in s.lower() for kw in _UNCERTAINTY_KW)][:5]

    if any(kw in tl for kw in _HIGH_CONFIDENCE_KW):
        confidence = "high"
    elif sum(tl.count(kw) for kw in _UNCERTAINTY_KW) > 5:
        confidence = "low"
    else:
        confidence = "medium"

    return {"confidence": confidence, "key_points": key_points}
```

- [ ] **Step 4: Run tests**

```bash
cd /Users/scorley/code && .venv/bin/pytest tests/test_weather_parser.py -v
```

Expected: 8 tests PASS

- [ ] **Step 5: Commit**

```bash
git add kalshi_trader/external/weather_parser.py tests/test_weather_parser.py
git commit -m "feat: add weather_parser for market title and NWS discussion parsing"
```

---

## Task 4: Build BaseAgent (tool-use loop)

**Files:**
- Create: `kalshi_trader/agents/base.py`
- Create: `tests/test_base_agent.py`

- [ ] **Step 1: Write failing tests**

```python
# tests/test_base_agent.py
import json
import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from kalshi_trader.agents.base import BaseAgent


def _make_end_turn_response(text: str):
    block = MagicMock()
    block.type = "text"
    block.text = text
    resp = MagicMock()
    resp.stop_reason = "end_turn"
    resp.content = [block]
    return resp


def _make_tool_use_response(tool_name: str, tool_input: dict, tool_use_id: str = "tu_1"):
    block = MagicMock()
    block.type = "tool_use"
    block.name = tool_name
    block.input = tool_input
    block.id = tool_use_id
    resp = MagicMock()
    resp.stop_reason = "tool_use"
    resp.content = [block]
    return resp


@pytest.mark.asyncio
async def test_base_agent_end_turn_returns_text():
    agent = BaseAgent(
        tools=[],
        handlers={},
        system_prompt="You are a test agent.",
    )
    mock_create = AsyncMock(return_value=_make_end_turn_response("hello world"))
    with patch.object(agent._client.messages, "create", mock_create):
        result = await agent.run("say hello")
    assert result == "hello world"


@pytest.mark.asyncio
async def test_base_agent_dispatches_tool_call():
    called_with = {}

    async def my_tool(x: int):
        called_with["x"] = x
        return {"result": x * 2}

    agent = BaseAgent(
        tools=[{"name": "my_tool", "description": "...", "input_schema": {}}],
        handlers={"my_tool": my_tool},
        system_prompt="Use tools.",
    )
    tool_resp = _make_tool_use_response("my_tool", {"x": 5})
    end_resp = _make_end_turn_response("done")

    mock_create = AsyncMock(side_effect=[tool_resp, end_resp])
    with patch.object(agent._client.messages, "create", mock_create):
        result = await agent.run("call my_tool")

    assert called_with["x"] == 5
    assert result == "done"


@pytest.mark.asyncio
async def test_base_agent_unknown_tool_returns_error():
    agent = BaseAgent(tools=[], handlers={}, system_prompt="test")
    tool_resp = _make_tool_use_response("nonexistent", {})
    end_resp = _make_end_turn_response("ok")

    mock_create = AsyncMock(side_effect=[tool_resp, end_resp])
    with patch.object(agent._client.messages, "create", mock_create):
        await agent.run("go")

    # Verify the tool_result message sent back contains an error
    second_call_messages = mock_create.call_args_list[1][1]["messages"]
    last_message = second_call_messages[-1]
    assert last_message["role"] == "user"
    content = last_message["content"][0]
    result_data = json.loads(content["content"])
    assert "error" in result_data
```

- [ ] **Step 2: Run to verify tests fail**

```bash
cd /Users/scorley/code && .venv/bin/pytest tests/test_base_agent.py -v
```

Expected: `ModuleNotFoundError: No module named 'kalshi_trader.agents.base'`

- [ ] **Step 3: Implement base.py**

```python
# kalshi_trader/agents/base.py
from __future__ import annotations
import json
from typing import Any, Callable, Coroutine
import anthropic


class BaseAgent:
    def __init__(
        self,
        tools: list[dict],
        handlers: dict[str, Callable[..., Coroutine[Any, Any, Any]]],
        system_prompt: str,
        model: str = "claude-sonnet-4-6",
        max_iterations: int = 30,
    ) -> None:
        self._client = anthropic.AsyncAnthropic()
        self._tools = tools
        self._handlers = handlers
        self._system = system_prompt
        self._model = model
        self._max_iterations = max_iterations

    async def run(self, user_message: str) -> str:
        messages: list[dict] = [{"role": "user", "content": user_message}]

        for _ in range(self._max_iterations):
            resp = await self._client.messages.create(
                model=self._model,
                max_tokens=4096,
                system=self._system,
                tools=self._tools,
                messages=messages,
            )
            messages.append({"role": "assistant", "content": resp.content})

            if resp.stop_reason == "end_turn":
                for block in resp.content:
                    if hasattr(block, "text"):
                        return block.text
                return ""

            if resp.stop_reason != "tool_use":
                break

            tool_results = []
            for block in resp.content:
                if block.type != "tool_use":
                    continue
                handler = self._handlers.get(block.name)
                if handler is None:
                    payload = {"error": f"Unknown tool: {block.name}"}
                else:
                    try:
                        payload = await handler(**block.input)
                    except Exception as exc:
                        payload = {"error": str(exc)}
                tool_results.append({
                    "type": "tool_result",
                    "tool_use_id": block.id,
                    "content": json.dumps(payload, default=str),
                })
            messages.append({"role": "user", "content": tool_results})

        return ""
```

- [ ] **Step 4: Run tests**

```bash
cd /Users/scorley/code && .venv/bin/pytest tests/test_base_agent.py -v
```

Expected: 3 tests PASS

- [ ] **Step 5: Commit**

```bash
git add kalshi_trader/agents/base.py tests/test_base_agent.py
git commit -m "feat: add BaseAgent with reusable Claude tool-use loop"
```

---

## Task 5: Build WeatherAgent (tools + agent)

**Files:**
- Create: `kalshi_trader/agents/weather_agent.py`
- Create: `tests/test_weather_agent.py`

- [ ] **Step 1: Write failing tests for the tool handlers**

```python
# tests/test_weather_agent.py
import json
import pytest
from datetime import date, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch
from kalshi_trader.agents.weather_agent import (
    _parse_weather_market,
    _estimate_probability,
    _combine_signals,
    _calculate_edge,
)


@pytest.mark.asyncio
async def test_parse_weather_market_delegates_to_parser():
    result = await _parse_weather_market(
        ticker="WEATHER-NYC-HIGH-JUNE3",
        title="NYC high temp June 3: above 80°F?",
    )
    assert result is not None
    assert result["metric"] == "temp_high"
    assert result["threshold"] == 80.0


@pytest.mark.asyncio
async def test_parse_weather_market_returns_none_for_unparseable():
    result = await _parse_weather_market(ticker="X", title="Some other market")
    assert result is None


@pytest.mark.asyncio
async def test_estimate_probability_temp_above():
    # mean=(90+75)/2=82.5, std=(90-75)/4=3.75 → P(X>80) ≈ 0.748
    forecast = {"temp_high": 90.0, "temp_low": 75.0, "precip_pct": 10, "data_age_minutes": 30}
    result = await _estimate_probability(
        metric="temp_high", threshold=80.0, operator="above", forecast=forecast
    )
    assert "probability" in result
    assert 0.6 < result["probability"] < 0.9
    assert result["source"] == "noaa_gfs"
    assert "data_issued_at" in result


@pytest.mark.asyncio
async def test_estimate_probability_precip():
    forecast = {"temp_high": 75.0, "temp_low": 60.0, "precip_pct": 70, "data_age_minutes": 60}
    result = await _estimate_probability(
        metric="precipitation", threshold=0, operator="above", forecast=forecast
    )
    assert result["probability"] == pytest.approx(0.70)
    assert result["uncertainty"] == pytest.approx(0.05)


@pytest.mark.asyncio
async def test_combine_signals_weighted_average():
    now = datetime.utcnow()
    estimates = [
        {
            "source": "noaa_gfs",
            "probability": 0.70,
            "uncertainty": 0.08,
            "weight": 0.85,
            "data_issued_at": (now - timedelta(minutes=30)).isoformat(),
        },
        {
            "source": "noaa_gfs_2",
            "probability": 0.60,
            "uncertainty": 0.10,
            "weight": 0.70,
            "data_issued_at": (now - timedelta(minutes=120)).isoformat(),
        },
    ]
    result = await _combine_signals(estimates=estimates)
    assert 0.60 < result["combined_probability"] < 0.70
    assert result["n_sources"] == 2
    assert "uncertainty" in result


@pytest.mark.asyncio
async def test_combine_signals_single():
    now = datetime.utcnow()
    estimates = [{
        "source": "noaa_gfs",
        "probability": 0.65,
        "uncertainty": 0.08,
        "weight": 0.85,
        "data_issued_at": (now - timedelta(minutes=10)).isoformat(),
    }]
    result = await _combine_signals(estimates=estimates)
    assert result["combined_probability"] == pytest.approx(0.65, abs=0.01)


@pytest.mark.asyncio
async def test_calculate_edge_worth_trading():
    result = await _calculate_edge(combined_probability=0.65, market_price_cents=40.0)
    assert result["edge_cents"] == pytest.approx(25.0)
    assert result["worth_trading"] is True


@pytest.mark.asyncio
async def test_calculate_edge_not_worth_trading():
    result = await _calculate_edge(combined_probability=0.42, market_price_cents=40.0)
    assert result["fee_adjusted_edge"] < 5.0
    assert result["worth_trading"] is False
```

- [ ] **Step 2: Run to verify they fail**

```bash
cd /Users/scorley/code && .venv/bin/pytest tests/test_weather_agent.py -v
```

Expected: `ModuleNotFoundError`

- [ ] **Step 3: Implement weather_agent.py**

```python
# kalshi_trader/agents/weather_agent.py
from __future__ import annotations
import json
import math
import re
from datetime import datetime, timedelta
from typing import Any
import scipy.stats
from kalshi_trader.models import Market, TradeIdea, Side, OrderAction, SignalEstimate
from kalshi_trader.external.noaa import NOAAClient
from kalshi_trader.external.weather_parser import parse_title, parse_discussion
from kalshi_trader.agents.base import BaseAgent

# ---------------------------------------------------------------------------
# Module-level tool handlers (no instance state needed)
# ---------------------------------------------------------------------------

async def _parse_weather_market(ticker: str, title: str) -> dict | None:
    return parse_title(ticker, title)


async def _estimate_probability(
    metric: str,
    threshold: float,
    operator: str,
    forecast: dict,
) -> dict:
    data_age = forecast.get("data_age_minutes", 0)
    issued_at = (datetime.utcnow() - timedelta(minutes=data_age)).isoformat()

    if metric in ("temp_high", "temp_low"):
        high = forecast.get("temp_high") or 85.0
        low = forecast.get("temp_low") or 65.0
        mean = (high + low) / 2.0
        std = max((high - low) / 4.0, 1.0)
        dist = scipy.stats.norm(mean, std)
        prob = float(dist.sf(threshold) if operator == "above" else dist.cdf(threshold))
        uncertainty = 0.08
    elif metric == "precipitation":
        prob = (forecast.get("precip_pct") or 0) / 100.0
        uncertainty = 0.05
    else:
        return {"error": f"Unsupported metric: {metric}"}

    return {
        "source": "noaa_gfs",
        "probability": round(min(max(prob, 0.01), 0.99), 4),
        "uncertainty": uncertainty,
        "weight": 0.85,
        "data_issued_at": issued_at,
        "metadata": {"metric": metric, "threshold": threshold, "operator": operator},
    }


async def _combine_signals(estimates: list[dict]) -> dict:
    if not estimates:
        return {"error": "No estimates provided"}

    total_w = 0.0
    w_prob = 0.0
    w_unc = 0.0
    max_staleness = 0.0

    for e in estimates:
        issued = datetime.fromisoformat(e["data_issued_at"])
        staleness = (datetime.utcnow() - issued).total_seconds() / 60
        eff_w = e["weight"] * math.exp(-staleness / 360.0)
        total_w += eff_w
        w_prob += eff_w * e["probability"]
        w_unc += eff_w * e["uncertainty"]
        max_staleness = max(max_staleness, staleness)

    if total_w == 0:
        return {"error": "All estimates have zero effective weight"}

    combined_prob = w_prob / total_w
    combined_unc = w_unc / total_w

    if len(estimates) > 1:
        probs = [e["probability"] for e in estimates]
        spread = max(probs) - min(probs)
        if spread > 0.10:
            combined_unc += spread * 0.5

    return {
        "combined_probability": round(combined_prob, 4),
        "uncertainty": round(combined_unc, 4),
        "staleness_minutes": round(max_staleness, 1),
        "n_sources": len(estimates),
    }


async def _calculate_edge(combined_probability: float, market_price_cents: float) -> dict:
    edge = combined_probability * 100 - market_price_cents
    c = market_price_cents / 100.0
    fee = 0.07 * c * (1.0 - c) * 100
    adj = edge - fee
    return {
        "edge_cents": round(edge, 2),
        "fee_adjusted_edge": round(adj, 2),
        "worth_trading": adj > 5.0,
    }


# ---------------------------------------------------------------------------
# Tool schemas
# ---------------------------------------------------------------------------

_SCHEMAS: list[dict] = [
    {
        "name": "list_weather_markets",
        "description": "List all open Kalshi weather markets with ticker, title, yes_price, volume_24h, and hours_to_close.",
        "input_schema": {"type": "object", "properties": {}, "required": []},
    },
    {
        "name": "parse_weather_market",
        "description": "Parse a Kalshi weather market title into a structured question (city, lat, lon, metric, threshold, operator, target_date). Returns null if unparseable — skip that market.",
        "input_schema": {
            "type": "object",
            "properties": {
                "ticker": {"type": "string"},
                "title": {"type": "string"},
            },
            "required": ["ticker", "title"],
        },
    },
    {
        "name": "get_noaa_forecast",
        "description": "Fetch NWS gridpoint forecast for a lat/lon and date. Returns temp_high, temp_low, precip_pct, wind_mph, short_forecast, data_age_minutes.",
        "input_schema": {
            "type": "object",
            "properties": {
                "lat": {"type": "number"},
                "lon": {"type": "number"},
                "date": {"type": "string", "description": "YYYY-MM-DD"},
            },
            "required": ["lat", "lon", "date"],
        },
    },
    {
        "name": "estimate_probability",
        "description": "Estimate the probability a weather condition meets the threshold using the NOAA forecast. Pass the full forecast dict returned by get_noaa_forecast.",
        "input_schema": {
            "type": "object",
            "properties": {
                "metric": {"type": "string", "enum": ["temp_high", "temp_low", "precipitation", "wind"]},
                "threshold": {"type": "number"},
                "operator": {"type": "string", "enum": ["above", "below"]},
                "forecast": {"type": "object"},
            },
            "required": ["metric", "threshold", "operator", "forecast"],
        },
    },
    {
        "name": "get_nws_discussion",
        "description": "Fetch and parse the NWS Area Forecast Discussion for a location. Returns confidence level ('high'/'medium'/'low') and key uncertainty sentences. Use for qualitative reasoning; do NOT pass to combine_signals.",
        "input_schema": {
            "type": "object",
            "properties": {
                "lat": {"type": "number"},
                "lon": {"type": "number"},
            },
            "required": ["lat", "lon"],
        },
    },
    {
        "name": "combine_signals",
        "description": "Combine a list of SignalEstimate dicts into one probability using staleness-discounted weighted averaging. Each estimate must have: source, probability, uncertainty, weight, data_issued_at.",
        "input_schema": {
            "type": "object",
            "properties": {
                "estimates": {"type": "array", "items": {"type": "object"}},
            },
            "required": ["estimates"],
        },
    },
    {
        "name": "calculate_edge",
        "description": "Calculate fee-adjusted edge between estimated probability and the current Kalshi YES ask price. Returns edge_cents, fee_adjusted_edge, and worth_trading (true if fee_adjusted_edge > 5 cents).",
        "input_schema": {
            "type": "object",
            "properties": {
                "combined_probability": {"type": "number"},
                "market_price_cents": {"type": "number"},
            },
            "required": ["combined_probability", "market_price_cents"],
        },
    },
]

_SYSTEM_PROMPT = """\
You are a weather market specialist for a Kalshi prediction market trading system.

Your job: identify Kalshi weather markets where NOAA forecast data implies a meaningfully different probability than the current market price.

## Workflow
1. If markets are not provided in the user message, call list_weather_markets.
2. For each market with volume_24h > 1000 and hours_to_close > 4:
   a. Call parse_weather_market. If it returns null, skip.
   b. Call get_noaa_forecast with the parsed lat, lon, and target_date.
   c. Call estimate_probability using the metric, threshold, operator, and the full forecast dict.
   d. Optionally call get_nws_discussion if the forecast shows precip_pct between 30-70% (high uncertainty zone) — use it for qualitative context in your reasoning only.
   e. Call combine_signals with your estimate(s) from step c.
   f. Call calculate_edge with the combined_probability and the market's yes_price.
3. Include only markets where worth_trading is true.

## Output
End your final response with exactly one fenced JSON block:
```json
[
  {
    "ticker": "WEATHER-NYC-RAIN-JUNE3",
    "side": "yes",
    "confidence": 0.73,
    "market_price": 18.0,
    "reasoning": "NOAA shows 73% precip vs 18 cent market. NWS discussion notes high confidence.",
    "signal_sources": ["noaa_gfs"]
  }
]
```
If no markets are worth trading, output: ```json\n[]\n```
"""


class WeatherAgent:
    def __init__(self, client: Any, scanner: Any) -> None:
        self._kalshi = client
        self._scanner = scanner
        self._noaa = NOAAClient()
        self._agent = BaseAgent(
            tools=_SCHEMAS,
            handlers={
                "list_weather_markets": self._list_weather_markets,
                "parse_weather_market": _parse_weather_market,
                "get_noaa_forecast": self._get_noaa_forecast,
                "estimate_probability": _estimate_probability,
                "get_nws_discussion": self._get_nws_discussion,
                "combine_signals": _combine_signals,
                "calculate_edge": _calculate_edge,
            },
            system_prompt=_SYSTEM_PROMPT,
        )

    async def run(self, markets: list[Market] | None = None) -> list[TradeIdea]:
        if markets is not None:
            now = datetime.utcnow()
            market_list = [
                {
                    "ticker": m.ticker,
                    "title": m.title,
                    "yes_price": m.yes_ask,
                    "volume_24h": m.volume_24h,
                    "hours_to_close": round(max(0.0, (m.close_time - now).total_seconds() / 3600), 1),
                }
                for m in markets
            ]
            prompt = f"Analyze these weather markets:\n{json.dumps(market_list, indent=2)}"
        else:
            prompt = "Find weather markets with edge."

        raw = await self._agent.run(prompt)
        return self._parse_ideas(raw)

    async def _list_weather_markets(self) -> list[dict]:
        all_markets = await self._scanner.get_open_markets()
        now = datetime.utcnow()
        keywords = ["weather", "temperature", "rain", "precip", "temp", "wind"]
        filtered = [
            m for m in all_markets
            if any(kw in (m.category + " " + m.title).lower() for kw in keywords)
        ]
        return [
            {
                "ticker": m.ticker,
                "title": m.title,
                "yes_price": m.yes_ask,
                "volume_24h": m.volume_24h,
                "hours_to_close": round(max(0.0, (m.close_time - now).total_seconds() / 3600), 1),
            }
            for m in filtered
        ]

    async def _get_noaa_forecast(self, lat: float, lon: float, date: str) -> dict:
        from datetime import date as date_type
        target = date_type.fromisoformat(date)
        result = await self._noaa.get_forecast(lat, lon, target)
        age = (datetime.utcnow() - result["generated_at"]).total_seconds() / 60
        return {
            "temp_high": result["temp_high"],
            "temp_low": result["temp_low"],
            "precip_pct": result["precip_pct"],
            "wind_mph": result["wind_mph"],
            "short_forecast": result["short_forecast"],
            "data_age_minutes": round(age, 1),
        }

    async def _get_nws_discussion(self, lat: float, lon: float) -> dict:
        result = await self._noaa.get_discussion(lat, lon)
        parsed = parse_discussion(result["text"])
        return {
            "confidence": parsed["confidence"],
            "key_points": parsed["key_points"],
            "issued_at": result["issuance_time"].isoformat(),
        }

    def _parse_ideas(self, raw: str) -> list[TradeIdea]:
        match = re.search(r"```json\s*(.*?)\s*```", raw, re.DOTALL)
        if not match:
            return []
        try:
            data = json.loads(match.group(1))
        except json.JSONDecodeError:
            return []
        return [
            TradeIdea(
                agent_id="weather",
                ticker=item["ticker"],
                side=Side(item.get("side", "yes")),
                action=OrderAction.BUY,
                confidence=float(item["confidence"]),
                market_price=float(item["market_price"]),
                reasoning=item.get("reasoning", ""),
                signal_sources=item.get("signal_sources", []),
            )
            for item in data
        ]

    async def close(self) -> None:
        await self._noaa.close()
```

- [ ] **Step 4: Run tool handler tests**

```bash
cd /Users/scorley/code && .venv/bin/pytest tests/test_weather_agent.py -v
```

Expected: 7 tests PASS

- [ ] **Step 5: Run the full test suite**

```bash
cd /Users/scorley/code && .venv/bin/pytest -v
```

Expected: All tests PASS (no regressions)

- [ ] **Step 6: Commit**

```bash
git add kalshi_trader/agents/weather_agent.py tests/test_weather_agent.py
git commit -m "feat: add WeatherAgent with 7 NOAA tools for weather market edge detection"
```

---

## Task 6: Final wiring check

- [ ] **Step 1: Verify scipy is in requirements.txt**

```bash
grep scipy /Users/scorley/code/requirements.txt
```

Expected: `scipy>=1.13`

If missing: add it and commit:
```bash
git add requirements.txt && git commit -m "chore: add scipy to requirements"
```

- [ ] **Step 2: Verify all imports resolve cleanly**

```bash
cd /Users/scorley/code && .venv/bin/python -c "
from kalshi_trader.models import SignalEstimate
from kalshi_trader.external.noaa import NOAAClient
from kalshi_trader.external.weather_parser import parse_title, parse_discussion
from kalshi_trader.agents.base import BaseAgent
from kalshi_trader.agents.weather_agent import WeatherAgent
print('All imports OK')
"
```

Expected: `All imports OK`

- [ ] **Step 3: Run full test suite one final time**

```bash
cd /Users/scorley/code && .venv/bin/pytest -v --tb=short
```

Expected: All tests PASS

- [ ] **Step 4: Final commit if any loose files**

```bash
git status
```

Only commit if there are unstaged changes. Otherwise skip.
