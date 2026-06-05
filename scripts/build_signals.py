#!/usr/bin/env python
"""Assemble the signals JSON from pre-written agent output files.

Instead of having Claude build a large JSON blob in-context, this script reads
the agent output files that weather-signal and market-maker-signal wrote during
Step 2 and merges them with the scout file's microstructure estimates.

Only markets that received a weather signal are included — markets with only a
microstructure estimate can't clear the n_sources >= 2 scoring filter anyway.

Usage:
    PYTHONPATH=. .venv/bin/python scripts/build_signals.py \\
        --scout-file /tmp/market_scout_TS.json \\
        --weather-dir /tmp/weather_signals_TS/ \\
        [--market-maker-dir /tmp/mm_signals_TS/] \\
        [--live-prices-file /tmp/live_prices_TS.json] \\
        > /tmp/signals_TS.json
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


def load_agent_outputs(directory: str | None) -> dict[str, list[dict]]:
    """Read every *.json file in directory; key by ticker (stem of filename)."""
    results: dict[str, list[dict]] = {}
    if not directory:
        return results
    agent_dir = Path(directory)
    if not agent_dir.is_dir():
        return results
    for json_file in agent_dir.glob("*.json"):
        estimates = json.loads(json_file.read_text())
        if estimates:
            results[json_file.stem] = estimates
    return results


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--scout-file", required=True)
    parser.add_argument("--weather-dir", required=True)
    parser.add_argument("--market-maker-dir", default=None)
    parser.add_argument("--live-prices-file", default=None)
    args = parser.parse_args()

    scout_markets: list[dict] = json.loads(Path(args.scout_file).read_text())
    scout_by_ticker = {m["best_market_ticker"]: m for m in scout_markets}

    weather_by_ticker = load_agent_outputs(args.weather_dir)
    mm_by_ticker = load_agent_outputs(args.market_maker_dir)

    live_prices: dict[str, dict] = {}
    if args.live_prices_file:
        content = Path(args.live_prices_file).read_text()
        # live_prices.py emits a status header line before the JSON object
        json_start = content.index("{") if "{" in content else 0
        live_prices = json.loads(content[json_start:])

    signals: list[dict] = []
    for ticker, weather_estimates in weather_by_ticker.items():
        scout_market = scout_by_ticker.get(ticker)
        if not scout_market:
            print(f"build_signals: warning — {ticker} not in scout file, skipping", file=sys.stderr)
            continue

        # Start with the scout's microstructure estimate
        combined_estimates = list(scout_market.get("signal_estimates", []))
        combined_estimates.extend(weather_estimates)
        if ticker in mm_by_ticker:
            combined_estimates.extend(mm_by_ticker[ticker])

        # Live prices override scout prices when available
        live = live_prices.get(ticker, {})
        yes_bid = live.get("yes_bid") or scout_market.get("yes_bid")
        yes_ask = live.get("yes_ask") or scout_market.get("yes_ask")

        signals.append({
            "ticker": ticker,
            "title": scout_market["title"],
            "category": scout_market["category"],
            "yes_bid": yes_bid,
            "yes_ask": yes_ask,
            "hours_to_close": scout_market.get("hours_to_close", 0),
            "actionability_score": scout_market.get("average_score", 0),
            "coverage_pct": scout_market.get("coverage_pct", 0),
            "volume_24h": scout_market.get("volume_24h", 0),
            "signal_estimates": combined_estimates,
        })

    print(json.dumps(signals, indent=2))
    print(f"build_signals: assembled {len(signals)} markets from {len(weather_by_ticker)} weather signals", file=sys.stderr)


if __name__ == "__main__":
    main()
