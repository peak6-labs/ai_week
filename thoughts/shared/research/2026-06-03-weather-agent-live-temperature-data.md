---
date: 2026-06-03T09:02:03-05:00
researcher: Alexandra Lewis
git_commit: 4cefa83b6c458f85a39d92674ac97c1b100d2da1
branch: main
repository: ai_week
topic: "Does the weather agent have live temperature data?"
tags: [research, codebase, weather-signal, noaa, nws, signals]
status: complete
last_updated: 2026-06-03
last_updated_by: Alexandra Lewis
---

# Research: Does the weather agent have live temperature data?

**Date**: 2026-06-03 09:02:03 CDT
**Researcher**: Alexandra Lewis
**Git Commit**: 4cefa83b6c458f85a39d92674ac97c1b100d2da1
**Branch**: main
**Repository**: ai_week

## Research Question
Does the weather agent have live temperature data?

## Summary
Yes. The weather agent fetches **live** temperature data at run time from the
**National Weather Service public API** (`https://api.weather.gov`), over real
HTTP requests made through `aiohttp`. There is no mocked, cached-to-disk, or
randomly-generated temperature data in the production path — each invocation
issues fresh GET requests and parses the returned forecast periods for the daily
high and low temperature.

Three nuances are worth documenting precisely (all describe what exists, not
problems):

1. **It is the NWS gridpoint forecast API, not the raw GFS model.** The
   `SignalEstimate` is branded `source="noaa_gfs"` and the narrative text says
   "NOAA GFS", but the actual data is pulled from `api.weather.gov` gridpoint
   forecasts (which NWS derives from GFS and other models). The code never
   queries NOMADS / the GFS grib files directly.
