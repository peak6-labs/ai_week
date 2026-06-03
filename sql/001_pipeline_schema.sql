-- Full pipeline schema for the Kalshi agentic trader — ONE migration.
-- Project: ai_week (xhyqdrhrwgebidvsnwbx) ONLY.
--
-- Apply once via Supabase dashboard → SQL Editor (or psql with the DB URL).
-- Everything is IF NOT EXISTS and additive; safe to re-run. No DELETEs — the
-- pipeline only INSERTs / UPSERTs, matching kalshi_trader/db.py rules.
--
-- The pipeline writes to these tables when present and falls back to a local
-- JSONL store (data/paper/) when they're absent, so this is an enabling upgrade,
-- not a hard dependency.

-- ===========================================================================
-- Paper-trade calibration: record recommendations, mark them to market.
-- ===========================================================================
create table if not exists public.recommendations (
    id                      uuid primary key default gen_random_uuid(),
    created_at              timestamptz not null default now(),
    cycle_ts                text not null,
    ticker                  text not null,
    side                    text not null check (side in ('yes','no')),
    entry_price_cents       numeric not null,
    predicted_prob          numeric,
    edge_cents              numeric,
    n_sources               int,
    sources                 jsonb,
    category                text,
    suggested_size_dollars  numeric,
    status                  text not null default 'open' check (status in ('open','resolved')),
    paper_only              boolean not null default true
);
create index if not exists recommendations_status_idx on public.recommendations (status);
create index if not exists recommendations_ticker_idx on public.recommendations (ticker);
create index if not exists recommendations_cycle_idx  on public.recommendations (cycle_ts);

create table if not exists public.recommendation_marks (
    id                   uuid primary key default gen_random_uuid(),
    recommendation_id    uuid not null references public.recommendations(id),
    checked_at           timestamptz not null default now(),
    current_value_cents  numeric,
    pnl_cents            numeric,
    would_profit         boolean,
    resolved             boolean not null default false
);
create index if not exists recommendation_marks_rec_idx on public.recommendation_marks (recommendation_id);

-- ===========================================================================
-- Per-cycle pipeline run metadata.
-- ===========================================================================
create table if not exists public.cycles (
    id                uuid primary key default gen_random_uuid(),
    cycle_ts          text not null unique,
    started_at        timestamptz not null default now(),
    markets_scored    int,
    markets_2plus_src int,
    candidates        int,
    approved_ideas    int,
    notes             text
);

-- ===========================================================================
-- Per-cycle snapshot of every scored market + its signals. Enables studying
-- which signals fire, backtesting, and tracking how prices/signals evolve.
-- ===========================================================================
create table if not exists public.scored_markets (
    id                    uuid primary key default gen_random_uuid(),
    created_at            timestamptz not null default now(),
    cycle_ts              text not null,
    ticker                text not null,
    event_ticker          text,
    title                 text,
    category              text,
    yes_bid               numeric,
    yes_ask               numeric,
    volume_24h            bigint,
    open_interest         bigint,
    composite_score       numeric,
    coverage_pct          numeric,
    signals               jsonb,   -- magnitude actionability signals
    signal_estimates      jsonb,   -- directional trade signal estimates
    combined_probability  numeric,
    edge_cents            numeric,
    n_sources             int,
    worth_trading         boolean,
    side                  text,
    unique (cycle_ts, ticker)
);
create index if not exists scored_markets_ticker_idx on public.scored_markets (ticker);
create index if not exists scored_markets_cycle_idx  on public.scored_markets (cycle_ts);

-- ===========================================================================
-- Kalshi market catalog (optional migration of live_markets.json). Lets the
-- scout read the filtered tradeable universe server-side instead of parsing the
-- 281MB snapshot. Refreshed by an ingest job; raw row kept in `raw` for fidelity.
-- ===========================================================================
create table if not exists public.markets (
    ticker         text primary key,
    event_ticker   text,
    series_ticker  text,
    title          text,
    category       text,
    status         text,
    yes_bid        numeric,
    yes_ask        numeric,
    last_price     numeric,
    volume_24h     bigint,
    open_interest  bigint,
    close_time     timestamptz,
    raw            jsonb,
    snapshot_at    timestamptz,
    updated_at     timestamptz not null default now()
);
create index if not exists markets_category_idx   on public.markets (category);
create index if not exists markets_close_time_idx  on public.markets (close_time);
create index if not exists markets_status_idx       on public.markets (status);
create index if not exists markets_event_idx        on public.markets (event_ticker);

-- ===========================================================================
-- Polymarket whale intelligence (from the on-chain chain fetcher analysis).
-- ===========================================================================
create table if not exists public.whales (
    id                uuid primary key default gen_random_uuid(),
    wallet            text not null unique,
    label             text,
    realized_pnl_usd  numeric,
    total_pnl_usd     numeric,
    volume_usd        numeric,
    trade_count       int,
    win_rate          numeric,
    markets_traded    int,
    first_seen        timestamptz,
    last_active       timestamptz,
    source            text,         -- e.g. 'chain_backfill', 'leaderboard'
    metadata          jsonb,
    updated_at        timestamptz not null default now()
);
create index if not exists whales_pnl_idx on public.whales (realized_pnl_usd desc);

create table if not exists public.whale_trades (
    id               uuid primary key default gen_random_uuid(),
    wallet           text not null,
    condition_id     text,
    market_question  text,
    outcome          text,         -- 'yes'/'no' or token outcome
    side             text,         -- 'buy'/'sell'
    size_usd         numeric,
    price            numeric,
    traded_at        timestamptz,
    tx_hash          text,
    metadata         jsonb,
    created_at       timestamptz not null default now()
);
create index if not exists whale_trades_wallet_idx on public.whale_trades (wallet);
create index if not exists whale_trades_condition_idx on public.whale_trades (condition_id);
-- Dedupe key for re-runnable backfills (tx_hash may be null → fall back to natural key).
create unique index if not exists whale_trades_dedupe_idx
    on public.whale_trades (wallet, condition_id, traded_at, size_usd, price);

-- ===========================================================================
-- Sportsbook odds cache (for the sports-market signal: DraftKings/FanDuel via
-- an aggregator, or ESPN). Implied probability to compare against Kalshi.
-- ===========================================================================
create table if not exists public.sportsbook_odds (
    id            uuid primary key default gen_random_uuid(),
    fetched_at    timestamptz not null default now(),
    sport         text,
    league        text,
    event_id      text,
    event_name    text,
    commence_time timestamptz,
    market_type   text,          -- 'h2h', 'spreads', 'totals', ...
    book          text,          -- 'draftkings', 'fanduel', 'consensus', ...
    outcome       text,
    implied_prob  numeric,
    american_odds numeric,
    decimal_odds  numeric,
    kalshi_ticker text,          -- set when matched to a Kalshi market
    metadata      jsonb
);
create index if not exists sportsbook_odds_event_idx  on public.sportsbook_odds (event_id);
create index if not exists sportsbook_odds_ticker_idx on public.sportsbook_odds (kalshi_ticker);
create index if not exists sportsbook_odds_fetched_idx on public.sportsbook_odds (fetched_at desc);
