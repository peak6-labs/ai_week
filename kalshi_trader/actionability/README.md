# Kalshi Market Scoring Pipeline

Ranks open Kalshi markets by **actionability** — a composite signal that surfaces markets with unusual volume, price movement, or order flow right now.

---

## How to run

### Step 1 — Fetch live markets (once per day)

```bash
KALSHI_ENV=prod python scripts/fetch_markets.py
```

Paginates through all ~480k open markets, fetches category data from the events API (~2,500 unique events), and saves to `live_markets.json`. Takes ~7 minutes on first run. Add `--verbose` to see per-page progress.

```
--output PATH     Save snapshot to PATH (default: live_markets.json)
--limit N         Stop after N markets — for testing only
--verbose         Show DEBUG-level detail
```

### Step 2 — Score markets (run as often as you like)

```bash
KALSHI_ENV=prod python scripts/score_markets.py --markets-file live_markets.json
```

Loads the snapshot, refreshes stale candle data (SQLite cache), fetches live trades and orderbooks for top markets, and prints the ranked list. Takes ~15s on a warm cache.

```
--markets-file PATH   Use pre-fetched snapshot (skip the ~7min API fetch)
--top N               Show top N markets (default: 10)
--category NAME       Filter to a single category (elections, economics, …)
--verbose             Show DEBUG-level cache detail
```

---

## Architecture

```
Morning (once)
  scripts/fetch_markets.py
       │
       ├── GET /markets  (paginated, ~480 pages × 1000 markets)
       ├── GET /events/{ticker}  (category enrichment, ~2500 unique events, parallel)
       └── live_markets.json

Scoring runs (repeatedly throughout the day)
  scripts/score_markets.py --markets-file live_markets.json
       │
       ├── load_snapshot()          live_markets.json
       ├── filter_markets()         close_time · SCORED_CATEGORIES · OI≥100 · vol≥10
       ├── store.refresh_stale()    candle_cache.db  (SQLite, WAL mode)
       │     └── GET /candlesticks  batched 100 tickers/req · Semaphore(8) · 30d daily + 48h hourly
       ├── scorer.score_all()       candle-based signals for all filtered markets
       ├── GET /trades  (top 50)    OFI signal
       ├── GET /orderbook  (top 20) depth skew signal
       └── scorer.enrich_with_live()  re-score + re-sort → ranked ScoredMarket list
```

---

## Signals

Each signal normalises to **[0.0, 1.0]**. Signals that need candle history return `None` until the cache warms up (first scoring run of the day).

| Signal | Weight | What it measures | Data required |
|--------|-------:|------------------|---------------|
| `relative_historical_volume` | **0.25** | Today's volume vs 30-day daily average — detects unusual activity | ≥ 3 daily candles |
| `volume_spike_short_term` | **0.20** | Latest active hour's volume vs prior active-hour average — detects sparse-market bursts | ≥ 2 hourly candles |
| `price_momentum` | **0.15** | Absolute price move over the last 4 hours (10¢ move = 1.0) | ≥ 4 hourly candles |
| `volume_oi_ratio` | **0.10** | Daily turnover rate: `volume_24h / open_interest` (capped at 0.5 = 1.0) | Market object only |
| `oi_change` | **0.10** | 24h open-interest growth — new money entering the market (10% = 1.0) | ≥ 2 hourly candles |
| `intraday_hl` | **0.08** | Where current price sits in today's high-low range (0 = at low, 1 = at high) | hourly candles |
| `ofi` | **0.07** | Order-flow imbalance: fraction of recent taker volume on the YES side | Live trade data (top 50 markets) |
| `weekly_hl` | **0.04** | Where current price sits in the 7-day high-low range | ≥ 7 daily candles |
| `orderbook_skew` | **0.01** | Bid/ask depth imbalance within 5¢ of mid-price | Live orderbook (top 20 markets) |

---

## Composite score formula

The raw composite score is a **weighted average over whichever signals are non-`None`**, re-normalised so absent signals don't artificially lower the score:

```
raw_composite = Σ (signal_value × weight)  /  Σ weight
                  for all non-None signals       for same signals
```

For example, if a market has no candle history yet (all candle signals are `None`), the raw composite is computed solely from `volume_oi_ratio` and any live signals available. Once the cache warms up the full set of weights applies.

The ranking score then applies two soft multipliers — a **spread liquidity penalty** and a **settlement-proximity preference**:

```
rank_score = raw_composite × spread_penalty_multiplier × settlement_proximity_multiplier
```

| YES bid/ask spread | Multiplier |
|--------------------|-----------:|
| one-sided book | 0.50 |
| ≤ 2¢ | 1.00 |
| ≤ 5¢ | 0.95 |
| ≤ 10¢ | 0.85 |
| ≤ 20¢ | 0.70 |
| > 20¢ | 0.55 |

The **settlement-proximity multiplier** favors markets that resolve soon over ones that settle a long time from now (capital turns over faster and the thesis is exposed to fewer unforeseen developments). It is a soft down-rank only — it never zeroes a market out, so every open market still appears, just lower:

| Time to close | Multiplier |
|---------------|-----------:|
| ≤ 24h | 1.00 |
| ≤ 3 days | 0.90 |
| ≤ 7 days | 0.78 |
| ≤ 30 days | 0.60 |
| ≤ 90 days | 0.42 |
| > 90 days | 0.28 |

`ScoredMarket.composite_score` stores the fully-adjusted rank score. The JSON output also includes `raw_best_score`, `spread_penalty_multiplier`, `settlement_proximity_multiplier`, and `hours_to_close` so analysts can see the original signal strength and each adjustment separately. Because this multiplier folds into the event `average_score`, the orchestrator's "top rows by score" deep-signal subset selection inherits the same preference for sooner-settling markets.

---

## Scored categories (default filter)

When `--category` is not specified, only markets in these categories are scored:

`elections` · `politics` · `entertainment` · `climate and weather` · `mentions` · `economics` · `science and technology`

Sports markets are excluded. Pass `--category sports` to score only sports, or any other category name to restrict to that category.
