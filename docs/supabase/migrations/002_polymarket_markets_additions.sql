-- ============================================================
-- Migration 002: polymarket_markets additions
-- Project: ai_week (xhyqdrhrwgebidvsnwbx)
-- ============================================================

ALTER TABLE polymarket_markets
    -- YES token ID needed to call clob midpoint and clob book
    ADD COLUMN IF NOT EXISTS clob_token_ids   jsonb,

    -- Human-readable slug for deduplication
    ADD COLUMN IF NOT EXISTS slug             text,

    -- Gamma snapshot spread — avoids a CLOB call on markets
    -- that clearly fail the depth filter
    ADD COLUMN IF NOT EXISTS best_bid         double precision,
    ADD COLUMN IF NOT EXISTS best_ask         double precision,

    -- Resolution date — needed for hours-to-close filter
    ADD COLUMN IF NOT EXISTS end_date         timestamptz,

    -- negRisk markets have different resolution mechanics;
    -- must not be conflated with standard binary markets
    ADD COLUMN IF NOT EXISTS neg_risk         boolean NOT NULL DEFAULT false;

CREATE UNIQUE INDEX IF NOT EXISTS polymarket_markets_slug_idx
    ON polymarket_markets (slug)
    WHERE slug IS NOT NULL;

CREATE INDEX IF NOT EXISTS polymarket_markets_volume_idx
    ON polymarket_markets (volume_24h DESC NULLS LAST);

CREATE INDEX IF NOT EXISTS polymarket_markets_end_date_idx
    ON polymarket_markets (end_date);
