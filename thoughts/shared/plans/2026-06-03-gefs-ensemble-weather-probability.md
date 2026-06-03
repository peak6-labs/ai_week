# GEFS Ensemble Weather Probability Implementation Plan

## Overview

Replace the weather signal's parametric normal-CDF probability with an
**ensemble empirical CDF**: pull the 31-member GEFS ensemble from the free
Open-Meteo Ensemble API, and for a temperature/precipitation threshold contract
set the model-implied probability equal to the **fraction of ensemble members
that satisfy the threshold**. This is the documented "biggest systematic edge"
methodology for weather prediction markets. The downstream edge-vs-market-price
comparison and the ~5¢ trade bar already exist and are unchanged.

## Current State Analysis

The weather signal today produces its probability from a **single deterministic
forecast** fit to a Gaussian:

- [kalshi_trader/external/noaa.py:60-93](kalshi_trader/external/noaa.py#L60-L93)
  fetches one NWS gridpoint forecast (`api.weather.gov`) → a single `temp_high` /
  `temp_low` / `precip_pct`. Despite the `noaa_gfs` branding there are no
  ensemble members.
- [kalshi_trader/signals/weather.py:27-41](kalshi_trader/signals/weather.py#L27-L41)
  `_metric_to_probability` fits `scipy.stats.norm(mean, std)` with
  `std = max((high - low) / 6.0, 2.0)` — a diurnal-range *proxy* for forecast
  error — then returns `dist.sf(threshold)` (above) or `dist.cdf(threshold)`
  (below). The code itself flags this std as un-calibrated ("left to the paper
  loop #25"). Precipitation probability is just `precip_pct / 100`.
- The `WeatherAgent` ([kalshi_trader/agents/weather_agent.py:105-217](kalshi_trader/agents/weather_agent.py#L105-L217))
  exposes tools `parse_weather_market`, `get_noaa_forecast`,
  `get_nws_discussion`, `build_weather_signal`, plus the authority pair
  `get_authority_forecast` / `build_authority_signal`. It emits up to two
  `SignalEstimate`s: `noaa_gfs` and `x_weather_authority`.

The **"compare to market price, trade when edge exceeds a threshold"** half of
the requested methodology already exists and needs **no change**:

- [scripts/score_signals.py:227-277](scripts/score_signals.py#L227-L277)
  `combine_signals` does a staleness-discounted weighted average of all
  `SignalEstimate`s.
- [scripts/score_signals.py:280-341](scripts/score_signals.py#L280-L341)
  `compute_edge_and_kelly` computes fee-adjusted edge vs. `yes_ask`, picks the
  YES/NO side, and gates on `min_edge_cents` (default **5¢** — exactly the 5–8%
  bar in the brief).
- The orchestrator keeps only markets where `worth_trading == true` AND
  `n_sources >= 2` ([.claude/skills/orchestrate/SKILL.md:285](.claude/skills/orchestrate/SKILL.md)).

### Key Discoveries

- **Open-Meteo Ensemble API verified live from this machine** (reachable through
  the corporate Zscaler proxy). `GET https://ensemble-api.open-meteo.com/v1/ensemble`
  with `models=gfs_seamless&daily=temperature_2m_max,temperature_2m_min&temperature_unit=fahrenheit&timezone=auto`
  returns **31 GEFS member series** already aggregated to a daily max/min in the
  location's local timezone:
  - `temperature_2m_max` (control) + `temperature_2m_max_member01 … member30` = 31
  - `temperature_2m_min*` for lows; `precipitation_sum*` for rain
  - No API key required. `daily.time` gives the dates so we select the target row.
- **GEFS is the GFS *ensemble* — the same model family as today's `noaa_gfs`
  signal.** The combiner's weighted average is **not** independence-aware (only
  the agreement *boost* is, via the `independent_of_noaa` flag in
  [scripts/score_signals.py:263-270](scripts/score_signals.py#L263-L270)). So we
  **replace** the parametric probability rather than adding a second GFS-family
  estimate — replacing inherits today's boost semantics untouched (an NWS-office
  authority stays circular; an independent broadcast met still earns the boost).
- **Corporate proxy TLS:** any new HTTPS client must trust the OS store. Reuse
  the exact `truststore` pattern from
  [kalshi_trader/external/noaa.py:11-24](kalshi_trader/external/noaa.py#L11-L24).
- **Shared calibration path:** `_metric_to_probability` is shared by the NOAA and
  authority builders; the ensemble builder is a *parallel* path (empirical, not
  parametric) and must not disturb the existing one.
- **`min_edge_cents` / `max_entry_price_cents`** are read from the config JSON in
  `score_signals.py` but are **absent from `config_manager.DEFAULTS`** — they fall
  back to in-code defaults (5.0 / 90.0). Tuning the edge bar from the UI is a
  separate concern and out of scope here.

## Desired End State

For a weather threshold market, the `WeatherAgent` emits a `gfs_ensemble`
`SignalEstimate` whose `probability` is the fraction of the 31 GEFS members
crossing the contract threshold (empirical CDF), with metadata carrying the
member count, ensemble mean/median, and the p10/p90 spread. The NWS gridpoint
forecast and Area Forecast Discussion are still fetched but only feed the
narrative and the discussion-confidence context — never the probability. If
Open-Meteo is unreachable or returns no members for the target date, the agent
falls back to the existing parametric `noaa_gfs` estimate so no signal is lost.
The `x_weather_authority` second source is unchanged. Downstream scoring, edge,
Kelly, and the orchestrator gates are unchanged.

**Verification of end state:** running
`python -m kalshi_trader.pipelines.weather --ticker <live temp ticker> --title "<title>"`
prints a JSON array whose first element has `source: "gfs_ensemble"`, a
`probability` equal to `members_past_threshold / member_count`, and
`metadata.member_count == 31` (for a GEFS-covered US city within the forecast
horizon).

## What We're NOT Doing

- **Not** changing `combine_signals`, `compute_edge_and_kelly`, the 5¢ edge bar,
  Kelly sizing, the risk agent, or the orchestrator gates. The edge-vs-price
  comparison already exists; we only improve the probability that feeds it.
- **Not** removing the parametric normal-CDF code — it is retained as the
  Open-Meteo-down fallback (`build_weather_signal` / `_metric_to_probability`
  stay).
- **Not** touching the `x_weather_authority` (broadcast-met) signal or
  `weather_authorities.py`.
- **Not** building a multi-model super-ensemble (GEFS + ECMWF-ENS + ICON). v1 is
  GEFS-only to match the brief; multi-model pooling is noted as a future
  enhancement only.
- **Not** updating the legacy `score_weather()` raw-signals path in
  [scripts/score_signals.py:45-77](scripts/score_signals.py#L45-L77) — the live
  orchestrate path uses the `signal_estimates` list emitted by the agent, not the
  legacy `signals` mapping. Left parametric; noted for a later sweep.
- **Not** adding `min_edge_cents` / `max_entry_price_cents` to the config UI.
- **Not** fixing the `weather-signal.md` agent file's stale `/Users/scorley/code`
  hardcoded repo paths (pre-existing; tracked separately).

## Implementation Approach

Mirror the established signal architecture exactly: external I/O isolated in
`kalshi_trader/external/`, raw-data→`SignalEstimate` conversion in
`kalshi_trader/signals/`, tool wiring in `kalshi_trader/agents/`, config in
`runtime_config.json` via `config_manager.DEFAULTS`. Build bottom-up (client →
signal → agent wiring → prompt → verification) so each layer is unit-tested
before the one above it depends on it.

---

## Phase 1: Open-Meteo GEFS ensemble client

### Overview
A new external client that fetches the 31-member GEFS daily ensemble for a
lat/lon and returns the per-member values for a target date, with the same
truststore TLS handling the NOAA client uses.

### Changes Required

#### 1. New external client
**File**: `kalshi_trader/external/open_meteo.py` (new)
**Changes**: `OpenMeteoClient` modeled on `NOAAClient` — lazy `aiohttp` session,
`_build_ssl_context()` copied/shared from the NOAA module, a 10s timeout, and one
public method.

```python
from __future__ import annotations
import ssl
from datetime import date, datetime, timezone
import aiohttp

ENSEMBLE_BASE = "https://ensemble-api.open-meteo.com/v1/ensemble"
_HEADERS = {"User-Agent": "kalshi-trader/1.0 scorley@peak6.com"}

# Kalshi metric → (Open-Meteo daily field, comparison direction handled by caller)
_METRIC_TO_DAILY_FIELD: dict[str, str] = {
    "temp_high": "temperature_2m_max",
    "temp_low": "temperature_2m_min",
    "precipitation": "precipitation_sum",
}

# GEFS at gfs_seamless reaches ~16 days; never request beyond this.
_MAX_FORECAST_DAYS = 16


def _build_ssl_context() -> ssl.SSLContext:
    """Trust the OS store so the cert chain validates behind the Zscaler proxy.

    Same rationale as kalshi_trader/external/noaa.py._build_ssl_context.
    """
    try:
        import truststore
        return truststore.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    except Exception:  # pragma: no cover - truststore optional
        return ssl.create_default_context()


class OpenMeteoClient:
    def __init__(self) -> None:
        self._session: aiohttp.ClientSession | None = None

    async def _get(self, params: dict) -> dict:
        if self._session is None:
            connector = aiohttp.TCPConnector(ssl=_build_ssl_context())
            self._session = aiohttp.ClientSession(headers=_HEADERS, connector=connector)
        async with self._session.get(
            ENSEMBLE_BASE, params=params, timeout=aiohttp.ClientTimeout(total=10)
        ) as api_response:
            api_response.raise_for_status()
            return await api_response.json()

    async def get_ensemble_members(
        self, lat: float, lon: float, target_date: date, metric: str
    ) -> dict:
        """Return the per-member GEFS daily values for target_date.

        Returns a dict:
          {"members": list[float], "member_count": int, "field": str,
           "units": str, "model": "gfs_seamless", "data_issued_at": datetime}
        `members` is empty when the target date is outside the forecast horizon
        or the API returns no usable series — the caller then falls back to the
        parametric NOAA path.
        """
        daily_field = _METRIC_TO_DAILY_FIELD.get(metric)
        if daily_field is None:
            return {"members": [], "member_count": 0, "field": "", "units": ""}

        days_ahead = (target_date - datetime.now(tz=timezone.utc).date()).days
        forecast_days = max(1, min(days_ahead + 1, _MAX_FORECAST_DAYS))
        if days_ahead < 0 or days_ahead >= _MAX_FORECAST_DAYS:
            return {"members": [], "member_count": 0, "field": daily_field, "units": ""}

        params = {
            "latitude": f"{lat:.4f}",
            "longitude": f"{lon:.4f}",
            "daily": daily_field,
            "models": "gfs_seamless",
            "timezone": "auto",
            "forecast_days": str(forecast_days),
            "temperature_unit": "fahrenheit",
            "precipitation_unit": "inch",
        }
        data = await self._get(params)
        daily = data.get("daily", {})
        dates = daily.get("time", [])
        target_iso = target_date.isoformat()
        if target_iso not in dates:
            return {"members": [], "member_count": 0, "field": daily_field, "units": ""}
        row_index = dates.index(target_iso)

        # Collect every member series for this field: the no-suffix control plus
        # *_memberNN. Skip None values (a member can be missing at long lead).
        members: list[float] = []
        for series_name, series_values in daily.items():
            if series_name == daily_field or series_name.startswith(f"{daily_field}_member"):
                if row_index < len(series_values) and series_values[row_index] is not None:
                    members.append(float(series_values[row_index]))

        return {
            "members": members,
            "member_count": len(members),
            "field": daily_field,
            "units": (data.get("daily_units", {}) or {}).get(daily_field, ""),
            "model": "gfs_seamless",
            "data_issued_at": datetime.now(tz=timezone.utc),
        }

    async def close(self) -> None:
        if self._session:
            await self._session.close()
            self._session = None
```

#### 2. Unit tests
**File**: `tests/test_open_meteo.py` (new)
**Changes**: Patch `OpenMeteoClient._get` (`AsyncMock`) with a recorded daily
payload (31 max series across 3 dates — capture a real response once and trim it
into the fixture). Assert:
- `member_count == 31` and `members` are the values at the target-date row.
- Target date **outside** the returned `time` array → `members == []`.
- `days_ahead < 0` and `days_ahead >= 16` → `members == []`, no HTTP call.
- `None` member values at the target row are skipped (count drops accordingly).
- Unknown metric → empty result, no HTTP call.

### Success Criteria

#### Automated Verification:
- [x] New file imports cleanly: `.venv/bin/python -c "from kalshi_trader.external.open_meteo import OpenMeteoClient"`
- [x] Client tests pass: `.venv/bin/pytest tests/test_open_meteo.py -q` (8 passed)
- [x] No regressions in NOAA tests: `.venv/bin/pytest tests/test_noaa.py -q` (6 passed)

#### Manual Verification:
- [x] A live one-off call for a US city returns `member_count == 31` for a date 1–3 days out (confirmed: Chicago, 2 days out, member_count=31 for temp_high/temp_low/precipitation).

**Implementation Note**: After this phase and all automated verification passes,
pause for manual confirmation before proceeding.

---

## Phase 2: Ensemble signal builder + config

### Overview
Convert the per-member list into a `gfs_ensemble` `SignalEstimate` whose
probability is the empirical fraction past the threshold, and add the config
keys that weight/score it.

### Changes Required

#### 1. Config keys
**File**: `kalshi_trader/ui/config_manager.py`
**Changes**: Add to `DEFAULTS` and `_NUMERIC_RANGES`:

```python
# DEFAULTS
"weight_ensemble": 0.85,            # the GEFS ensemble takes the NOAA weight role
"uncertainty_ensemble_temp": 0.07,
"uncertainty_ensemble_precip": 0.05,
"ensemble_min_members": 10,         # below this, treat as no usable ensemble

# _NUMERIC_RANGES
"weight_ensemble": (0.0, 1.0),
"uncertainty_ensemble_temp": (0.0, 0.5),
"uncertainty_ensemble_precip": (0.0, 0.5),
"ensemble_min_members": (1, 100),
```

**File**: `runtime_config.json`
**Changes**: Add the same four keys with their default values so the live config
file carries them (the manager merges defaults, but keeping the file explicit
matches the existing convention). `weight_noaa` stays for the fallback path.

#### 2. Ensemble signal builder
**File**: `kalshi_trader/signals/weather.py`
**Changes**: Add `build_ensemble_signal`. Probability is the empirical fraction;
above → strict `>`, below → strict `<` (parity with the parametric `sf`/`cdf`).
For precipitation with a missing/zero threshold, count members exceeding a
measurable-precip epsilon (0.01") — the standard "did it rain" definition.

```python
# Standard NWS "measurable precipitation" threshold (inches) for rain markets
# that ask "will it rain" with no explicit amount.
_MEASURABLE_PRECIP_INCHES = 0.01


def build_ensemble_signal(
    ticker: str,
    metric: str,
    threshold: float,
    operator: str,
    ensemble: dict,
) -> SignalEstimate:
    """Build a `gfs_ensemble` SignalEstimate from GEFS member values.

    probability = fraction of members satisfying the threshold (empirical CDF):
      above → members strictly greater than threshold
      below → members strictly less than threshold
    For precipitation with threshold <= 0, "satisfying" means > 0.01" (measurable).

    When member_count < ensemble_min_members the estimate is flagged
    data_quality == "empty" with uncertainty 1.0 so the scorer drops it and the
    agent's parametric NOAA fallback stands instead.
    """
    members: list[float] = [float(value) for value in ensemble.get("members", [])]
    member_count = len(members)
    minimum_members = int(cfg.get("ensemble_min_members"))

    if member_count < minimum_members:
        return SignalEstimate(
            source="gfs_ensemble",
            probability=0.5,
            uncertainty=1.0,
            weight=cfg.get("weight_ensemble"),
            data_issued_at=datetime.now(tz=timezone.utc),
            metadata={
                "ticker": ticker,
                "narrative": (
                    f"GEFS ensemble unavailable for {ticker} "
                    f"({member_count} members); falling back to NOAA parametric."
                ),
                "data_quality": "empty",
                "forecast_model": "gfs_ensemble",
                "member_count": member_count,
            },
        )

    if metric == "precipitation":
        effective_threshold = threshold if threshold and threshold > 0 else _MEASURABLE_PRECIP_INCHES
        satisfying = sum(1 for value in members if value > effective_threshold)
    elif operator == "above":
        satisfying = sum(1 for value in members if value > threshold)
    else:  # below
        satisfying = sum(1 for value in members if value < threshold)

    raw_probability = satisfying / member_count
    probability = min(max(raw_probability, 0.01), 0.99)

    sorted_members = sorted(members)
    ensemble_mean = sum(members) / member_count
    ensemble_median = sorted_members[member_count // 2]
    percentile_10 = sorted_members[max(0, int(0.10 * (member_count - 1)))]
    percentile_90 = sorted_members[min(member_count - 1, int(0.90 * (member_count - 1)))]

    uncertainty = (
        cfg.get("uncertainty_ensemble_precip")
        if metric == "precipitation"
        else cfg.get("uncertainty_ensemble_temp")
    )

    narrative = (
        f"GEFS 31-member ensemble: {satisfying}/{member_count} members "
        f"{operator} {threshold} for {ticker}. P = {probability:.2%}. "
        f"Ensemble median {ensemble_median:.1f}, p10–p90 "
        f"[{percentile_10:.1f}, {percentile_90:.1f}]."
    )

    return SignalEstimate(
        source="gfs_ensemble",
        probability=probability,
        uncertainty=uncertainty,
        weight=cfg.get("weight_ensemble"),
        data_issued_at=ensemble.get("data_issued_at") or datetime.now(tz=timezone.utc),
        metadata={
            "ticker": ticker,
            "narrative": narrative,
            "data_quality": "fresh",
            "forecast_model": "gfs_ensemble",
            "member_count": member_count,
            "members_satisfying": satisfying,
            "ensemble_mean": round(ensemble_mean, 2),
            "ensemble_median": round(ensemble_median, 2),
            "percentile_10": round(percentile_10, 2),
            "percentile_90": round(percentile_90, 2),
        },
    )
```

#### 3. Unit tests
**File**: `tests/test_signals_weather.py`
**Changes**: Add `build_ensemble_signal` cases:
- `temp_high` above: members `[70,71,...]`, threshold 75 → probability equals
  `count(>75)/n` exactly.
- `temp_low` below: symmetric check.
- precipitation: members in inches, threshold 0 → fraction `> 0.01`.
- probability clamps to `[0.01, 0.99]` (all members one side).
- `member_count < ensemble_min_members` → `uncertainty == 1.0`,
  `data_quality == "empty"` (so `usable_estimates` drops it).
- `source == "gfs_ensemble"`, `weight == cfg.weight_ensemble`, metadata carries
  `member_count`, `ensemble_median`, `percentile_10/90`.

### Success Criteria

#### Automated Verification:
- [x] Signal tests pass: `.venv/bin/pytest tests/test_signals_weather.py -q`
- [x] Config tests pass (new keys validate/range-check): `.venv/bin/pytest tests/ -q -k config`
- [x] `usable_estimates` drops the empty ensemble estimate (covered by an added
      assertion or an existing scorer test): `.venv/bin/pytest tests/ -q -k score_signals`
- [x] Combined signals/scorer/config suites: 90 passed.

#### Manual Verification:
- [ ] Hand-check one case: 8 of 31 members above the threshold → probability ≈ 0.258. *(Performed during implementation: 8/31 → probability 0.2581 — awaiting your confirmation.)*

**Implementation Note**: Pause for manual confirmation after automated checks pass.

---

## Phase 3: Wire the ensemble into the WeatherAgent (replace parametric, keep fallback)

### Overview
Add the two agent tools (`get_ensemble_forecast`, `build_ensemble_signal`), make
the ensemble the primary quantitative signal with a parametric fallback, and
update the prompt so the agent forecasts the probability off the ensemble while
still using NWS gridpoint/AFD for narrative + discussion context.

### Changes Required

#### 1. Agent tools + handlers
**File**: `kalshi_trader/agents/weather_agent.py`
**Changes**:
- Construct an `OpenMeteoClient` in `__init__` alongside `NOAAClient`/`XClient`;
  close it in `close()`.
- Add two `_SCHEMAS` entries:
  - `get_ensemble_forecast(lat, lon, date, metric)` → returns the dict from
    `OpenMeteoClient.get_ensemble_members` (members list + count + units). Doc:
    "Fetch the 31-member GEFS daily ensemble. If member_count < the minimum,
    there is no usable ensemble — build the parametric NOAA signal instead."
  - `build_ensemble_signal(ticker, metric, threshold, operator, ensemble)` →
    wraps `signals.weather.build_ensemble_signal` via `estimate_to_dict`.
- Add handlers `_get_ensemble_forecast` and `_build_ensemble_signal` mirroring
  the existing `_get_noaa_forecast` / `_build_weather_signal` shape.

```python
async def _get_ensemble_forecast(self, lat: float, lon: float, date: str, metric: str) -> dict:
    target = date_type.fromisoformat(date)
    return await self._open_meteo.get_ensemble_members(lat, lon, target, metric)

async def _build_ensemble_signal(
    self, ticker: str, metric: str, threshold: float, operator: str, ensemble: dict
) -> dict:
    estimate = build_ensemble_signal(ticker, metric, threshold, operator, ensemble)
    return estimate_to_dict(estimate)
```

#### 2. Prompt: ensemble-first workflow
**File**: `kalshi_trader/agents/prompts/weather.md`
**Changes**: Rewrite the workflow so the **quantitative probability comes from the
ensemble**, NWS is context, and the parametric path is the explicit fallback:
1. `parse_weather_market` (unchanged; null → `[]`).
2. `get_ensemble_forecast(lat, lon, target_date, metric)`.
3. If `member_count >= 10`: `build_ensemble_signal(...)` → this is the primary
   `gfs_ensemble` estimate.
   Else (ensemble unavailable): `get_noaa_forecast(...)` then
   `build_weather_signal(...)` → fallback `noaa_gfs` estimate.
4. Always `get_noaa_forecast(...)` for the narrative/point context, and
   `get_nws_discussion(...)` when `precip_pct` is 30–70% — but these inform
   commentary only, **not** the probability when the ensemble succeeded.
5. Authority flow (`get_authority_forecast` / `build_authority_signal`)
   unchanged.
6. Emit the array: `[gfs_ensemble | noaa_gfs] (+ x_weather_authority)`.

Update the example JSON block to show a `gfs_ensemble` first element with its
metadata (`member_count`, `ensemble_median`, `percentile_10/90`). Keep the
"copy the build_* values exactly, never invent" guardrail.

#### 3. Agent tests
**File**: `tests/test_weather_agent.py`
**Changes**: With the external clients mocked (`AsyncMock`):
- `_get_ensemble_forecast` delegates to `OpenMeteoClient.get_ensemble_members`
  with the parsed `date`/`metric`.
- `_build_ensemble_signal` returns a dict with `source == "gfs_ensemble"`.
- `close()` closes the `OpenMeteoClient` too.
- The existing `_parse_estimates` test that references `noaa_gfs` still passes
  (fallback path unchanged).

### Success Criteria

#### Automated Verification:
- [x] Agent tests pass: `.venv/bin/pytest tests/test_weather_agent.py -q` (13 passed)
- [x] Full weather suite passes: `.venv/bin/pytest tests/ -q -k "weather or noaa or open_meteo or signals_weather"` (87 passed, 1 skipped)
- [x] Whole suite green: `.venv/bin/pytest -q` (950 passed; 2 pre-existing failures in `test_web_links.py` / `test_market_scout.py` from the branch's unrelated link-resolver work — not touched by this plan).

#### Manual Verification:
- [x] In the prompt, the probability is sourced from the ensemble and NWS is
      explicitly context-only; the fallback branch reads unambiguously.

**Implementation deviation (data_issued_at):** `BaseAgent` JSON-encodes tool
results with `default=str` ([base.py:61](kalshi_trader/agents/base.py#L61)), so
the client's `data_issued_at` datetime would round-trip back into
`build_ensemble_signal` as a *string* and crash `estimate_to_dict`. Per the
agreed "data_issued_at = now" decision, the builder now always stamps
`datetime.now(tz=timezone.utc)` and ignores the round-tripped value; a regression
test guards this. The two-tool design (get/build) from the plan is kept as-is.

**Implementation Note**: Pause for manual confirmation after automated checks pass.

---

## Phase 4: End-to-end verification + docs

### Overview
Prove the live pipeline emits the ensemble signal, and record the change in the
research note so the codebase docs stay accurate.

### Changes Required

#### 1. Live pipeline run (no code change)
Run for a real GEFS-covered temperature market a few days out:
```bash
PYTHONPATH=/Users/llewis/ai_week /Users/llewis/ai_week/.venv/bin/python \
  -m kalshi_trader.pipelines.weather \
  --ticker KXHIGHTCHI-26JUN05-T80 \
  --title "Chicago high temperature on June 5: above 80°F?"
```
Confirm the first array element is `source: "gfs_ensemble"` with
`metadata.member_count == 31` and a probability matching `members_satisfying / 31`.

#### 2. Update the research note
**File**: `thoughts/shared/research/2026-06-03-weather-agent-live-temperature-data.md`
**Changes**: Append a dated "Update" section noting the probability is now the
GEFS ensemble empirical CDF (Open-Meteo), with the parametric NWS normal-CDF
retained as fallback and NWS gridpoint/AFD demoted to context.

### Success Criteria

#### Automated Verification:
- [x] Pipeline exits 0 and prints a non-empty JSON array: `KXHIGHTCHI-26JUN05-T80` → one `gfs_ensemble` estimate, exit 0.

#### Manual Verification:
- [x] First element is `gfs_ensemble`, `member_count == 31`, probability ==
      `members_satisfying / 31`. *(Live: 11/31 = 0.3548.)*
- [x] Fallback verified: a date beyond the 16-day horizon (`26JUN25`) made the
      agent emit `noaa_gfs` instead, still producing a usable signal.
- [ ] A weather market with an authority still produces 2 sources and can clear
      `n_sources >= 2`; the agreement boost fires for `gfs_ensemble` +
      independent broadcast met and is suppressed for an NWS-office authority.
      *(Human spot-check — depends on an authority handle returning live posts;
      not forced during implementation.)*

---

## Testing Strategy

### Unit Tests
- `OpenMeteoClient`: member extraction, target-date row selection, horizon
  guards, `None`-member skipping, unknown-metric short-circuit (mock `_get`).
- `build_ensemble_signal`: exact fraction math for above/below/precip, `[0.01,
  0.99]` clamp, `< ensemble_min_members` → empty/uncertainty 1.0, metadata
  fields, `gfs_ensemble` source/weight.
- Config: new keys load, range-validate, and reject out-of-range/wrong-type.

### Integration Tests
- `score_signals.usable_estimates` drops an empty ensemble estimate; a healthy
  `gfs_ensemble` + `x_weather_authority` pair combines and can clear the edge bar.
- `WeatherAgent` handler delegation and `close()` fan-out with mocked clients.

### Manual Testing Steps
1. Run the Phase 4 pipeline command for a 1–3 day-out US temperature market;
   confirm `gfs_ensemble` / `member_count == 31`.
2. Force the fallback (unreachable host or >16-day date); confirm `noaa_gfs`.
3. Spot-check the empirical probability against the printed p10/p90 spread for
   plausibility.

## Performance Considerations

One extra ~200ms Open-Meteo call per weather market, run live on-demand only for
weather/climate markets in the deep-signal subset — negligible. Open-Meteo's free
tier (~10k calls/day, no key) far exceeds the per-cycle weather-market count. The
client reuses a single `aiohttp` session per run like `NOAAClient`.

## Migration Notes

No state/schema migration. `weight_noaa` is retained for the fallback path;
adding `weight_ensemble` etc. is backward compatible (the manager merges defaults
over any existing `runtime_config.json`). The `gfs_ensemble` source string is new;
no downstream code keys on the literal `noaa_gfs` string (the agreement boost uses
the `independent_of_noaa` flag, and `_SOURCE_FAMILY_PREFIXES` lists only
`x_grok`), so the rename is safe.

## References

- Methodology brief: ensemble forecasting + fraction-of-members probability,
  edge vs. Kalshi price, trade when edge > 5–8%.
- Current parametric path: `kalshi_trader/signals/weather.py:27-41`
- NWS single-forecast fetch: `kalshi_trader/external/noaa.py:60-93`
- Agent + tools: `kalshi_trader/agents/weather_agent.py:20-217`
- Prompt: `kalshi_trader/agents/prompts/weather.md`
- Edge/Kelly + combine (unchanged): `scripts/score_signals.py:227-341`
- Orchestrator gate (`worth_trading` + `n_sources >= 2`):
  `.claude/skills/orchestrate/SKILL.md:285`
- Prior research: `thoughts/shared/research/2026-06-03-weather-agent-live-temperature-data.md`
- Open-Meteo Ensemble API: `https://ensemble-api.open-meteo.com/v1/ensemble`
  (`models=gfs_seamless`, 31 GEFS members, verified live)
