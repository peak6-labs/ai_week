#!/usr/bin/env python
"""Paper-trade tracker CLI — record recommendations and mark them to market.

No execution. Records what the pipeline *would* have traded, then checks later
whether it would have made money. Drives signal calibration / weight tuning.

Usage:
    # After the pipeline produces ideas (same dicts written to reports/orchestrator-*.json):
    python scripts/paper_track.py record --ideas-file reports/orchestrator-TS.json --cycle-ts TS

    # Later (e.g. start of each cycle) mark all open recommendations to market:
    KALSHI_ENV=prod python scripts/paper_track.py mark

    # Print the running scorecard:
    python scripts/paper_track.py report
"""
from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path

import kalshi_trader.config  # noqa: F401 — loads .env
from kalshi_trader import paper


def _mirror_to_supabase(coro_factory) -> None:
    """Best-effort Supabase write; never breaks the local-first paper loop."""
    try:
        asyncio.run(coro_factory())
    except Exception:
        pass


def _cmd_record(args) -> None:
    ideas = json.loads(Path(args.ideas_file).read_text())
    recorded = 0
    recorded_rows: list[dict] = []
    for idea in ideas:
        side = idea.get("side", "yes")
        entry = float(idea.get("market_price", 0) or 0)
        if entry <= 0 or entry >= 100:
            continue
        confidence = float(idea.get("confidence", 0) or 0)
        sources = idea.get("signal_sources", []) or []
        rec_id = paper.record_recommendation(
            cycle_ts=args.cycle_ts,
            ticker=idea["ticker"],
            side=side,
            entry_cents=entry,
            predicted_prob=confidence,
            edge_cents=confidence * 100 - entry,
            n_sources=len(sources),
            sources=sources,
            category=idea.get("category", ""),
            suggested_size_dollars=idea.get("suggested_size_dollars"),
        )
        recorded_rows.append({
            "rec_id": rec_id, "cycle_ts": args.cycle_ts, "ticker": idea["ticker"],
            "side": side, "entry_price_cents": entry, "predicted_prob": confidence,
            "edge_cents": confidence * 100 - entry, "n_sources": len(sources),
            "sources": sources, "category": idea.get("category", ""),
            "suggested_size_dollars": idea.get("suggested_size_dollars"),
        })
        recorded += 1

    async def _mirror():
        from kalshi_trader import db
        for row in recorded_rows:
            try:
                await db.insert_recommendation(row)
            except Exception:
                pass
    if recorded_rows:
        _mirror_to_supabase(_mirror)
    print(f"recorded {recorded} paper recommendations (cycle {args.cycle_ts})")


async def _cmd_mark(args) -> None:
    from kalshi_trader.client import KalshiClient

    open_recs = paper.open_recommendations()
    if not open_recs:
        print("no open recommendations to mark")
        return
    client = KalshiClient()
    marked = 0
    resolved = 0
    try:
        for rec in open_recs:
            ticker = rec["ticker"]
            try:
                market_data = await client.get_market(ticker)
                market = market_data.get("market", market_data)
            except Exception as exc:
                paper.append_mark(rec["rec_id"], ticker, {"error": str(exc)[:120],
                                                           "pnl_cents": None, "would_profit": None})
                continue
            status = (market.get("status") or "").lower()
            result = (market.get("result") or "").lower()  # "yes"/"no" when settled
            resolved_yes = None
            if status in ("settled", "finalized", "closed") and result in ("yes", "no"):
                resolved_yes = result == "yes"
            mark = paper.compute_mark(
                side=rec["side"],
                entry_cents=float(rec["entry_price_cents"]),
                yes_bid=market.get("yes_bid"),
                yes_ask=market.get("yes_ask"),
                resolved_yes=resolved_yes,
            )
            paper.append_mark(rec["rec_id"], ticker, mark)
            marked += 1
            # Mirror the mark to Supabase (best-effort).
            try:
                from kalshi_trader import db
                await db.insert_recommendation_mark(rec["rec_id"], mark)
            except Exception:
                pass
            if mark.get("resolved"):
                paper.close_recommendation(rec["rec_id"])
                resolved += 1
                try:
                    from kalshi_trader import db
                    await db.resolve_recommendation(rec["rec_id"])
                    await db.resolve_market(ticker, bool(resolved_yes))
                except Exception:
                    pass
    finally:
        if hasattr(client, "close"):
            await client.close()
    summary = paper.performance_summary()
    print(f"marked {marked} ({resolved} resolved). scorecard: {summary}")


def _cmd_report(args) -> None:
    summary = paper.performance_summary()
    open_n = len(paper.open_recommendations())
    print(json.dumps({"open_recommendations": open_n, **summary}, indent=2))


def main() -> None:
    parser = argparse.ArgumentParser(description="Paper-trade tracker (no execution)")
    sub = parser.add_subparsers(dest="command", required=True)

    rec = sub.add_parser("record", help="Record recommendations from an ideas file")
    rec.add_argument("--ideas-file", required=True)
    rec.add_argument("--cycle-ts", required=True)

    sub.add_parser("mark", help="Mark all open recommendations to current market")
    sub.add_parser("report", help="Print the running scorecard")

    args = parser.parse_args()
    if args.command == "record":
        _cmd_record(args)
    elif args.command == "mark":
        asyncio.run(_cmd_mark(args))
    elif args.command == "report":
        _cmd_report(args)


if __name__ == "__main__":
    main()
