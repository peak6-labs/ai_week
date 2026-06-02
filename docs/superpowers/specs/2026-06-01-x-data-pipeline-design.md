# X Social Signal Pipeline — Design Spec

**Date:** 2026-06-01
**Project:** Kalshi Agentic Trading System
**Scope:** Python tools for a Claude agent to gather edge on Kalshi markets via live X (Twitter) data using the xAI/Grok API

---

## 1. Goal

Build a set of Python tools, callable via the Claude API `tool_use` interface, that give a Claude agent social signal from X for any Kalshi market category. Grok handles live X search and initial analysis; Claude owns the final probability estimate when confidence is low. All output is `SignalEstimate` objects — compatible with the weather agent and any future signal source.

---

## 2. Architecture

```
kalshi_trader/
  external/
    x_client.py       # xAI API wrapper — live search + completions
    x_strategies.py   # Strategy classes + category registry
  agents/
    x_agent.py        # Claude tools: search_x_signal, override_x_strategies
```

`models.py` requires no changes — `SignalEstimate.metadata` carries all X-specific extras.

---

## 3. Strategy Registry

Each market category maps to an ordered list of strategies. Order determines priority when results are combined.

```python
CATEGORY_STRATEGIES: dict[str, list[type[BaseXStrategy]]] = {
    "weather":  [ExpertOpinionStrategy, NewsDetectionStrategy],
    "mentions": [BuzzStrategy, SentimentStrategy],
    "politics": [SentimentStrategy, NewsDetectionStrategy],
    "sports":   [NewsDetectionStrategy, SentimentStrategy],
}
```

Adding a new category requires one line in this dict — no other code changes.

---

## 4. Strategy Classes

All strategies inherit from `BaseXStrategy`:

```python
class BaseXStrategy:
    source_tag: str                         # e.g. "x_grok_sentiment"
    def build_query(self, market_title: str) -> str: ...
    async def run(self, market_title: str, client: XClient) -> list[SignalEstimate]: ...
```

### Strategies

| Class | `source_tag` | What it searches for |
|---|---|---|
| `SentimentStrategy` | `x_grok_sentiment` | General crowd opinion on the market question |
| `NewsDetectionStrategy` | `x_grok_news` | Breaking news or events relevant to the market |
| `ExpertOpinionStrategy` | `x_grok_experts` | Domain experts (meteorologists, analysts, etc.) posting probabilistic takes |
| `BuzzStrategy` | `x_grok_buzz` | Post volume and velocity — are people talking about this at all? |

---

## 5. Data Flow

```
Claude calls search_x_signal(ticker, category, market_title)
  │
  ├── Look up default strategies from CATEGORY_STRATEGIES[category]
  │
  ├── Run all strategies in parallel (semaphore: max 3 concurrent)
  │   │
  │   └── Per strategy:
  │       1. build_query(market_title) → search string
  │       2. XClient.live_search(query) → Grok summary + estimate + metadata
  │       3. If Grok uncertainty > 0.15:
  │             pass summary to Claude → second-pass SignalEstimate
  │       4. Return 1–2 SignalEstimate objects
  │
  └── Return combined list[SignalEstimate] to Claude
```

---

## 6. XClient

Thin wrapper around `api.x.ai`. One method:

```python
class XClient:
    async def live_search(self, query: str) -> GrokSearchResult: ...
```

The Claude second-pass (when uncertainty > 0.15) is handled by `x_agent.py` using the existing Anthropic SDK — not by `XClient`. `XClient` only wraps xAI.

`GrokSearchResult` is a typed dict:

```python
class GrokSearchResult(TypedDict):
    probability: float        # Grok's estimate, 0.0–1.0
    uncertainty: float        # Grok's stated confidence band
    summary: str              # Prose summary of what X is saying
    key_quotes: list[str]     # 2–3 representative posts
    sentiment_breakdown: dict # {"positive": 0.6, "negative": 0.2, "neutral": 0.2}
    source_quality: dict      # {"high_follower": 0.4, "general": 0.6}
    velocity: dict            # {"1h": 12, "6h": 47, "24h": 180} post counts
    key_entities: list[str]   # Named people, teams, events
    contrarian_signal: str    # Notable minority view, or "" if none
    issued_at: str            # ISO timestamp from Grok response
```

---

## 7. SignalEstimate Mapping

```python
SignalEstimate(
    source=strategy.source_tag,           # e.g. "x_grok_sentiment"
    probability=result["probability"],
    uncertainty=result["uncertainty"],
    weight=0.6,                           # xAI Grok baseline weight
    data_issued_at=parse(result["issued_at"]),
    metadata={
        "summary": result["summary"],
        "key_quotes": result["key_quotes"],
        "sentiment_breakdown": result["sentiment_breakdown"],
        "source_quality": result["source_quality"],
        "velocity": result["velocity"],
        "key_entities": result["key_entities"],
        "contrarian_signal": result["contrarian_signal"],
    }
)
```

When Claude produces a second-pass estimate, `source` is set to `"x_claude_<strategy_type>"` and `weight` is `0.75` (Claude's reasoning over Grok's raw estimate).

---

## 8. Claude Tools

### `search_x_signal`

```python
{
  "name": "search_x_signal",
  "description": "Search X for social signal on a Kalshi market. Returns SignalEstimate list from all default strategies for the market category.",
  "input_schema": {
    "ticker": str,
    "category": str,       # "weather" | "politics" | "sports" | "mentions" | ...
    "market_title": str
  }
}
```

### `override_x_strategies`

```python
{
  "name": "override_x_strategies",
  "description": "Run specific X search strategies instead of the category defaults. Use when the market is ambiguous or you want additional signal types.",
  "input_schema": {
    "ticker": str,
    "market_title": str,
    "strategies": list[str]  # e.g. ["sentiment", "experts", "news"]
  }
}
```

---

## 9. Error Handling

- Network failures and xAI API errors return an empty list — a missing X signal never blocks a trade decision.
- Unknown category falls back to `["mentions"]` strategies (broadest signal).
- Semaphore (max 3 concurrent xAI calls) prevents rate limit exhaustion across parallel strategies.
- `XAI_API_KEY` absence raises `ConfigurationError` at agent instantiation, not at call time.

---

## 10. Testing

| Test type | File | Notes |
|---|---|---|
| Unit — query builders | `tests/test_x_strategies.py` | No API calls; verify search strings per strategy + category |
| Unit — agent tools | `tests/test_x_agent.py` | Mock `XClient`; verify routing, escalation logic, empty-list fallback |
| Integration | `tests/test_x_integration.py` | Requires `XAI_API_KEY`; skipped in CI; verifies live search parses cleanly |

---

## 11. Configuration

Add to `config.py`:

```python
XAI_API_KEY = os.environ.get("XAI_API_KEY", "")
XAI_BASE_URL = "https://api.x.ai/v1"
XAI_MODEL = "grok-3"
X_GROK_UNCERTAINTY_THRESHOLD = 0.15   # escalate to Claude above this
X_MAX_CONCURRENT_SEARCHES = 3
X_GROK_SIGNAL_WEIGHT = 0.6
X_CLAUDE_SIGNAL_WEIGHT = 0.75
```
