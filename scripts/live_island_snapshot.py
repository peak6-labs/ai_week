#!/usr/bin/env python
"""Price snapshot for Love Island USA S8E2 live-play arb monitoring.

Detects significant price moves between polls, flags cross-market arb
opportunities (elimination markets are mutually exclusive), and outputs
structured JSON for the /live-island skill to act on.

Usage:
    # First run of the session — saves baseline AND last-poll state:
    KALSHI_ENV=prod PYTHONPATH=. python scripts/live_island_snapshot.py --save-baseline

    # Every subsequent poll during the episode:
    KALSHI_ENV=prod PYTHONPATH=. python scripts/live_island_snapshot.py
"""
from __future__ import annotations

import argparse
import asyncio
import json
from datetime import datetime, timezone
from pathlib import Path

import kalshi_trader.config  # noqa: F401 — loads .env
from kalshi_trader.client import KalshiClient

# Hard exit deadline: 11 PM ET = 03:00 UTC June 4
_HARD_EXIT_UTC = datetime(2026, 6, 4, 3, 0, 0, tzinfo=timezone.utc)
_WARN_EXIT_UTC = datetime(2026, 6, 4, 2, 30, 0, tzinfo=timezone.utc)

_BASELINE_FILE = Path("/tmp/li_baseline.json")
_LAST_POLL_FILE = Path("/tmp/li_last_poll.json")

# Spread threshold: skip entry on markets wider than this
_MAX_ENTRY_SPREAD_CENTS = 3

# Movement thresholds that trigger arb signals
_ELIM_ALERT_CENTS = 5    # minor alert — watch this market
_ELIM_STRONG_CENTS = 15  # strong signal — cross-market arb likely
_MENTION_ALERT_CENTS = 10  # mention market spike — word likely said

_ELIM_TICKERS = [
    "KXLIUSAELIMINATION-26JUN03-KEN",
    "KXLIUSAELIMINATION-26JUN03-BRY",
    "KXLIUSAELIMINATION-26JUN03-TRI",
    "KXLIUSAELIMINATION-26JUN03-SEA",
    "KXLIUSAELIMINATION-26JUN03-BEA",
    "KXLIUSAELIMINATION-26JUN03-ZAC",
]

_MENTION_TICKERS = [
    "KXLOVEISLMENTION-26JUN03-BOMB",
    "KXLOVEISLMENTION-26JUN03-DRAM",
    "KXLOVEISLMENTION-26JUN03-PRIZ",
]

_BOMBSHELL_TICKER = "KXLIUSABOMBSHELL-26JUN03-2"

_LABELS = {
    "KXLIUSAELIMINATION-26JUN03-KEN": "Kenzie",
    "KXLIUSAELIMINATION-26JUN03-BRY": "Bryce",
    "KXLIUSAELIMINATION-26JUN03-TRI": "Trinity",
    "KXLIUSAELIMINATION-26JUN03-SEA": "Sean",
    "KXLIUSAELIMINATION-26JUN03-BEA": "Beatriz",
    "KXLIUSAELIMINATION-26JUN03-ZAC": "Zach",
    "KXLIUSABOMBSHELL-26JUN03-2":     "2nd bombshell",
    "KXLOVEISLMENTION-26JUN03-BOMB":  "'Bombshell'",
    "KXLOVEISLMENTION-26JUN03-DRAM":  "'Drama'",
    "KXLOVEISLMENTION-26JUN03-PRIZ":  "'Prize/100k'",
}

_ALL_TICKERS = _ELIM_TICKERS + [_BOMBSHELL_TICKER] + _MENTION_TICKERS


