"""CLI: python -m kalshi_trader.pipelines.kalshi_bias --ticker X --title "..." [--category ""] [--hours-to-close 72.0] [--midpoint 35]

Deterministic Kalshi calibration-bias signal — pure math, no LLM. Prints a
list[SignalEstimate] JSON to stdout ([] when there is no meaningful bias edge).

If --midpoint (cents) is given it is used directly (fast, no API call). Otherwise
the current market midpoint is fetched once from Kalshi.
"""
from __future__ import annotations
import argparse
import asyncio
import json
import sys
import kalshi_trader.config  # noqa: F401 — loads .env
from kalshi_trader.agents.kalshi_bias_agent import build_bias_estimate
from kalshi_trader.agents.parsing import estimate_to_dict
from kalshi_trader.client import KalshiClient


def main() -> None:
    parser = argparse.ArgumentParser(description="Deterministic Kalshi bias signal")
    parser.add_argument("--ticker", required=True)
    parser.add_argument("--title", required=True)
    parser.add_argument("--category", default="")
    parser.add_argument("--hours-to-close", type=float, default=72.0, dest="hours_to_close")
    parser.add_argument("--midpoint", type=float, default=None,
                        help="Market midpoint in cents (0-100). If omitted, fetched from Kalshi.")
    args = parser.parse_args()

    async def run() -> None:
        price_cents = args.midpoint
        client = None
        if price_cents is None:
            client = KalshiClient()
            try:
                market_data = await client.get_market(args.ticker)
                market = market_data.get("market", market_data)
                yes_bid = market.get("yes_bid", 0) or 0
                yes_ask = market.get("yes_ask", 0) or 0
                price_cents = (yes_bid + yes_ask) / 2.0
            except Exception as exc:
                print(f"Error: {exc}", file=sys.stderr)
                print(json.dumps([]))
                if hasattr(client, "close"):
                    await client.close()
                return
            finally:
                if client is not None and hasattr(client, "close"):
                    await client.close()

        estimate = build_bias_estimate(
            args.ticker, args.title, args.category, args.hours_to_close, float(price_cents or 0)
        )
        print(json.dumps([estimate_to_dict(estimate)] if estimate else [], default=str))

    asyncio.run(run())


if __name__ == "__main__":
    main()
