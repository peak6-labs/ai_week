#!/usr/bin/env python3
"""Execute approved night-mode trades: weather-only, sized, no double-down.

Rules applied per candidate (in order):
  1. Session cap:          trades_placed >= 10 or dollars_spent >= $200 → stop all
  1.5. Cycle cap:          cycle_trades_placed >= 3 → stop this cycle
  2. Duplicate guard:      ticker already attempted this session/run → skip
  3. Love island filter:   category contains "love island" → skip (always excluded)
  4. Weather-only filter:  non-weather category → skip
  5. Edge gate:            confidence - market_price/100 < 0.05 → skip
  6. Unquoted guard:       market_price <= 0 or >= 100 → skip
  7. Entry band:           market_price < 20 or > 80 → skip
  8. Settlement proximity: hours_to_close < 2 → skip
                           hours_to_close < 12 → skip (same-day weather excluded overnight)
  9. Size:                 use upstream approved/suggested size when present,
                           otherwise compute via RiskManager
  10. Execute:             place one limit order per ticker at the sized count

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
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, ".")

import kalshi_trader.config  # noqa: F401 — loads .env
from kalshi_trader import db, paper
from kalshi_trader.client import KalshiClient
from kalshi_trader.dashboard.portfolio_mapping import parse_fixed_point
from kalshi_trader.models import OrderAction, PortfolioState, Side, TradeIdea
from kalshi_trader.risk import RiskManager

SESSION_TRADE_CAP = 9999  # effectively unlimited; dollar cap is the binding constraint
SESSION_DOLLAR_CAP = 200.0
CYCLE_TRADE_CAP = 3
MAX_TRADE_SIZE_DOLLARS = 20.0
MIN_ENTRY_PRICE_CENTS = 20.0
MAX_ENTRY_PRICE_CENTS = 80.0
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


def _normalize_session(session: dict) -> dict:
    session.setdefault("tickers_traded", [])
    return session


def _is_weather_category(category: str | None) -> bool:
    return (category or "").lower() in WEATHER_CATEGORIES


def _candidate_sort_key(candidate: dict) -> tuple[int, float, float]:
    category = candidate.get("category")
    market_price = float(candidate.get("market_price", 0))
    confidence = float(candidate.get("confidence", 0))
    edge = confidence - market_price / 100.0
    return (0 if _is_weather_category(category) else 1, -edge, -confidence)


def _load_portfolio_state(balance_response: dict, positions_response: dict) -> PortfolioState:
    balance_dollars = float(balance_response.get("balance_dollars") or 0.0)
    if not balance_dollars and balance_response.get("balance") is not None:
        balance_dollars = float(balance_response["balance"]) / 100.0

    total_exposure = 0.0
    exposure_by_category: dict[str, float] = {}
    for position in positions_response.get("market_positions", []):
        exposure = parse_fixed_point(position.get("market_exposure_dollars"))
        if exposure <= 0.0:
            continue
        total_exposure += exposure
        category = (position.get("category") or "").strip()
        if category:
            exposure_by_category[category] = exposure_by_category.get(category, 0.0) + exposure

    return PortfolioState(
        balance_dollars=balance_dollars,
        total_exposure_dollars=round(total_exposure, 2),
        exposure_by_category={key: round(value, 2) for key, value in exposure_by_category.items()},
    )


def apply_rules(
    candidate: dict,
    session: dict,
    seen_tickers: set[str] | None = None,
    cycle_trades_placed: int = 0,
) -> str | None:
    """Return rejection_reason if candidate should not be executed, else None.

    Pure function — no I/O. Checks rules in priority order:
    session cap → cycle cap → duplicate guard → love island → weather-only → edge
    → unquoted → entry band → settlement proximity.
    """
    session = _normalize_session(session)
    if (
        session["trades_placed"] >= SESSION_TRADE_CAP
        or session["dollars_spent"] >= SESSION_DOLLAR_CAP
    ):
        return "session_cap_reached"

    if cycle_trades_placed >= CYCLE_TRADE_CAP:
        return "cycle_cap_reached"

    ticker = candidate.get("ticker", "")
    if ticker and (ticker in session["tickers_traded"] or (seen_tickers and ticker in seen_tickers)):
        return "duplicate_trade"

    category = (candidate.get("category") or "").lower()
    if LOVE_ISLAND_CATEGORY in category:
        return "love_island_excluded"
    if not _is_weather_category(category):
        return "non_weather_excluded"

    confidence = float(candidate.get("confidence", 0))
    market_price = float(candidate.get("market_price", 0))

    if market_price <= 0 or market_price >= 100:
        return "unquoted"
    if market_price < MIN_ENTRY_PRICE_CENTS or market_price > MAX_ENTRY_PRICE_CENTS:
        return "entry_price_out_of_band"

    edge = round(confidence - market_price / 100.0, 10)
    if edge < EDGE_MINIMUM:
        return "edge_insufficient"

    hours_to_close = candidate.get("hours_to_close")
    if hours_to_close is not None:
        hours = float(hours_to_close)
        if hours < SETTLEMENT_PROXIMITY_HOURS:
            return "settlement_proximity"
        if hours < WEATHER_SETTLEMENT_PROXIMITY_HOURS:
            return "weather_same_day_excluded"

    return None


def _cap_trade_size(size_dollars: float) -> float:
    return round(min(size_dollars, MAX_TRADE_SIZE_DOLLARS), 2)


def _size_from_candidate(candidate: dict) -> float | None:
    for field_name in ("approved_size_dollars", "suggested_size_dollars"):
        value = candidate.get(field_name)
        if value is None:
            continue
        size = float(value)
        if size > 0:
            return _cap_trade_size(size)
    return None


def _close_time_from_candidate(candidate: dict) -> datetime | None:
    hours_to_close = candidate.get("hours_to_close")
    if hours_to_close is None:
        return None
    return datetime.now(timezone.utc) + timedelta(hours=float(hours_to_close))


def _build_trade_idea(candidate: dict) -> TradeIdea:
    return TradeIdea(
        agent_id=candidate.get("agent_id", "night_mode"),
        ticker=candidate["ticker"],
        side=Side(candidate.get("side", "yes")),
        action=OrderAction.BUY,
        confidence=float(candidate.get("confidence", 0)),
        market_price=float(candidate.get("market_price", 0)),
        reasoning=candidate.get("reasoning", ""),
        signal_sources=candidate.get("signal_sources", []),
        suggested_size_dollars=float(candidate.get("suggested_size_dollars", 0) or 0),
        category=candidate.get("category", ""),
    )


def _resolve_size_dollars(
    candidate: dict,
    risk: RiskManager,
    portfolio: PortfolioState,
    session: dict,
) -> tuple[float, str | None]:
    explicit_size = _size_from_candidate(candidate)
    if explicit_size is not None:
        return explicit_size, None

    # Scale Kelly to the remaining session budget, not the full account balance.
    # This keeps trade sizes proportional to what the session can actually absorb —
    # a 15% Kelly fraction against a $100 session cap gives ~$15, not $55 capped
    # to the flat $20 max that erases Kelly differentiation across trade quality.
    session_remaining = max(
        SESSION_DOLLAR_CAP - float(session.get("dollars_spent", 0.0)), 0.0
    )
    kelly_base = min(portfolio.balance_dollars, session_remaining)
    session_scaled_portfolio = PortfolioState(
        balance_dollars=kelly_base,
        total_exposure_dollars=portfolio.total_exposure_dollars,
        exposure_by_category=portfolio.exposure_by_category,
    )
    decision = risk.check_trade(
        _build_trade_idea(candidate),
        session_scaled_portfolio,
        close_time=_close_time_from_candidate(candidate),
    )
    if not decision.approved:
        return 0.0, decision.rejection_reason
    return _cap_trade_size(decision.approved_size_dollars), None


def _build_record(
    candidate: dict,
    cycle_ts: str,
    dry_run: bool,
    rejection_reason: str | None,
    session: dict,
    size_dollars: float,
) -> dict:
    market_price = float(candidate.get("market_price", 0))
    side = candidate.get("side", "yes")
    yes_price = round(market_price) if side == "yes" else round(100.0 - market_price)
    contract_count = (
        math.floor(size_dollars / (market_price / 100.0))
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
        "suggested_size_dollars": size_dollars if rejection_reason is None else 0.0,
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


async def _record_executed_trade(record: dict, cycle_ts: str) -> None:
    """Write an executed night-mode trade to the local paper store and Supabase.

    Wrapped in try/except so a Supabase failure never stops the pipeline.
    """
    sources = record.get("signal_sources") or []
    entry_cents = float(record.get("market_price", 0))
    confidence = float(record.get("confidence", 0))
    try:
        rec_id = paper.record_recommendation(
            cycle_ts=cycle_ts,
            ticker=record["ticker"],
            side=record["side"],
            entry_cents=entry_cents,
            predicted_prob=confidence,
            edge_cents=float(record.get("edge_cents", 0)),
            n_sources=len(sources),
            sources=sources,
            category=record.get("category", ""),
            suggested_size_dollars=float(record.get("suggested_size_dollars", 0)),
            disposition="executed",
        )
        await db.insert_recommendation({
            "rec_id": rec_id,
            "cycle_ts": cycle_ts,
            "ticker": record["ticker"],
            "side": record["side"],
            "entry_price_cents": entry_cents,
            "predicted_prob": confidence,
            "edge_cents": float(record.get("edge_cents", 0)),
            "n_sources": len(sources),
            "sources": sources,
            "category": record.get("category", ""),
            "suggested_size_dollars": float(record.get("suggested_size_dollars", 0)),
            "disposition": "executed",
            "recorded_at": record.get("logged_at"),
        })
    except Exception as write_exception:
        print(f"WARN: recommendation write failed for {record['ticker']}: {write_exception}", file=sys.stderr)


async def _fetch_live_midmarket_yes_price(
    client: KalshiClient, ticker: str
) -> int | None:
    """Fetch current midmarket yes_price for maker order placement (zero fees).

    Returns round((yes_bid + yes_ask) / 2), or None if the fetch fails or the
    market is one-sided.  Callers fall back to the stale candidate price on None.
    """
    try:
        response = await client.get_market(ticker)
        market = response.get("market", response)
        yes_bid = market.get("yes_bid")
        yes_ask = market.get("yes_ask")
        if yes_bid is not None and yes_ask is not None and float(yes_ask) > float(yes_bid):
            return round((float(yes_bid) + float(yes_ask)) / 2.0)
    except Exception as fetch_exception:
        print(
            f"WARN: live price fetch failed for {ticker}: {fetch_exception}",
            file=sys.stderr,
        )
    return None


async def run(
    candidates: list[dict],
    session_file: str,
    cycle_ts: str,
    dry_run: bool = False,
    log_dir: str = "reports",
) -> list[dict]:
    """Apply rules and execute approved candidates. Returns one result dict per candidate."""
    session = _normalize_session(_load_session(session_file))
    date_str = datetime.now(timezone.utc).strftime("%Y%m%d")
    results: list[dict] = []
    ordered_candidates = sorted(candidates, key=_candidate_sort_key)
    seen_tickers: set[str] = set()
    risk = RiskManager()

    async with KalshiClient() as client:
        balance_response = await client.get_balance()
        positions_response = await client.get_positions()
        portfolio = _load_portfolio_state(balance_response, positions_response)

        cycle_trades_placed = 0

        for index, candidate in enumerate(ordered_candidates):
            rejection_reason = apply_rules(
                candidate, session,
                seen_tickers=seen_tickers,
                cycle_trades_placed=cycle_trades_placed,
            )
            size_dollars = 0.0
            if rejection_reason is None:
                size_dollars, rejection_reason = _resolve_size_dollars(candidate, risk, portfolio, session)

            record = _build_record(
                candidate, cycle_ts, dry_run, rejection_reason, session, size_dollars
            )

            if rejection_reason in ("session_cap_reached", "cycle_cap_reached"):
                _append_jsonl(log_dir, date_str, record)
                results.append(record)
                for remaining_candidate in ordered_candidates[index + 1:]:
                    remaining_record = _build_record(
                        remaining_candidate,
                        cycle_ts,
                        dry_run,
                        rejection_reason,
                        session,
                        0.0,
                    )
                    _append_jsonl(log_dir, date_str, remaining_record)
                    results.append(remaining_record)
                break

            if rejection_reason is not None:
                _append_jsonl(log_dir, date_str, record)
                results.append(record)
                continue

            seen_tickers.add(record["ticker"])

            # Refresh price to midmarket so the order rests as a maker (no fees).
            # yes_price = round((yes_bid + yes_ask) / 2) for both YES and NO sides.
            live_mid = await _fetch_live_midmarket_yes_price(client, record["ticker"])
            if live_mid is not None:
                record["yes_price"] = live_mid
                cost_cents = live_mid if record["side"] == "yes" else (100 - live_mid)
                if cost_cents > 0:
                    record["contract_count"] = math.floor(
                        size_dollars / (cost_cents / 100.0)
                    )

            if dry_run:
                print(
                    f"[DRY-RUN] Would buy {record['side'].upper()} {record['contract_count']}x "
                    f"{record['ticker']} at yes_price={record['yes_price']}¢ "
                    f"size=${record['suggested_size_dollars']:.2f}"
                )
                record["order_status"] = "dry_run"
            else:
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
            session["dollars_spent"] += float(record["suggested_size_dollars"])
            session["tickers_traded"].append(record["ticker"])
            record["session_trade_number"] = session["trades_placed"]
            record["session_dollars_spent"] = round(session["dollars_spent"], 2)
            cycle_trades_placed += 1

            portfolio.total_exposure_dollars += float(record["suggested_size_dollars"])
            category = record.get("category", "")
            if category:
                portfolio.exposure_by_category[category] = round(
                    portfolio.exposure_by_category.get(category, 0.0)
                    + float(record["suggested_size_dollars"]),
                    2,
                )

            if not dry_run:
                _save_session(session_file, session)
                await _record_executed_trade(record, cycle_ts)

            _append_jsonl(log_dir, date_str, record)
            results.append(record)

    return results


def _main() -> None:
    parser = argparse.ArgumentParser(
        description="Night-mode execution: weather-only sized buys with session caps"
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
    session_capped = sum(1 for record in results if record["rejection_reason"] == "session_cap_reached")
    cycle_capped = sum(1 for record in results if record["rejection_reason"] == "cycle_cap_reached")
    capped = session_capped + cycle_capped
    print(
        f"Night mode: {executed} executed, "
        f"{len(results) - executed - capped} rejected, "
        f"{capped} capped"
    )


if __name__ == "__main__":
    _main()
