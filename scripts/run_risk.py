#!/usr/bin/env python
"""CLI wrapper around RiskManager — deterministic risk checks and Kelly sizing.

Usage:
    python scripts/run_risk.py --ideas-file /tmp/ideas.json --balance 800 [--positions-file /tmp/positions.json]

Input: JSON array of trade ideas with ticker, side, confidence, market_price, category.
Output: JSON array with approved=true/false, approved_size_dollars, rejection_reason.
"""
from __future__ import annotations

import argparse, json, sys
from pathlib import Path
from kalshi_trader.risk import RiskManager
from kalshi_trader.models import TradeIdea, PortfolioState, Side, OrderAction


def main() -> None:
    parser = argparse.ArgumentParser(description="Deterministic risk checks via RiskManager")
    parser.add_argument("--ideas-file", required=True)
    parser.add_argument("--balance", type=float, required=True, help="Available balance in dollars")
    parser.add_argument("--positions-file", default="", help="JSON with open positions (optional)")
    args = parser.parse_args()

    ideas_data = json.loads(Path(args.ideas_file).read_text())

    # Build PortfolioState
    from kalshi_trader.models import Position
    from datetime import datetime, timezone, timedelta
    positions = []
    if args.positions_file:
        for p in json.loads(Path(args.positions_file).read_text()):
            positions.append(Position(
                ticker=p["ticker"], side=Side(p["side"]),
                quantity=int(p.get("quantity", 0)),
                avg_price=float(p.get("avg_price", 50)),
                current_price=float(p.get("current_price", 50)),
                unrealized_pnl=float(p.get("unrealized_pnl", 0)),
                category=p.get("category", ""),
                close_time=datetime.now(tz=timezone.utc),
            ))
    exposure = sum(float(p.get("size_dollars", 0)) for p in (json.loads(Path(args.positions_file).read_text()) if args.positions_file else []))
    portfolio = PortfolioState(
        balance_dollars=args.balance,
        positions=positions,
        total_exposure_dollars=exposure,
    )

    rm = RiskManager()
    results = []
    for idea_data in ideas_data:
        idea = TradeIdea(
            agent_id=idea_data.get("agent_id", "data_orchestrator"),
            ticker=idea_data["ticker"],
            side=Side(idea_data["side"]),
            action=OrderAction.BUY,
            confidence=float(idea_data["confidence"]),
            market_price=float(idea_data["market_price"]),
            reasoning=idea_data.get("reasoning", ""),
            signal_sources=idea_data.get("signal_sources", []),
            category=idea_data.get("category", ""),
        )
        # Settlement-proximity gate, fail-closed: an idea without hours_to_close
        # cannot be confirmed safe, so it is rejected rather than silently skipping
        # the MIN_HOURS_BEFORE_SETTLEMENT check.
        hours_to_close = idea_data.get("hours_to_close")
        if hours_to_close is None:
            results.append({
                **idea_data, "approved": False, "approved_size_dollars": 0.0,
                "rejection_reason": "missing hours_to_close (settlement gate fail-closed)",
            })
            continue
        close_time = datetime.now(tz=timezone.utc) + timedelta(hours=float(hours_to_close))
        decision = rm.check_trade(idea, portfolio, close_time=close_time)
        results.append({
            **idea_data,
            "approved": decision.approved,
            "approved_size_dollars": decision.approved_size_dollars,
            "rejection_reason": decision.rejection_reason,
        })
        # Update portfolio exposure for subsequent ideas in batch
        if decision.approved:
            portfolio.total_exposure_dollars += decision.approved_size_dollars

    approved = sum(1 for r in results if r["approved"])
    print(json.dumps(results, indent=2, default=str))
    print(f"{approved}/{len(results)} ideas approved", file=sys.stderr)


if __name__ == "__main__":
    main()
