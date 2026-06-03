-- Migration 002: add `disposition` to recommendations (backtest support).
--
-- The pipeline now records EVERY scored 2+ source market, not just the
-- risk-approved slate, so we can mark rejected/insufficient-edge candidates to
-- market and check whether the edge bar is calibrated correctly (see
-- scripts/paper_track.py `record-scored` and the by-edge-bucket scorecard).
--
-- `status` stays the lifecycle (open → resolved); `disposition` is the
-- orthogonal classification the pipeline assigned at record time.
--
-- Run this in the Supabase SQL editor (DDL cannot go through the service key).
-- Project: ai_week (xhyqdrhrwgebidvsnwbx) ONLY.

alter table public.recommendations
    add column if not exists disposition text not null default 'candidate';

create index if not exists recommendations_disposition_idx
    on public.recommendations (disposition);
