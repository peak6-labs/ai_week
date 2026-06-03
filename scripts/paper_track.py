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


def _mirror_recommendations(recorded_rows: list[dict]) -> None:
    """Best-effort mirror of recorded recommendations into Supabase."""
    async def _mirror():
        from kalshi_trader import db
        for row in recorded_rows:
            try:
                await db.insert_recommendation(row)
            except Exception:
                pass
    if recorded_rows:
        _mirror_to_supabase(_mirror)


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
            disposition=args.disposition,
        )
        recorded_rows.append({
            "rec_id": rec_id, "cycle_ts": args.cycle_ts, "ticker": idea["ticker"],
            "side": side, "entry_price_cents": entry, "predicted_prob": confidence,
            "edge_cents": confidence * 100 - entry, "n_sources": len(sources),
            "sources": sources, "category": idea.get("category", ""),
            "suggested_size_dollars": idea.get("suggested_size_dollars"),
            "disposition": args.disposition,
        })
        recorded += 1

    _mirror_recommendations(recorded_rows)
    print(f"recorded {recorded} paper recommendations "
          f"(cycle {args.cycle_ts}, disposition={args.disposition})")


def _cmd_record_scored(args) -> None:
    """Record EVERY scored market (not just the approved slate) for backtest.

    Reads the score_signals output and records each market that clears
    --min-sources with a disposition derived from worth_trading. Tickers in
    --exclude-file (the risk-approved slate, recorded separately as
    ``approved``) are skipped so they are not double-counted. Marking these to
    market over later cycles is how we judge whether the edge bar is right.
    """
    scored = json.loads(Path(args.scored_file).read_text())
    excluded: set[str] = set()
    if args.exclude_file and Path(args.exclude_file).exists():
        for idea in json.loads(Path(args.exclude_file).read_text()):
            excluded.add(idea.get("ticker", ""))

    # Dedup: with whole-board coverage the same market recurs every cycle. Record
    # a (ticker, side) once while it is still open, then just keep marking it —
    # re-recording would flood the store with duplicates and skew the backtest.
    already_open = {(rec["ticker"], rec.get("side"))
                    for rec in paper.open_recommendations()}

    recorded = 0
    skipped_open = 0
    by_disposition: dict[str, int] = {}
    recorded_rows: list[dict] = []
    for market in scored:
        ticker = market.get("ticker", "")
        if not ticker or ticker in excluded:
            continue
        n_sources = int(market.get("n_sources", 0) or 0)
        if n_sources < args.min_sources:
            continue
        side = market.get("side", "yes")
        if (ticker, side) in already_open:
            skipped_open += 1
            continue
        yes_ask = float(market.get("yes_ask", 0) or 0)
        yes_bid = float(market.get("yes_bid", yes_ask) or yes_ask)
        # Taker entry cost on the chosen side, mirroring paper.entry_price_cents.
        entry = yes_ask if side == "yes" else 100.0 - yes_bid
        if entry <= 0 or entry >= 100:
            continue
        combined_probability = float(market.get("combined_probability", 0.5) or 0.5)
        predicted_prob = combined_probability if side == "yes" else 1.0 - combined_probability
        edge_cents = float(market.get("fee_adjusted_edge", market.get("edge_cents", 0)) or 0)
        disposition = "worth_trading" if market.get("worth_trading") else "insufficient_edge"
        sources = market.get("sources", []) or []
        rec_id = paper.record_recommendation(
            cycle_ts=args.cycle_ts,
            ticker=ticker,
            side=side,
            entry_cents=entry,
            predicted_prob=predicted_prob,
            edge_cents=edge_cents,
            n_sources=n_sources,
            sources=sources,
            category=market.get("category", ""),
            disposition=disposition,
        )
        recorded_rows.append({
            "rec_id": rec_id, "cycle_ts": args.cycle_ts, "ticker": ticker,
            "side": side, "entry_price_cents": round(entry, 2), "predicted_prob": predicted_prob,
            "edge_cents": edge_cents, "n_sources": n_sources, "sources": sources,
            "category": market.get("category", ""), "suggested_size_dollars": None,
            "disposition": disposition,
        })
        by_disposition[disposition] = by_disposition.get(disposition, 0) + 1
        recorded += 1

    _mirror_recommendations(recorded_rows)
    print(f"recorded {recorded} scored markets for backtest "
          f"(cycle {args.cycle_ts}, min_sources={args.min_sources}, "
          f"by_disposition={by_disposition}, skipped {skipped_open} already-open)")


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
    open_n = len(paper.open_recommendations())
    out = {"open_recommendations": open_n, "overall": paper.performance_summary()}
    if args.by_source:
        out["by_source"] = paper.performance_by_source()
    if args.by_disposition:
        out["by_disposition"] = paper.performance_by_disposition()
    if args.by_edge_bucket:
        out["by_edge_bucket"] = paper.performance_by_edge_bucket()
    print(json.dumps(out, indent=2))


def main() -> None:
    parser = argparse.ArgumentParser(description="Paper-trade tracker (no execution)")
    sub = parser.add_subparsers(dest="command", required=True)

    rec = sub.add_parser("record", help="Record recommendations from an ideas file")
    rec.add_argument("--ideas-file", required=True)
    rec.add_argument("--cycle-ts", required=True)
    rec.add_argument("--disposition", default="approved",
                     help="Disposition tag for these recommendations (default: approved)")

    rec_scored = sub.add_parser("record-scored",
                                help="Record ALL scored markets for backtest (incl. rejected)")
    rec_scored.add_argument("--scored-file", required=True,
                            help="score_signals.py output JSON")
    rec_scored.add_argument("--cycle-ts", required=True)
    rec_scored.add_argument("--exclude-file", default="",
                            help="Approved-slate JSON whose tickers to skip (recorded separately)")
    rec_scored.add_argument("--min-sources", type=int, default=2,
                            help="Only record markets with at least this many signal sources")

    sub.add_parser("mark", help="Mark all open recommendations to current market")
    report = sub.add_parser("report", help="Print the running scorecard")
    report.add_argument("--by-source", action="store_true", help="Break the scorecard down by signal source")
    report.add_argument("--by-disposition", action="store_true",
                        help="Break the scorecard down by disposition (approved/worth_trading/insufficient_edge)")
    report.add_argument("--by-edge-bucket", action="store_true",
                        help="Break the scorecard down by edge_cents bucket (calibrates the edge bar)")

    args = parser.parse_args()
    if args.command == "record":
        _cmd_record(args)
    elif args.command == "record-scored":
        _cmd_record_scored(args)
    elif args.command == "mark":
        asyncio.run(_cmd_mark(args))
    elif args.command == "report":
        _cmd_report(args)


if __name__ == "__main__":
    main()
