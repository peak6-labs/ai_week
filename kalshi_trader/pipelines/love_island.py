"""CLI: python -m kalshi_trader.pipelines.love_island --ticker X --title "..." --category entertainment

Prints list[SignalEstimate] JSON to stdout. Empty list [] on no signal or error.
"""
from __future__ import annotations
import argparse
import asyncio
import json
import sys
import kalshi_trader.config  # noqa: F401 — loads .env so API keys are set
from kalshi_trader.agents.love_island_agent import LoveIslandAgent
from kalshi_trader.agents.parsing import estimate_to_dict


def main() -> None:
    parser = argparse.ArgumentParser(description="Love Island signal pipeline agent")
    parser.add_argument("--ticker", required=True)
    parser.add_argument("--title", required=True)
    parser.add_argument("--category", default="entertainment")
    args = parser.parse_args()

    async def run() -> None:
        agent = LoveIslandAgent()
        try:
            estimates = await agent.run(args.ticker, args.title, args.category)
            print(json.dumps([estimate_to_dict(estimate) for estimate in estimates], default=str))
        except Exception as caught_exception:
            print(f"Error: {caught_exception}", file=sys.stderr)
            print(json.dumps([]))
        finally:
            await agent.close()

    asyncio.run(run())


if __name__ == "__main__":
    main()
