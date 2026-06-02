# Pipeline Agent System Prompts — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Implement four pipeline agents (weather, polymarket_price, polymarket_whale, x) each driven by a `.md` system prompt that instructs Claude to call Python tools, returning `list[SignalEstimate]` JSON.

**Architecture:** Signal construction is cleanly separated from data collection. Each agent has: (1) a `.md` system prompt in `agents/prompts/`, (2) a Python class using `BaseAgent` with tool schemas + handlers, (3) a thin CLI wrapper in `pipelines/`. A new `signals/` module contains the Python converter functions that transform raw API data into `SignalEstimate` objects — this is where you edit to change how data becomes a signal.

**Tech Stack:** Python 3.11+, `anthropic` SDK, `scipy`, `aiohttp`, `truststore`, existing `NOAAClient`, `PolymarketClient`, `XClient`

---

## File Map

**Create:**
- `kalshi_trader/signals/__init__.py`
- `kalshi_trader/signals/weather.py` — `build_weather_signal()`
- `kalshi_trader/signals/polymarket.py` — `build_price_signal()`, `build_whale_signal()`
- `kalshi_trader/signals/x.py` — `build_x_signal()`
- `kalshi_trader/agents/parsing.py` — `parse_signal_estimates()`, `estimate_to_dict()`
- `kalshi_trader/agents/prompts/weather.md`
- `kalshi_trader/agents/prompts/polymarket_price.md`
- `kalshi_trader/agents/prompts/polymarket_whale.md`
- `kalshi_trader/agents/prompts/x.md`
- `kalshi_trader/agents/polymarket_price_agent.py`
- `kalshi_trader/agents/polymarket_whale_agent.py`
- `kalshi_trader/pipelines/__init__.py`
- `kalshi_trader/pipelines/weather.py`
- `kalshi_trader/pipelines/polymarket_price.py`
- `kalshi_trader/pipelines/polymarket_whale.py`
- `kalshi_trader/pipelines/x.py`
- `tests/test_signals_weather.py`
- `tests/test_signals_polymarket.py`
- `tests/test_signals_x.py`
- `tests/test_parsing.py`
- `tests/test_polymarket_price_agent.py`
- `tests/test_polymarket_whale_agent.py`
- `tests/test_pipelines.py`

**Modify:**
- `kalshi_trader/external/polymarket.py` — add `match_market_with_score()`
- `kalshi_trader/agents/weather_agent.py` — load prompt from `.md`, new tools, `run(ticker, title)`
- `kalshi_trader/agents/x_agent.py` — load prompt from `.md`, add `build_x_signal` tool, `BaseAgent`-driven
- `tests/test_weather_agent.py` — update for new interface
- `tests/test_x_agent.py` — update for new interface

---

## Task 1: `signals/` converter layer + `agents/parsing.py`

**Files:**
- Create: `kalshi_trader/signals/__init__.py`
- Create: `kalshi_trader/signals/weather.py`
- Create: `kalshi_trader/signals/polymarket.py`
- Create: `kalshi_trader/signals/x.py`
- Create: `kalshi_trader/agents/parsing.py`
- Create: `tests/test_signals_weather.py`
- Create: `tests/test_signals_polymarket.py`
- Create: `tests/test_signals_x.py`
- Create: `tests/test_parsing.py`

- [ ] **Step 1: Write failing tests for `signals/weather.py`**

```python
# tests/test_signals_weather.py
from datetime import datetime, timezone, timedelta
from kalshi_trader.signals.weather import build_weather_signal


def test_build_weather_signal_precipitation_basic():
    forecast = {"precip_pct": 73, "data_age_minutes": 30}
    result = build_weather_signal("WEATHER-NYC-RAIN", "precipitation", 0.0, "above", forecast)
    assert result.source == "noaa_gfs"
    assert result.probability == 0.73
    assert result.uncertainty == 0.05
    assert result.weight == 0.85
    assert result.metadata["ticker"] == "WEATHER-NYC-RAIN"
    assert result.metadata["data_quality"] == "fresh"
    assert result.metadata["forecast_model"] == "noaa_gfs"
    assert "narrative" in result.metadata


def test_build_weather_signal_precipitation_stale():
    forecast = {"precip_pct": 40, "data_age_minutes": 120}
    result = build_weather_signal("WEATHER-NYC-RAIN", "precipitation", 0.0, "above", forecast)
    assert result.metadata["data_quality"] == "stale"


def test_build_weather_signal_precipitation_unavailable():
    forecast = {"precip_pct": 40, "data_age_minutes": 400}
    result = build_weather_signal("WEATHER-NYC-RAIN", "precipitation", 0.0, "above", forecast)
    assert result.metadata["data_quality"] == "unavailable"


def test_build_weather_signal_temp_above():
    forecast = {"temp_high": 90, "temp_low": 70, "data_age_minutes": 20}
    result = build_weather_signal("WEATHER-NYC-TEMP", "temp_high", 85.0, "above", forecast)
    assert result.source == "noaa_gfs"
    assert result.probability > 0.5  # temp_high=90 is clearly above 85


def test_build_weather_signal_temp_below():
    forecast = {"temp_high": 60, "temp_low": 50, "data_age_minutes": 20}
    result = build_weather_signal("WEATHER-NYC-TEMP", "temp_low", 75.0, "below", forecast)
    assert result.probability > 0.5  # mean=55 is well below 75


def test_build_weather_signal_with_discussion():
    forecast = {"precip_pct": 50, "data_age_minutes": 10}
    discussion = {"confidence": "high", "key_points": ["Storm timing uncertain."]}
    result = build_weather_signal("WEATHER-NYC-RAIN", "precipitation", 0.0, "above", forecast, discussion)
    assert result.metadata["nws_confidence"] == "high"
    assert result.metadata["key_uncertainty"] == "Storm timing uncertain."


def test_build_weather_signal_discussion_no_key_points():
    forecast = {"precip_pct": 50, "data_age_minutes": 10}
    discussion = {"confidence": "medium", "key_points": []}
    result = build_weather_signal("WEATHER-NYC-RAIN", "precipitation", 0.0, "above", forecast, discussion)
    assert result.metadata["nws_confidence"] == "medium"
    assert "key_uncertainty" not in result.metadata


def test_build_weather_signal_probability_clamped():
    forecast = {"precip_pct": 0, "data_age_minutes": 10}
    result = build_weather_signal("WEATHER-NYC-RAIN", "precipitation", 0.0, "above", forecast)
    assert result.probability >= 0.01


def test_build_weather_signal_issued_at_reflects_age():
    forecast = {"precip_pct": 50, "data_age_minutes": 60}
    before = datetime.now(tz=timezone.utc)
    result = build_weather_signal("WEATHER-NYC-RAIN", "precipitation", 0.0, "above", forecast)
    after = datetime.now(tz=timezone.utc)
    expected_issued = before - timedelta(minutes=60)
    assert abs((result.data_issued_at - expected_issued).total_seconds()) < 5
```

- [ ] **Step 2: Run to verify failure**

```
.venv/bin/pytest tests/test_signals_weather.py -v
```
Expected: `ModuleNotFoundError: No module named 'kalshi_trader.signals'`

- [ ] **Step 3: Write failing tests for `signals/polymarket.py`**

```python
# tests/test_signals_polymarket.py
from datetime import datetime, timezone
from kalshi_trader.signals.polymarket import build_price_signal, build_whale_signal


def test_build_price_signal_basic():
    result = build_price_signal("NBA-CELTICS", 0.45, 18.0, 0.91)
    assert result.source == "polymarket_price"
    assert result.probability == 0.45
    assert result.uncertainty == 0.03
    assert result.weight == 0.75
    assert result.metadata["ticker"] == "NBA-CELTICS"
    assert result.metadata["gap_cents"] == 18.0
    assert result.metadata["match_score"] == 0.91
    assert result.metadata["data_quality"] == "fresh"
    assert "narrative" in result.metadata


def test_build_price_signal_negative_gap():
    result = build_price_signal("NBA-CELTICS", 0.22, -8.0, 0.80)
    assert result.probability == 0.22
    assert result.metadata["gap_cents"] == -8.0


def test_build_whale_signal_yes_entries():
    entries = [
        {"wallet_address": "0xabc", "side": "YES", "entry_price": 0.62, "size_usd": 2000.0,
         "timestamp": datetime.now(tz=timezone.utc)},
        {"wallet_address": "0xdef", "side": "YES", "entry_price": 0.58, "size_usd": 1000.0,
         "timestamp": datetime.now(tz=timezone.utc)},
    ]
    result = build_whale_signal("NBA-CELTICS", entries)
    assert result is not None
    assert result.source == "polymarket_whale"
    assert result.weight == 0.60
    assert result.uncertainty == 0.10  # 2 whales → default uncertainty
    assert result.metadata["whale_count"] == 2
    assert result.metadata["ticker"] == "NBA-CELTICS"
    # Weighted avg: (0.62*2000 + 0.58*1000) / 3000 ≈ 0.607
    assert abs(result.probability - 0.607) < 0.01


def test_build_whale_signal_no_entries():
    result = build_whale_signal("NBA-CELTICS", [])
    assert result is None


def test_build_whale_signal_single_whale_higher_uncertainty():
    entries = [{"wallet_address": "0xabc", "side": "YES", "entry_price": 0.70,
                "size_usd": 5000.0, "timestamp": datetime.now(tz=timezone.utc)}]
    result = build_whale_signal("NBA-CELTICS", entries)
    assert result is not None
    assert result.uncertainty == 0.15  # single whale → higher uncertainty


def test_build_whale_signal_no_side_entry():
    # NO entry at 0.60 → implied YES probability = 1 - 0.60 = 0.40
    entries = [{"wallet_address": "0xabc", "side": "NO", "entry_price": 0.60,
                "size_usd": 3000.0, "timestamp": datetime.now(tz=timezone.utc)}]
    result = build_whale_signal("NBA-CELTICS", entries)
    assert result is not None
    assert abs(result.probability - 0.40) < 0.01
```

