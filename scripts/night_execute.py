#!/usr/bin/env python3
"""Execute approved night-mode trades: flat $10, hard session caps, no human confirmation.

Rules applied per candidate (in order):
  1. Session cap:          trades_placed >= 10 or dollars_spent >= $100 → stop all
  2. Love island filter:   category contains "love island" → skip (always excluded)
  3. Edge gate:            confidence - market_price/100 < 0.05 → skip
  4. Unquoted guard:       market_price <= 0 or >= 100 → skip
  5. Settlement proximity: hours_to_close < 2 → skip (all categories)
                           hours_to_close < 12 → skip (weather/climate only — filters same-day markets)
  6. Execute:              buy $10 flat limit order

Safety guarantee: Step 0.5 (evaluate_portfolio.py) always runs BEFORE this script
in the night-mode pipeline. Exit orders from that script are completely independent
of the session cap tracked here.

Usage:
  KALSHI_ENV=prod PYTHONPATH=. python scripts/night_execute.py \\
    --candidates-file /tmp/candidates_TS.json \\
    --session-file    reports/night-mode-session-YYYYMMDD.json \\
    --out             /tmp/night_executed_TS.json \\
    --cycle-ts        TS

  # Dry-run (no orders placed, session not updated):
  ... --dry-run
"""
from __future__ import annotations

import argparse
import asyncio
import json
import math
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, ".")

import kalshi_trader.config  # noqa: F401 — loads .env
from kalshi_trader.client import KalshiClient

SESSION_TRADE_CAP = 10
SESSION_DOLLAR_CAP = 100.0
FLAT_TRADE_SIZE_DOLLARS = 10.0
EDGE_MINIMUM = 0.05
SETTLEMENT_PROXIMITY_HOURS = 2.0
WEATHER_SETTLEMENT_PROXIMITY_HOURS = 12.0  # same-day weather markets excluded overnight
WEATHER_CATEGORIES = {"climate and weather", "weather"}
LOVE_ISLAND_CATEGORY = "love island"


def _load_session(session_file: str) -> dict:
    path = Path(session_file)
    if path.exists():
        return json.loads(path.read_text())
    return {
        "started_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "trades_placed": 0,
        "dollars_spent": 0.0,
        "tickers_traded": [],
    }


def _save_session(session_file: str, session: dict) -> None:
    Path(session_file).parent.mkdir(parents=True, exist_ok=True)
    Path(session_file).write_text(json.dumps(session, indent=2, default=str))


def _append_jsonl(log_dir: str, date_str: str, record: dict) -> None:
    log_path = Path(log_dir) / f"night-mode-{date_str}.jsonl"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with log_path.open("a") as log_file:
        log_file.write(json.dumps(record, default=str) + "\n")


def apply_rules(candidate: dict, session: dict) -> str | None:
    """Return rejection_reason if candidate should not be executed, else None.

    Pure function — no I/O. Checks rules in priority order:
    session cap → love island → edge → unquoted → settlement proximity.
    """
    if (
        session["trades_placed"] >= SESSION_TRADE_CAP
        or session["dollars_spent"] >= SESSION_DOLLAR_CAP
    ):
        return "session_cap_reached"

    category = (candidate.get("category") or "").lower()
    if LOVE_ISLAND_CATEGORY in category:
        return "love_island_excluded"

    confidence = float(candidate.get("confidence", 0))
    market_price = float(candidate.get("market_price", 0))

    if market_price <= 0 or market_price >= 100:
        return "unquoted"

    edge = round(confidence - market_price / 100.0, 10)
    if edge < EDGE_MINIMUM:
        return "edge_insufficient"

    hours_to_close = candidate.get("hours_to_close")
    if hours_to_close is not None:
        hours = float(hours_to_close)
        if hours < SETTLEMENT_PROXIMITY_HOURS:
            return "settlement_proximity"
        if category in WEATHER_CATEGORIES and hours < WEATHER_SETTLEMENT_PROXIMITY_HOURS:
            return "weather_same_day_excluded"

    return None


