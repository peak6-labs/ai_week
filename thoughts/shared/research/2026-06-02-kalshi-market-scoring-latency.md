---
date: 2026-06-02T08:16:31-05:00
researcher: Alexandra Lewis
git_commit: ba694195d5e70ce398916bd806e4bb888ae3a9f8
branch: lle-market-scoring
repository: peak6-labs/ai_week
topic: "Why pulling all Kalshi markets to score is slow, and how to minimize latency"
tags: [research, codebase, kalshi, latency, scanner, actionability, candles]
status: complete
last_updated: 2026-06-02
last_updated_by: Alexandra Lewis
---

# Research: Kalshi market-scoring latency

**Date**: 2026-06-02T08:16:31-05:00
**Researcher**: Alexandra Lewis
**Git Commit**: ba69419 (`lle-market-scoring`)
**Repository**: peak6-labs/ai_week

## Research Question
Why does it take so long to pull all the markets in Kalshi to score, and how can we
improve this process so that latency is minimized?

## Summary

The scoring run does three network-heavy things in sequence, and each one is shaped
in a way that multiplies round trips:

1. **Pull every open market** via serial cursor pagination at `limit=200`
   ([scanner.py:16-32](kalshi_trader/scanner.py#L16-L32)). At prod scale
   (~58k markets, per `live_markets.csv`) that is ~**290 sequential HTTP round
   trips** that cannot overlap because each page's cursor comes from the previous
   page's response.
2. **Backfill candles for every ticker** on a cold cache — daily (30-day) + hourly
   (48h) — in ~**1,160 batch requests** ([store.py:177-261](kalshi_trader/actionability/store.py#L177-L261)).
3. **Score all markets and enrich the top N** with live trades/orderbooks
   ([scanner.py:39-99](kalshi_trader/scanner.py#L39-L99)).

Four properties of the current implementation make this slower than the request
count alone would suggest:

- **No `requests.Session`** anywhere — every call uses module-level `requests.get`
  ([kalshi_auth.py:102](kalshi_auth.py#L102)), so each of the hundreds-to-thousands
  of requests pays a fresh TCP + TLS handshake (through the corporate
  TLS-intercepting proxy noted in `kalshi_auth.py`), with no keep-alive reuse.
- **"Parallel" batches are capped at ~22**, not unbounded. The async layer is
  `asyncio.to_thread` over the blocking `requests` library
  ([client.py:18-19](kalshi_trader/client.py#L18-L19)); `to_thread` uses the default
  thread pool whose ceiling here is `min(32, cpu+4) = 22`. So the ~1,160 candle
  batches drain ~22 at a time despite the "parallel batches" log line.
- **Per-ticker SQLite commits** — `_upsert_candles` commits once per ticker
  ([store.py:156-171](kalshi_trader/actionability/store.py#L156-L171)). On a cold
  cache that is up to ~116k synchronous `commit()` calls (fsync each).
- **The cache is effectively always cold** — `candle_cache.db` is 20 KB (schema
  only), so the expensive backfill in step 2 runs nearly every time instead of being
  skipped by the TTLs.

The whole `--category` path also scores the *entire* universe and filters only at the
very end ([score_markets.py:31-32](score_markets.py#L31-L32)), even though the scanner
can push a `series_ticker` filter into the API query
([scanner.py:20-24](kalshi_trader/scanner.py#L20-L24)).

> Note: this report answers a performance question, so it includes both a description
> of the current behavior (Detailed Findings) and improvement options (How to Minimize
> Latency), per the explicit ask.

## Detailed Findings

### The scoring pipeline (orchestration)

`score_markets.py` → `MarketScanner.get_scored_markets(...)` runs the full flow
([scanner.py:39-99](kalshi_trader/scanner.py#L39-L99)):

1. `get_open_markets()` — fetch all open markets (live).
2. `store.refresh_stale(all_tickers, client, now)` — refresh stale candles.
3. `scorer.score_all(markets, store)` — compute candle-based signals from cache.
4. `asyncio.gather(...)` live trades for top 50 + orderbooks for top 20.
5. `scorer.enrich_with_live(...)` — add OFI / skew, re-rank.

Steps 1 → 2 → 3 are strictly sequential; the whole universe must be paginated before
any candle refresh starts, and all candles must land before scoring.

### Phase 1 — Pulling all markets (serial cursor pagination)

[scanner.py:16-32](kalshi_trader/scanner.py#L16-L32):

```python
cursor = ""
while True:
    resp = await self._client.get_markets(status="open", cursor=cursor, limit=200, **kwargs)
    for m in resp.get("markets", []):
        markets.append(self._parse_market(m))
    cursor = resp.get("cursor", "")
    if not cursor:
        break
```

- `limit=200` is hardcoded in both the scanner and the client default
  ([client.py:47-53](kalshi_trader/client.py#L47-L53)).
- Pagination is inherently serial: page N+1 needs the cursor returned by page N, so
  these requests cannot be issued concurrently.
- At ~58k markets that is ~290 sequential round trips. This cost is paid on **every**
  run regardless of cache warmth.
- `_fetch_markets.py` (a separate ad-hoc script) does the same serial loop against
  `/events?with_nested_markets=true` and produced the 58,005-row `live_markets.csv`,
  confirming the universe size.

### Phase 2 — Candle backfill (the cold-cache cost)

`SnapshotStore.refresh_stale` ([store.py:177-218](kalshi_trader/actionability/store.py#L177-L218)):

- Computes stale daily and stale hourly ticker lists, then runs the daily and hourly
  fetches concurrently via `asyncio.gather`.
- `_fetch_and_store` ([store.py:220-261](kalshi_trader/actionability/store.py#L220-L261))
  chunks tickers into `BATCH_SIZE = 100` and fires all batches with `asyncio.gather`,
  using `get_market_candlesticks_batch` ([client.py:85-103](kalshi_trader/client.py#L85-L103))
  — up to 100 tickers per request.
- On a cold cache with ~58k tickers: ~580 daily + ~580 hourly = **~1,160 batch
  requests**. Batch payload sizes are safe (100 × 30 daily = 3,000 candles; 100 × 48
  hourly = 4,800; both under the 10,000-candle cap).

Why it is slower than "1,160 parallel requests":

- **Thread-pool ceiling.** `client.get` → `asyncio.to_thread(self._sync.get, ...)`
  ([client.py:18-19](kalshi_trader/client.py#L18-L19)) → blocking `requests`. `to_thread`
  uses the default `ThreadPoolExecutor`, ceiling `min(32, cpu+4)`; on this 18-core
  machine that is **22 concurrent requests max**. The ~1,160 batches drain in ~53
  waves.
- **Commit storm.** After the gather, results are written serially and
  `_upsert_candles` commits **once per ticker**
  ([store.py:156-171](kalshi_trader/actionability/store.py#L156-L171), called from
  [store.py:255-260](kalshi_trader/actionability/store.py#L255-L260)). Cold cache ⇒ up
  to ~116k synchronous commits (each an fsync) on a single connection.

TTLs ([store.py:91-92](kalshi_trader/actionability/store.py#L91-L92)): daily 23h,
hourly 55min. These are designed to make warm runs cheap — but `candle_cache.db` is
currently 20 KB (schema only), so the backfill effectively runs every time.

### Phase 3 — Scoring and live enrichment

- `score_all` ([scorer.py:40-48](kalshi_trader/actionability/scorer.py#L40-L48)) is
  CPU + SQLite-read bound: `_score_one` issues 2 indexed SELECTs per market
  ([scorer.py:50-52](kalshi_trader/actionability/scorer.py#L50-L52),
  [store.py:126-144](kalshi_trader/actionability/store.py#L126-L144)). At 58k markets
  that is ~116k reads on the single event-loop-thread connection — fast per query
  (covered by the `(ticker, period_interval, end_period_ts)` PK) but a lot of
  round trips. Secondary to phases 1–2.
- Live enrichment fetches trades for the top 50 and orderbooks for the top 20
  concurrently ([scanner.py:76-85](kalshi_trader/scanner.py#L76-L85)) — 70 requests,
  also capped at ~22 threads. Minor.

### The HTTP layer (applies to every phase)

- `kalshi_auth.KalshiClient.get` calls module-level `requests.get(...)`
  ([kalshi_auth.py:99-109](kalshi_auth.py#L99-L109)) — **no `requests.Session`**, so
  no connection pooling / keep-alive. Every request reopens TCP and renegotiates TLS.
- `client.py` `post`/`delete` likewise use module-level `requests.post/delete`
  ([client.py:21-45](kalshi_trader/client.py#L21-L45)).
- `aiohttp>=3.13.5` is already a declared dependency (`requirements.txt`) and
  installed, but the scanner/candle path does not use it — it goes through
  `requests` + threads.
- TLS cost is amplified by the corporate TLS-intercepting proxy that `truststore`
  exists to satisfy ([kalshi_auth.py:27-32](kalshi_auth.py#L27-L32)); handshakes are
  not free in this environment.

### The `--category` path scores everything first

`get_scored_markets` calls `get_open_markets()` with **no** category
([scanner.py:59](kalshi_trader/scanner.py#L59)), so the full universe is pulled,
candle-refreshed, and scored; the category filter is applied only afterward in the
CLI ([score_markets.py:31-32](score_markets.py#L31-L32)). The scanner already supports
pushing a `series_ticker` filter into the `/markets` query
([scanner.py:20-24](kalshi_trader/scanner.py#L20-L24)), but that branch is unused by
the scoring entrypoint.

## How to Minimize Latency (options)

Ordered by leverage-to-effort.

1. **Filter the universe before backfilling candles.** Latency scales with the candle
   set, and most of the ~58k markets have ~zero volume. Pre-filtering by
   volume/open-interest/liquidity (the data is already in the market payload, and
   `live_markets.csv` is sorted by volume) to the few thousand that matter shrinks
   phases 2, 3, and 4 proportionally. Highest leverage.
2. **Reuse one `requests.Session`** (or move the hot path to `aiohttp`, already a
   dependency). Connection pooling + keep-alive removes a TCP+TLS handshake from every
   one of the hundreds-to-thousands of calls — helps all three phases. Low effort.
3. **Push `--category`/`series_ticker` into the API query** instead of pulling-then-
   filtering, so the category path stops paying for the full universe
   ([scanner.py:59](kalshi_trader/scanner.py#L59) → pass the category through).
4. **Raise pagination page size** from 200 toward Kalshi's documented max (1000) to
   cut phase-1 round trips ~5× (~290 → ~58). Verify the current max against the live
   API. Pagination stays serial, but there are far fewer hops.
5. **Lift the candle-fetch concurrency ceiling.** Either bump the executor
   (`ThreadPoolExecutor(max_workers=...)` via `loop.set_default_executor`, or a
   dedicated executor) or switch to `aiohttp` with a bounded `Semaphore`, so the
   ~1,160 batches don't drain ~22 at a time. Pair with respect for Kalshi rate limits.
6. **Collapse the SQLite commit storm.** Accumulate upserts and commit per batch (or
   once at the end) instead of per ticker, and set `PRAGMA journal_mode=WAL` +
   `synchronous=NORMAL`. Turns ~116k fsyncs into a handful.
7. **Keep the cache actually warm.** With phases 2's TTLs (23h/55min), a persisted,
   pre-warmed `candle_cache.db` lets most runs skip the backfill entirely. The 20 KB
   db suggests it is being recreated cold.
8. **Pipeline pagination with refresh.** Pagination must stay serial, but candle
   refresh for a page's tickers can start as soon as that page lands, overlapping
   phase 1 with phase 2 instead of waiting for the entire universe first.

## Code References
- `kalshi_trader/scanner.py:16-32` — serial cursor pagination, `limit=200`
- `kalshi_trader/scanner.py:39-99` — `get_scored_markets` pipeline orchestration
- `kalshi_trader/scanner.py:59` — full universe fetched even when a category is set
- `kalshi_trader/client.py:18-19` — `asyncio.to_thread` over blocking `requests`
- `kalshi_trader/client.py:47-53` — `get_markets` default `limit=200`
- `kalshi_trader/client.py:85-103` — batch candlesticks (≤100 tickers/request)
- `kalshi_trader/actionability/store.py:177-218` — `refresh_stale` staleness + gather
- `kalshi_trader/actionability/store.py:220-261` — `_fetch_and_store` batching
- `kalshi_trader/actionability/store.py:156-171` — per-ticker commit (fsync storm)
- `kalshi_trader/actionability/store.py:91-92` — daily/hourly TTLs
- `kalshi_trader/actionability/scorer.py:40-79` — `score_all` / `_score_one` (2 reads each)
- `kalshi_auth.py:99-109` — module-level `requests.get`, no Session
- `kalshi_auth.py:27-32` — truststore / corporate TLS proxy context
- `score_markets.py:29-32` — entrypoint; category filtered post-scoring

## Architecture Documentation
- Async wrapper pattern: blocking `requests` calls are offloaded with
  `asyncio.to_thread` rather than using a native async HTTP client; concurrency is
  therefore bounded by the default thread pool.
- Two-tier data model: a slow-changing candle history cached in local SQLite
  (`SnapshotStore`) feeds bulk scoring, while fast-changing trades/orderbooks are
  fetched live only for the top-ranked subset.
- Weighted-signal scoring with re-normalization over present signals
  ([scorer.py:110-121](kalshi_trader/actionability/scorer.py#L110-L121)); live signals
  (OFI, orderbook skew) are layered on after the bulk pass.

## Historical Context (from thoughts/)
No `thoughts/` directory existed prior to this research; this is the first document
under `thoughts/shared/research/`. (The repo's `hack/spec_metadata.sh` and
`humanlayer` sync tooling referenced by the research workflow are not present here, so
metadata was gathered manually and no sync was run.)

## Related Research
None yet — first research doc in this repo.

## Open Questions
- Live count and distribution of open markets returned by `/markets?status=open`
  (vs the 58k events-flattened figure) and how many clear a sensible volume floor.
- Kalshi's current per-endpoint rate limits and the true max `limit` for `/markets`,
  which bound how aggressively phases 1 and 2 can be widened.
- Whether `candle_cache.db` is intended to persist between runs or is recreated each
  time (it is currently 20 KB / schema-only).
