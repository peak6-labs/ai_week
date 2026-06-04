---
date: 2026-06-02T12:01:36-05:00
researcher: Alexandra Lewis
git_commit: 42bc01462c4857d77d3dd227c656c6164fed3c05
branch: main
repository: peak6-labs/ai_week
topic: "Project summary: Kalshi Market Scorer"
tags: [research, codebase, kalshi, scoring, actionability, risk, kelly, summary]
status: complete
last_updated: 2026-06-02
last_updated_by: Alexandra Lewis
---

# Research: Project summary — Kalshi Market Scorer

**Date**: 2026-06-02T12:01:36-05:00
**Researcher**: Alexandra Lewis
**Git Commit**: 42bc01462c4857d77d3dd227c656c6164fed3c05
**Branch**: main
**Repository**: peak6-labs/ai_week

## Research Question

"Give me a summary of the project so far" — a comprehensive map of the Kalshi Market Scorer as it currently exists: its purpose, structure, scoring methodology, Kalshi API integration, and supporting documentation.

## Summary

The repository is an **agentic prediction-market trading system** being built for the Peak6 / CapMan "AI Immersion" competition (June 1–5, 2026, $500 budget, Kalshi as the primary venue). The piece that is built and working today is the **Market Scorer** — a signal layer that ranks open Kalshi markets by "actionability" (unusual volume, price movement, and order flow happening *right now*).

The working pipeline is two-phase:

1. **Fetch once** (`scripts/fetch_markets.py`): paginate every open Kalshi market (~480k), enrich each with its category from the events API, and write a snapshot to `live_markets.json`.
2. **Score repeatedly** (`scripts/score_markets.py`): load the snapshot, filter to a tradeable universe, refresh a SQLite candle cache, compute **9 weighted signals** per market, enrich the top markets with live trades/orderbooks, and print a ranking grouped by event.

Around this core sit the supporting layers for the broader trading system: an async Kalshi API client with RSA-PSS request signing (`kalshi_auth.py` + `kalshi_trader/client.py`), a `RiskManager` with hard limits and half-Kelly position sizing (`kalshi_trader/risk.py`), a `TradeExecutor` (`kalshi_trader/executor.py`), data models (`kalshi_trader/models.py`), and a full test suite. A master plan (`plans/trading_plan.md`) describes a 6-specialist + 1-coordinator multi-agent architecture that the scorer is intended to feed; the agent layer (`kalshi_trader/agents/`, `kalshi_trader/external/`) is currently placeholder.

## Detailed Findings

### Project layout & purpose

The main package is `kalshi_trader/`. Top-level files of note:

- `kalshi_auth.py` — standalone synchronous Kalshi client (auth, signing, HTTP). Also runnable as a connectivity test.
- `kalshi_trader/` — the package (models, client, scanner, scoring, risk, executor, config, web links).
- `scripts/fetch_markets.py`, `scripts/score_markets.py` — the two user-facing CLI entry points.
- `live_markets.json` — generated market snapshot (~281 MB; gitignored as regenerable).
- `requirements.txt`, `CLAUDE.md`, `KALSHI_SETUP.md`, `documentation/openapi.yaml`, `plans/trading_plan.md`.

Module responsibilities (`kalshi_trader/`):

| Module | Responsibility |
|--------|----------------|
| `models.py` | Core dataclasses: `Market`, `ScoredMarket`, `Candle`, `TradeIdea`, `RiskDecision`, `OrderResult`, `Position`, `PortfolioState` |
| `client.py` | Async wrapper over `kalshi_auth.KalshiClient`; runs blocking calls in a thread executor; exposes `get_markets`, `get_events`, `get_market`, `get_orderbook`, `get_trades`, `get_market_candlesticks_batch`, `create_order`, `cancel_order`, etc. |
| `scanner.py` | `MarketScanner`: fetch open markets, filter universe, enrich categories from events API, orchestrate scoring in `get_scored_markets()` |
| `actionability/` | The scoring subpackage (signals, scorer, SQLite candle store) |
| `risk.py` | `RiskManager`: hard limits + half-Kelly sizing |
| `executor.py` | `TradeExecutor`: places/cancels orders for approved ideas |
| `config.py` | Env-driven config: environment selection, base URLs, risk thresholds, agent models, Telegram |
| `market_snapshot.py` | Serialize/deserialize the market snapshot JSON |
| `web_links.py` | `kalshi_market_url(ticker)` → safe series-level Kalshi URL |
| `_retry.py` | `with_retry()` exponential backoff on HTTP 429 |
| `agents/`, `external/` | Empty placeholders for future specialist agents / external signal feeds |

