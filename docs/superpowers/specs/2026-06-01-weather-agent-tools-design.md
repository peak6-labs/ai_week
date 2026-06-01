# Weather Market Agent Tools — Design Spec

**Date:** 2026-06-01  
**Project:** Kalshi Agentic Trading System  
**Scope:** Python tools for a Claude agent to gather edge on Kalshi weather markets

---

## 1. Goal

Build a set of Python tools, callable via the Claude API `tool_use` interface, that give a Claude agent everything it needs to identify mispriced Kalshi weather markets. All math and data parsing stays in Python. Claude's job is to decide which markets to investigate, which tools to call, and whether the resulting edge is worth trading.

This is the first of several specialist pipelines. The `SignalEstimate` data model is designed so future pipelines (mentions, cross-platform, sports) produce compatible output that a shared combining layer can aggregate.

---

## 2. Architecture

```
kalshi_trader/
  external/
    noaa.py           # NWS HTTP client — returns typed dicts, no Claude
    weather_parser.py # Regex market title → structured question
  agents/
    base.py           # Reusable tool-use loop for all specialist agents
    weather_agent.py  # Weather specialist: tools + system prompt + run()
```

`models.py` receives one new dataclass: `SignalEstimate`.

---

## 3. Data Model Addition

### `SignalEstimate` (added to `models.py`)

```python
@dataclass
class SignalEstimate:
    source: str             # e.g. "noaa_gfs", "nws_discussion", "polymarket"
    probability: float      # 0.0–1.0
    uncertainty: float      # ± band, e.g. 0.08 = ±8 percentage points
    weight: float           # source trustworthiness, 0.0–1.0
    data_issued_at: datetime  # timestamp from API response (not fetch time)
    metadata: dict          # source-specific extras, not used in math

    @property
    def staleness_minutes(self) -> float:
        """Always current — computed from stored timestamp, not baked in."""
        return (datetime.utcnow() - self.data_issued_at).total_seconds() / 60
```

`staleness_minutes` is a computed property so it reflects actual age at the moment it is read, not at fetch time. `weight` is fixed and reflects the source's inherent precision; `staleness_minutes` is the time dimension layered on top when combining.

---

## 4. External Clients

### `kalshi_trader/external/noaa.py`

NWS API client. Pure HTTP via `aiohttp`, no Claude. Exposes three async methods:

| Method | NWS endpoint | Returns |
|--------|-------------|---------|
| `get_forecast(lat, lon, date)` | `/points/{lat},{lon}` → `/gridpoints/{wfo}/{x},{y}/forecast` | `{temp_high, temp_low, precip_pct, wind_mph, short_forecast, generated_at}` |
| `get_hourly(lat, lon, date)` | Same grid → `/forecast/hourly` | `[{time, temp, precip_pct}]` for the target date |
| `get_discussion(lat, lon)` | `/products?type=AFD&...` | `{text, issuance_time}` — raw text, parsed by `weather_parser.py` |

`generated_at` / `issuance_time` are returned as `datetime` objects so callers can set `data_issued_at` on `SignalEstimate` directly.

### `kalshi_trader/external/weather_parser.py`

Two responsibilities:

1. **Market title parser** — regex patterns against known Kalshi weather market title formats. Extracts `{city, lat, lon, metric, threshold, operator, target_date}`. Returns `None` on no match (agent skips that market). Has a hardcoded `CITY_COORDS` lookup dict for common Kalshi market cities.

2. **Discussion parser** — extracts uncertainty-relevant sentences from NWS Area Forecast Discussion text. Returns `{confidence: "high"|"medium"|"low", key_points: list[str]}`. Rules: presence of "uncertain", "possible", "confidence" etc. map to confidence levels.

---

## 5. The Seven Tools

All tools are async Python functions. Each has a companion JSON schema dict for the Claude API `tools=` parameter.

