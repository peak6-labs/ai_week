"""CLI: python -m kalshi_trader.pipelines.kalshi_bias --ticker X --title "..." [--category ""] [--hours-to-close 72.0]

Prints list[SignalEstimate] JSON to stdout. Empty list [] on no signal or error.
"""
from __future__ import annotations
import argparse
import asyncio
import json
import sys
import kalshi_trader.config  # noqa: F401 — loads .env so ANTHROPIC_API_KEY is set
from kalshi_trader.agents.kalshi_bias_agent import KalshiBiasAgent
from kalshi_trader.agents.parsing import estimate_to_dict
from kalshi_trader.client import KalshiClient


def main() -> None:
    parser = argparse.ArgumentParser(description="Kalshi bias pipeline agent")
    parser.add_argument("--ticker", required=True)
    parser.add_argument("--title", required=True)
    parser.add_argument("--category", default="")
    parser.add_argument("--hours-to-close", type=float, default=72.0, dest="hours_to_close")
    args = parser.parse_args()

    async def run() -> None:
        client = KalshiClient()
        agent = KalshiBiasAgent(client)
        try:
            estimates = await agent.run(args.ticker, args.title, args.category, args.hours_to_close)
            print(json.dumps([estimate_to_dict(e) for e in estimates], default=str))
        except Exception as exc:
            print(f"Error: {exc}", file=sys.stderr)
            print(json.dumps([]))
        finally:
            if hasattr(client, "close"):
                await client.close()

    asyncio.run(run())


if __name__ == "__main__":
    main()