**Entry points & usage:**

- `python scripts/fetch_markets.py [--output FILE] [--limit N] [--verbose]` — builds the snapshot (~7 min first run). Flow: `KalshiClient` → `MarketScanner.get_open_markets()` → `enrich_categories()` → `save_snapshot()`.
- `python scripts/score_markets.py [--top N] [--category CAT] [--markets-file FILE] [--verbose] [--debug]` — ranks markets (~15 s on a warm cache). Flow: `load_snapshot()` → `filter_markets()` → `store.refresh_stale()` → `scorer.score_all()` → live trades/orderbook enrichment → grouped print.
- `python kalshi_auth.py` (honors `KALSHI_ENV`) — auth/connectivity smoke test, prints balance.

**Dependencies** (`requirements.txt`): `requests>=2.32`, `cryptography>=42` (RSA signing), `truststore>=0.9` (OS trust store for corporate proxies), `anthropic>=0.40` (future agents), plus `aiohttp`, `pydantic`, `rapidfuzz`, `feedparser` (declared, not yet actively used), and `pytest` / `pytest-asyncio` / `pytest-mock` for tests.

### Scoring methodology (the actionability engine)

Located in `kalshi_trader/actionability/`. There are **9 signals**, each normalized to `[0.0, 1.0]` (or `None` when there is insufficient data), defined in `signals.py`:

| Signal | Idea | Core formula (as written) | Data needed |
|--------|------|---------------------------|-------------|
| `volume_oi_ratio` | daily turnover | `min(1.0, volume_24h / open_interest / 0.50)` | market object |
| `relative_historical_volume` | volume vs 30-day baseline | `min(1, max(0, (ratio − 1) / 2))`, `ratio = volume_24h / avg(daily volumes)` | ≥3 daily candles |
| `volume_spike_short_term` | last-hour vs prior 3h | `min(1, max(0, (ratio − 1) / 1.5))`, `ratio = last_hour_vol / avg(prior 3h)` | ≥4 hourly candles |
| `oi_change` | OI growth over 24h | `min(1, max(0, oi_delta_pct / 0.10))` | ≥2 hourly candles |
| `price_momentum` | price move over 4h | `min(1, abs(close[-1] − close[0]) / 10.0)` (cents) | ≥4 hourly candles w/ trades |
| `intraday_hl` | position in intraday range | `abs(normalized_position − 0.5) * 2`, range ≥2¢ | ≥2 hourly candles + midpoint |
| `weekly_hl` | position in 7-day range | same as intraday over 7 daily candles | ≥2 daily candles + midpoint |
| `ofi` (order-flow imbalance) | taker side skew | `abs(yes_vol − no_vol) / (yes_vol + no_vol)` | live trades |
| `orderbook_skew` | depth skew near midpoint | `abs(yes_side_skew − 0.5) * 2` within 5¢ window | live orderbook |

**Composite score** (`scorer.py`) is a coverage-aware weighted average over the *non-None* signals:

```python
WEIGHTS = {
    "relative_historical_volume": 0.25,
    "volume_spike_short_term":    0.20,
    "price_momentum":             0.15,
    "volume_oi_ratio":            0.10,
    "oi_change":                  0.10,
    "intraday_hl":                0.08,
    "ofi":                        0.07,
    "weekly_hl":                  0.04,
    "orderbook_skew":             0.01,
}
# composite = weighted_sum / total_present_weight
# returns 0.0 if present weight / 1.00 < MIN_COVERAGE (0.30)
```

Missing signals don't drag the score down — weights are re-normalized over what's present — but a market scored on thin coverage is flagged via the coverage metric. `ofi` and `orderbook_skew` start as `None` and are filled in by `enrich_with_live()` for the top markets, after which composites are recomputed and re-sorted.

**Data model** (`models.py`): `Market` (ticker, event_ticker, series_ticker, title, yes_bid/ask, last_price, volume_24h, open_interest, category, close_time, status), `Candle` (OHLCV + open_interest, with `price_*` optional/None when a period had no trades), and `ScoredMarket` (the `Market` plus each signal value and the composite).

**Candle cache** (`store.py`): `SnapshotStore` backs candles in SQLite (`candle_cache.db`, WAL mode). Daily candles have a 23h TTL with a 30-day lookback; hourly candles a 55-min TTL with a 48h lookback. `refresh_stale()` batch-fetches stale tickers (100 per request, `Semaphore(8)`) and re-caches.

