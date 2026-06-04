#!/usr/bin/env python3
"""Execute specific approved trade ideas as live Kalshi market orders.

Usage:
  # Dry-run first — always do this before live execution:
  python scripts/execute_slate.py --slate-file reports/orchestrator-20260603T120000Z.json --dry-run

  # Execute only specific tickers from a slate:
  python scripts/execute_slate.py --slate-file reports/orchestrator-*.json --tickers KXFOO-25DEC01 KXBAR-25DEC01

  # Execute all approved ideas in a slate:
  python scripts/execute_slate.py --slate-file reports/orchestrator-20260603T120000Z.json
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys

sys.path.insert(0, ".")

import kalshi_trader.config  # noqa: F401 — loads .env
from kalshi_trader.client import KalshiClient
from kalshi_trader.executor import TradeExecutor
from kalshi_trader.models import OrderAction, RiskDecision, Side, TradeIdea
from kalshi_trader.risk import RiskManager


async def _main() -> None:
    parser = argparse.ArgumentParser(description="Execute approved trade ideas as live Kalshi orders")
    parser.add_argument("--slate-file", required=True, help="Path to orchestrator JSON (approved ideas)")
    parser.add_argument("--tickers", nargs="*", help="Only execute these tickers (default: all in slate)")
    parser.add_argument("--dry-run", action="store_true", help="Print what would execute; no orders placed")
    args = parser.parse_args()

    with open(args.slate_file) as slate_file_handle:
        slate = json.load(slate_file_handle)

    if args.tickers:
        ticker_set = set(args.tickers)
        slate = [idea_dict for idea_dict in slate if idea_dict["ticker"] in ticker_set]

    if not slate:
        print("No matching ideas to execute.")
        return

    print(f"{'[DRY-RUN] ' if args.dry_run else ''}Preparing to execute {len(slate)} idea(s):")
    for idea_dict in slate:
        print(
            f"  {idea_dict['ticker']} {idea_dict['side'].upper()} "
            f"conf={idea_dict['confidence']:.2f} price={idea_dict['market_price']}¢ "
            f"size=${idea_dict['suggested_size_dollars']:.0f}"
        )

    if args.dry_run:
        return

    confirm = input("\nType 'execute' to confirm live order placement: ").strip()
    if confirm != "execute":
        print("Aborted.")
        return

    risk = RiskManager()
    async with KalshiClient() as client:
        executor = TradeExecutor(client, risk)

        for idea_dict in slate:
            idea = TradeIdea(
                agent_id=idea_dict.get("agent_id", "orchestrator"),
                ticker=idea_dict["ticker"],
                side=Side(idea_dict["side"]),
                action=OrderAction.BUY,
                confidence=idea_dict["confidence"],
                market_price=idea_dict["market_price"],
                reasoning=idea_dict.get("reasoning", ""),
                signal_sources=idea_dict.get("signal_sources", []),
                suggested_size_dollars=idea_dict["suggested_size_dollars"],
                category=idea_dict.get("category", ""),
            )
            decision = RiskDecision(
                approved=True,
                approved_size_dollars=idea_dict["suggested_size_dollars"],
                rejection_reason=None,
                fees_estimate_cents=0,
            )
            try:
                result = await executor.execute(idea, decision)
                print(
                    f"EXECUTED {result.ticker} {result.side.value.upper()} "
                    f"order_id={result.order_id} fill={result.fill_price}¢ "
                    f"size=${result.size_dollars:.2f} status={result.status}"
                )
            except Exception as caught_exception:
                print(f"FAILED {idea.ticker}: {caught_exception}", file=sys.stderr)
            # Space consecutive placements to stay under the 429 rate limit.
            await asyncio.sleep(kalshi_trader.config.INTER_ORDER_DELAY_SECONDS)


if __name__ == "__main__":
    asyncio.run(_main())
