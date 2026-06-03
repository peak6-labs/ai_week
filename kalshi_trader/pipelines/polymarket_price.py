"""CLI: python -m kalshi_trader.pipelines.polymarket_price --ticker X --title "..." --midpoint 35.5 --open-interest 1200 --hours-to-close 24

Prints list[SignalEstimate] JSON to stdout. Empty list [] on no signal or error.
"""
from __future__ import annotations
import argparse
import asyncio
import json
import sys
import kalshi_trader.config  # noqa: F401 — loads .env so API keys are set
from kalshi_trader.agents.polymarket_price_agent import PolymarketPriceAgent
from kalshi_trader.agents.parsing import estimate_to_dict


def main() -> None:
    parser = argparse.ArgumentParser(description="Polymarket price pipeline agent")
    parser.add_argument("--ticker", required=True)
    parser.add_argument("--title", required=True)
    parser.add_argument("--midpoint", type=float, required=True, help="Kalshi yes midpoint in cents (0-100)")
    parser.add_argument("--open-interest", type=int, default=0, dest="open_interest",
                        help="Accepted for caller compatibility; not used by the price agent.")
    parser.add_argument("--hours-to-close", type=float, required=True, dest="hours_to_close")
    args = parser.parse_args()

    async def run() -> None:
        agent = PolymarketPriceAgent()
        try:
            estimates = await agent.run(
                args.ticker, args.title,
                args.midpoint, args.hours_to_close,
            )
            print(json.dumps([estimate_to_dict(e) for e in estimates], default=str))
        except Exception as exc:
            print(f"Error: {exc}", file=sys.stderr)
            print(json.dumps([]))
        finally:
            await agent.close()

    asyncio.run(run())


if __name__ == "__main__":
    main()
