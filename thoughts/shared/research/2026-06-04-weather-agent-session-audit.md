# Audit: Weather-trading agent session — 2026-06-04

Independent review (by a sub-agent) of a Claude Code session that scanned Kalshi
weather markets, scored edges, and placed one live prod order. All claims were
verified against the code; inaccuracies in the original session summary are
flagged inline. Analysis only — no code changed, no orders touched.

## Executive summary (highest priority)

- **P0 — Rounding mismatch in `build_ensemble_signal` is the root cause of the fake "edges."** The ensemble buckets *raw* float members against integer-degree buckets without rounding to Kalshi's integer-degree settlement convention. Demonstrated: an already-locked realized low of 60.8°F (settles bucket `[61,62]`) scores **0% / 1%-floored** under current code but **80%** when members are rounded to integers first. A true correctness bug, not just calibration.
- **P0 — A real-money prod order was placed on the weakest idea, sized outside `RiskManager`.** Order hand-built with `RiskDecision(approved=True, approved_size_dollars=12.50)`; the settlement-proximity gate never ran (`run_risk.py` never passes `close_time`) and these markets settle within hours. "Quarter-Kelly" was a post-hoc halving the `RiskManager` does not model.
- **P1 — Production `WeatherAgent` forecasts the GEFS ensemble at the city centroid, not the settlement station.** `_get_ensemble_forecast` uses `parse_title`'s `CITY_COORDS`; only the *observation* path resolves the real station. No station→coords table exists in the repo.
- **P1 — `get_or_fetch_many` fans out unbounded `asyncio.gather` with `return_exceptions=True`** — cold bulk fetches get 429'd and silently dropped. NOTE: `scanner.py` is already bounded; that suspicion was wrong.
- **Git-state corrections:** `series_contract_terms.json` is currently a clean *staged add* (617 lines / 117 keys) with no conflict markers — not the `UU`/72-key state described. The working tree also has a large unrelated `settlement_proximity_multiplier` feature + mentions changes not mentioned in the trace.

---

## 1. Code bugs / correctness

**B-1 — Rounding mismatch: ensemble buckets raw forecasts against integer-degree settlement buckets. [P0, not-fixed]**
`build_ensemble_signal` counts members with raw float comparisons — `threshold <= value <= band_high` (between), `value > threshold` (above), `value < threshold` (below) — but Kalshi temperature markets settle on the integer-rounded daily extreme. Evidence: `kalshi_trader/signals/weather.py:296-305`; realized extreme rounded only to 0.1°F at `kalshi_trader/external/noaa.py:240`; clamp at `weather.py:290-293` also uses un-rounded values. Demonstrated: members near 60.8°F vs bucket `[61,62]` → current code 0/10 (floored to 1%); round-to-int → 8/10 (80%); realized 60.8 rounds to 61 and settles the bucket TRUE. Fix: round each member to the nearest integer before bucketing (and round the clamp bound the same way). Confirm the exact NWS rounding rule (half-to-even vs half-up) against a settled market first. `_metric_to_probability` (`weather.py:74-88`) has the same conceptual issue (CDF at exact bucket edges, no continuity correction) but matters less since the ensemble is primary.

**D-1 — Ensemble forecast taken at city centroid, not settlement station. [P1, not-fixed]**
`_get_ensemble_forecast(lat, lon, …)` (`kalshi_trader/agents/weather_agent.py:237-241`) forecasts at `parse_title`'s `CITY_COORDS` centroid (`kalshi_trader/external/weather_parser.py:5,137`); the station is resolved only in `_get_observed_extreme` (`weather_agent.py:285-292`). So the model is scored at e.g. downtown LA while the contract settles at LAX. Fix: have `resolve_settlement_station` return station coordinates (from `api.weather.gov/stations/<id>` geometry, cached with the terms) and forecast there; until then, log when forecast point and station differ materially.