def _build_record(
    candidate: dict,
    cycle_ts: str,
    dry_run: bool,
    rejection_reason: str | None,
    session: dict,
) -> dict:
    market_price = float(candidate.get("market_price", 0))
    side = candidate.get("side", "yes")
    yes_price = round(market_price) if side == "yes" else round(100.0 - market_price)
    contract_count = (
        math.floor(FLAT_TRADE_SIZE_DOLLARS / (market_price / 100.0))
        if market_price > 0 else 0
    )
    return {
        "cycle_ts": cycle_ts,
        "ticker": candidate.get("ticker", ""),
        "side": side,
        "confidence": float(candidate.get("confidence", 0)),
        "market_price": market_price,
        "edge_cents": round(
            (float(candidate.get("confidence", 0)) - market_price / 100.0) * 100.0, 2
        ),
        "hours_to_close": candidate.get("hours_to_close"),
        "category": candidate.get("category", ""),
        "signal_sources": candidate.get("signal_sources", []),
        "suggested_size_dollars": FLAT_TRADE_SIZE_DOLLARS if rejection_reason is None else 0.0,
        "yes_price": yes_price,
        "contract_count": contract_count,
        "order_id": None,
        "order_status": None,
        "rejection_reason": rejection_reason,
        "session_trade_number": None,
        "session_dollars_spent": session["dollars_spent"],
        "logged_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "dry_run": dry_run,
    }


async def run(
    candidates: list[dict],
    session_file: str,
    cycle_ts: str,
    dry_run: bool = False,
    log_dir: str = "reports",
) -> list[dict]:
    """Apply rules and execute approved candidates. Returns one result dict per candidate."""
    session = _load_session(session_file)
    date_str = datetime.now(timezone.utc).strftime("%Y%m%d")
    results: list[dict] = []

    for index, candidate in enumerate(candidates):
        rejection_reason = apply_rules(candidate, session)
        record = _build_record(candidate, cycle_ts, dry_run, rejection_reason, session)

        if rejection_reason == "session_cap_reached":
            _append_jsonl(log_dir, date_str, record)
            results.append(record)
            for remaining_candidate in candidates[index + 1:]:
                remaining_record = _build_record(
                    remaining_candidate, cycle_ts, dry_run, "session_cap_reached", session
                )
                _append_jsonl(log_dir, date_str, remaining_record)
                results.append(remaining_record)
            break

        if rejection_reason is not None:
            _append_jsonl(log_dir, date_str, record)
            results.append(record)
            continue

        if dry_run:
            print(
                f"[DRY-RUN] Would buy {record['side'].upper()} {record['contract_count']}x "
                f"{record['ticker']} at yes_price={record['yes_price']}¢"
            )
            record["order_status"] = "dry_run"
        else:
            async with KalshiClient() as client:
                try:
                    order_response = await client.create_order(
                        ticker=record["ticker"],
                        action="buy",
                        side=record["side"],
                        count=record["contract_count"],
                        order_type="limit",
                        yes_price=record["yes_price"],
                    )
                    order_data = order_response.get("order", {})
                    record["order_id"] = order_data.get("order_id")
                    record["order_status"] = order_data.get("status")
                    print(
                        f"PLACED {record['side'].upper()} {record['contract_count']}x "
                        f"{record['ticker']} at yes_price={record['yes_price']}¢ "
                        f"order={record['order_id']}"
                    )
                except Exception as caught_exception:
                    record["rejection_reason"] = f"order_failed: {caught_exception}"
                    print(f"ERROR {record['ticker']}: {caught_exception}", file=sys.stderr)
                    _append_jsonl(log_dir, date_str, record)
                    results.append(record)
                    continue

        session["trades_placed"] += 1
        session["dollars_spent"] += FLAT_TRADE_SIZE_DOLLARS
        session["tickers_traded"].append(record["ticker"])
        record["session_trade_number"] = session["trades_placed"]
        record["session_dollars_spent"] = session["dollars_spent"]

        if not dry_run:
            _save_session(session_file, session)

        _append_jsonl(log_dir, date_str, record)
        results.append(record)

    return results


def _main() -> None:
    parser = argparse.ArgumentParser(
        description="Night-mode execution: flat $10 buys with session caps"
    )
    parser.add_argument("--candidates-file", required=True)
    parser.add_argument("--session-file", required=True)
    parser.add_argument("--out", required=True, help="Write results JSON to this path")
    parser.add_argument("--cycle-ts", required=True)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    candidates = json.loads(Path(args.candidates_file).read_text())
    results = asyncio.run(
        run(
            candidates=candidates,
            session_file=args.session_file,
            cycle_ts=args.cycle_ts,
            dry_run=args.dry_run,
        )
    )
    Path(args.out).write_text(json.dumps(results, indent=2, default=str))

    executed = sum(1 for record in results if record["rejection_reason"] is None)
    capped = sum(1 for record in results if record["rejection_reason"] == "session_cap_reached")
    print(
        f"Night mode: {executed} executed, "
        f"{len(results) - executed - capped} rejected, "
        f"{capped} capped"
    )


if __name__ == "__main__":
    _main()
