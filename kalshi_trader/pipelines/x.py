"""CLI: python -m kalshi_trader.pipelines.x --ticker X --title "..." --category sports

Prints list[SignalEstimate] JSON to stdout. Empty list [] on no signal or error.
"""
from __future__ import annotations
import argparse
import asyncio
import json
import sys
from kalshi_trader.agents.x_agent import XAgent
from kalshi_trader.agents.parsing import estimate_to_dict


def main() -> None:
    parser = argparse.ArgumentParser(description="X social signal pipeline agent")
    parser.add_argument("--ticker", required=True)
    parser.add_argument("--title", required=True)
    parser.add_argument("--category", required=True)
    args = parser.parse_args()

    async def run() -> None:
        agent = XAgent()
        try:
            estimates = await agent.run(args.ticker, args.category, args.title)
            print(json.dumps([estimate_to_dict(e) for e in estimates], default=str))
        except Exception as exc:
            print(f"Error: {exc}", file=sys.stderr)
            print(json.dumps([]))
        finally:
            await agent.close()

    asyncio.run(run())


if __name__ == "__main__":
    main()