| # | Name | Input | Python does | Returns to Claude |
|---|------|-------|-------------|-------------------|
| 1 | `list_weather_markets` | _(none)_ | Kalshi API, filter category=weather | `[{ticker, title, yes_price, volume_24h, hours_to_close}]` |
| 2 | `parse_weather_market` | `ticker, title` | `weather_parser.parse_title()` | `{city, lat, lon, metric, threshold, operator, target_date}` or `null` |
| 3 | `get_noaa_forecast` | `lat, lon, date` | `noaa.get_forecast()` | `{temp_high, temp_low, precip_pct, wind_mph, short_forecast, data_age_minutes}` |
| 4 | `estimate_probability` | `metric, threshold, operator, forecast` | Gaussian (temp) or direct pass-through (precip) | `SignalEstimate` as dict |
| 5 | `get_nws_discussion` | `lat, lon` | `noaa.get_discussion()` + `weather_parser.parse_discussion()` | `SignalEstimate` as dict (lower weight than gridded forecast) |
| 6 | `combine_signals` | `estimates: list[dict]` | Staleness-discounted weighted average | `{combined_probability, uncertainty, staleness_minutes, n_sources}` |
| 7 | `calculate_edge` | `combined_probability, market_price_cents` | Arithmetic + Kalshi fee formula | `{edge_cents, fee_adjusted_edge, worth_trading: bool}` |

### Probability estimation detail (Tool 4)

- **Temperature (above/below threshold):** Fit a normal distribution with `mean = (temp_high + temp_low) / 2` and `std = (temp_high - temp_low) / 4`. Use `scipy.stats.norm.sf` (or `cdf`) for the tail probability.
- **Precipitation:** `precip_pct / 100` directly. Uncertainty is fixed at `±0.05` (NWS precip probabilities are rounded to 10% increments).
- `weight` defaults: `noaa_gfs = 0.85`, `nws_discussion = 0.40`.

### Combining detail (Tool 6)

Staleness discount: `effective_weight = weight × exp(-staleness_minutes / 360)` (half-weight at 6 hours). Weighted average of `probability` values; combined `uncertainty` is the weighted average of individual uncertainties plus a small disagreement penalty if estimates diverge by more than 0.10.

### Edge calculation detail (Tool 7)

```
edge_cents = combined_probability * 100 - market_price_cents
fee_cents  = 0.07 * (market_price_cents/100) * (1 - market_price_cents/100) * 100
fee_adjusted_edge = edge_cents - fee_cents
worth_trading = fee_adjusted_edge > 5  # 5-cent minimum after fees
```

---

## 6. Agent

### `kalshi_trader/agents/base.py`

Reusable tool-use loop. Constructor takes `model`, `tools` (list of schema dicts), `handlers` (dict mapping tool name → async callable), and `system_prompt`. `run(user_message) -> str` loops: call Claude API, dispatch tool calls to handlers, feed results back, repeat until `stop_reason == "end_turn"`.

### `kalshi_trader/agents/weather_agent.py`

```python
class WeatherAgent:
    async def run(self, markets: list[Market] | None = None) -> list[TradeIdea]:
        ...
```

- `markets` is optional. If provided, the agent skips calling `list_weather_markets` and analyzes the given markets directly. This allows external market selectors, screeners, or the coordinator to pre-filter and feed markets in.
- If `markets=None`, the agent calls `list_weather_markets()` itself as the first tool call.
- System prompt instructs Claude to: prioritize markets with >$2,000 open interest, skip markets closing in <4 hours, call `combine_signals` only after gathering at least one `estimate_probability` result, and emit a `TradeIdea` for each market where `worth_trading=True`.
- The system prompt instructs Claude to end its response with a fenced ````json` block containing a list of trade ideas, each with keys `ticker`, `side`, `confidence`, `market_price`, `reasoning`, `signal_sources`. `WeatherAgent.run()` extracts and parses that block into `TradeIdea` objects.

---

## 7. What Future Pipelines Plug Into

Any future specialist pipeline (mentions, Polymarket, sports) must:
1. Produce `SignalEstimate` objects with a `data_issued_at` datetime from the source API
2. Expose an `async run(markets: list[Market] | None = None) -> list[TradeIdea]` interface

A future coordinator can collect estimates from multiple agents on the same market ticker, call `combine_signals` with all of them, and compute a single `fee_adjusted_edge` that reflects all available data.

---

## 8. Out of Scope

- WebSocket / real-time streaming (future)
- Historical backtesting of NOAA forecast accuracy (future)
- Mentions markets, sports, cross-platform tools (separate specs)
- Coordinator agent (separate spec)
- Overnight scheduling / APScheduler integration (separate spec)