**F-1 — `observation_lock_fraction` uses the observation's own timestamp as "now". [P1, not-fixed]**
Only caller passes no `now` (`weather_agent.py:296`), so `weather.py:40` uses `local_now = latest` (obs time, not wall-clock). A stale or mid-window obs can read lock 1.0 prematurely (Miami low at obs ~01:40 local showed lock 1.0 for a `temp_low` whose lock hour is 09:00). Fix: pass `now=datetime.now(timezone.utc)`, and separately lower confidence on stale observations.

**C-1 — `get_or_fetch_many` unbounded gather + silently swallowed exceptions. [P1, partially-fixed in practice]**
`kalshi_trader/contract_terms.py:104-112` fires every cache-miss via `asyncio.gather(..., return_exceptions=True)` with no cap and no retry; 429s are caught and the series silently omitted. `scripts/market_rules.py:57` bounds its own `get_market` calls but then calls this unbounded helper. Fix: bound with a semaphore + `with_retry`; distinguish "permanently failed" from "rate-limited, not yet fetched." CORRECTION: `scanner.py:152` and `:334-336` are already bounded (semaphore + `with_retry`); only `get_or_fetch_many` has the bug.

**Bonus — `with_retry` returns `{}` after exhausting retries instead of raising. [P2, not-fixed]**
`kalshi_trader/_retry.py:16` returns an empty dict when all attempts 429; callers see "no data" silently. Fix: re-raise the last exception.

---

## 2. System & design improvements

**A-1 — `get_events` nested-market normalization fix is correct and complete. [P2, fixed]**
`kalshi_trader/client.py:108-116` mirrors `get_markets`; new test `tests/test_client_schema.py:41-56` passes (54 passed). Audited all endpoints: `get_market`, `get_markets`, `get_events` all normalize; no other endpoint returns nested markets.

**E-1 — GEFS-only fast scoring shares a bias with NWS; ensemble-only edges should not be tradeable raw. [P1, not-fixed]**
The diurnal-range compression is a property of ensemble smoothing + gridpoint-vs-station siting, not a field bug (`open_meteo.py:25-27` maps fields correctly). Fix: (a) bias-correct the ensemble against recent realized station extremes (rolling per-station offset); (b) require corroboration from the parametric NOAA gridpoint or an authority before trading a pure-ensemble edge; (c) gate any trade on B-1 landing first.

**Design — No station→coords lookup exists. [P2, not-fixed]**
Only `CITY_COORDS` (centroids) and `resolve_settlement_station` (station_id, no coords) exist. A `series_settlement_stations.json` (station_id → lat/lon from NWS geometry) would let D-1 be fixed cleanly and cached.

**Design — `series_contract_terms.json` has 117 keys, not 72. [info]**
Staged file is valid JSON, 117 keys, all carrying `settlement_sources`; clean staged add (`git diff --cached` shows `617 +`), no conflict markers. The `UU`/merge-conflict state was already resolved before the audit. A leftover `git stash@{0}` ("KXRUBIOMENTION") exists.

---

## 3. Risk & safety

**G-1 — Settlement-proximity gate never fires from the risk CLI. [P0, not-fixed]**
`scripts/run_risk.py:63` calls `check_trade` with no `close_time`, so the `MIN_HOURS_BEFORE_SETTLEMENT` guard (`risk.py:47-53`, 2h in `config.py:44`) is skipped. Weather markets settle within hours — exactly the guard that should have bound. Fix: make `close_time` required in the ideas JSON and pass it through; refuse to score without it.

**G-2 — Real order sized outside `RiskManager` via a hand-built `RiskDecision`. [P0, not-fixed]**
`RiskManager` only implements half-Kelly (`risk.py:117-128`); "quarter-Kelly" was a manual halving fed to `TradeExecutor.execute` as `RiskDecision(approved=True, approved_size_dollars=12.50)`. The executor recomputes `count = floor(12.50/0.62) = 20` (`executor.py:21`), so the halving set only a dollar cap that the executor re-expanded into contracts at bucket price; `MIN_SINGLE_POSITION_DOLLARS`, the fee estimate, and the settlement gate were all bypassed. Fix: route every order through `check_trade` with `close_time`; add a `kelly_fraction` config (default 0.5) inside `_half_kelly_size` rather than post-halving; never hand-construct `RiskDecision(approved=True, …)` in execution code.

