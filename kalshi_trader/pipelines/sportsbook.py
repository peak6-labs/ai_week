"""CLI: python -m kalshi_trader.pipelines.sportsbook --ticker X --title "..." [--league nba]

Deterministic sportsbook-odds signal — pulls the DraftKings/FanDuel moneyline
from ESPN's free API, de-vigs it, matches the Kalshi YES outcome, and prints a
list[SignalEstimate] JSON ([] when no line/match). Pure Python, no LLM.
"""
from __future__ import annotations
import argparse
import json
import sys
import kalshi_trader.config  # noqa: F401 — loads .env
from kalshi_trader.agents.parsing import estimate_to_dict
from kalshi_trader.signals.sportsbook import sportsbook_signal


def main() -> None:
    parser = argparse.ArgumentParser(description="Sportsbook-odds signal")
    parser.add_argument("--ticker", required=True)
    parser.add_argument("--title", required=True)
    parser.add_argument("--league", default=None,
                        help="Optional league hint (nba, nhl, mlb, wta, atp, ...). Auto-detected if omitted.")
    args = parser.parse_args()
    try:
        estimate = sportsbook_signal(args.ticker, args.title, league=args.league)
        print(json.dumps([estimate_to_dict(estimate)] if estimate else [], default=str))
    except Exception as exc:
        print(f"Error: {exc}", file=sys.stderr)
        print(json.dumps([]))


if __name__ == "__main__":
    main()
