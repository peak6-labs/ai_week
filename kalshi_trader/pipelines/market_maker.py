"""CLI: python -m kalshi_trader.pipelines.market_maker --ticker X --title "..."

Prints list[SignalEstimate] JSON to stdout. Empty list [] on no signal or error.
"""
from __future__ import annotations
import argparse
import asyncio
import json
import sys
import kalshi_trader.config  # noqa: F401 — loads .env so ANTHROPIC_API_KEY is set
from kalshi_trader.agents.market_maker_agent import MarketMakerAgent
from kalshi_trader.agents.parsing import estimate_to_dict
from kalshi_trader.client import KalshiClient


def main() -> None:
    parser = argparse.ArgumentParser(description="Market maker pipeline agent")
    parser.add_argument("--ticker", required=True)
    parser.add_argument("--title", required=True)
    args = parser.parse_args()

    async def run() -> None:
        client = KalshiClient()
        agent = MarketMakerAgent(client)
        try:
            estimates = await agent.run(args.ticker, args.title)
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