- [ ] **Step 4: Write failing tests for `signals/x.py`**

```python
# tests/test_signals_x.py
from datetime import datetime, timezone
from kalshi_trader.signals.x import build_x_signal


def test_build_x_signal_basic():
    raw = {
        "source": "x_sentiment",
        "probability": 0.62,
        "uncertainty": 0.14,
        "weight": 0.55,
        "data_issued_at": datetime.now(tz=timezone.utc).isoformat(),
    }
    result = build_x_signal(
        ticker="NBA-CELTICS",
        raw_signal=raw,
        narrative="Bullish sentiment on Celtics.",
        sentiment_direction="bullish",
        sentiment_reasoning="3 analysts posted YES takes.",
        strategies_used="sentiment,buzz",
        post_count=17,
    )
    assert result.source == "x_sentiment"
    assert result.probability == 0.62
    assert result.weight == 0.55
    assert result.metadata["ticker"] == "NBA-CELTICS"
    assert result.metadata["sentiment_direction"] == "bullish"
    assert result.metadata["sentiment_reasoning"] == "3 analysts posted YES takes."
    assert result.metadata["post_count"] == 17
    assert result.metadata["strategies_used"] == "sentiment,buzz"
```

- [ ] **Step 5: Write failing tests for `agents/parsing.py`**

