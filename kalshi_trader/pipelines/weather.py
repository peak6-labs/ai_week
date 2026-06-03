"""CLI: python -m kalshi_trader.pipelines.weather --ticker X --title "..."

Prints list[SignalEstimate] JSON to stdout. Empty list [] on no signal or error.
"""
from __future__ import annotations
import argparse
import asyncio
import json
import sys
import kalshi_trader.config  # noqa: F401 — loads .env so API keys are set
from kalshi_trader.agents.weather_agent import WeatherAgent
from kalshi_trader.agents.parsing import estimate_to_dict
from kalshi_trader.agents.settlement_context import (
    format_settlement_block,
    parse_settlement_arg,
)


def main() -> None:
    parser = argparse.ArgumentParser(description="Weather pipeline agent")
    parser.add_argument("--ticker", required=True)
    parser.add_argument("--title", required=True)
    parser.add_argument(
        "--settlement-json",
        dest="settlement_json",
        default=None,
        help="JSON object of contract settlement context (rules_primary, "
        "settlement_sources, ...) from market_rules.py. The agent forecasts off "
        "the contract's settlement source/station, or down-weights if it can't.",
    )
    args = parser.parse_args()

    async def run() -> None:
        settlement_context = format_settlement_block(parse_settlement_arg(args.settlement_json))
        agent = WeatherAgent()
        try:
            estimates = await agent.run(args.ticker, args.title, settlement_context or None)
            print(json.dumps([estimate_to_dict(e) for e in estimates], default=str))
        except Exception as exc:
            print(f"Error: {exc}", file=sys.stderr)
            print(json.dumps([]))
        finally:
            await agent.close()

    asyncio.run(run())


if __name__ == "__main__":
    main()
