#!/usr/bin/env python
"""Persist a cycle's scored markets + run stats to Supabase (best-effort).

Merges the market-scout JSON (prices, signals, signal_estimates) with the
score_signals output (combined_probability, edge, n_sources, worth_trading, side)
and upserts one scored_markets row per market, plus a cycles summary row.

Usage:
    python scripts/persist_cycle.py --scout-file /tmp/market_scout_TS.json \
        --scored-file /tmp/scored_TS.json --cycle-ts TS

Never raises — Supabase is optional; if it's unreachable the cycle still runs.
"""
from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path

import kalshi_trader.config  # noqa: F401 — loads .env


def _load(path: str) -> list:
    try:
        return json.loads(Path(path).read_text())
    except Exception:
        return []


async def _run(scout_file: str, scored_file: str, cycle_ts: str) -> None:
    from kalshi_trader import db

    scout = {r.get("best_market_ticker") or r.get("ticker"): r for r in _load(scout_file)}
    scored = {r.get("ticker"): r for r in _load(scored_file)}

    rows = []
    for ticker, srow in scout.items():
        if not ticker:
            continue
        scored_row = scored.get(ticker, {})
        rows.append({
            "cycle_ts": cycle_ts,
            "ticker": ticker,
            "event_ticker": srow.get("event_ticker"),
            "title": srow.get("title"),
            "category": srow.get("category"),
            "yes_bid": srow.get("yes_bid"),
            "yes_ask": srow.get("yes_ask"),
            "volume_24h": srow.get("volume_24h"),
            "open_interest": srow.get("open_interest"),
            "composite_score": srow.get("average_score"),
            "coverage_pct": srow.get("coverage_pct"),
            "signals": srow.get("signals"),
            "signal_estimates": srow.get("signal_estimates"),
            "combined_probability": scored_row.get("combined_probability"),
            "edge_cents": scored_row.get("edge_cents"),
            "n_sources": scored_row.get("n_sources"),
            "worth_trading": scored_row.get("worth_trading"),
            "side": scored_row.get("side"),
        })

    markets_2plus = sum(1 for r in rows if (r["n_sources"] or 0) >= 2)
    candidates = sum(1 for r in rows if r["worth_trading"] and (r["n_sources"] or 0) >= 2)

    try:
        written = await db.upsert_scored_markets(rows)
        await db.upsert_cycle(cycle_ts, {
            "markets_scored": len(rows),
            "markets_2plus_src": markets_2plus,
            "candidates": candidates,
        })
        print(f"persisted {written} scored_markets + cycle {cycle_ts} "
              f"({len(rows)} scored, {markets_2plus} 2+src, {candidates} candidates)")
    except Exception as exc:
        print(f"persist skipped (Supabase unavailable): {str(exc)[:100]}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Persist a cycle to Supabase")
    parser.add_argument("--scout-file", required=True)
    parser.add_argument("--scored-file", default="")
    parser.add_argument("--cycle-ts", required=True)
    args = parser.parse_args()
    asyncio.run(_run(args.scout_file, args.scored_file, args.cycle_ts))


if __name__ == "__main__":
    main()