async def fetch_prices(tickers: list[str]) -> dict[str, dict]:
    prices: dict[str, dict] = {t: {"yes_bid": None, "yes_ask": None, "last_price": None} for t in tickers}
    async with KalshiClient() as client:
        response = await client.get_markets(tickers=",".join(tickers), limit=len(tickers) + 5)
        for market in response.get("markets") or []:
            ticker = market.get("ticker")
            if ticker in prices:
                prices[ticker] = {
                    "yes_bid": market.get("yes_bid"),
                    "yes_ask": market.get("yes_ask"),
                    "last_price": market.get("last_price"),
                }
    return prices


async def fetch_li_positions() -> dict[str, dict]:
    li_positions: dict[str, dict] = {}
    async with KalshiClient() as client:
        response = await client.get_positions()
        for position in response.get("market_positions") or []:
            ticker = position.get("ticker", "")
            if any(t in ticker for t in ("KXLIUSA", "KXLOVEISLMENTION-26JUN03")):
                net_position = position.get("position", 0)
                if net_position != 0:
                    li_positions[ticker] = {
                        "quantity": abs(net_position),
                        "side": "yes" if net_position > 0 else "no",
                    }
    return li_positions


async def _fetch_all(tickers: list[str]) -> tuple[dict, dict]:
    prices, li_positions = await asyncio.gather(
        fetch_prices(tickers),
        fetch_li_positions(),
    )
    return prices, li_positions


def _load_prices(path: Path) -> dict[str, dict]:
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text()).get("prices", {})
    except Exception:
        return {}


def _fmt_delta(delta: float | None, *, flag_strong: bool = False) -> str:
    if delta is None:
        return "  n/a"
    sign = "+" if delta >= 0 else ""
    marker = "!" if flag_strong and abs(delta) >= _ELIM_STRONG_CENTS else ""
    return f"{sign}{delta:.0f}¢{marker}"


