-- ============================================================
-- Migration 001: initial schema
-- Project: ai_week (xhyqdrhrwgebidvsnwbx)
-- ============================================================

-- ------------------------------------------------------------
-- 1. trades
--    Populated from TradeIdea + OrderResult + executor count.
--    Created before signals since signals has a FK here.
-- ------------------------------------------------------------
CREATE TABLE trades (
    id                      uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    created_at              timestamptz NOT NULL DEFAULT now(),
    ticker                  text NOT NULL,
    side                    text NOT NULL CHECK (side IN ('yes', 'no')),
    action                  text NOT NULL CHECK (action IN ('buy', 'sell')),
    contracts               integer NOT NULL,
    entry_price_cents       double precision NOT NULL,   -- TradeIdea.market_price
    fill_price_cents        double precision,            -- OrderResult.fill_price, null until filled
    size_dollars            double precision NOT NULL,   -- OrderResult.size_dollars
    suggested_size_dollars  double precision,            -- RiskDecision.approved_size_dollars
    status                  text NOT NULL,               -- OrderResult.status
    kalshi_order_id         text,                        -- OrderResult.order_id
    agent_id                text NOT NULL,               -- TradeIdea.agent_id
    confidence              double precision NOT NULL,   -- TradeIdea.confidence
    reasoning               text NOT NULL,               -- TradeIdea.reasoning
    category                text NOT NULL DEFAULT '',    -- TradeIdea.category
    exit_reason             text,                        -- 'take_profit', 'volume_spike', 'stale_thesis', 'manual'
    realized_pnl_dollars    double precision,            -- null until position closed
    closed_at               timestamptz                  -- null until position closed
);

ALTER TABLE trades ENABLE ROW LEVEL SECURITY;

CREATE INDEX trades_ticker_idx     ON trades (ticker);
CREATE INDEX trades_created_at_idx ON trades (created_at);
CREATE INDEX trades_agent_id_idx   ON trades (agent_id);

-- ------------------------------------------------------------
-- 2. signals
--    One row per SignalEstimate. trade_id is null when the
--    signal did not result in an executed trade.
--    Unique constraint: at most one signal per source per trade.
-- ------------------------------------------------------------
CREATE TABLE signals (
    id                  uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    created_at          timestamptz NOT NULL DEFAULT now(),
    ticker              text NOT NULL,
    source              text NOT NULL,              -- 'noaa_gfs', 'polymarket_price', 'polymarket_whale', etc.
    probability         double precision NOT NULL,  -- SignalEstimate.probability (0.0–1.0)
    uncertainty         double precision NOT NULL,  -- SignalEstimate.uncertainty
    weight              double precision NOT NULL,  -- SignalEstimate.weight
    data_issued_at      timestamptz NOT NULL,       -- SignalEstimate.data_issued_at (from API, not fetch time)
    metadata            jsonb NOT NULL DEFAULT '{}', -- SignalEstimate.metadata (narrative, gap_cents, etc.)
    trade_id            uuid REFERENCES trades (id), -- null if signal didn't lead to a trade
    market_resolved_yes boolean,                    -- filled after market settles
    brier_score         double precision            -- (probability - outcome)^2, filled after settlement
);

ALTER TABLE signals ENABLE ROW LEVEL SECURITY;

-- Enforce one signal per source per trade (nulls excluded)
CREATE UNIQUE INDEX signals_trade_source_unique
    ON signals (trade_id, source)
    WHERE trade_id IS NOT NULL;

CREATE INDEX signals_ticker_idx     ON signals (ticker);
CREATE INDEX signals_source_idx     ON signals (source);
CREATE INDEX signals_created_at_idx ON signals (created_at);
CREATE INDEX signals_trade_id_idx   ON signals (trade_id);

-- ------------------------------------------------------------
-- 3. positions
--    Current and historical positions. Partial unique index
--    prevents two open positions on the same ticker.
-- ------------------------------------------------------------
CREATE TABLE positions (
    id                    uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    ticker                text NOT NULL,
    side                  text NOT NULL CHECK (side IN ('yes', 'no')),
    contracts             integer NOT NULL,          -- Position.quantity
    category              text NOT NULL DEFAULT '',  -- Position.category
    avg_entry_price_cents double precision NOT NULL, -- Position.avg_price
    opened_at             timestamptz NOT NULL,
    closed_at             timestamptz,               -- null while open
    opening_trade_id      uuid NOT NULL REFERENCES trades (id),
    closing_trade_id      uuid REFERENCES trades (id), -- null while open
    realized_pnl_dollars  double precision            -- null while open
);

ALTER TABLE positions ENABLE ROW LEVEL SECURITY;

-- Only one open position per ticker at a time
CREATE UNIQUE INDEX positions_open_ticker_unique
    ON positions (ticker)
    WHERE closed_at IS NULL;

CREATE INDEX positions_ticker_idx ON positions (ticker);

-- ------------------------------------------------------------
-- 4. polymarket_markets
--    Lookup catalog refreshed every 1-2 hours via UPSERT.
--    outcome_prices: insert json.loads(market["outcomePrices"])
--    before storing — API returns it as a JSON string, not array.
-- ------------------------------------------------------------
CREATE TABLE polymarket_markets (
    condition_id      text PRIMARY KEY,        -- market["conditionId"]
    question          text NOT NULL,           -- market["question"]
    yes_price         double precision,        -- float(json.loads(outcomePrices)[0])
    active            boolean NOT NULL DEFAULT true,
    closed            boolean NOT NULL DEFAULT false,
    volume_24h        double precision,        -- float(market["volume24hr"])
    outcome_prices    jsonb,                   -- json.loads(market["outcomePrices"])
    last_refreshed_at timestamptz NOT NULL DEFAULT now()
);

ALTER TABLE polymarket_markets ENABLE ROW LEVEL SECURITY;

CREATE INDEX polymarket_markets_active_idx
    ON polymarket_markets (active, closed);
