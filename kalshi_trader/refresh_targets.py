"""CLI to refresh targets.json from live Polymarket data.

Usage:
    # V1 scorer (win-rate only, default)
    python -m kalshi_trader.refresh_targets

    # V2 scorer (composite: win-rate + direction + evidence)
    WHALE_SCORER_V2=true python -m kalshi_trader.refresh_targets

Both commands save under separate keys so neither overwrites the other.
Load with: load_whale_targets(scorer="winrate") or scorer="harvard".
"""
from __future__ import annotations

import argparse
import asyncio

from kalshi_trader import config
from kalshi_trader.external.polymarket import (
    PolymarketClient,
    save_whale_targets,
)


async def _run(min_score: float, top_n: int) -> None:
    scorer_key = "harvard" if config.WHALE_SCORER_V2 else "winrate"
    print(
        f"Scanning Polymarket for top-{top_n} wallets "
        f"(scorer={scorer_key}, min_score={min_score})…"
    )
    async with PolymarketClient() as client:
        wallets = await client.bootstrap_whale_targets(min_score=min_score, top_n=top_n)
    save_whale_targets(wallets, scorer=scorer_key)
    print(f"Saved {len(wallets)} wallet(s) to targets.json under key '{scorer_key}'")
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
