# Live-Observation Override for Same-Day Weather Markets

> **Status (2026-06-03): implemented, all phases.** Full suite green
> (1162 passed; the one `test_market_scout.py` failure is a pre-existing,
> unrelated link-resolver test on this branch). New tests:
> `tests/test_weather_settlement.py` (7), `get_observed_extreme` cases in
> `tests/test_noaa.py`, clamp/lock cases in `tests/test_signals_weather.py`, and
> override-wiring cases in `tests/test_weather_agent.py`. Live end-to-end verified
> on three Jun-3 markets (ATL `B57.5` 0.010→0.990, AUS `B70.5` 0.774→0.010, MSP
> `B85.5` 0.032→0.032). The agent pipeline runs the new tool and safely no-ops to
> the pure ensemble when a series' settlement terms aren't cached — the documented
> activation prerequisite (Open Questions).

## Overview

For a **same-day** temperature/precipitation market, the day's extreme has often
already occurred by the time we score it, so a stale GEFS run can be flatly
contradicted by the live observation. This plan conditions the existing
[`build_ensemble_signal`](kalshi_trader/signals/weather.py) empirical CDF on
**what has actually been observed so far today**, using a monotonicity fact:

> The realized **max so far is a lower bound** on the final daily max (it can only
> rise). The realized **min so far is an upper bound** on the final daily min (it
> can only fall). Cumulative precipitation so far is a **lower bound** on the
> daily total.

So instead of replacing the ensemble, we **clamp each member to respect what is
already realized**, then recompute the same fraction-in-band. A member forecast
that the live observation has already falsified is moved to the realized bound;
members still in play keep their value. The estimate self-collapses toward the
realized outcome as the day's extreme locks in, and is a *no-op early in the day*
when the bound hasn't bitten yet.

Crucially, the observation is read **off the contract's actual settlement
station**, resolved from `settlement_sources` — **not** a hardcoded airport. The
two series we have terms for settle differently (`KXHIGHLAX` → NWS CLI report
issued by LAX; `KXTEMPNYCH` → AccuWeather), so the override only fires when the
settlement source is a queryable NWS station and otherwise leaves the pure
ensemble untouched.

## Current State Analysis

- The weather probability is the GEFS empirical CDF from
  [`build_ensemble_signal`](kalshi_trader/signals/weather.py)
  (`build_ensemble_signal(ticker, metric, threshold, operator, ensemble, threshold_high=None)`),
  fed by [`OpenMeteoClient.get_ensemble_members`](kalshi_trader/external/open_meteo.py).
  The ensemble work is complete (see
  [2026-06-03-gefs-ensemble-weather-probability.md](thoughts/shared/plans/2026-06-03-gefs-ensemble-weather-probability.md)).
- [`NOAAClient`](kalshi_trader/external/noaa.py) fetches **forecasts only**
  (`get_forecast`, `get_discussion`) — there is **no realized-observation
  endpoint** today. The shared `_get(url)` + `_build_ssl_context()` (truststore)
  pattern and `NWS_BASE = "https://api.weather.gov"` are reusable.
- [`scripts/market_rules.py`](scripts/market_rules.py) already fetches per-series
  `settlement_sources` (a list of `{name, url}`) and caches them into
  [`kalshi_trader/series_contract_terms.json`](kalshi_trader/series_contract_terms.json).
- The `WeatherAgent` ([weather_agent.py](kalshi_trader/agents/weather_agent.py))
  parses the market, fetches the ensemble, builds the signal, and optionally adds
  the X authority. Coordinates come from
  [`weather_parser.parse_title`](kalshi_trader/external/weather_parser.py), which
  returns **city-center** lat/lon (downtown), not the settlement station.

### Key Discoveries

- **Validated the failure mode live on 2026-06-03** across three same-day
  markets. The ensemble compresses the diurnal range — it ran **warm on overnight
  lows** and **cool on the daytime high** — and every band outcome was already
  decided by the realized extreme:
  | Market | GEFS P | Realized (locked) | Clamp would give |
  |---|---|---|---|
  | `KXHIGHTMIN-26JUN03-B85.5` (high 85–86) | 3% | 84.2°F, climbing | members floored to ≥84.2 → P rises |
  | `KXLOWTAUS-26JUN03-B70.5` (low 70–71) | 77% | 69.8°F (KAUS) | members capped to ≤69.8 → P→~0 |
  | `KXLOWTATL-26JUN03-B57.5` (low 57–58) | 1% | 57.2°F→57° | members capped to ≤57.2 → P→ high |
