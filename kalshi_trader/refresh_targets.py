"""CLI to refresh targets.json from live Polymarket data.

Usage:
    python -m kalshi_trader.refresh_targets [--min-score 0.6] [--top-n 50]
"""
from __future__ import annotations

import argparse
import asyncio

from kalshi_trader.external.polymarket import (
    PolymarketClient,
    load_whale_targets,
    save_whale_targets,
)


async def _run(min_score: float, top_n: int) -> None:
    client = PolymarketClient()
    print(f"Scanning Polymarket for top-{top_n} wallets (min_score={min_score})…")
    wallets = await client.bootstrap_whale_targets(min_score=min_score, top_n=top_n)
    save_whale_targets(wallets)
    print(f"Saved {len(wallets)} whale wallet(s) to targets.json")
    for w in wallets:
        print(f"  {w}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Refresh Polymarket whale targets")
    parser.add_argument("--min-score", type=float, default=0.6)
    parser.add_argument("--top-n", type=int, default=50)
    args = parser.parse_args()
    asyncio.run(_run(args.min_score, args.top_n))


if __name__ == "__main__":
    main()