2. **There is a hardcoded fallback for missing temperatures.** If a live
   forecast period is fetched but its temperature fields come back `None`, the
   signal builder substitutes literal defaults of `85.0°F` high / `65.0°F` low
   ([signals/weather.py:37-38](kalshi_trader/signals/weather.py#L37-L38)). The
   data is live; the constants only apply when the live response lacks the
   field.
3. **The freshness of the live data is measured and labeled.** The forecast's
   `generatedAt` timestamp drives a `data_age_minutes` value that is labeled
   `fresh` (<60 min), `stale` (<360 min), or `unavailable` (≥360 min).

## Detailed Findings

### The weather agent and its entry point

The weather agent is a signal-agent defined as a Claude Code subagent:

- **Agent definition**: [.claude/agents/weather-signal.md](.claude/agents/weather-signal.md)
  - Description: "Fetches NOAA GFS forecast for a Kalshi weather market and
    returns a probability signal." ([weather-signal.md:3-6](.claude/agents/weather-signal.md#L3-L6))
  - It is read-only and only runs the pipeline CLI; it never places orders
    ([weather-signal.md:20-23](.claude/agents/weather-signal.md#L20-L23)).
  - It invokes the CLI:
    `python -m kalshi_trader.pipelines.weather --ticker TICKER --title "TITLE"`
    ([weather-signal.md:38-42](.claude/agents/weather-signal.md#L38-L42)).
  - Note (documentary): the agent file's `allowedTools` and workflow hardcode
    the repo root as `/Users/scorley/code`
    ([weather-signal.md:9-10](.claude/agents/weather-signal.md#L9-L10),
    [weather-signal.md:34-42](.claude/agents/weather-signal.md#L34-L42)), which
    is a different home directory than this checkout (`/Users/llewis/ai_week`).

- **CLI entry point**: [kalshi_trader/pipelines/weather.py](kalshi_trader/pipelines/weather.py)
  - `main()` parses `--ticker` / `--title`, constructs a `WeatherAgent`, runs
    it, and prints the resulting `SignalEstimate` list as JSON
    ([weather.py:15-32](kalshi_trader/pipelines/weather.py#L15-L32)).
  - On any exception it prints `[]` to stdout and the error to stderr
    ([weather.py:26-28](kalshi_trader/pipelines/weather.py#L26-L28)).

### The agent orchestration layer

[kalshi_trader/agents/weather_agent.py](kalshi_trader/agents/weather_agent.py)
defines `WeatherAgent`, an LLM tool-use agent (`BaseAgent`) with four tools
([weather_agent.py:18-72](kalshi_trader/agents/weather_agent.py#L18-L72)):

1. `parse_weather_market` → `parse_title()` — turns the title into
   `(city, lat, lon, metric, threshold, operator, target_date)`.
2. `get_noaa_forecast` → `_get_noaa_forecast()` — fetches the live forecast.
3. `get_nws_discussion` → `_get_nws_discussion()` — fetches the Area Forecast
   Discussion (instructed to be called when precip is 30–70%).
4. `build_weather_signal` → converts the forecast dict to a `SignalEstimate`.

`_get_noaa_forecast` ([weather_agent.py:98-112](kalshi_trader/agents/weather_agent.py#L98-L112))
calls the live client `NOAAClient.get_forecast(...)`, then computes
`data_age_minutes` as `now − generatedAt`. It returns `temp_high`, `temp_low`,
`precip_pct`, `wind_mph`, `short_forecast`, and `data_age_minutes`.

The `NOAAClient` is constructed in `WeatherAgent.__init__`
([weather_agent.py:76-77](kalshi_trader/agents/weather_agent.py#L76-L77)) and
closed in `close()` ([weather_agent.py:134-135](kalshi_trader/agents/weather_agent.py#L134-L135)).

### The live HTTP data source — this is the core of the answer

[kalshi_trader/external/noaa.py](kalshi_trader/external/noaa.py) is where the
real network calls happen.

- **Base host**: `NWS_BASE = "https://api.weather.gov"`
  ([noaa.py:7](kalshi_trader/external/noaa.py#L7)).
- **User-Agent header** identifies the client to NWS
  ([noaa.py:8](kalshi_trader/external/noaa.py#L8)).
- **`_get(url)`** ([noaa.py:40-46](kalshi_trader/external/noaa.py#L40-L46))
  lazily creates an `aiohttp.ClientSession` and issues a real GET with a
  10-second total timeout, raising on non-2xx and returning parsed JSON. This is
  a genuine live request, not a stub.
- **`_grid(lat, lon)`** ([noaa.py:48-58](kalshi_trader/external/noaa.py#L48-L58))
  GETs `…/points/{lat},{lon}` to resolve the gridpoint's `forecast` URL,
  `forecastHourly` URL, and weather forecast office (`cwa`). Results are cached
  **in memory for the lifetime of the client instance** (`self._grid_cache`),
  not persisted.
- **`get_forecast(lat, lon, target_date)`** ([noaa.py:60-93](kalshi_trader/external/noaa.py#L60-L93))
  GETs the resolved `forecast_url`, reads `generatedAt`, and iterates the
  forecast `periods`. For periods matching `target_date`:
  - daytime period → `temp_high = float(period["temperature"])`, precip, wind,
    short forecast ([noaa.py:78-82](kalshi_trader/external/noaa.py#L78-L82)).
  - nighttime period → `temp_low = float(period["temperature"])`
    ([noaa.py:83-84](kalshi_trader/external/noaa.py#L83-L84)).
  Returns `temp_high`, `temp_low`, `precip_pct`, `wind_mph`, `short_forecast`,
  `generated_at`.
- **`get_discussion(lat, lon)`** ([noaa.py:95-105](kalshi_trader/external/noaa.py#L95-L105))
  GETs `…/products?type=AFD&location={wfo}` then the first product's text. If no
  products are returned, it falls back to empty text with the current time.
- **TLS handling**: `_build_ssl_context()` uses `truststore` so the NWS cert
  chain validates behind the corporate (Zscaler) proxy, falling back to the
  default context if `truststore` is unavailable
  ([noaa.py:11-24](kalshi_trader/external/noaa.py#L11-L24)).

There are **no** `requests`/`httpx`/`urllib` calls to any other weather source,
no hardcoded temperature payloads, and no random number generation in the
fetch path.

### Converting live temperatures into a probability

[kalshi_trader/signals/weather.py](kalshi_trader/signals/weather.py) —
`build_weather_signal(...)` ([weather.py:12-103](kalshi_trader/signals/weather.py#L12-L103)):

- For `temp_high` / `temp_low` metrics, it reads the live `temp_high` /
  `temp_low`. **Fallback constants** apply only if those are `None`:
  `high = … else 85.0`, `low = … else 65.0`
  ([weather.py:37-38](kalshi_trader/signals/weather.py#L37-L38)).
- It centers a normal distribution on the metric being traded (the high for
  `temp_high`, the low for `temp_low`), with
  `std = max((high - low) / 6.0, 2.0)` as a forecast-error proxy
  ([weather.py:45-47](kalshi_trader/signals/weather.py#L45-L47)).
- Probability is `dist.sf(threshold)` for "above" or `dist.cdf(threshold)` for
  "below", clamped to `[0.01, 0.99]`
  ([weather.py:48-56](kalshi_trader/signals/weather.py#L48-L56)).
- For `precipitation`, probability is `precip_pct / 100.0`
  ([weather.py:50-52](kalshi_trader/signals/weather.py#L50-L52)).
- Freshness: `data_issued_at = now − data_age_minutes`; quality label is
  `fresh` (<60 min) / `stale` (<360 min) / `unavailable` (≥360 min)
  ([weather.py:58-67](kalshi_trader/signals/weather.py#L58-L67)).
- The emitted `SignalEstimate` has `source="noaa_gfs"`, a config-driven
  `uncertainty` and `weight`, and metadata including the narrative,
  `data_quality`, `forecast_model="noaa_gfs"`, and (when a discussion was
  fetched) `nws_confidence` / `key_uncertainty`
  ([weather.py:83-103](kalshi_trader/signals/weather.py#L83-L103)).

### How "live" plays out at run time

- Each pipeline invocation creates a fresh `WeatherAgent` → fresh `NOAAClient`
  → fresh HTTP session, so there is no cross-run caching of the temperature
  data; only the in-process grid lookup is cached for the duration of a single
  run.
- The `comment` in [signals/weather.py:43-44](kalshi_trader/signals/weather.py#L43-L44)
  notes the std-dev calibration is left to the paper-trade loop (referenced as
  issue #25).

## Code References
- `.claude/agents/weather-signal.md:3-6` — agent description ("Fetches NOAA GFS forecast…")
- `.claude/agents/weather-signal.md:34-42` — CLI command the agent runs
- `kalshi_trader/pipelines/weather.py:15-32` — CLI entry, JSON output, `[]` on error
- `kalshi_trader/agents/weather_agent.py:18-72` — tool schemas
- `kalshi_trader/agents/weather_agent.py:98-112` — `_get_noaa_forecast`, computes data age
- `kalshi_trader/external/noaa.py:7-8` — NWS base host + User-Agent
- `kalshi_trader/external/noaa.py:40-46` — live `aiohttp` GET with 10s timeout
- `kalshi_trader/external/noaa.py:48-58` — `…/points/{lat},{lon}` grid resolution + in-memory cache
- `kalshi_trader/external/noaa.py:60-93` — gridpoint forecast fetch; high from daytime, low from nighttime period
- `kalshi_trader/external/noaa.py:95-105` — Area Forecast Discussion fetch
- `kalshi_trader/external/noaa.py:11-24` — truststore SSL context for corporate proxy
- `kalshi_trader/signals/weather.py:37-38` — 85.0/65.0 fallback when live temps are None
- `kalshi_trader/signals/weather.py:45-56` — normal-distribution probability from forecast temps
- `kalshi_trader/signals/weather.py:58-67` — freshness / data_quality labeling

## Architecture Documentation
The weather signal follows the same shape as the other signal agents in this
repo: a Claude Code subagent definition (`.claude/agents/*.md`) wraps a Python
pipeline CLI (`kalshi_trader/pipelines/*.py`), which constructs an LLM tool-use
agent (`kalshi_trader/agents/*_agent.py`) backed by `BaseAgent`. External I/O is
isolated in `kalshi_trader/external/` (here, `NOAAClient`), and the
raw-data→`SignalEstimate` conversion lives in `kalshi_trader/signals/`. The
agent emits a uniform `SignalEstimate` JSON carrying a `data_issued_at`
timestamp, which downstream scoring uses for staleness decay.

## Historical Context (from thoughts/)
- `thoughts/shared/research/2026-06-03-agent-structure-trade-idea-evaluation.md`
  — documents that the weather-signal agent sources data from NOAA/NWS GFS,
  builds a probability with `scipy.stats.norm` around forecast data
  (`kalshi_trader/signals/weather.py:96-103`), is dispatched only for
  weather/climate-category markets in the deep-signal subset, and is run
  **live on-demand** in Step 2 of the orchestration pipeline (not pre-cached),
  with `data_issued_at` driving a ~6-hour staleness decay in the combiner.
- `thoughts/shared/research/2026-06-02-project-summary.md` — lists
  weather/climate as one of the scored market categories; focuses on the
  scanner/actionability layer rather than signal live-ness.

## Related Research
- `thoughts/shared/research/2026-06-03-agent-structure-trade-idea-evaluation.md`
- `thoughts/shared/research/2026-06-02-project-summary.md`

## Open Questions
- None for the core question. (Documentary note, not a critique: the
  `source`/`forecast_model` label is `noaa_gfs` while the actual endpoint is the
  NWS gridpoint forecast API, and the agent definition's repo paths point at
  `/Users/scorley/code` rather than this checkout.)

## Update (2026-06-03): probability now from the GEFS ensemble

The quantitative weather probability is no longer the parametric normal-CDF
proxy described above — it is now the **GEFS ensemble empirical CDF**. See the
plan `thoughts/shared/plans/2026-06-03-gefs-ensemble-weather-probability.md`.

What changed:
- New `OpenMeteoClient` ([kalshi_trader/external/open_meteo.py](kalshi_trader/external/open_meteo.py))
  fetches the 31-member GEFS daily ensemble from the free Open-Meteo Ensemble API
  (`https://ensemble-api.open-meteo.com/v1/ensemble`, `models=gfs_seamless`,
  `timezone=auto`), returning the per-member daily max/min (or precip) for the
  target local day.
- New `build_ensemble_signal` ([kalshi_trader/signals/weather.py](kalshi_trader/signals/weather.py))
  sets `probability = fraction of members past the threshold` (above → `>`, below
  → `<`, precip → `> 0.01"` when the threshold is 0), clamped to `[0.01, 0.99]`.
  Source is **`gfs_ensemble`** (weight `weight_ensemble=0.85`, uncertainty
  `uncertainty_ensemble_temp=0.07` / `_precip=0.05`). Below `ensemble_min_members`
  (10) it flags `data_quality="empty"` so the scorer drops it.
- The `WeatherAgent` is **ensemble-first**: it builds `gfs_ensemble` as the
  primary estimate and only falls back to the parametric `build_weather_signal`
  (`noaa_gfs`) when the ensemble is unavailable (Open-Meteo unreachable or date
  beyond the ~16-day horizon). The NWS gridpoint forecast and Area Forecast
  Discussion are **still fetched but are context only** (narrative + the precip
  discussion trigger) — they no longer set the probability.
- `data_issued_at` is stamped at build time (`now`); the GEFS forecast is treated
  as fresh each cycle (it updates on a 6-hour model cadence). `build_ensemble_signal`
  deliberately ignores any `data_issued_at` in the round-tripped ensemble dict
  because `BaseAgent` JSON-encodes tool results with `default=str`, which would
  otherwise deliver it back as a string.

Verified live (2026-06-03): `KXHIGHTCHI-26JUN05-T80` →
`source="gfs_ensemble"`, `member_count=31`, `probability=11/31=0.3548`; a
beyond-horizon date (`26JUN25`) correctly fell back to `source="noaa_gfs"`.

The "compare to market price, trade when edge > 5–8%" half of the methodology
already existed (`scripts/score_signals.py` `compute_edge_and_kelly`, default 5¢
bar) and was unchanged — this work only improved the model-implied probability
feeding it.

---

## Update (2026-06-03): same-day live-observation override

For **same-day** temperature markets the day's min/max has often already
occurred, so a stale GEFS run can be flatly contradicted by what was actually
observed. Validated live on three same-day band markets: the ensemble compresses
the diurnal range (ran warm on overnight lows in ATL/AUS, cool on the MSP high),
and the realized extreme decided the outcome.

The `gfs_ensemble` empirical CDF is now optionally **conditioned on the realized
extreme** (`build_ensemble_signal(..., observation, lock_fraction)`): each member
is clamped by monotonicity — `min(member, realized)` for `temp_low`,
`max(member, realized)` for `temp_high`/`precipitation` — and the fraction-in-band
is recomputed on the clamped members. Uncertainty collapses toward
`observation_uncertainty_floor` in proportion to `lock_fraction` (how far past the
climatological extreme hour we are). With no observation the signal is
byte-for-byte the pure ensemble.

The realized extreme is read off the contract's **settlement station**, resolved
from the series' `settlement_sources` (`kalshi_trader/external/weather_settlement.py`)
— never a guessed airport. Settlement is not uniform (LAX → NWS CLI report issued
by LAX; NYC → AccuWeather), so the override only runs when an NWS station resolves
and otherwise no-ops. New pieces: `NOAAClient.get_observed_extreme`,
`weather_settlement.resolve_settlement_station`, the `get_observed_extreme` agent
tool, and config keys `enable_observation_override` / `observation_uncertainty_floor`.

Live end-to-end (Jun 3 2026): ATL `B57.5` pure P 0.010 → clamped 0.990 (realized
57.2°F in band); AUS `B70.5` 0.774 → 0.010 (realized 69.8°F below band); MSP
`B85.5` 0.032 → 0.032 (realized 84.2°F still below the band, lock 0.83 — no
fabrication). Activation prerequisite: the series' settlement terms must be cached
(via `scripts/market_rules.py`); until then the override safely no-ops.

Plan: `thoughts/shared/plans/2026-06-03-live-observation-override-weather.md`.