- **Settlement is not uniformly the airport.** `series_contract_terms.json` shows
  `KXHIGHLAX` settling on the NWS **CLI climatological report `issuedby=LAX`** and
  `KXTEMPNYCH` on **AccuWeather**. The station must be resolved from
  `settlement_sources`, and the override disabled for non-station sources.
- **api.weather.gov observations** are at `/stations/<id>/observations`; station
  metadata at `/stations/<id>` carries `properties.timeZone` (needed to define the
  contract's local calendar day). Confirmed reachable from this machine with the
  truststore SSL context (same proxy handling as `NOAAClient`).
- **Monotonicity makes clamping principled** and keeps the "ensemble = empirical
  CDF" framing — one coherent number, no second source for the combiner to
  reconcile.

## Desired End State

For a **same-day** temperature/precip market whose settlement source resolves to
an NWS station, the `WeatherAgent` fetches the realized extreme so far and the
`gfs_ensemble` `SignalEstimate` is computed on **clamped members**. Its metadata
records the realized value, the observation timestamp, the station id, and a
`lock_fraction`; its `uncertainty` shrinks as the extreme locks in. When the
target date is not today, the settlement source is not a station, observations
are unavailable, or the bound has not yet bitten, the behavior is **identical to
today's pure ensemble**. Downstream scoring/edge/Kelly/orchestrator gates are
unchanged.

**Verification of end state:** for a same-day temperature market past its
climatological extreme time, the pipeline prints a `gfs_ensemble` estimate whose
`metadata.realized_extreme` matches the live `api.weather.gov` reading for the
settlement station and whose `probability` equals
`clamped_members_in_band / member_count`.

## What We're NOT Doing

- **Not** hardcoding airport coordinates (the rejected idea). The override reads
  the settlement station; the ensemble's forecast coords are addressed only as the
  optional Phase 1 note below.
- **Not** changing `combine_signals`, `compute_edge_and_kelly`, the 5¢ edge bar,
  Kelly, the risk agent, or orchestrator gates.
- **Not** adding a competing `live_observation` source — we condition the existing
  `gfs_ensemble` in place.
- **Not** parsing free-text/PDF contract terms for a station id. v1 supports only
  the structured case (NWS CLI-product URL with an `issuedby=<station>` param, or
  an explicit station id in `settlement_sources`); anything else disables the
  override.
- **Not** building a remaining-day warming/cooling model. v1 clamps to the
  realized bound (slightly conservative); a remaining-day delta is a future
  enhancement.
- **Not** touching the AccuWeather-settled path (`KXTEMPNYCH`) beyond detecting it
  and skipping the override.

## Implementation Approach

Bottom-up, mirroring the ensemble plan: settlement→station resolution, then the
observation client method, then the clamp in the signal builder, then agent/prompt
wiring, then end-to-end verification. Each layer is unit-tested before the layer
above depends on it. Every new HTTPS client path reuses the truststore SSL context.

---

## Phase 1: Settlement → station resolution

### Overview
Map a series ticker to `{station_id, timezone, source_type}` from its
`settlement_sources`, so the override measures off the contract's real station.

### Changes Required

#### 1. Resolver
**File**: `kalshi_trader/external/weather_settlement.py` (new)
**Changes**: `resolve_settlement_station(series_ticker, settlement_sources) ->
dict | None`. Returns `{"station_id": "LAX", "source_type": "nws_station"}` when a
source is an NWS CLI-product URL (`product=CLI&issuedby=<station>`) or carries an
explicit station id; returns `{"source_type": "accuweather"}` (or similar) with no
station for non-station sources; returns `None` when nothing is resolvable. The
caller treats anything without a `station_id` as "override disabled."

#### 2. Tests
**File**: `tests/test_weather_settlement.py` (new)
**Changes**: `KXHIGHLAX` CLI URL → `station_id == "LAX"`; `KXTEMPNYCH` AccuWeather →
no station, `source_type == "accuweather"`; empty/garbage sources → `None`.

#### Optional note (not blocking)
The ensemble forecast still uses `parse_title` downtown coords. Aligning those to
the resolved station's lat/lon is a small, separate follow-up — call it out, don't
do it here.

### Success Criteria

#### Automated Verification:
- [ ] `.venv/bin/python -c "from kalshi_trader.external.weather_settlement import resolve_settlement_station"`
- [ ] `.venv/bin/pytest tests/test_weather_settlement.py -q`

#### Manual Verification:
- [ ] `resolve_settlement_station` returns the LAX station for `KXHIGHLAX` and
      disables the override for `KXTEMPNYCH` against the live
      `series_contract_terms.json` entries.

**Pause for confirmation after automated checks pass.**

---

## Phase 2: Observed-extreme fetch in NOAAClient

### Overview
Add `get_observed_extreme(station_id, target_date_local, metric)` returning the
realized min/max (or cumulative precip) for the station's local calendar day so
far, plus the observation timestamp, station timezone, and coverage flag.

### Changes Required

#### 1. New client method
**File**: `kalshi_trader/external/noaa.py`
**Changes**: Add `get_observed_extreme`. Fetch `/stations/<id>` once for
`properties.timeZone`; compute the local-day UTC window for `target_date`; fetch
`/stations/<id>/observations?limit=...`; filter `properties.temperature` (°C→°F)
or precip within the window; return:
```python
{
  "station_id": str, "timezone": str, "metric": str,
  "realized_extreme": float | None,   # max for temp_high, min for temp_low, sum for precip
  "at_timestamp": str | None,
  "obs_count": int,
  "covers_window": bool,              # obs span the expected extreme time
}
```
`realized_extreme is None` (no obs / fetch error) → caller skips the clamp and
keeps the pure ensemble. Reuse `_get` and the truststore context; never fabricate.

#### 2. Tests
**File**: `tests/test_noaa.py`
**Changes**: Mock `_get` with a station-metadata payload + an observations payload.
Assert: max selected for `temp_high`, min for `temp_low`, sum for precip; °C→°F
conversion; obs outside the local-day window excluded; empty obs → `realized_extreme
is None`; no-station-metadata error path → `None`.

### Success Criteria

#### Automated Verification:
- [ ] `.venv/bin/pytest tests/test_noaa.py -q`

#### Manual Verification:
- [ ] A live call for KATL on 2026-06-03 returns a realized min near the
      observed overnight low and the correct local-day window.

**Pause for confirmation after automated checks pass.**

---

## Phase 3: Clamp members in the signal builder + config + lock fraction

### Overview
Extend `build_ensemble_signal` with optional `realized_extreme` and
`lock_fraction`, clamp members by monotonicity, recompute the empirical CDF, and
scale uncertainty by how locked the extreme is.

### Changes Required

#### 1. Builder
**File**: `kalshi_trader/signals/weather.py`
**Changes**: Add params `realized_extreme: float | None = None`,
`lock_fraction: float = 0.0`. When `realized_extreme is not None`, clamp each
member before the fraction-in-band computation:
```python
# temp_high: realized max is a LOWER bound (final high ≥ realized)
# temp_low:  realized min is an UPPER bound (final low ≤ realized)
# precip:    realized sum is a LOWER bound (final total ≥ realized)
if metric == "temp_low":
    members = [min(member, realized_extreme) for member in members]
else:  # temp_high, precipitation
    members = [max(member, realized_extreme) for member in members]
```
Then the existing fraction-in-band runs on the clamped members. Reduce
`uncertainty` toward a small floor as `lock_fraction → 1`
(e.g. `uncertainty * (1 - lock_fraction) + floor * lock_fraction`). Add
`realized_extreme`, `at_timestamp`, `station_id`, `lock_fraction`, and
`members_clamped` to metadata, and note the clamp in the narrative. When
`realized_extreme is None`, the function is byte-for-byte the current behavior.

#### 2. Lock fraction
**File**: `kalshi_trader/signals/weather.py` (helper) or the agent handler.
**Changes**: `lock_fraction(metric, station_timezone, now)` from time-of-day vs the
climatological extreme window (lows ~sunrise, highs ~mid/late afternoon; precip ramps
with fraction-of-day elapsed). 0 before the window, ramping to ~1 once clearly past
it. Keep it simple and documented; this only governs uncertainty + the apply guard.

#### 3. Config keys
**File**: `kalshi_trader/ui/config_manager.py` (+ `runtime_config.json`)
**Changes**: `enable_observation_override` (bool, default `true`),
`observation_uncertainty_floor` (default `0.02`), and any lock-window hours, with
`_NUMERIC_RANGES` entries.

#### 4. Tests
**File**: `tests/test_signals_weather.py`
**Changes**: temp_high with `realized_extreme` above a cool ensemble → members
floored, P rises; temp_low with realized below the band → P→~0; precip floor;
`realized_extreme is None` → identical to current output (regression);
`lock_fraction == 1` → uncertainty at the floor; metadata fields present.

### Success Criteria

#### Automated Verification:
- [ ] `.venv/bin/pytest tests/test_signals_weather.py -q`
- [ ] `.venv/bin/pytest tests/ -q -k config`
- [ ] Regression: existing ensemble cases unchanged when `realized_extreme is None`.

#### Manual Verification:
- [ ] Hand-check the three 2026-06-03 cases reproduce the "clamp would give"
      column above.

**Pause for confirmation after automated checks pass.**

---

## Phase 4: Wire the override into the WeatherAgent + prompt

### Overview
Add a `get_observed_extreme` tool, resolve the station, fetch the realized extreme
for same-day markets, and pass it (with `lock_fraction`) into `build_ensemble_signal`.

### Changes Required

#### 1. Agent tool + handler
**File**: `kalshi_trader/agents/weather_agent.py`
**Changes**: Add a `get_observed_extreme(series_ticker, station_id, target_date,
metric)` schema + `_get_observed_extreme` handler delegating to
`NOAAClient.get_observed_extreme`; compute `lock_fraction` from the returned
timezone. Thread `realized_extreme` + `lock_fraction` into the existing
`build_ensemble_signal` tool params. No new client object (reuses `self._noaa`).

#### 2. Prompt
**File**: `kalshi_trader/agents/prompts/weather.md`
**Changes**: After parsing + the ensemble fetch, when `target_date == today`,
resolve the station and call `get_observed_extreme`; pass `realized_extreme` +
`lock_fraction` into `build_ensemble_signal`. Spell out: override only for
same-day + NWS-station settlement; skip silently otherwise; keep the "copy
`build_*` values exactly, never invent" guardrail. Update the example JSON to show
the clamp metadata.

#### 3. Tests
**File**: `tests/test_weather_agent.py`
**Changes**: With clients mocked — `_get_observed_extreme` delegates correctly; the
realized value flows into `build_ensemble_signal`; a non-station settlement source
skips the override; a future-dated market skips it.

### Success Criteria

#### Automated Verification:
- [ ] `.venv/bin/pytest tests/test_weather_agent.py -q`
- [ ] `.venv/bin/pytest -q` (no new failures vs the branch baseline)

#### Manual Verification:
- [ ] Prompt makes the same-day + station guard unambiguous.

**Pause for confirmation after automated checks pass.**

---

## Phase 5: End-to-end verification + docs

### Changes Required

#### 1. Live run (no code change)
Run the pipeline for a same-day temperature market past its extreme time and
confirm `metadata.realized_extreme` matches the live `api.weather.gov` reading and
`probability == clamped_members_in_band / member_count`. Re-run one of the three
2026-06-03 cases as the worked example.

#### 2. Docs
**File**: `thoughts/shared/research/2026-06-03-weather-agent-live-temperature-data.md`
**Changes**: Append an "Update" noting the same-day observation override
(settlement-station-aware, monotonic clamp) and that it is a no-op for future
dates / non-station settlement.

### Success Criteria

#### Automated Verification:
- [ ] Pipeline exits 0 and prints a non-empty array with the clamp metadata.

#### Manual Verification:
- [ ] Realized value matches the live station reading; probability matches the
      clamped fraction; future-dated control still emits the pure ensemble.

---

## Testing Strategy

- **Unit**: settlement resolver (CLI-URL / AccuWeather / garbage); observed-extreme
  selection + timezone windowing + °C→°F; clamp math for high/low/precip; `None`
  → regression-identical; `lock_fraction` → uncertainty floor.
- **Integration**: agent threads realized value into the builder; non-station and
  future-date short-circuits; `combine_signals` still consumes a single
  `gfs_ensemble`.
- **Manual**: reproduce the three 2026-06-03 cases; force fallback (no obs / future
  date) → pure ensemble.

## Performance Considerations

Two extra `api.weather.gov` calls (`/stations/<id>` once, `/observations` once) per
same-day weather market, on the existing `aiohttp` session — negligible.
Station/timezone is cacheable per series alongside `series_contract_terms.json`.

## Open Questions

- **Station id source coverage**: only `KXHIGHLAX` (CLI URL) and `KXTEMPNYCH`
  (AccuWeather) have cached terms. ATL/AUS/MSP need their `settlement_sources`
  fetched via `market_rules.py` first; if a station id isn't structurally
  derivable, the override stays disabled (safe default) until we add term parsing.
- **Lock-fraction calibration**: the climatological extreme windows are a first
  cut; worth tuning against a few days of obs vs settlement.

## References

- Completed ensemble plan: `thoughts/shared/plans/2026-06-03-gefs-ensemble-weather-probability.md`
- Signal builder: `kalshi_trader/signals/weather.py`
- NOAA client (forecast-only today): `kalshi_trader/external/noaa.py`
- Settlement source fetch: `scripts/market_rules.py`, `kalshi_trader/series_contract_terms.json`
- Agent + prompt: `kalshi_trader/agents/weather_agent.py`, `kalshi_trader/agents/prompts/weather.md`
- Live validation (2026-06-03): MSP/AUS/ATL same-day band markets
