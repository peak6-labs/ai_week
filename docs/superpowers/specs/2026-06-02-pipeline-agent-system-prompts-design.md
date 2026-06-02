# Pipeline Agent System Prompts — Design Spec

**Date:** 2026-06-02  
**Status:** Approved  
**Context:** Agentic Kalshi trading system, Peak6/CapMan AI Immersion (June 1–5 2026)

---

## Overview

Each data pipeline agent is a Claude instance whose intelligence lives in a `.md` system prompt file. The Python layer provides HTTP clients, math, and tool-use loop plumbing — no decision logic. Agents are invoked by the orchestrator as tools (via pipeline CLI scripts) and return structured signals.

This spec covers the four initial pipeline agents. New agents follow the same pattern described in the [Extensibility](#extensibility) section.

---

## File Layout

```
kalshi_trader/
  agents/
    prompts/
      weather.md           ← this spec
      polymarket_price.md  ← this spec
      polymarket_whale.md  ← this spec
      x.md                 ← this spec
      orchestrator.md      ← future spec
  pipelines/
    weather.py             ← CLI wrapper, calls WeatherPipelineAgent
    polymarket_price.py    ← CLI wrapper, calls PolymarketPriceAgent
    polymarket_whale.py    ← CLI wrapper, calls PolymarketWhaleAgent
    x.py                   ← CLI wrapper, calls XPipelineAgent
```

Each pipeline CLI accepts `--ticker` and `--title` (plus `--category` for x), runs the agent, and prints `list[SignalEstimate]` JSON to stdout. Empty list `[]` on no signal or failure — missing signal never blocks.

---

## Shared Output Schema

All pipeline agents return a `list[SignalEstimate]` JSON block. Empty list is valid (no signal found).

```json
[
  {
    "source": "<string>",
    "probability": 0.0,
    "uncertainty": 0.0,
    "weight": 0.0,
    "data_issued_at": "<ISO datetime>",
    "metadata": {
      "ticker": "<string>",
      "narrative": "<string>",
      "data_quality": "fresh|stale|unavailable",
      "<agent-specific fields>": "..."
    }
  }
]
```

### Common metadata fields (all agents)

| Field | Type | Description |
|-------|------|-------------|
| `ticker` | `str` | Kalshi ticker for traceability |
| `narrative` | `str` | 1–2 sentence plain-English summary for the orchestrator |
| `data_quality` | `str` | `"fresh"` / `"stale"` / `"unavailable"` based on data age |

> These fields are the stable interface. Agent-specific fields may be added or changed as signals are tuned.

---

## Agent Designs

### 1. `weather.md`

**Role:** Analyze a single Kalshi weather market against NOAA/NWS forecast data and return a probability signal.

**Tools:**

| Tool | Returns |
|------|---------|
| `parse_weather_market(ticker, title)` | `{city, lat, lon, metric, threshold, operator, target_date}` or null |
| `get_noaa_forecast(lat, lon, date)` | `{temp_high, temp_low, precip_pct, wind_mph, short_forecast, data_age_minutes}` |
| `estimate_probability(metric, threshold, operator, forecast)` | `{probability, uncertainty, weight, data_issued_at}` |
| `get_nws_discussion(lat, lon)` | `{confidence, key_points, issued_at}` |
| `combine_signals(estimates)` | `{combined_probability, uncertainty, staleness_minutes, n_sources}` |

**Workflow:**

1. Call `parse_weather_market(ticker, title)` — if null, return `[]`
2. Call `get_noaa_forecast(lat, lon, target_date)`
3. Call `estimate_probability(metric, threshold, operator, forecast)`
4. **Judgment point:** If `precip_pct` is between 30–70% (ambiguous zone), also call `get_nws_discussion(lat, lon)`. Use its `confidence` as `nws_confidence` and first key point as `key_uncertainty` in metadata.
5. Set `data_quality`: age < 60 min → `"fresh"`, < 360 min → `"stale"`, else → `"unavailable"`
6. Return one `SignalEstimate` — `source="noaa_gfs"`, weight=0.85

**Additional metadata fields:**

| Field | Type | Description |
|-------|------|-------------|
| `forecast_model` | `str` | Always `"noaa_gfs"` for now |
| `nws_confidence` | `str` | `"high"` / `"medium"` / `"low"` — only present if `get_nws_discussion` was called |
| `key_uncertainty` | `str` | First key point from NWS discussion — only present if called |

---

### 2. `polymarket_price.md`

**Role:** Find the Kalshi market's counterpart on Polymarket and return the price gap as a signal. Does not look at whale activity.

**Tools:**

| Tool | Returns |
|------|---------|
| `get_poly_markets()` | `list[dict]` — active Polymarket markets |
| `match_kalshi_market(title, poly_markets)` | Best-matching Polymarket market dict or null |
| `score_market(ticker, kalshi_midpoint_cents, poly_prob)` | `{gap_cents, match_score}` or null |

**Workflow:**

1. Call `get_poly_markets()`
2. Call `match_kalshi_market(title, poly_markets)` — no match → return `[]`
3. Call `score_market(ticker, kalshi_midpoint, poly_prob)` — filtered out (gap < 7¢, OI < 500, hours out of range) → return `[]`
4. Return one `SignalEstimate` — `source="polymarket_price"`, `probability=poly_prob`, weight=0.75

**Additional metadata fields:**

| Field | Type | Description |
|-------|------|-------------|
| `gap_cents` | `float` | Polymarket price minus Kalshi midpoint, in cents |
| `match_score` | `float` | Fuzzy match confidence between the two markets (0–1) |

---

### 3. `polymarket_whale.md`

**Role:** Check whether tracked high-performing wallets are positioned on this market. Fully decoupled from price gap — reports activity regardless of gap size.

**Tools:**

| Tool | Returns |
|------|---------|
| `load_whale_targets()` | `list[str]` — target wallet addresses |
| `get_poly_markets()` | `list[dict]` — active Polymarket markets |
| `match_kalshi_market(title, poly_markets)` | Best-matching Polymarket market dict or null |
| `get_large_trades(condition_id)` | `list[dict]` — recent large trades |
| `detect_whale_entries(trades)` | `list[dict]` — `{wallet_address, side, size_usd, timestamp}` |

**Workflow:**

1. Call `load_whale_targets()` and `get_poly_markets()` in parallel
2. Call `match_kalshi_market(title, poly_markets)` — no match → return `[]`
3. Call `get_large_trades(condition_id)` then `detect_whale_entries(trades)`
4. Filter entries to those whose `wallet_address` is in target wallets
5. No target entries → return `[]`
6. `probability` = weighted average entry price of target whale trades (the price they actually paid — their revealed implied probability). This is independent of the Polymarket/Kalshi price gap.
7. Return one `SignalEstimate` — `source="polymarket_whale"`, weight=0.60

**Additional metadata fields:**

| Field | Type | Description |
|-------|------|-------------|
| `whale_count` | `int` | Number of target wallets with entries |

---

### 4. `x.md`

**Role:** Search X for social signal on a specific market using category-appropriate strategies and return probability estimates. Uses Claude's judgment for sentiment synthesis — no threshold-based rules.

**Tools:**

| Tool | Returns |
|------|---------|
| `search_x_signal(ticker, category, market_title)` | `list[SignalEstimate dicts]` — default strategies for the category |
| `override_x_strategies(ticker, market_title, strategies)` | `list[SignalEstimate dicts]` — specific strategies |

**Workflow:**

1. Call `search_x_signal(ticker, category, market_title)` — runs all default strategies for the category
2. **Judgment point A:** If all estimates have `uncertainty > 0.15`, also call `override_x_strategies(ticker, market_title, ["experts", "news"])` for higher-quality signal
3. **Judgment point B:** If estimates spread > 0.20 (contradictory signals), note in narrative as high-disagreement
4. Synthesize `sentiment_direction` and `sentiment_reasoning` qualitatively from the actual post summaries and content — this is Claude's judgment, not a threshold rule. The reasoning should explain what specific signals (accounts, themes, volume of posts, expert consensus, etc.) led to the assessment.
5. Add metadata to each estimate and return the full list — one `SignalEstimate` per strategy that produced a result

**Additional metadata fields:**

| Field | Type | Description |
|-------|------|-------------|
| `post_count` | `int` | Total relevant posts found across all strategies |
| `sentiment_direction` | `str` | Claude's qualitative assessment of direction — e.g. `"bullish"`, `"bearish"`, `"mixed"`, `"neutral"` |
| `sentiment_reasoning` | `str` | Explanation of what drove the sentiment assessment — which signals, accounts, or themes were most influential |
| `strategies_used` | `str` | Comma-separated names of strategies that ran |

---

## Extensibility

To add a new pipeline agent:

1. **Create `kalshi_trader/agents/prompts/<name>.md`** — follow the structure above: Role, Tools table, Workflow (numbered steps with judgment points annotated), Additional metadata fields
2. **Expose Python tools** — wrap existing client methods as async tool handler functions with JSON schemas; register them in the agent's Python class
3. **Create `kalshi_trader/pipelines/<name>.py`** — thin CLI wrapper: parse `--ticker`, `--title`, `[--category]` args → run agent → print JSON to stdout
4. **Add to orchestrator routing map** — in `agents/prompts/orchestrator.md`, add the new pipeline to the relevant market categories

The Python agent class should be minimal: load the `.md` file as system prompt, register tools, call `BaseAgent.run()`. No decision logic in Python.

---

## Notes

- All metadata fields are intentionally mutable — expect these to be tuned as signals are evaluated against outcomes
- `data_quality` thresholds (60 min / 360 min for weather) are starting points; adjust after observing data freshness in practice
- The `weight` values (0.85 weather, 0.75 polymarket_price, 0.60 polymarket_whale, variable for x) will be tuned once we have trade outcome data
- `polymarket_whale` is an independent positive signal — the probability is derived from where smart wallets actually entered, not from the Polymarket/Kalshi price gap. It can fire on markets where the price gap is small or nonexistent.