```python
# tests/test_parsing.py
from datetime import datetime, timezone
from kalshi_trader.agents.parsing import parse_signal_estimates, estimate_to_dict
from kalshi_trader.models import SignalEstimate


def test_parse_signal_estimates_valid():
    raw = '''Some text before.
```json
[
  {
    "source": "noaa_gfs",
    "probability": 0.73,
    "uncertainty": 0.08,
    "weight": 0.85,
    "data_issued_at": "2026-06-02T10:00:00+00:00",
    "metadata": {"ticker": "WEATHER-NYC-RAIN", "data_quality": "fresh"}
  }
]
```
Some text after.'''
    results = parse_signal_estimates(raw)
    assert len(results) == 1
    assert results[0].source == "noaa_gfs"
    assert results[0].probability == 0.73
    assert results[0].metadata["ticker"] == "WEATHER-NYC-RAIN"


def test_parse_signal_estimates_empty_list():
    raw = '```json\n[]\n```'
    assert parse_signal_estimates(raw) == []


def test_parse_signal_estimates_no_json_block():
    assert parse_signal_estimates("no json here") == []


def test_parse_signal_estimates_bad_json():
    assert parse_signal_estimates("```json\n{broken\n```") == []


def test_parse_signal_estimates_skips_invalid_items():
    raw = '''```json
[
  {"source": "noaa_gfs", "probability": 0.73, "uncertainty": 0.08, "weight": 0.85, "data_issued_at": "2026-06-02T10:00:00+00:00"},
  {"missing_required_fields": true}
]
```'''
    results = parse_signal_estimates(raw)
    assert len(results) == 1
    assert results[0].source == "noaa_gfs"


def test_estimate_to_dict_roundtrip():
    e = SignalEstimate(
        source="noaa_gfs",
        probability=0.73,
        uncertainty=0.08,
        weight=0.85,
        data_issued_at=datetime(2026, 6, 2, 10, 0, 0, tzinfo=timezone.utc),
        metadata={"ticker": "WEATHER-NYC-RAIN"},
    )
    d = estimate_to_dict(e)
    assert d["source"] == "noaa_gfs"
    assert d["probability"] == 0.73
    assert "data_issued_at" in d
    assert d["metadata"]["ticker"] == "WEATHER-NYC-RAIN"
```

- [ ] **Step 6: Run all new tests to verify failure**

```
.venv/bin/pytest tests/test_signals_weather.py tests/test_signals_polymarket.py tests/test_signals_x.py tests/test_parsing.py -v
```
Expected: all fail with `ModuleNotFoundError`

- [ ] **Step 7: Implement `kalshi_trader/signals/__init__.py`**

```python
# kalshi_trader/signals/__init__.py
```
(empty)

- [ ] **Step 8: Implement `kalshi_trader/signals/weather.py`**

```python
# kalshi_trader/signals/weather.py
from __future__ import annotations
import math
from datetime import datetime, timedelta, timezone
import scipy.stats
from kalshi_trader.models import SignalEstimate

_WEIGHT = 0.85
_FRESH_MINUTES = 60
_STALE_MINUTES = 360


def build_weather_signal(
    ticker: str,
    metric: str,
    threshold: float,
    operator: str,
    forecast: dict,
    discussion: dict | None = None,
) -> SignalEstimate:
    """Convert NOAA forecast + optional NWS discussion into a weather SignalEstimate.

    Edit this function to change how NOAA data becomes a signal (weights, uncertainty model, etc.).
    """
    data_age = forecast.get("data_age_minutes", 0)
    issued_at = datetime.now(tz=timezone.utc) - timedelta(minutes=data_age)

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
        raise ValueError(f"Unsupported metric: {metric}")

    prob = round(min(max(prob, 0.01), 0.99), 4)

    if data_age < _FRESH_MINUTES:
        data_quality = "fresh"
    elif data_age < _STALE_MINUTES:
        data_quality = "stale"
    else:
        data_quality = "unavailable"

    direction = "YES" if prob > 0.5 else "NO"
    narrative = (
        f"NOAA GFS shows {prob:.0%} probability for {ticker}. "
        f"Forecast supports {direction}."
    )
    if discussion:
        conf = discussion.get("confidence", "unknown")
        narrative += f" NWS discussion confidence: {conf}."

    metadata: dict = {
        "ticker": ticker,
        "narrative": narrative,
        "data_quality": data_quality,
        "forecast_model": "noaa_gfs",
    }

    if discussion is not None:
        metadata["nws_confidence"] = discussion.get("confidence", "unknown")
        key_points = discussion.get("key_points", [])
        if key_points:
            metadata["key_uncertainty"] = key_points[0]

    return SignalEstimate(
        source="noaa_gfs",
        probability=prob,
        uncertainty=uncertainty,
        weight=_WEIGHT,
        data_issued_at=issued_at,
        metadata=metadata,
    )
```

- [ ] **Step 9: Implement `kalshi_trader/signals/polymarket.py`**

```python
# kalshi_trader/signals/polymarket.py
from __future__ import annotations
from datetime import datetime, timezone
from kalshi_trader.models import SignalEstimate

_PRICE_WEIGHT = 0.75
_WHALE_WEIGHT = 0.60
_PRICE_UNCERTAINTY = 0.03
_WHALE_DEFAULT_UNCERTAINTY = 0.10
_WHALE_SINGLE_UNCERTAINTY = 0.15


def build_price_signal(
    ticker: str,
    poly_prob: float,
    gap_cents: float,
    match_score: float,
    fetched_at: datetime | None = None,
) -> SignalEstimate:
    """Convert a Polymarket price gap into a SignalEstimate.

    Edit this function to change how the cross-platform price gap becomes a signal.
    """
    if fetched_at is None:
        fetched_at = datetime.now(tz=timezone.utc)
    direction = "YES" if gap_cents > 0 else "NO"
    narrative = (
        f"Polymarket prices this at {poly_prob:.0%}. "
        f"Gap vs Kalshi: {gap_cents:+.1f}¢ (Polymarket {'higher' if gap_cents > 0 else 'lower'}). "
        f"Match confidence: {match_score:.2f}. Supports {direction}."
    )
    return SignalEstimate(
        source="polymarket_price",
        probability=poly_prob,
        uncertainty=_PRICE_UNCERTAINTY,
        weight=_PRICE_WEIGHT,
        data_issued_at=fetched_at,
        metadata={
            "ticker": ticker,
            "narrative": narrative,
            "data_quality": "fresh",
            "gap_cents": round(gap_cents, 2),
            "match_score": round(match_score, 4),
        },
    )


def build_whale_signal(
    ticker: str,
    whale_entries: list[dict],
    fetched_at: datetime | None = None,
) -> SignalEstimate | None:
    """Build a whale signal from target wallet entries.

    Returns None if no entries. Edit this function to change how whale activity
    becomes a probability estimate (entry price weighting, uncertainty scaling, etc.).
    """
    if not whale_entries:
        return None
    if fetched_at is None:
        fetched_at = datetime.now(tz=timezone.utc)

    total_size = sum(e.get("size_usd", 1.0) for e in whale_entries) or float(len(whale_entries))

    # Normalize to implied YES probability: YES entry at p → p, NO entry at p → 1-p
    weighted_prob = sum(
        (e["entry_price"] if e["side"].upper() == "YES" else 1.0 - e["entry_price"])
        * e.get("size_usd", 1.0)
        for e in whale_entries
    ) / total_size

    whale_count = len({e["wallet_address"] for e in whale_entries})
    uncertainty = _WHALE_SINGLE_UNCERTAINTY if whale_count == 1 else _WHALE_DEFAULT_UNCERTAINTY

    largest = max(whale_entries, key=lambda e: e.get("size_usd", 0))
    direction = "YES" if weighted_prob > 0.5 else "NO"
    narrative = (
        f"{whale_count} tracked whale wallet(s) entered {direction} "
        f"at avg implied probability {weighted_prob:.0%}. "
        f"Largest entry: ${largest.get('size_usd', 0):,.0f}."
    )

    timestamps = [e.get("timestamp") for e in whale_entries if e.get("timestamp")]
    if timestamps:
        most_recent = max(
            (datetime.fromisoformat(t) if isinstance(t, str) else t for t in timestamps)
        )
    else:
        most_recent = fetched_at

    return SignalEstimate(
        source="polymarket_whale",
        probability=round(min(max(weighted_prob, 0.01), 0.99), 4),
        uncertainty=uncertainty,
        weight=_WHALE_WEIGHT,
        data_issued_at=most_recent,
        metadata={
            "ticker": ticker,
            "narrative": narrative,
            "data_quality": "fresh",
            "whale_count": whale_count,
        },
    )
```

- [ ] **Step 10: Implement `kalshi_trader/signals/x.py`**

```python
# kalshi_trader/signals/x.py
from __future__ import annotations
from datetime import datetime, timezone
from kalshi_trader.models import SignalEstimate


def build_x_signal(
    ticker: str,
    raw_signal: dict,
    narrative: str,
    sentiment_direction: str,
    sentiment_reasoning: str,
    strategies_used: str,
    post_count: int,
) -> SignalEstimate:
    """Attach Claude's qualitative assessment to a raw X signal estimate.

    Edit this function to change how X social signal data maps to a SignalEstimate.
    The raw_signal dict provides the quantitative base; Claude provides the qualitative fields.
    """
    issued_str = raw_signal.get("data_issued_at")
    if issued_str:
        issued: datetime = (
            datetime.fromisoformat(issued_str) if isinstance(issued_str, str) else issued_str
        )
    else:
        issued = datetime.now(tz=timezone.utc)

    return SignalEstimate(
        source=raw_signal.get("source", "x_unknown"),
        probability=float(raw_signal.get("probability", 0.5)),
        uncertainty=float(raw_signal.get("uncertainty", 0.15)),
        weight=float(raw_signal.get("weight", 0.55)),
        data_issued_at=issued,
        metadata={
            "ticker": ticker,
            "narrative": narrative,
            "data_quality": "fresh",
            "post_count": post_count,
            "sentiment_direction": sentiment_direction,
            "sentiment_reasoning": sentiment_reasoning,
            "strategies_used": strategies_used,
        },
    )
```

- [ ] **Step 11: Implement `kalshi_trader/agents/parsing.py`**

```python
# kalshi_trader/agents/parsing.py
from __future__ import annotations
import json
import re
from datetime import datetime
from kalshi_trader.models import SignalEstimate


def parse_signal_estimates(raw: str) -> list[SignalEstimate]:
    """Parse a list[SignalEstimate] JSON block from a Claude response string."""
    match = re.search(r"```json\s*(.*?)\s*```", raw, re.DOTALL)
    if not match:
        return []
    try:
        data = json.loads(match.group(1))
    except json.JSONDecodeError:
        return []
    if not isinstance(data, list):
        return []
    results = []
    for item in data:
        try:
            issued = item["data_issued_at"]
            if isinstance(issued, str):
                issued = datetime.fromisoformat(issued)
            results.append(SignalEstimate(
                source=item["source"],
                probability=float(item["probability"]),
                uncertainty=float(item["uncertainty"]),
                weight=float(item["weight"]),
                data_issued_at=issued,
                metadata=item.get("metadata", {}),
            ))
        except (KeyError, ValueError, TypeError):
            continue
    return results


def estimate_to_dict(e: SignalEstimate) -> dict:
    """Serialize a SignalEstimate to a plain dict for tool results and JSON output."""
    return {
        "source": e.source,
        "probability": e.probability,
        "uncertainty": e.uncertainty,
        "weight": e.weight,
        "data_issued_at": e.data_issued_at.isoformat(),
        "metadata": e.metadata,
    }
```

- [ ] **Step 12: Run all tests — expect passing**

```
.venv/bin/pytest tests/test_signals_weather.py tests/test_signals_polymarket.py tests/test_signals_x.py tests/test_parsing.py -v
```
Expected: all pass

- [ ] **Step 13: Commit**

```bash
git add kalshi_trader/signals/ kalshi_trader/agents/parsing.py tests/test_signals_weather.py tests/test_signals_polymarket.py tests/test_signals_x.py tests/test_parsing.py
git commit -m "feat: add signals/ converter layer and agents/parsing utility"
```

---

## Task 2: System prompt `.md` files

**Files:**
- Create: `kalshi_trader/agents/prompts/weather.md`
- Create: `kalshi_trader/agents/prompts/polymarket_price.md`
- Create: `kalshi_trader/agents/prompts/polymarket_whale.md`
- Create: `kalshi_trader/agents/prompts/x.md`

- [ ] **Step 1: Create `kalshi_trader/agents/prompts/weather.md`**

```markdown
You are a weather market specialist for a Kalshi prediction market trading system.

Your job: analyze a single Kalshi weather market and return a probability signal as a `list[SignalEstimate]` JSON block.

## Workflow

1. Call `parse_weather_market(ticker, title)` — if it returns null, the market title is unparseable; respond with `[]`.
2. Call `get_noaa_forecast(lat, lon, target_date)` using the values from step 1.
3. **Judgment point:** If `precip_pct` from the forecast is between 30–70%, also call `get_nws_discussion(lat, lon)` to get qualitative NWS context.
4. Call `build_weather_signal(ticker, metric, threshold, operator, forecast)` — if you fetched a discussion, pass it as `discussion`.
5. Return the result from step 4 as your final answer.

## Output format

Your final response must contain exactly one fenced JSON block — copy the result from `build_weather_signal` exactly, do not modify any values:

```json
[
  {
    "source": "noaa_gfs",
    "probability": 0.73,
    "uncertainty": 0.08,
    "weight": 0.85,
    "data_issued_at": "2026-06-02T10:00:00+00:00",
    "metadata": {
      "ticker": "WEATHER-NYC-RAIN-JUNE3",
      "narrative": "NOAA GFS shows 73% precipitation probability...",
      "data_quality": "fresh",
      "forecast_model": "noaa_gfs"
    }
  }
]
```

If the market cannot be parsed or no signal is available, respond with:
```json
[]
```
```

- [ ] **Step 2: Create `kalshi_trader/agents/prompts/polymarket_price.md`**

```markdown
You are a cross-platform price signal specialist for a Kalshi prediction market trading system.

Your job: find this market's counterpart on Polymarket, check the price gap, and return a signal as `list[SignalEstimate]` JSON.

## Workflow

1. Call `find_polymarket_match(kalshi_title)` — if it returns null (no match found), respond with `[]`.
2. Call `check_price_gap(ticker, kalshi_midpoint_cents, poly_prob, open_interest, hours_to_close)` — use the values provided in the user message for kalshi_midpoint_cents, open_interest, and hours_to_close. If it returns null (gap too small, OI too low, or hours out of range), respond with `[]`.
3. Call `build_price_signal(ticker, poly_prob, gap_cents, match_score)` — use gap_cents from step 2 and match_score from step 1.
4. Return the result from step 3 as your final answer.

## Output format

Your final response must contain exactly one fenced JSON block — copy the result from `build_price_signal` exactly:

```json
[
  {
    "source": "polymarket_price",
    "probability": 0.45,
    "uncertainty": 0.03,
    "weight": 0.75,
    "data_issued_at": "2026-06-02T12:00:00+00:00",
    "metadata": {
      "ticker": "SPORTS-NBA-CELTICS",
      "narrative": "Polymarket prices this at 45%...",
      "data_quality": "fresh",
      "gap_cents": 18.0,
      "match_score": 0.91
    }
  }
]
```

If no match or the gap is too small, respond with:
```json
[]
```
```

- [ ] **Step 3: Create `kalshi_trader/agents/prompts/polymarket_whale.md`**

```markdown
You are a whale activity monitor for a Kalshi prediction market trading system.

Your job: detect whether tracked high-performing wallets are positioned on this market. This signal is independent of the Polymarket/Kalshi price gap — you are looking for smart money positioning, not price discrepancies.

## Workflow

1. Call `load_whale_targets()` to get the list of tracked wallet addresses.
2. Call `find_polymarket_match(kalshi_title)` — if it returns null, respond with `[]`.
3. Call `get_whale_entries(condition_id, target_wallets)` using the condition_id from step 2 and the wallets from step 1.
4. Call `build_whale_signal(ticker, whale_entries)` — if it returns null (no target entries), respond with `[]`.
5. Return the result from step 4 as your final answer.

## Output format

Your final response must contain exactly one fenced JSON block — copy the result from `build_whale_signal` exactly:

```json
[
  {
    "source": "polymarket_whale",
    "probability": 0.62,
    "uncertainty": 0.10,
    "weight": 0.60,
    "data_issued_at": "2026-06-02T11:30:00+00:00",
    "metadata": {
      "ticker": "SPORTS-NBA-CELTICS",
      "narrative": "3 tracked whale wallets entered YES...",
      "data_quality": "fresh",
      "whale_count": 3
    }
  }
]
```

If no target whale entries are found, respond with:
```json
[]
```
```

- [ ] **Step 4: Create `kalshi_trader/agents/prompts/x.md`**

```markdown
You are an X (Twitter) social signal specialist for a Kalshi prediction market trading system.

Your job: search X for social signal on a specific market and return probability estimates as `list[SignalEstimate]` JSON.

## Workflow

1. Call `search_x_signal(ticker, category, market_title)` to run default strategies for the category.
2. **Judgment point A:** If all returned estimates have `uncertainty > 0.15`, also call `override_x_strategies(ticker, market_title, ["experts", "news"])` for higher-quality signal.
3. Review the actual signal content — summaries, post counts, expert positions, news items.
4. **Judgment point B:** If estimates spread > 0.20, note "high-disagreement" in your narrative.
5. For each signal estimate, call `build_x_signal(ticker, raw_signal, narrative, sentiment_direction, sentiment_reasoning, strategies_used, post_count)` to attach your qualitative assessment.
6. Return the full list of results from step 5.

## Sentiment synthesis

Do NOT apply a probability threshold to determine sentiment direction. Read the actual content:
- What are prominent accounts saying?
- Is there expert consensus?
- Are posts speculative or information-rich?
- Does recent news favor one outcome?

Express your assessment as `sentiment_direction` (e.g. "bullish", "bearish", "mixed", "neutral") and explain in `sentiment_reasoning` — cite specific signals, accounts, or themes that drove the assessment.

## Output format

Your final response must contain exactly one fenced JSON block — the list of results from `build_x_signal`:

```json
[
  {
    "source": "x_sentiment",
    "probability": 0.62,
    "uncertainty": 0.14,
    "weight": 0.55,
    "data_issued_at": "2026-06-02T12:00:00+00:00",
    "metadata": {
      "ticker": "SPORTS-NBA-CELTICS",
      "narrative": "X sentiment skews bullish on Celtics...",
      "data_quality": "fresh",
      "post_count": 17,
      "sentiment_direction": "bullish",
      "sentiment_reasoning": "Three prominent NBA analysts posted YES takes. 12 positive vs 3 negative posts.",
      "strategies_used": "sentiment,buzz,experts"
    }
  }
]
```

If no relevant signal is found, respond with:
```json
[]
```
```

- [ ] **Step 5: Verify all four files are loadable**

```
python -c "
from pathlib import Path
base = Path('kalshi_trader/agents/prompts')
for name in ['weather.md', 'polymarket_price.md', 'polymarket_whale.md', 'x.md']:
    text = (base / name).read_text()
    assert len(text) > 100, f'{name} is too short'
    print(f'{name}: {len(text)} chars OK')
"
```
Expected: four lines each ending in `OK`

- [ ] **Step 6: Commit**

```bash
git add kalshi_trader/agents/prompts/
git commit -m "feat: add pipeline agent system prompt .md files"
```

---

## Task 3: Update `WeatherAgent`

**Files:**
- Modify: `kalshi_trader/agents/weather_agent.py`
- Modify: `tests/test_weather_agent.py`

- [ ] **Step 1: Write failing test for new `WeatherAgent` interface**

Add to `tests/test_weather_agent.py`:

```python
# Add at top of file (with other imports):
from unittest.mock import AsyncMock, patch, MagicMock
from kalshi_trader.models import SignalEstimate


def test_parse_signal_estimates_valid_response():
    """WeatherAgent._parse_estimates returns SignalEstimate list from JSON block."""
    from kalshi_trader.agents.weather_agent import WeatherAgent
    agent = WeatherAgent.__new__(WeatherAgent)  # skip __init__
    raw = '''```json
[
  {
    "source": "noaa_gfs",
    "probability": 0.73,
    "uncertainty": 0.08,
    "weight": 0.85,
    "data_issued_at": "2026-06-02T10:00:00+00:00",
    "metadata": {"ticker": "WEATHER-NYC-RAIN", "data_quality": "fresh"}
  }
]
```'''
    results = agent._parse_estimates(raw)
    assert len(results) == 1
    assert isinstance(results[0], SignalEstimate)
    assert results[0].source == "noaa_gfs"
    assert results[0].probability == 0.73


def test_parse_signal_estimates_empty():
    from kalshi_trader.agents.weather_agent import WeatherAgent
    agent = WeatherAgent.__new__(WeatherAgent)
    assert agent._parse_estimates("```json\n[]\n```") == []


def test_parse_signal_estimates_no_block():
    from kalshi_trader.agents.weather_agent import WeatherAgent
    agent = WeatherAgent.__new__(WeatherAgent)
    assert agent._parse_estimates("nothing useful") == []
```

- [ ] **Step 2: Run test to verify failure**

```
.venv/bin/pytest tests/test_weather_agent.py::test_parse_signal_estimates_valid_response -v
```
Expected: FAIL — `WeatherAgent` has no `_parse_estimates` method yet

- [ ] **Step 3: Rewrite `kalshi_trader/agents/weather_agent.py`**

Replace the entire file:

```python
from __future__ import annotations
import json
from datetime import date as date_type, datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from kalshi_trader.models import SignalEstimate
from kalshi_trader.external.noaa import NOAAClient
from kalshi_trader.external.weather_parser import parse_title, parse_discussion
from kalshi_trader.agents.base import BaseAgent
from kalshi_trader.agents.parsing import parse_signal_estimates, estimate_to_dict
from kalshi_trader.signals.weather import build_weather_signal

_PROMPTS_DIR = Path(__file__).parent / "prompts"


async def _parse_weather_market(ticker: str, title: str) -> dict | None:
    return parse_title(ticker, title)


_SCHEMAS: list[dict] = [
    {
        "name": "parse_weather_market",
        "description": "Parse a Kalshi weather market title into a structured question (city, lat, lon, metric, threshold, operator, target_date). Returns null if unparseable — stop and return [] if null.",
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
        "name": "get_nws_discussion",
        "description": "Fetch and parse the NWS Area Forecast Discussion. Returns confidence ('high'/'medium'/'low') and key_points list. Call this when precip_pct is between 30-70%.",
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
        "name": "build_weather_signal",
        "description": "Convert NOAA forecast data into a SignalEstimate dict. Pass the full forecast dict from get_noaa_forecast and optionally the discussion dict from get_nws_discussion.",
        "input_schema": {
            "type": "object",
            "properties": {
                "ticker": {"type": "string"},
                "metric": {"type": "string", "enum": ["temp_high", "temp_low", "precipitation"]},
                "threshold": {"type": "number"},
                "operator": {"type": "string", "enum": ["above", "below"]},
                "forecast": {"type": "object"},
                "discussion": {"type": "object"},
            },
            "required": ["ticker", "metric", "threshold", "operator", "forecast"],
        },
    },
]


class WeatherAgent:
    def __init__(self) -> None:
        self._noaa = NOAAClient()
        system_prompt = (_PROMPTS_DIR / "weather.md").read_text()
        self._agent = BaseAgent(
            tools=_SCHEMAS,
            handlers={
                "parse_weather_market": _parse_weather_market,
                "get_noaa_forecast": self._get_noaa_forecast,
                "get_nws_discussion": self._get_nws_discussion,
                "build_weather_signal": self._build_weather_signal,
            },
            system_prompt=system_prompt,
        )

    async def run(self, ticker: str, title: str) -> list[SignalEstimate]:
        prompt = f"Analyze this Kalshi weather market:\nticker: {ticker}\ntitle: {title}"
        raw = await self._agent.run(prompt)
        return self._parse_estimates(raw)

    def _parse_estimates(self, raw: str) -> list[SignalEstimate]:
        return parse_signal_estimates(raw)

    async def _get_noaa_forecast(self, lat: float, lon: float, date: str) -> dict:
        target = date_type.fromisoformat(date)
        result = await self._noaa.get_forecast(lat, lon, target)
        generated_at = result["generated_at"]
        if generated_at.tzinfo is None:
            generated_at = generated_at.replace(tzinfo=timezone.utc)
        age = (datetime.now(tz=timezone.utc) - generated_at).total_seconds() / 60
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
        }

    async def _build_weather_signal(
        self,
        ticker: str,
        metric: str,
        threshold: float,
        operator: str,
        forecast: dict,
        discussion: dict | None = None,
    ) -> dict:
        estimate = build_weather_signal(ticker, metric, threshold, operator, forecast, discussion)
        return estimate_to_dict(estimate)

    async def close(self) -> None:
        await self._noaa.close()
```

- [ ] **Step 4: Run tests**

```
.venv/bin/pytest tests/test_weather_agent.py -v
```
Expected: all pass (some old tests may need updating — see step 5 if any fail)

- [ ] **Step 5: Fix any failing old tests in `test_weather_agent.py`**

The old tests for `_combine_signals` and `_estimate_probability` reference functions no longer in `weather_agent.py`. Those functions are now in `signals/weather.py`. Update any imports at the top of the test file:

```python
# Replace old import:
# from kalshi_trader.agents.weather_agent import _combine_signals, _estimate_probability
# With:
from kalshi_trader.signals.weather import build_weather_signal
```

If tests for `_combine_signals` / `_estimate_probability` exist, they now live in `tests/test_signals_weather.py` — remove duplicates from `test_weather_agent.py`.

- [ ] **Step 6: Run full test suite**

```
.venv/bin/pytest tests/ -q
```
Expected: same pass count as before (178+) with no regressions

- [ ] **Step 7: Commit**

```bash
git add kalshi_trader/agents/weather_agent.py tests/test_weather_agent.py
git commit -m "feat: update WeatherAgent to load prompt from .md and return SignalEstimate"
```

---

## Task 4: Add `match_market_with_score` to `PolymarketClient`

**Files:**
- Modify: `kalshi_trader/external/polymarket.py`
- Modify: `tests/test_polymarket.py`

- [ ] **Step 1: Write failing test**

Add to `tests/test_polymarket.py`:

```python
def test_match_market_with_score_returns_score():
    client = PolymarketClient()
    poly_markets = [
        {"question": "Will the Boston Celtics win the NBA championship?", "conditionId": "0xabc",
         "outcomePrices": "[0.45, 0.55]", "active": True, "closed": False},
    ]
    result = client.match_market_with_score(
        "Will the Boston Celtics win the 2026 NBA Finals?", poly_markets
    )
    assert result is not None
    match, score = result
    assert match["conditionId"] == "0xabc"
    assert 0.0 < score <= 1.0


def test_match_market_with_score_no_match_returns_none():
    client = PolymarketClient()
    poly_markets = [
        {"question": "Will it rain in Seattle tomorrow?", "conditionId": "0xdef",
         "outcomePrices": "[0.70, 0.30]", "active": True, "closed": False},
    ]
    result = client.match_market_with_score("Will the Lakers win the championship?", poly_markets)
    assert result is None
```

- [ ] **Step 2: Run to verify failure**

```
.venv/bin/pytest tests/test_polymarket.py::test_match_market_with_score_returns_score -v
```
Expected: FAIL — `PolymarketClient` has no `match_market_with_score`

- [ ] **Step 3: Add `match_market_with_score` to `PolymarketClient`**

In `kalshi_trader/external/polymarket.py`, add this method directly after `match_market`:

```python
def match_market_with_score(
    self, kalshi_title: str, poly_markets: list[dict]
) -> tuple[dict, float] | None:
    """Like match_market but also returns the Jaccard similarity score."""
    kalshi_words, kalshi_nums = _tokenize(kalshi_title)
    best_score = 0.0
    best_market = None
    for market in poly_markets:
        poly_words, poly_nums = _tokenize(market["question"])
        if not _numbers_compatible(kalshi_nums, poly_nums):
            continue
        union = len(kalshi_words | poly_words)
        if not union:
            continue
        score = len(kalshi_words & poly_words) / union
        if score > best_score:
            best_score = score
            best_market = market
    if best_score < 0.20 or not best_market:
        return None
    best_words, _ = _tokenize(best_market["question"])
    if len(kalshi_words & best_words) < 2:
        return None
    return best_market, best_score
```

- [ ] **Step 4: Run tests**

```
.venv/bin/pytest tests/test_polymarket.py -v
```
Expected: all pass

- [ ] **Step 5: Commit**

```bash
git add kalshi_trader/external/polymarket.py tests/test_polymarket.py
git commit -m "feat: add match_market_with_score to PolymarketClient"
```

---

## Task 5: Create `PolymarketPriceAgent`

**Files:**
- Create: `kalshi_trader/agents/polymarket_price_agent.py`
- Create: `tests/test_polymarket_price_agent.py`

- [ ] **Step 1: Write failing tests**

```python
# tests/test_polymarket_price_agent.py
from kalshi_trader.agents.polymarket_price_agent import PolymarketPriceAgent
from kalshi_trader.models import SignalEstimate


def test_parse_estimates_valid():
    agent = PolymarketPriceAgent.__new__(PolymarketPriceAgent)
    raw = '''```json
[
  {
    "source": "polymarket_price",
    "probability": 0.45,
    "uncertainty": 0.03,
    "weight": 0.75,
    "data_issued_at": "2026-06-02T12:00:00+00:00",
    "metadata": {"ticker": "NBA-CELTICS", "gap_cents": 18.0, "match_score": 0.91,
                 "data_quality": "fresh", "narrative": "..."}
  }
]
```'''
    results = agent._parse_estimates(raw)
    assert len(results) == 1
    assert isinstance(results[0], SignalEstimate)
    assert results[0].source == "polymarket_price"


def test_parse_estimates_empty():
    agent = PolymarketPriceAgent.__new__(PolymarketPriceAgent)
    assert agent._parse_estimates("```json\n[]\n```") == []
```

- [ ] **Step 2: Run to verify failure**

```
.venv/bin/pytest tests/test_polymarket_price_agent.py -v
```
Expected: `ModuleNotFoundError`

- [ ] **Step 3: Implement `kalshi_trader/agents/polymarket_price_agent.py`**

```python
from __future__ import annotations
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from kalshi_trader.models import Market, SignalEstimate
from kalshi_trader.external.polymarket import PolymarketClient
from kalshi_trader.external.market_scorer import score_market
from kalshi_trader.agents.base import BaseAgent
from kalshi_trader.agents.parsing import parse_signal_estimates, estimate_to_dict
from kalshi_trader.signals.polymarket import build_price_signal

_PROMPTS_DIR = Path(__file__).parent / "prompts"

_SCHEMAS: list[dict] = [
    {
        "name": "find_polymarket_match",
        "description": "Fetch active Polymarket markets and find the best match for the Kalshi title. Returns {condition_id, poly_prob, match_score} or null if no match found.",
        "input_schema": {
            "type": "object",
            "properties": {"kalshi_title": {"type": "string"}},
            "required": ["kalshi_title"],
        },
    },
    {
        "name": "check_price_gap",
        "description": "Check whether the Polymarket/Kalshi price gap and market quality pass filters. Returns {gap_cents} if the market is worth trading, null if filtered out (gap < 7¢, OI < 500, or hours out of range).",
        "input_schema": {
            "type": "object",
            "properties": {
                "ticker": {"type": "string"},
                "kalshi_midpoint_cents": {"type": "number"},
                "poly_prob": {"type": "number"},
                "open_interest": {"type": "integer"},
                "hours_to_close": {"type": "number"},
            },
            "required": ["ticker", "kalshi_midpoint_cents", "poly_prob", "open_interest", "hours_to_close"],
        },
    },
    {
        "name": "build_price_signal",
        "description": "Convert a Polymarket price gap into a SignalEstimate dict.",
        "input_schema": {
            "type": "object",
            "properties": {
                "ticker": {"type": "string"},
                "poly_prob": {"type": "number"},
                "gap_cents": {"type": "number"},
                "match_score": {"type": "number"},
            },
            "required": ["ticker", "poly_prob", "gap_cents", "match_score"],
        },
    },
]


class PolymarketPriceAgent:
    def __init__(self, client: PolymarketClient | None = None) -> None:
        self._client = client or PolymarketClient()
        system_prompt = (_PROMPTS_DIR / "polymarket_price.md").read_text()
        self._agent = BaseAgent(
            tools=_SCHEMAS,
            handlers={
                "find_polymarket_match": self._find_polymarket_match,
                "check_price_gap": self._check_price_gap,
                "build_price_signal": self._build_price_signal,
            },
            system_prompt=system_prompt,
        )

    async def run(
        self,
        ticker: str,
        title: str,
        kalshi_midpoint_cents: float,
        open_interest: int,
        hours_to_close: float,
    ) -> list[SignalEstimate]:
        prompt = (
            f"Analyze this Kalshi market:\n"
            f"ticker: {ticker}\n"
            f"title: {title}\n"
            f"kalshi_midpoint_cents: {kalshi_midpoint_cents}\n"
            f"open_interest: {open_interest}\n"
            f"hours_to_close: {hours_to_close}"
        )
        raw = await self._agent.run(prompt)
        return self._parse_estimates(raw)

    def _parse_estimates(self, raw: str) -> list[SignalEstimate]:
        return parse_signal_estimates(raw)

    async def _find_polymarket_match(self, kalshi_title: str) -> dict | None:
        poly_markets = await self._client.get_markets()
        result = self._client.match_market_with_score(kalshi_title, poly_markets)
        if result is None:
            return None
        market, score = result
        poly_prob = float(json.loads(market["outcomePrices"])[0])
        return {
            "condition_id": market["conditionId"],
            "poly_prob": poly_prob,
            "match_score": round(score, 4),
        }

    async def _check_price_gap(
        self,
        ticker: str,
        kalshi_midpoint_cents: float,
        poly_prob: float,
        open_interest: int,
        hours_to_close: float,
    ) -> dict | None:
        close_time = datetime.now(tz=timezone.utc) + timedelta(hours=hours_to_close)
        market = Market(
            ticker=ticker,
            event_ticker="", series_ticker="", title="",
            yes_bid=kalshi_midpoint_cents - 0.5,
            yes_ask=kalshi_midpoint_cents + 0.5,
            last_price=kalshi_midpoint_cents,
            volume_24h=0,
            open_interest=open_interest,
            category="",
            close_time=close_time,
            status="open",
        )
        result = score_market(market, poly_prob)
        if result is None:
            return None
        gap_cents = (poly_prob - kalshi_midpoint_cents / 100.0) * 100.0
        return {"gap_cents": round(gap_cents, 2)}

    async def _build_price_signal(
        self,
        ticker: str,
        poly_prob: float,
        gap_cents: float,
        match_score: float,
    ) -> dict:
        estimate = build_price_signal(ticker, poly_prob, gap_cents, match_score)
        return estimate_to_dict(estimate)
```

- [ ] **Step 4: Run tests**

```
.venv/bin/pytest tests/test_polymarket_price_agent.py -v
```
Expected: pass

- [ ] **Step 5: Run full suite**

```
.venv/bin/pytest tests/ -q
```
Expected: all pass

- [ ] **Step 6: Commit**

```bash
git add kalshi_trader/agents/polymarket_price_agent.py tests/test_polymarket_price_agent.py
git commit -m "feat: add PolymarketPriceAgent with polymarket_price.md system prompt"
```

---

## Task 6: Create `PolymarketWhaleAgent`

**Files:**
- Create: `kalshi_trader/agents/polymarket_whale_agent.py`
- Create: `tests/test_polymarket_whale_agent.py`

- [ ] **Step 1: Write failing tests**

```python
# tests/test_polymarket_whale_agent.py
from kalshi_trader.agents.polymarket_whale_agent import PolymarketWhaleAgent
from kalshi_trader.models import SignalEstimate


def test_parse_estimates_valid():
    agent = PolymarketWhaleAgent.__new__(PolymarketWhaleAgent)
    raw = '''```json
[
  {
    "source": "polymarket_whale",
    "probability": 0.62,
    "uncertainty": 0.10,
    "weight": 0.60,
    "data_issued_at": "2026-06-02T11:30:00+00:00",
    "metadata": {"ticker": "NBA-CELTICS", "whale_count": 3, "data_quality": "fresh", "narrative": "..."}
  }
]
```'''
    results = agent._parse_estimates(raw)
    assert len(results) == 1
    assert isinstance(results[0], SignalEstimate)
    assert results[0].source == "polymarket_whale"
    assert results[0].metadata["whale_count"] == 3


def test_parse_estimates_empty():
    agent = PolymarketWhaleAgent.__new__(PolymarketWhaleAgent)
    assert agent._parse_estimates("```json\n[]\n```") == []
```

- [ ] **Step 2: Run to verify failure**

```
.venv/bin/pytest tests/test_polymarket_whale_agent.py -v
```
Expected: `ModuleNotFoundError`

- [ ] **Step 3: Implement `kalshi_trader/agents/polymarket_whale_agent.py`**

```python
from __future__ import annotations
import json
from pathlib import Path
from kalshi_trader.models import SignalEstimate
from kalshi_trader.external.polymarket import PolymarketClient, load_whale_targets
from kalshi_trader.agents.base import BaseAgent
from kalshi_trader.agents.parsing import parse_signal_estimates, estimate_to_dict
from kalshi_trader.signals.polymarket import build_whale_signal

_PROMPTS_DIR = Path(__file__).parent / "prompts"

_SCHEMAS: list[dict] = [
    {
        "name": "load_whale_targets",
        "description": "Load the list of tracked high-performing wallet addresses.",
        "input_schema": {"type": "object", "properties": {}, "required": []},
    },
    {
        "name": "find_polymarket_match",
        "description": "Fetch active Polymarket markets and find the best match for the Kalshi title. Returns {condition_id, poly_prob, match_score} or null.",
        "input_schema": {
            "type": "object",
            "properties": {"kalshi_title": {"type": "string"}},
            "required": ["kalshi_title"],
        },
    },
    {
        "name": "get_whale_entries",
        "description": "Fetch recent large trades for a Polymarket condition and return entries from target wallets. Returns list of {wallet_address, side, entry_price, size_usd, timestamp}.",
        "input_schema": {
            "type": "object",
            "properties": {
                "condition_id": {"type": "string"},
                "target_wallets": {"type": "array", "items": {"type": "string"}},
            },
            "required": ["condition_id", "target_wallets"],
        },
    },
    {
        "name": "build_whale_signal",
        "description": "Build a whale SignalEstimate from target wallet entries. Returns a SignalEstimate dict, or null if no entries.",
        "input_schema": {
            "type": "object",
            "properties": {
                "ticker": {"type": "string"},
                "whale_entries": {"type": "array", "items": {"type": "object"}},
            },
            "required": ["ticker", "whale_entries"],
        },
    },
]


class PolymarketWhaleAgent:
    def __init__(self, client: PolymarketClient | None = None) -> None:
        self._client = client or PolymarketClient()
        system_prompt = (_PROMPTS_DIR / "polymarket_whale.md").read_text()
        self._agent = BaseAgent(
            tools=_SCHEMAS,
            handlers={
                "load_whale_targets": self._load_whale_targets,
                "find_polymarket_match": self._find_polymarket_match,
                "get_whale_entries": self._get_whale_entries,
                "build_whale_signal": self._build_whale_signal,
            },
            system_prompt=system_prompt,
        )

    async def run(self, ticker: str, title: str) -> list[SignalEstimate]:
        prompt = f"Analyze this Kalshi market:\nticker: {ticker}\ntitle: {title}"
        raw = await self._agent.run(prompt)
        return self._parse_estimates(raw)

    def _parse_estimates(self, raw: str) -> list[SignalEstimate]:
        return parse_signal_estimates(raw)

    async def _load_whale_targets(self) -> list[str]:
        return load_whale_targets()

    async def _find_polymarket_match(self, kalshi_title: str) -> dict | None:
        poly_markets = await self._client.get_markets()
        result = self._client.match_market_with_score(kalshi_title, poly_markets)
        if result is None:
            return None
        market, score = result
        poly_prob = float(json.loads(market["outcomePrices"])[0])
        return {
            "condition_id": market["conditionId"],
            "poly_prob": poly_prob,
            "match_score": round(score, 4),
        }

    async def _get_whale_entries(
        self, condition_id: str, target_wallets: list[str]
    ) -> list[dict]:
        trades = await self._client.get_large_trades(condition_id)
        signals = self._client.detect_whale_entries(trades)
        target_set = set(target_wallets)
        return [
            {
                "wallet_address": s.wallet_address,
                "side": s.side,
                "entry_price": s.entry_price,
                "size_usd": s.size_usd,
                "timestamp": s.timestamp.isoformat(),
            }
            for s in signals
            if s.wallet_address in target_set
        ]

    async def _build_whale_signal(self, ticker: str, whale_entries: list[dict]) -> dict | None:
        estimate = build_whale_signal(ticker, whale_entries)
        if estimate is None:
            return None
        return estimate_to_dict(estimate)
```

- [ ] **Step 4: Run tests**

```
.venv/bin/pytest tests/test_polymarket_whale_agent.py -v
```
Expected: pass

- [ ] **Step 5: Run full suite**

```
.venv/bin/pytest tests/ -q
```
Expected: all pass

- [ ] **Step 6: Commit**

```bash
git add kalshi_trader/agents/polymarket_whale_agent.py tests/test_polymarket_whale_agent.py
git commit -m "feat: add PolymarketWhaleAgent with polymarket_whale.md system prompt"
```

---

## Task 7: Update `XAgent`

**Files:**
- Modify: `kalshi_trader/agents/x_agent.py`
- Modify: `tests/test_x_agent.py`

- [ ] **Step 1: Write failing test for new interface**

Add to `tests/test_x_agent.py`:

```python
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
```

- [ ] **Step 2: Run to verify failure**

```
.venv/bin/pytest tests/test_x_agent.py::test_x_agent_parse_estimates_valid -v
```
Expected: FAIL — `XAgent` has no `_parse_estimates` yet

- [ ] **Step 3: Rewrite `kalshi_trader/agents/x_agent.py`**

Replace the entire file:

```python
from __future__ import annotations
import asyncio
from pathlib import Path
from kalshi_trader import config
from kalshi_trader.models import SignalEstimate
from kalshi_trader.external.x_client import XClient
from kalshi_trader.external.x_strategies import (
    CATEGORY_STRATEGIES,
    STRATEGY_NAME_MAP,
    FALLBACK_STRATEGIES,
)
from kalshi_trader.agents.base import BaseAgent
from kalshi_trader.agents.parsing import parse_signal_estimates, estimate_to_dict
from kalshi_trader.signals.x import build_x_signal

_PROMPTS_DIR = Path(__file__).parent / "prompts"

_SCHEMAS: list[dict] = [
    {
        "name": "search_x_signal",
        "description": "Search X for social signal on a Kalshi market using default strategies for the category. Returns a list of raw SignalEstimate dicts.",
        "input_schema": {
            "type": "object",
            "properties": {
                "ticker": {"type": "string"},
                "category": {"type": "string"},
                "market_title": {"type": "string"},
            },
            "required": ["ticker", "category", "market_title"],
        },
    },
    {
        "name": "override_x_strategies",
        "description": "Run specific X search strategies instead of category defaults. Use when all estimates have uncertainty > 0.15.",
        "input_schema": {
            "type": "object",
            "properties": {
                "ticker": {"type": "string"},
                "market_title": {"type": "string"},
                "strategies": {
                    "type": "array",
                    "items": {"type": "string", "enum": ["sentiment", "news", "experts", "buzz"]},
                },
            },
            "required": ["ticker", "market_title", "strategies"],
        },
    },
    {
        "name": "build_x_signal",
        "description": "Attach your qualitative sentiment assessment to a raw X signal estimate. Call once per raw signal. Provide your narrative, sentiment_direction, and sentiment_reasoning.",
        "input_schema": {
            "type": "object",
            "properties": {
                "ticker": {"type": "string"},
                "raw_signal": {"type": "object", "description": "One element from search_x_signal or override_x_strategies"},
                "narrative": {"type": "string"},
                "sentiment_direction": {"type": "string"},
                "sentiment_reasoning": {"type": "string"},
                "strategies_used": {"type": "string", "description": "Comma-separated strategy names"},
                "post_count": {"type": "integer"},
            },
            "required": ["ticker", "raw_signal", "narrative", "sentiment_direction", "sentiment_reasoning", "strategies_used", "post_count"],
        },
    },
]


def _estimate_to_raw(e: SignalEstimate) -> dict:
    return estimate_to_dict(e)


class XAgent:
    def __init__(self) -> None:
        self._client = XClient()
        self._semaphore = asyncio.Semaphore(config.X_MAX_CONCURRENT_SEARCHES)
        system_prompt = (_PROMPTS_DIR / "x.md").read_text()
        self._agent = BaseAgent(
            tools=_SCHEMAS,
            handlers={
                "search_x_signal": self._search_x_signal,
                "override_x_strategies": self._override_x_strategies,
                "build_x_signal": self._build_x_signal,
            },
            system_prompt=system_prompt,
        )

    async def run(self, ticker: str, category: str, title: str) -> list[SignalEstimate]:
        prompt = (
            f"Analyze this Kalshi market for X social signal:\n"
            f"ticker: {ticker}\n"
            f"category: {category}\n"
            f"title: {title}"
        )
        raw = await self._agent.run(prompt)
        return self._parse_estimates(raw)

    def _parse_estimates(self, raw: str) -> list[SignalEstimate]:
        return parse_signal_estimates(raw)

    async def _run_strategy(self, strategy_cls: type, market_title: str) -> dict | None:
        async with self._semaphore:
            strategy = strategy_cls()
            result = await strategy.run(market_title, self._client)
        return estimate_to_dict(strategy.to_signal_estimate(result))

    async def _search_x_signal(
        self, ticker: str, category: str, market_title: str
    ) -> list[dict]:
        strategy_classes = CATEGORY_STRATEGIES.get(category, list(FALLBACK_STRATEGIES))
        results = await asyncio.gather(
            *[self._run_strategy(cls, market_title) for cls in strategy_classes],
            return_exceptions=True,
        )
        return [r for r in results if isinstance(r, dict)]

    async def _override_x_strategies(
        self, ticker: str, market_title: str, strategies: list[str]
    ) -> list[dict]:
        classes = [STRATEGY_NAME_MAP[n] for n in strategies if n in STRATEGY_NAME_MAP]
        if not classes:
            classes = list(FALLBACK_STRATEGIES)
        results = await asyncio.gather(
            *[self._run_strategy(cls, market_title) for cls in classes],
            return_exceptions=True,
        )
        return [r for r in results if isinstance(r, dict)]

    async def _build_x_signal(
        self,
        ticker: str,
        raw_signal: dict,
        narrative: str,
        sentiment_direction: str,
        sentiment_reasoning: str,
        strategies_used: str,
        post_count: int,
    ) -> dict:
        estimate = build_x_signal(
            ticker, raw_signal, narrative, sentiment_direction,
            sentiment_reasoning, strategies_used, post_count,
        )
        return estimate_to_dict(estimate)

    async def close(self) -> None:
        await self._client.close()
```

- [ ] **Step 4: Run tests**

```
.venv/bin/pytest tests/test_x_agent.py -v
```
Expected: all pass (remove any old tests that tested the removed `_claude_second_pass` method)

- [ ] **Step 5: Run full suite**

```
.venv/bin/pytest tests/ -q
```
Expected: all pass

- [ ] **Step 6: Commit**

```bash
git add kalshi_trader/agents/x_agent.py tests/test_x_agent.py
git commit -m "feat: update XAgent to load prompt from x.md and use BaseAgent tool-use loop"
```

---

## Task 8: Create pipeline CLIs

**Files:**
- Create: `kalshi_trader/pipelines/__init__.py`
- Create: `kalshi_trader/pipelines/weather.py`
- Create: `kalshi_trader/pipelines/polymarket_price.py`
- Create: `kalshi_trader/pipelines/polymarket_whale.py`
- Create: `kalshi_trader/pipelines/x.py`
- Create: `tests/test_pipelines.py`

- [ ] **Step 1: Write failing tests**

```python
# tests/test_pipelines.py
import importlib
import pytest


def test_weather_pipeline_importable():
    mod = importlib.import_module("kalshi_trader.pipelines.weather")
    assert hasattr(mod, "main")


def test_polymarket_price_pipeline_importable():
    mod = importlib.import_module("kalshi_trader.pipelines.polymarket_price")
    assert hasattr(mod, "main")


def test_polymarket_whale_pipeline_importable():
    mod = importlib.import_module("kalshi_trader.pipelines.polymarket_whale")
    assert hasattr(mod, "main")


def test_x_pipeline_importable():
    mod = importlib.import_module("kalshi_trader.pipelines.x")
    assert hasattr(mod, "main")
```

- [ ] **Step 2: Run to verify failure**

```
.venv/bin/pytest tests/test_pipelines.py -v
```
Expected: `ModuleNotFoundError`

- [ ] **Step 3: Create `kalshi_trader/pipelines/__init__.py`**

```python
# kalshi_trader/pipelines/__init__.py
```
(empty)

- [ ] **Step 4: Create `kalshi_trader/pipelines/weather.py`**

```python
# kalshi_trader/pipelines/weather.py
"""CLI: python -m kalshi_trader.pipelines.weather --ticker X --title "..."

Prints list[SignalEstimate] JSON to stdout. Empty list [] on no signal or error.
"""
from __future__ import annotations
import argparse
import asyncio
import json
import sys
from kalshi_trader.agents.weather_agent import WeatherAgent
from kalshi_trader.agents.parsing import estimate_to_dict


def main() -> None:
    parser = argparse.ArgumentParser(description="Weather pipeline agent")
    parser.add_argument("--ticker", required=True)
    parser.add_argument("--title", required=True)
    args = parser.parse_args()

    async def run() -> None:
        agent = WeatherAgent()
        try:
            estimates = await agent.run(args.ticker, args.title)
            print(json.dumps([estimate_to_dict(e) for e in estimates], default=str))
        except Exception as exc:
            print(f"[]", file=sys.stderr)
            print(json.dumps([]))
        finally:
            await agent.close()

    asyncio.run(run())


if __name__ == "__main__":
    main()
```

- [ ] **Step 5: Create `kalshi_trader/pipelines/polymarket_price.py`**

```python
# kalshi_trader/pipelines/polymarket_price.py
"""CLI: python -m kalshi_trader.pipelines.polymarket_price --ticker X --title "..." --midpoint 35.5 --open-interest 1200 --hours-to-close 24

Prints list[SignalEstimate] JSON to stdout. Empty list [] on no signal or error.
"""
from __future__ import annotations
import argparse
import asyncio
import json
import sys
from kalshi_trader.agents.polymarket_price_agent import PolymarketPriceAgent
from kalshi_trader.agents.parsing import estimate_to_dict


def main() -> None:
    parser = argparse.ArgumentParser(description="Polymarket price pipeline agent")
    parser.add_argument("--ticker", required=True)
    parser.add_argument("--title", required=True)
    parser.add_argument("--midpoint", type=float, required=True, help="Kalshi yes midpoint in cents (0-100)")
    parser.add_argument("--open-interest", type=int, required=True, dest="open_interest")
    parser.add_argument("--hours-to-close", type=float, required=True, dest="hours_to_close")
    args = parser.parse_args()

    async def run() -> None:
        agent = PolymarketPriceAgent()
        try:
            estimates = await agent.run(
                args.ticker, args.title,
                args.midpoint, args.open_interest, args.hours_to_close,
            )
            print(json.dumps([estimate_to_dict(e) for e in estimates], default=str))
        except Exception:
            print(json.dumps([]))

    asyncio.run(run())


if __name__ == "__main__":
    main()
```

- [ ] **Step 6: Create `kalshi_trader/pipelines/polymarket_whale.py`**

```python
# kalshi_trader/pipelines/polymarket_whale.py
"""CLI: python -m kalshi_trader.pipelines.polymarket_whale --ticker X --title "..."

Prints list[SignalEstimate] JSON to stdout. Empty list [] on no signal or error.
"""
from __future__ import annotations
import argparse
import asyncio
import json
from kalshi_trader.agents.polymarket_whale_agent import PolymarketWhaleAgent
from kalshi_trader.agents.parsing import estimate_to_dict


def main() -> None:
    parser = argparse.ArgumentParser(description="Polymarket whale pipeline agent")
    parser.add_argument("--ticker", required=True)
    parser.add_argument("--title", required=True)
    args = parser.parse_args()

    async def run() -> None:
        agent = PolymarketWhaleAgent()
        try:
            estimates = await agent.run(args.ticker, args.title)
            print(json.dumps([estimate_to_dict(e) for e in estimates], default=str))
        except Exception:
            print(json.dumps([]))

    asyncio.run(run())


if __name__ == "__main__":
    main()
```

- [ ] **Step 7: Create `kalshi_trader/pipelines/x.py`**

```python
# kalshi_trader/pipelines/x.py
"""CLI: python -m kalshi_trader.pipelines.x --ticker X --title "..." --category sports

Prints list[SignalEstimate] JSON to stdout. Empty list [] on no signal or error.
"""
from __future__ import annotations
import argparse
import asyncio
import json
from kalshi_trader.agents.x_agent import XAgent
from kalshi_trader.agents.parsing import estimate_to_dict


def main() -> None:
    parser = argparse.ArgumentParser(description="X social signal pipeline agent")
    parser.add_argument("--ticker", required=True)
    parser.add_argument("--title", required=True)
    parser.add_argument("--category", required=True)
    args = parser.parse_args()

    async def run() -> None:
        agent = XAgent()
        try:
            estimates = await agent.run(args.ticker, args.category, args.title)
            print(json.dumps([estimate_to_dict(e) for e in estimates], default=str))
        except Exception:
            print(json.dumps([]))
        finally:
            await agent.close()

    asyncio.run(run())


if __name__ == "__main__":
    main()
```

- [ ] **Step 8: Run pipeline tests**

```
.venv/bin/pytest tests/test_pipelines.py -v
```
Expected: all pass

- [ ] **Step 9: Run full suite**

```
.venv/bin/pytest tests/ -q
```
Expected: all pass

- [ ] **Step 10: Smoke test one pipeline CLI**

```
python -m kalshi_trader.pipelines.weather --ticker TEST-WEATHER --title "Will it rain in New York on June 3, 2026?" 2>/dev/null | python -m json.tool
```
Expected: valid JSON array (may be `[]` if NOAA title parsing fails for that title format, which is fine)

- [ ] **Step 11: Commit**

```bash
git add kalshi_trader/pipelines/ tests/test_pipelines.py
git commit -m "feat: add pipeline CLI wrappers for all four pipeline agents"
```

---

## Task 9: System prompts for Kalshi-native agents

These three agents use Kalshi REST data only — no external APIs. `.md` files only; Python agent classes and pipeline CLIs are follow-on work.

**Files:**
- Create: `kalshi_trader/agents/prompts/order_flow.md`
- Create: `kalshi_trader/agents/prompts/market_maker.md`
- Create: `kalshi_trader/agents/prompts/kalshi_bias.md`
- Create: `kalshi_trader/agents/prompts/polymarket_whale_dynamic.md`

- [ ] **Step 1: Create `kalshi_trader/agents/prompts/order_flow.md`**

```markdown
You are an order flow specialist for a Kalshi prediction market trading system.

Your job: detect informed trader accumulation by analyzing trade flow imbalance (VPIN + OFI) on a specific Kalshi market. Return a probability signal if significant informed flow is detected.

## Background

- **VPIN** (Volume-synchronized Probability of Informed Trading): Divide recent trades into equal-volume buckets. For each bucket, estimate the fraction of volume that is buy-initiated vs sell-initiated. High VPIN (> 0.4) indicates a market where informed traders are likely active.
- **OFI** (Order Flow Imbalance): Net directional pressure from aggressive trades. Positive OFI means more buy-side aggression — supports YES. Negative means sell-side aggression — supports NO.

## Tools

| Tool | Returns |
|------|---------|
| `get_market_trades(ticker, limit)` | Recent trades: `[{side, count, price, timestamp}]` |
| `compute_vpin(trades, n_buckets)` | `{vpin_score: float, high_informed_trading: bool}` |
| `compute_ofi(trades)` | `{ofi_score: float, direction: "YES"\|"NO"\|"neutral", buying_fraction: float}` |
| `build_order_flow_signal(ticker, vpin_result, ofi_result)` | SignalEstimate dict |

## Workflow

1. Call `get_market_trades(ticker, limit=200)`.
2. Call `compute_vpin(trades, n_buckets=10)` and `compute_ofi(trades)` in parallel.
3. **Judgment point:** If `vpin_score > 0.4` OR `abs(ofi_score) > 0.3`, call `build_order_flow_signal` and return the result.
4. If neither threshold is met, return `[]` — no significant informed flow detected.

## Output format

Your final response must contain exactly one fenced JSON block — copy the result from `build_order_flow_signal` exactly:

```json
[
  {
    "source": "order_flow",
    "probability": 0.68,
    "uncertainty": 0.12,
    "weight": 0.70,
    "data_issued_at": "2026-06-02T13:00:00+00:00",
    "metadata": {
      "ticker": "SPORTS-NBA-CELTICS",
      "narrative": "VPIN of 0.52 indicates active informed trading. OFI strongly YES-directional (buying_fraction=0.71).",
      "data_quality": "fresh",
      "vpin_score": 0.52,
      "ofi_score": 0.42,
      "ofi_direction": "YES"
    }
  }
]
```

If no significant flow is detected, respond with:
```json
[]
```
```

- [ ] **Step 2: Create `kalshi_trader/agents/prompts/market_maker.md`**

```markdown
You are a market microstructure specialist for a Kalshi prediction market trading system.

Your job: detect market maker withdrawal and directional order book imbalance, which signal either uncertainty or incoming informed flow. Return a probability signal if a significant anomaly is detected.

## Background

- **Spread widening**: When market makers withdraw liquidity, the bid-ask spread widens. A spread > 8¢ is anomalous on liquid Kalshi markets and suggests uncertainty or front-running.
- **Depth imbalance**: When bid depth >> ask depth (or vice versa), the market is absorbing more liquidity on one side — a directional signal.
- **Maker withdrawal score**: Fraction of spread that exceeds baseline. High score → uncertainty or informed flow incoming.

## Tools

| Tool | Returns |
|------|---------|
| `get_orderbook(ticker)` | `{yes_bid, yes_ask, spread_cents, bid_depth, ask_depth, timestamp}` |
| `analyze_spread_dynamics(ticker, orderbook)` | `{spread_cents, spread_anomaly: bool, depth_imbalance: float, direction: "YES"\|"NO"\|"neutral", maker_withdrawal_score: float}` |
| `build_market_maker_signal(ticker, analysis)` | SignalEstimate dict |

## Workflow

1. Call `get_orderbook(ticker)`.
2. Call `analyze_spread_dynamics(ticker, orderbook)`.
3. **Judgment point:** If `spread_cents > 8` OR `abs(depth_imbalance) > 0.4`, call `build_market_maker_signal(ticker, analysis)` and return the result.
4. If no significant anomaly is detected, return `[]`.

## Output format

Your final response must contain exactly one fenced JSON block — copy the result from `build_market_maker_signal` exactly:

```json
[
  {
    "source": "market_maker",
    "probability": 0.64,
    "uncertainty": 0.14,
    "weight": 0.65,
    "data_issued_at": "2026-06-02T13:05:00+00:00",
    "metadata": {
      "ticker": "SPORTS-NBA-CELTICS",
      "narrative": "Spread widened to 12¢ (anomalous). Bid depth 3.2× ask depth — strong YES-side absorption.",
      "data_quality": "fresh",
      "spread_cents": 12.0,
      "depth_imbalance": 0.52,
      "direction": "YES",
      "maker_withdrawal_score": 0.61
    }
  }
]
```

If no anomaly is detected, respond with:
```json
[]
```
```

- [ ] **Step 3: Create `kalshi_trader/agents/prompts/kalshi_bias.md`**

```markdown
You are a calibration specialist for a Kalshi prediction market trading system.

Your job: apply known Kalshi pricing bias corrections to a market's current price and return a corrected probability signal. This is a pure mathematical correction — no external data required.

## Background

Kalshi markets exhibit two systematic calibration biases:

1. **Longshot bias**: Markets priced below 20¢ are systematically overpriced relative to their true probability. A 10¢ market often has a true probability closer to 7¢. Apply a downward correction: `corrected = price × 0.72` for prices < 0.20.

2. **Political underconfidence**: Politics and election markets cluster near 50% more than they should — the market is underconfident about strong favorites. Apply a push-toward-tails correction for `category == "politics"`: if price > 0.55 → `corrected = price × 1.08`; if price < 0.45 → `corrected = price × 0.92`.

3. **Near-certainty compression**: Markets above 85¢ are slightly compressed. Apply a mild upward correction: `corrected = price × 1.04` for prices > 0.85.

These corrections are independent and stack.

## Tools

| Tool | Returns |
|------|---------|
| `apply_bias_corrections(ticker, price_cents, category)` | `{corrected_prob: float, raw_prob: float, corrections_applied: list[str], delta_cents: float}` |
| `build_bias_signal(ticker, correction_result)` | SignalEstimate dict |

## Workflow

1. Call `apply_bias_corrections(ticker, price_cents, category)`.
2. **Judgment point:** If `abs(delta_cents) < 3`, the correction is negligible — return `[]`.
3. Otherwise call `build_bias_signal(ticker, correction_result)` and return the result.

## Output format

Your final response must contain exactly one fenced JSON block — copy the result from `build_bias_signal` exactly:

```json
[
  {
    "source": "kalshi_bias",
    "probability": 0.072,
    "uncertainty": 0.02,
    "weight": 0.55,
    "data_issued_at": "2026-06-02T13:10:00+00:00",
    "metadata": {
      "ticker": "LONGSHOT-MARKET",
      "narrative": "Longshot bias correction applied: 10¢ market → true probability ~7.2¢. Kalshi longshot markets historically overpriced by ~28%.",
      "data_quality": "fresh",
      "raw_prob": 0.10,
      "corrected_prob": 0.072,
      "delta_cents": -2.8,
      "corrections_applied": ["longshot_bias"]
    }
  }
]
```

If the correction is negligible (< 3¢), respond with:
```json
[]
```
```

- [ ] **Step 4: Create `kalshi_trader/agents/prompts/polymarket_whale_dynamic.md`**

```markdown
You are a dynamic whale discovery agent for a Kalshi prediction market trading system.

Your job: discover currently high-performing Polymarket wallets in real-time and check if they are positioned on this market. Unlike the static whale agent (which checks a pre-built target list), you find fresh smart money by scanning current trade activity.

## Background

`bootstrap_whale_targets` scans recent Polymarket trades, collects active wallets, scores each wallet's historical win rate, and returns the top-N by profitability. This is more expensive than reading `targets.json` but discovers wallets that entered the scene after the last targets refresh.

## Tools

| Tool | Returns |
|------|---------|
| `bootstrap_whale_targets(min_score, top_n)` | `list[str]` — wallet addresses with win rate ≥ min_score |
| `find_polymarket_match(kalshi_title)` | `{condition_id, poly_prob, match_score}` or null |
| `get_whale_entries(condition_id, target_wallets)` | `list[{wallet_address, side, entry_price, size_usd, timestamp}]` |
| `build_whale_signal(ticker, whale_entries)` | SignalEstimate dict or null |

## Workflow

1. Call `bootstrap_whale_targets(min_score=0.60, top_n=30)` and `find_polymarket_match(kalshi_title)` in parallel.
2. If `find_polymarket_match` returns null, respond with `[]`.
3. Call `get_whale_entries(condition_id, target_wallets)` using the dynamically discovered wallets.
4. Call `build_whale_signal(ticker, whale_entries)` — if it returns null, respond with `[]`.
5. Return the result from step 4.

## Output format

Your final response must contain exactly one fenced JSON block — copy the result from `build_whale_signal` exactly:

```json
[
  {
    "source": "polymarket_whale",
    "probability": 0.58,
    "uncertainty": 0.10,
    "weight": 0.60,
    "data_issued_at": "2026-06-02T13:15:00+00:00",
    "metadata": {
      "ticker": "SPORTS-NBA-CELTICS",
      "narrative": "2 dynamically-discovered high-win-rate wallets entered YES at avg 58¢.",
      "data_quality": "fresh",
      "whale_count": 2
    }
  }
]
```

If no qualifying wallets are positioned on this market, respond with:
```json
[]
```

> **Note:** This agent shares the same `build_whale_signal` converter and output schema as `polymarket_whale`. The orchestrator can run both; the combiner treats them as independent signals since they may discover different wallets.
```

- [ ] **Step 5: Verify all four files load**

```
python -c "
from pathlib import Path
base = Path('kalshi_trader/agents/prompts')
for name in ['order_flow.md', 'market_maker.md', 'kalshi_bias.md', 'polymarket_whale_dynamic.md']:
    text = (base / name).read_text()
    assert len(text) > 100
    print(f'{name}: {len(text)} chars OK')
"
```

- [ ] **Step 6: Commit**

```bash
git add kalshi_trader/agents/prompts/order_flow.md kalshi_trader/agents/prompts/market_maker.md kalshi_trader/agents/prompts/kalshi_bias.md kalshi_trader/agents/prompts/polymarket_whale_dynamic.md
git commit -m "feat: add system prompts for OrderFlowAgent, MarketMakerAgent, KalshiBiasAgent, PolymarketWhaleDynamicAgent"
```