**G-3 — Traded the idea the agent itself rated weakest. [P1, behavior]**
From `/tmp/ideas.json`: `KXLOWTHOU-26JUN04-B72.5` had the lowest confidence (0.80) of five. The 5¢-edge gate correctly rejected the other four (mkt 86-94 vs conf 0.82-0.97 → edge < 0.05), so the survivor was the lowest-conviction idea — and per B-1/D-1 its edge is likely a rounding/centroid artifact. The gate worked; the failure is trading at all when the only survivor is weakest and the model has known bias. Fix: an illiquidity guard (book-volume floor) and a "lowest-confidence-of-slate" flag blocking auto-execution.

**Where guards belong:** quarter-Kelly fraction → `config.py` + `_half_kelly_size`; settlement-proximity → already in `check_trade`, must be called with `close_time`; illiquidity (min book depth/volume) → new hard check in `check_trade`.

---

## 4. Agent process / behavior

**H-1 — No read-after-write confirm/retry on order state. [P2, not-fixed]**
No helper exists; `executor.py:55` reads `/portfolio/orders` directly and `get_orders` (`client.py:147-149`) is a bare GET. Eventual consistency means an immediate read after create/cancel can return empty (observed twice this session). Fix: a `confirm_order(order_id, expect_status, attempts=5, base_delay=0.3)` helper that polls with backoff before declaring success.

**I-1 — 40-min batch run before recognizing 6× redundancy. [P2, process]**
Per-bucket subprocess design recomputed each city's forecast once per bucket and span an LLM loop per market. Heuristic: before any batch, estimate `total_work = n_units × per_unit_cost`, dedupe the work unit (city/metric event, not bucket) first, cap exploratory runs at a few minutes and extrapolate.

**I-2 — Several rounds to find the centroid/station and merge-conflict issues. [P2, process]**
"Suspiciously large edges on liquid markets" is the tell that the model, not the market, is wrong — it should trigger an immediate model-vs-settlement audit (is the forecast point the settlement point? is bucketing on the settlement convention?) before sizing trades. Add a startup self-check that `load_contract_terms()` parses and resolved station coords ≈ forecast coords.

**I-3 — Uncommitted, unrelated changes muddy the working tree. [P2, process]**
Tree mixes the weather fixes (`client.py`, `test_client_schema.py`) with a large unrelated `settlement_proximity_multiplier` feature (`actionability/scorer.py`, `signals.py`, `__init__.py`, `models.py`, `grouping.py`, `README.md`, `test_actionability.py`) and mentions changes (`mentions_parser.py`, `signals/mentions.py`, `pipelines/mentions.py`). Separately, `tests/test_mentions_parser.py` (+4 others) fails at collection — `normalize_for_match` imported but absent from HEAD and the working tree (pre-existing on this branch, not caused by this session) — so the suite is red. Fix: land weather fixes on their own branch; quarantine/fix the broken mentions import so CI is green.

---

### Verified-correct items (no action)
- `get_events` normalization + its test: correct, passing.
- `settlement_proximity_multiplier` feature: logic and tests sound (54 passed), naming follows CLAUDE.md — just shouldn't be entangled with the weather fixes.
- Executor count math (20 @ 62¢ = $12.40) is arithmetically consistent.

### Claims in the original session summary that were wrong
- `series_contract_terms.json` is not in `UU`/conflict now; clean staged add, 117 keys (not 72), no markers.
- `scanner.py` gathers are already bounded; the unbounded fan-out is only in `get_or_fetch_many`.
- The working tree has far more uncommitted than "client.py + test."
