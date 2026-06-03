-- Migration 003: add `reasoning` to recommendations.
-- Stores the orchestrator's adversarial-challenge text for approved ideas.
-- worth_trading / insufficient_edge ideas have no reasoning (they come from
-- the deterministic scoring pipeline, not the LLM challenge step).

alter table public.recommendations
    add column if not exists reasoning text default '';
