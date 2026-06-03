#!/usr/bin/env python
"""Evaluate Polymarket whale selection/scoring methods on test markets.

For each scorer (winrate, harvard, leaderboard_alltime, leaderboard_week — and any
new chain-fetcher-derived list once it's added to targets.json), run the whale
signal on the given Kalshi markets, tag the result by scorer, and record it as a
paper recommendation (`source = whale:<scorer>`). The paper loop then marks these
to market over time, so `paper_track.py report --by-source` shows which scorer's
whales actually predict outcomes best.

Usage:
    KALSHI_ENV=prod python scripts/eval_whales.py --tickers KXFOO-1 KXBAR-2 --cycle-ts TS

A correct *historical* backtest (per-wallet realized P&L on resolved markets)
additionally needs the chain fetcher's clean resolution data — see
docs/signal_research.md / TODO. This live path needs no chain data.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import subprocess

import kalshi_trader.config  # noqa: F401 — loads .env
from kalshi_trader.agents.polymarket_whale_agent import WHALE_SCORERS
from kalshi_trader import paper


def _run_whale(ticker: str, title: str, scorer: str) -> list[dict]:
    """Run the whale pipeline CLI for one ticker+scorer; return the signal array."""
    proc = subprocess.run(
        ["/Users/scorley/code/.venv/bin/python", "-m", "kalshi_trader.pipelines.polymarket_whale",
         "--ticker", ticker, "--title", title, "--scorer", scorer],
        capture_output=True, text=True, env={**_env()},
    )
    try:
        return json.loads(proc.stdout.strip() or "[]")
    except json.JSONDecodeError:
        return []


def _env() -> dict:
    import os
    return {**os.environ, "PYTHONPATH": "/Users/scorley/code", "KALSHI_ENV": os.environ.get("KALSHI_ENV", "prod")}


async def _market_price(client, ticker: str) -> tuple[float, float]:
    try:
        response = await client.get_market(ticker)
        market = response.get("market", response)
        return float(market.get("yes_bid") or 0), float(market.get("yes_ask") or 0)
    except Exception:
        return 0.0, 0.0


async def main_async(tickers: list[str], titles: dict[str, str], cycle_ts: str) -> None:
    from kalshi_trader.client import KalshiClient
    client = KalshiClient()
    grid: dict[str, dict[str, str]] = {}  # ticker -> {scorer -> summary}
    for ticker in tickers:
        title = titles.get(ticker) or ticker
        if title == ticker:  # fetch title if not supplied
            try:
                response = await client.get_market(ticker)
                title = response.get("market", response).get("title", ticker)
            except Exception:
                pass
        yes_bid, yes_ask = await _market_price(client, ticker)
        grid[ticker] = {}
        for scorer in WHALE_SCORERS:
            estimates = _run_whale(ticker, title, scorer)
            if not estimates:
                grid[ticker][scorer] = "—"
                continue
            est = estimates[0]
            prob = float(est.get("probability", 0.5))
            side = "yes" if prob > (yes_ask / 100.0 if yes_ask else 0.5) else "no"
            entry = yes_ask if side == "yes" else (100.0 - yes_bid)
            if 0 < entry < 100:
                paper.record_recommendation(
                    cycle_ts=cycle_ts, ticker=ticker, side=side, entry_cents=entry,
                    predicted_prob=prob, edge_cents=prob * 100 - entry, n_sources=1,
                    sources=[f"whale:{scorer}"], category="whale_eval",
                )
            grid[ticker][scorer] = f"{side} p={prob:.2f}"
    if hasattr(client, "close"):
        await client.close()

    print(f"\nWhale scorer comparison ({cycle_ts}):")
    header = "market".ljust(34) + "".join(s[:14].ljust(16) for s in WHALE_SCORERS)
    print(header)
    for ticker, row in grid.items():
        print(ticker[:33].ljust(34) + "".join(row.get(s, "—").ljust(16) for s in WHALE_SCORERS))
    print("\nRecorded scorer-tagged recommendations; run `paper_track.py report --by-source` later.")


def main() -> None:
    parser = argparse.ArgumentParser(description="Evaluate whale scorers on test markets")
    parser.add_argument("--tickers", nargs="+", required=True)
    parser.add_argument("--titles-file", default="", help="Optional JSON {ticker: title}")
    parser.add_argument("--cycle-ts", default="whale_eval")
    args = parser.parse_args()
    titles = {}
    if args.titles_file:
        import pathlib
        titles = json.loads(pathlib.Path(args.titles_file).read_text())
    asyncio.run(main_async(args.tickers, titles, args.cycle_ts))


if __name__ == "__main__":
    main()
