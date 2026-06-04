#!/usr/bin/env python3
"""Execute approved weather-market trade ideas with session cap tracking.

Rules:
  - Weather markets only (category contains "weather" or "climate")
  - Skip if yes_bid < 20 or yes_ask > 80 (avoid near-resolved markets)
  - Maker-only pricing (no fees) via place_order.py midmarket_maker
  - Max $25 per trade, configurable
  - Session dollar cap (default $100), tracked in a session file
  - Skip tickers already executed this session

Usage:
  KALSHI_ENV=prod PYTHONPATH=. python scripts/auto_execute_weather.py \\
    --slate-file reports/orchestrator-<TS>.json \\
    --session-file /tmp/weather_session.json \\
    --max-per-trade 25 --session-cap 100 [--dry-run]
"""
from __future__ import annotations

import argparse, asyncio, json, subprocess, sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, ".")
import kalshi_trader.config  # noqa — loads .env

WEATHER_CATEGORIES = {"climate and weather", "weather"}


def load_session(session_file: str) -> dict:
    path = Path(session_file)
    if path.exists():
        return json.loads(path.read_text())
    return {"dollars_spent": 0.0, "executed": []}


def save_session(session_file: str, session: dict) -> None:
    Path(session_file).write_text(json.dumps(session, indent=2))


def run_place_order(ticker: str, side: str, amount: float, dry_run: bool) -> bool:
    cmd = [
        sys.executable, "scripts/place_order.py",
        "--ticker", ticker,
        "--amount", str(int(amount)),
        f"buy {side} no fees",
    ]
    if dry_run:
        print(f"  [DRY-RUN] Would run: {' '.join(cmd)}")
        return True
    result = subprocess.run(cmd, capture_output=True, text=True)
    output = (result.stdout + result.stderr).strip()
    print(f"  {output}")
    return result.returncode == 0


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--slate-file", required=True)
    parser.add_argument("--session-file", default="/tmp/weather_session.json")
    parser.add_argument("--max-per-trade", type=float, default=25.0)
    parser.add_argument("--session-cap", type=float, default=100.0)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    slate = json.loads(Path(args.slate_file).read_text())
    session = load_session(args.session_file)

    print(f"Session so far: ${session['dollars_spent']:.2f} / ${args.session_cap:.2f}")

    for idea in slate:
        ticker = idea["ticker"]
        side = idea["side"]
        category = idea.get("category", "").lower()
        market_price = idea.get("market_price", 0)
        suggested = idea.get("suggested_size_dollars", args.max_per_trade)

        # Weather filter
        if not any(w in category for w in WEATHER_CATEGORIES):
            print(f"  SKIP {ticker} — not weather ({category})")
            continue

        # Price range filter (20-80 on the side we're buying)
        if side == "yes":
            yes_bid_approx = market_price  # yes_ask was used for yes side
            if yes_bid_approx < 20 or yes_bid_approx > 80:
                print(f"  SKIP {ticker} — YES price {yes_bid_approx}¢ outside 20-80 range")
                continue
        else:  # NO side: market_price = 100 - yes_bid
            yes_bid_approx = 100 - market_price
            if yes_bid_approx < 20 or yes_bid_approx > 80:
                print(f"  SKIP {ticker} — YES bid {yes_bid_approx}¢ (NO market) outside 20-80 range")
                continue

        # Duplicate guard
        if ticker in session["executed"]:
            print(f"  SKIP {ticker} — already executed this session")
            continue

        # Session cap check
        remaining = args.session_cap - session["dollars_spent"]
        if remaining < 1:
            print(f"  STOP — session cap reached (${session['dollars_spent']:.2f})")
            break

        amount = min(args.max_per_trade, suggested, remaining)
        amount = max(1.0, round(amount))

        print(f"  EXECUTE {ticker} {side.upper()} ${amount}")
        ok = run_place_order(ticker, side, amount, args.dry_run)
        if ok and not args.dry_run:
            session["dollars_spent"] += amount
            session["executed"].append(ticker)
            save_session(args.session_file, session)

    print(f"Session total: ${session['dollars_spent']:.2f}")


if __name__ == "__main__":
    main()