def _kalshi_fee(size_dollars: float, price_cents: float, side: str) -> float:
    """Kalshi taker fee: 0.07 × size × p × (1-p), with p = price fraction."""
    price_frac = price_cents / 100.0
    if side == "yes":
        return round(0.07 * size_dollars * (1.0 - price_frac), 3)
    else:
        return round(0.07 * size_dollars * price_frac, 3)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--save-baseline", action="store_true",
                        help="Save current prices as session baseline and last-poll state")
    args = parser.parse_args()

    prices, li_positions = asyncio.run(_fetch_all(_ALL_TICKERS))
    now_utc = datetime.now(timezone.utc)

    if args.save_baseline:
        payload = {"saved_at": now_utc.isoformat(), "prices": prices}
        _BASELINE_FILE.write_text(json.dumps(payload))
        _LAST_POLL_FILE.write_text(json.dumps(payload))
        print(f"Baseline + last-poll saved.")
        return

    baseline_prices = _load_prices(_BASELINE_FILE)
    last_poll_prices = _load_prices(_LAST_POLL_FILE)

    # Save this poll as the new last-poll immediately
    _LAST_POLL_FILE.write_text(json.dumps({"saved_at": now_utc.isoformat(), "prices": prices}))

    # Time to exit
    secs_to_warn = (_WARN_EXIT_UTC - now_utc).total_seconds()
    secs_to_hard = (_HARD_EXIT_UTC - now_utc).total_seconds()
    if secs_to_hard <= 0:
        exit_label = "HARD EXIT PAST — close all positions NOW"
    elif secs_to_warn <= 0:
        exit_label = f"EXIT WINDOW — {int(secs_to_hard/60)}m to hard exit (11 PM ET)"
    else:
        exit_label = f"OK — warn in {int(secs_to_warn/60)}m, hard exit in {int(secs_to_hard/60)}m"

    # Exposure
    total_li_exposure = 0.0
    for ticker, pos in li_positions.items():
        data = prices.get(ticker, {})
        bid, ask = data.get("yes_bid"), data.get("yes_ask")
        if bid is not None and ask is not None:
            midpoint = (bid + ask) / 2.0
            cost_per_contract = midpoint if pos["side"] == "yes" else (100 - midpoint)
            total_li_exposure += pos["quantity"] * cost_per_contract / 100.0
    remaining_headroom = 100.0 - total_li_exposure

    # Deduct night-mode session spend toward the shared $100 nightly cap
    _session_date = now_utc.strftime("%Y%m%d")
    _session_path = Path(f"reports/night-mode-session-{_session_date}.json")
    night_mode_dollars_spent = 0.0
    if _session_path.exists():
        try:
            _session_data = json.loads(_session_path.read_text())
            night_mode_dollars_spent = float(_session_data.get("dollars_spent", 0.0))
        except Exception:
            pass
    remaining_headroom = max(0.0, 100.0 - total_li_exposure - night_mode_dollars_spent)

    # Build per-market data
    market_rows = []
    for ticker in _ALL_TICKERS:
        data = prices.get(ticker, {})
        bid = data.get("yes_bid")
        ask = data.get("yes_ask")
        spread = (ask - bid) if (bid is not None and ask is not None) else None
        wide = spread is not None and spread > _MAX_ENTRY_SPREAD_CENTS

        last_bid = last_poll_prices.get(ticker, {}).get("yes_bid")
        base_bid = baseline_prices.get(ticker, {}).get("yes_bid")
        delta_poll = (bid - last_bid) if (bid is not None and last_bid is not None) else None
        delta_session = (bid - base_bid) if (bid is not None and base_bid is not None) else None

        pos = li_positions.get(ticker)
        market_rows.append({
            "ticker": ticker,
            "label": _LABELS.get(ticker, ticker),
            "yes_bid": bid,
            "yes_ask": ask,
            "spread_cents": spread,
            "wide_spread": wide,
            "delta_poll": delta_poll,
            "delta_session": delta_session,
            "position": pos,
            "fee_yes_per_5": _kalshi_fee(5.0, ask, "yes") if ask else None,
            "fee_no_per_5": _kalshi_fee(5.0, bid, "no") if bid else None,
        })

    # Detect arb signals
    alerts = []
    arb_opportunities = []

    elim_rows = [r for r in market_rows if r["ticker"] in _ELIM_TICKERS]
    mention_rows = [r for r in market_rows if r["ticker"] in _MENTION_TICKERS]

    for row in elim_rows:
        if row["delta_poll"] is None or row["wide_spread"]:
            continue
        if row["delta_poll"] >= _ELIM_STRONG_CENTS:
            # Strong spike — cross-market arb: buy NO on all other untouched elimination markets
            other_targets = [
                r for r in elim_rows
                if r["ticker"] != row["ticker"]
                and not r["wide_spread"]
                and r["yes_bid"] is not None
                and r["yes_bid"] >= 2  # still has profit margin
                and (r["position"] is None or r["position"]["side"] != "no")
            ]
            arb_opportunities.append({
                "type": "elim_cross_arb",
                "trigger_ticker": row["ticker"],
                "trigger_label": row["label"],
                "trigger_delta_poll": row["delta_poll"],
                "trigger_yes_bid": row["yes_bid"],
                "action": "BUY_NO_OTHERS",
                "targets": [
                    {"ticker": t["ticker"], "label": t["label"],
                     "yes_bid": t["yes_bid"], "yes_ask": t["yes_ask"],
                     "fee_no_per_5": t["fee_no_per_5"]}
                    for t in other_targets
                ],
            })
            alerts.append(f"STRONG: {row['label']} YES +{row['delta_poll']:.0f}¢ this poll — cross-arb triggered")
        elif row["delta_poll"] >= _ELIM_ALERT_CENTS:
            alerts.append(f"ALERT: {row['label']} YES +{row['delta_poll']:.0f}¢ this poll — watch")
        elif row["delta_poll"] <= -_ELIM_ALERT_CENTS:
            alerts.append(f"DROP: {row['label']} YES {row['delta_poll']:.0f}¢ this poll")

    for row in mention_rows:
        if row["delta_poll"] is None or row["wide_spread"]:
            continue
        if row["delta_poll"] >= _MENTION_ALERT_CENTS:
            arb_opportunities.append({
                "type": "mention_spike",
                "trigger_ticker": row["ticker"],
                "trigger_label": row["label"],
                "trigger_delta_poll": row["delta_poll"],
                "action": "BUY_YES",
                "yes_ask": row["yes_ask"],
                "fee_yes_per_5": row["fee_yes_per_5"],
            })
            alerts.append(f"MENTION SPIKE: {row['label']} YES +{row['delta_poll']:.0f}¢ — BUY YES")
        elif row["delta_poll"] <= -_MENTION_ALERT_CENTS and row["yes_bid"] and row["yes_bid"] >= 15:
            arb_opportunities.append({
                "type": "mention_drop",
                "trigger_ticker": row["ticker"],
                "trigger_label": row["label"],
                "trigger_delta_poll": row["delta_poll"],
                "action": "BUY_NO",
                "yes_bid": row["yes_bid"],
                "fee_no_per_5": row["fee_no_per_5"],
            })
            alerts.append(f"MENTION DROP: {row['label']} YES {row['delta_poll']:.0f}¢ — consider NO")

    # Print table
    print(f"\n{'='*76}")
    print(f"  LOVE ISLAND USA S8E2 — LIVE ARB MONITOR  {now_utc.strftime('%H:%M:%S UTC')}")
    print(f"  {exit_label}")
    print(f"  LI Exposure: ${total_li_exposure:.2f}  Night-mode: ${night_mode_dollars_spent:.2f}  Headroom: ${remaining_headroom:.2f} / $100")
    if alerts:
        print(f"  *** {' | '.join(alerts)}")
    print(f"{'='*76}")
    print(f"  {'Market':<28} {'Bid':>5} {'Ask':>5} {'Sprd':>5} {'Δ Poll':>8} {'Δ Sess':>8} {'Pos':>10}")
    print(f"  {'-'*74}")

    for row in market_rows:
        bid_s = f"{row['yes_bid']:.0f}¢" if row["yes_bid"] is not None else "  n/a"
        ask_s = f"{row['yes_ask']:.0f}¢" if row["yes_ask"] is not None else "  n/a"
        sprd_s = (f"{row['spread_cents']:.0f}¢!" if row["wide_spread"] else
                  f"{row['spread_cents']:.0f}¢") if row["spread_cents"] is not None else "  n/a"
        dp_s = _fmt_delta(row["delta_poll"], flag_strong=row["ticker"] in _ELIM_TICKERS)
        ds_s = _fmt_delta(row["delta_session"])
        pos_s = f"{row['position']['side'].upper()} x{row['position']['quantity']:.0f}" if row["position"] else ""
        short = row["ticker"].split("-")[-1]
        print(f"  {short+' '+row['label']:<28} {bid_s:>5} {ask_s:>5} {sprd_s:>5} {dp_s:>8} {ds_s:>8} {pos_s:>10}")

    print(f"  '!' on spread = avoid entry  |  '!' on Δ Poll = strong signal (≥{_ELIM_STRONG_CENTS}¢)")
    print(f"{'='*76}\n")

    output = {
        "timestamp": now_utc.isoformat(),
        "alerts": alerts,
        "arb_opportunities": arb_opportunities,
        "markets": market_rows,
        "li_positions": li_positions,
        "total_li_exposure_dollars": round(total_li_exposure, 2),
        "night_mode_dollars_spent": round(night_mode_dollars_spent, 2),
        "remaining_headroom_dollars": round(remaining_headroom, 2),
        "minutes_to_hard_exit": max(0, int(secs_to_hard / 60)),
        "past_hard_exit": secs_to_hard <= 0,
        "tradeable_tickers": [r["ticker"] for r in market_rows
                               if not r["wide_spread"] and r["yes_ask"] is not None],
    }
    print(json.dumps(output))


if __name__ == "__main__":
    main()
