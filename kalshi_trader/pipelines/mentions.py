"""CLI: python -m kalshi_trader.pipelines.mentions --ticker X --title "..."

Deterministic GDELT "mentions" signal — parses the market title into a phrase +
TV station, pulls the historical closed-caption match-percent timeline from the
GDELT TV API, reduces it to a base rate, and prints a list[SignalEstimate] JSON.
Empty list [] on no signal or error. Pure parse+fetch+build, no LLM.
"""
from __future__ import annotations
import argparse
import asyncio
import json
import sys
import kalshi_trader.config  # noqa: F401 — loads .env so API keys are set
from kalshi_trader.agents.parsing import estimate_to_dict
from kalshi_trader.external.gdelt import GDELTClient
from kalshi_trader.external.mentions_parser import parse_mention_title, base_rate_from_points
from kalshi_trader.signals.mentions import build_mentions_signal


def main() -> None:
    parser = argparse.ArgumentParser(description="GDELT mentions signal pipeline")
    parser.add_argument("--ticker", required=True)
    parser.add_argument("--title", required=True)
    args = parser.parse_args()

    async def run() -> None:
        client = GDELTClient()
        try:
            parsed = parse_mention_title(args.ticker, args.title)
            if parsed is None:
                print(json.dumps([]))
                return
            timeline = await client.get_mention_timeline(
                parsed["phrase"], station=parsed["station"]
            )
            base_rate = base_rate_from_points(timeline["points"])
            if base_rate["period_count"] == 0:
                print(json.dumps([]))
                return
            estimate = build_mentions_signal(
                ticker=args.ticker,
                phrase=parsed["phrase"],
                station=parsed["station"],
                base_rate=base_rate,
                speaker=parsed["speaker"],
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
