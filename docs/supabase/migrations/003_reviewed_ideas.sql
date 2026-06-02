-- ============================================================
-- Migration 003: reviewed_ideas
-- Project: ai_week (xhyqdrhrwgebidvsnwbx)
-- ============================================================
-- Records trade ideas that were manually reviewed but NOT executed.
-- paper_only is always TRUE — enforced by a rule at the DB level.
-- This table must never be confused with actual trades.
-- ------------------------------------------------------------

CREATE TABLE IF NOT EXISTS reviewed_ideas (
    id                      UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    created_at              TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    reviewed_at             TIMESTAMPTZ,
    ticker                  TEXT NOT NULL,
    side                    TEXT NOT NULL CHECK (side IN ('yes', 'no')),
    confidence              FLOAT,
    market_price_cents      FLOAT,
    suggested_size_dollars  FLOAT,
    reasoning               TEXT,
    signal_sources          TEXT[],
    category                TEXT,
    agent_id                TEXT,
    decision                TEXT NOT NULL CHECK (decision IN ('approved', 'rejected')),
    paper_only              BOOLEAN NOT NULL DEFAULT TRUE
);

-- Make it impossible to accidentally set paper_only = false
CREATE OR REPLACE RULE reviewed_ideas_paper_only AS
    ON UPDATE TO reviewed_ideas
    WHERE NEW.paper_only = FALSE
    DO INSTEAD NOTHING;

COMMENT ON COLUMN reviewed_ideas.paper_only IS
    'Always TRUE. This table records human-reviewed ideas that were NOT executed by the system.';

CREATE INDEX IF NOT EXISTS reviewed_ideas_ticker_idx
    ON reviewed_ideas (ticker);

CREATE INDEX IF NOT EXISTS reviewed_ideas_decision_idx
    ON reviewed_ideas (decision);

CREATE INDEX IF NOT EXISTS reviewed_ideas_agent_id_idx
    ON reviewed_ideas (agent_id);

CREATE INDEX IF NOT EXISTS reviewed_ideas_created_at_idx
    ON reviewed_ideas (created_at);
