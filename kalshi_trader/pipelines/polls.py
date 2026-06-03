"""CLI: python -m kalshi_trader.pipelines.polls --ticker X --title "..."

Deterministic FiveThirtyEight polling signal — parses the election-market title
into a poll type + state + candidate, pulls the matching 538 polls CSV, computes
the recent quality-weighted margin, maps it to a win probability via a normal
model around the margin, and prints a list[SignalEstimate] JSON. Empty list []
on no signal or error. Pure parse+fetch+build, no LLM.
"""
from __future__ import annotations
import argparse
import asyncio
import json
import sys
import kalshi_trader.config  # noqa: F401 — loads .env so API keys are set
from kalshi_trader.agents.parsing import estimate_to_dict
from kalshi_trader.external.fivethirtyeight import FiveThirtyEightClient
from kalshi_trader.external.polls_parser import parse_election_title, recent_margin
from kalshi_trader.signals.polls import build_polls_signal


def main() -> None:
    parser = argparse.ArgumentParser(description="FiveThirtyEight polling signal pipeline")
    parser.add_argument("--ticker", required=True)
    parser.add_argument("--title", required=True)
    args = parser.parse_args()

    async def run() -> None:
        client = FiveThirtyEightClient()
        try:
            parsed = parse_election_title(args.ticker, args.title)
            if parsed is None:
                print(json.dumps([]))
                return
            rows = await client.get_polls(parsed["poll_type"])
            margin_summary = recent_margin(
                rows, candidate=parsed["candidate"], state=parsed["state"]
            )
            if margin_summary is None:
                print(json.dumps([]))
                return
            estimate = build_polls_signal(
                ticker=args.ticker,
                margin_summary=margin_summary,
                poll_type=parsed["poll_type"],
                state=parsed["state"],
            )
            print(json.dumps([estimate_to_dict(estimate)], default=str))
        except Exception as caught_exception:
            print(f"Error: {caught_exception}", file=sys.stderr)
            print(json.dumps([]))
        finally:
            await client.close()

    asyncio.run(run())


if __name__ == "__main__":
    main()