**Filtering** (`scanner.py`): keep markets with `close_time > now`, in the scored categories (elections, politics, entertainment, climate/weather, mentions, economics, science/technology — sports excluded unless `--category` overrides), excluding equity-index tickers, with `open_interest ≥ 100` and `volume_24h ≥ 10`.

**Output** (`score_markets.py`): markets are grouped by `event_ticker`, each event scored by the average of its markets, and the top N events are printed as a table (`EVENT | AVG | N | OI% | HIST | SPIKE | MOM | OFI | TITLE`). `--debug` prints the full 9-signal breakdown per event's best market.

### Kelly sizing & risk (`risk.py`)

`RiskManager` checks each `TradeIdea` against hard limits (daily loss, total exposure, per-category exposure, settlement proximity, minimum edge) and computes a half-Kelly position size:

```python
yes_net_odds        = (1.0 - market_prob) / market_prob
full_kelly_fraction = (probability * yes_net_odds - complement_probability) / yes_net_odds
half_kelly_fraction = max(full_kelly_fraction / 2.0, 0.0)
# after 3 consecutive losses in a category, multiply by 0.5 (→ quarter Kelly)
size_dollars        = half_kelly_fraction * balance   # then capped by config limits
```

Risk thresholds live in `config.py`: max single position $100, max single-trade loss $50, max total exposure $400, max per-category exposure $250, max 10 open positions, daily loss limit $100, no trading within 2h of settlement.

### Kalshi API integration

**Sync core** (`kalshi_auth.py`): `KalshiClient.from_env()` loads credentials from `.env` (without overwriting already-set env vars) and an RSA private key from a `.pem` file via `cryptography`. Requests are signed with **RSA-PSS / SHA-256 / MGF1**, salt length = digest length, over the message `timestamp_ms + METHOD + path`; the base64 signature plus key id and timestamp go in the `KALSHI-ACCESS-*` headers. It uses a pooled `requests.Session` (`HTTPAdapter`, 4 connections / 64 max per host) with a 45s GET timeout, and installs the OS trust store via `truststore` so corporate TLS-intercepting proxies work.

**Async wrapper** (`client.py`): wraps the sync client and runs blocking calls in a dedicated 64-worker `ThreadPoolExecutor` (`kalshi-http`) via `run_in_executor`, with shorter (15s) timeouts on POST/DELETE. Endpoints exposed include `/series`, `/events`, `/markets`, `/markets/{ticker}`, `/markets/{ticker}/orderbook`, `/markets/candlesticks` (batch, up to 100 tickers), `/markets/trades`, `/portfolio/balance|positions|fills|orders`.

**Environments** (`config.py`): demo at `https://demo-api.kalshi.co/trade-api/v2`, prod at `https://api.elections.kalshi.com/trade-api/v2`, selected by `KALSHI_ENV` (default `demo`). WebSocket URLs are configured but unused in the current scope.

**Concurrency / retries**: scanner uses `Semaphore(20)` for category enrichment and `Semaphore(10)` for live trade/orderbook fetches; the candle store uses `Semaphore(8)`. `_retry.with_retry()` does exponential backoff (2^attempt s) on HTTP 429 across 4 attempts and returns `{}` on exhaustion.

**Web links** (`web_links.py`): `kalshi_market_url(ticker)` reduces any series/event/market ticker to its series prefix and returns `https://kalshi.com/markets/<series_ticker lowercased>` — the only form verified to resolve (the site redirects to fill in the slug). E.g. `KXHIGHNY-26JUN02-B57.5` → `https://kalshi.com/markets/kxhighny`.

### Tests

`tests/` covers the core modules: `test_actionability.py` (signals/scoring), `test_config.py`, `test_executor.py`, `test_models.py`, `test_risk.py`, `test_scanner.py`. Run with `pytest`.

## Code References

GitHub permalink base (commit `42bc014`, pushed to `origin/main`):
`https://github.com/peak6-labs/ai_week/blob/42bc01462c4857d77d3dd227c656c6164fed3c05/<path>#L<line>`

- `kalshi_trader/actionability/signals.py` — the 9 signal functions, each normalized to `[0,1]`
- `kalshi_trader/actionability/scorer.py:33` — `WEIGHTS` dict and the composite formula (`_composite`, `MIN_COVERAGE = 0.30`)
- `kalshi_trader/actionability/store.py` — `SnapshotStore`, SQLite candle cache, TTLs and batch refresh
- `kalshi_trader/scanner.py` — `MarketScanner`, universe filter, `enrich_categories()`, `get_scored_markets()`
- `kalshi_trader/models.py:18` — `Market`; `:90` — `Candle`; `:102` — `ScoredMarket`
- `kalshi_trader/risk.py:82-93` — half-Kelly sizing math
- `kalshi_auth.py:82-102` — RSA-PSS request signing; `:24-33` — truststore install
- `kalshi_trader/client.py` — async wrapper + 64-worker executor + endpoint methods
- `kalshi_trader/web_links.py` — `kalshi_market_url()`
- `kalshi_trader/_retry.py` — `with_retry()` 429 backoff
- `kalshi_trader/config.py:35-42` — risk thresholds; base URLs; agent models
- `scripts/fetch_markets.py`, `scripts/score_markets.py` — CLI entry points
- `scripts/score_markets.py:54-66` — `_group_by_event`; `:80-102` — output table

## Architecture Documentation

- **Two-phase pipeline**: expensive universe fetch is done once and snapshotted; scoring runs cheaply and repeatedly against the snapshot plus a warm candle cache.
- **Two-tier data model**: cached candle history (daily + hourly via SQLite) provides most signals; a small set of live signals (`ofi`, `orderbook_skew`) are fetched only for the top-ranked markets to bound API cost.
- **Coverage-aware scoring**: composite re-normalizes over present signals and gates on a 30% minimum coverage, so partial data neither inflates nor deflates rankings silently.
- **Sync core / async shell**: a single signed `requests`-based client is the source of truth; an async wrapper fans calls out across a thread pool with semaphores and 429 backoff.
- **Demo-by-default safety**: environment is env-var driven and defaults to demo; secrets (`.pem`, key ids, `.env`) are gitignored and per-developer (see `KALSHI_SETUP.md`).
- **Conventions** (`CLAUDE.md`): fully spelled-out variable names (no abbreviations); market tickers shown in backticks and linked only via the safe `kalshi_market_url` series URL.

## Historical Context (from thoughts/ and plans/)

- `thoughts/shared/research/2026-06-02-kalshi-market-scoring-latency.md` — latency analysis of the scoring pipeline (serial cursor pagination, cold SQLite cache, per-ticker commits, thread-pool ceiling) with eight ranked improvement options. Note: parts of this document describe an earlier client state (e.g. "no `requests.Session`", a ~22-thread ceiling); the current `kalshi_auth.py`/`client.py` use a pooled session and a 64-worker executor. Treat the live code as the source of truth and this doc as point-in-time context.
- `plans/trading_plan.md` — "Agentic Prediction Market Trading System — Master Plan." Describes the full intended system: 6 specialist agents (conditional-event underpricing, flow/volume, cross-platform arbitrage, external datasets, sentiment/news, illiquid market-making) + 1 coordinator (dedupe, scoring, portfolio constraints, Kelly sizing). Documents the **Kalshi fee structure** — taker fee `ceil(0.07 · C · P · (1−P))` rounded up per order, $0 maker on most markets, and a `0.035` tier for S&P/Nasdaq index markets — and flags that `risk.estimate_fee_dollars` applies a flat `0.07` with no round-up or per-market tier. Roadmap: Days 1–2 foundation + agents A1/A3 with a human review gate; Days 3–4 coordinator + market-making and partial automation; Day 5 wind down.
- `kalshi_trader/actionability/README.md` — architecture doc for the scoring pipeline (two-step process, signal definitions, scored categories).
- `kalshi_trader/actionability/EXAMPLE_SCORE.md` — a captured run (prod, read-only, 2026-06-02): 482,115 open markets → 570 active after filters → 205 events; top idea `KXMARTINDNCOUT-26MAY` (0.852, 100% coverage).
- `CLAUDE.md` — coding standards (naming, ticker formatting). `KALSHI_SETUP.md` — per-developer credential setup and a note that an old committed key remains in git history and should be rotated in the Kalshi portal.

## Related Research

- `thoughts/shared/research/2026-06-02-kalshi-market-scoring-latency.md` — performance/latency deep-dive on the same pipeline.

## Open Questions

- The scoring/signal layer is built and demonstrated; the multi-agent trade-generation layer (`kalshi_trader/agents/`, `kalshi_trader/external/`) is still placeholder, so the path from "scored markets" to "executed trades" is partially manual today.
- The fee-estimation discrepancy noted in `plans/trading_plan.md` (flat `0.07` in code vs. the documented per-order-type / per-market / per-cent-rounded structure) is recorded but not yet reconciled.
