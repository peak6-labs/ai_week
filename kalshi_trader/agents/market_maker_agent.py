from __future__ import annotations
import asyncio
from datetime import datetime, timezone
from typing import Any

from kalshi_trader.models import SignalEstimate


def _parse_orderbook(raw: dict) -> dict:
    """Extract best bid, best ask, spread, and imbalance from REST orderbook response.

    Kalshi orderbook response shape:
      {"orderbook": {"yes": [[price_cents, size], ...], "no": [[price_cents, size], ...]}}
    or
      {"yes": [...], "no": [...]}
    """
    ob = raw.get("orderbook", raw)
    yes_levels = ob.get("yes", [])
    no_levels = ob.get("no", [])

    # yes levels = bids (people willing to buy YES)
    # no levels = asks on YES (buying NO = selling YES)
    bid_prices = [lvl[0] for lvl in yes_levels if len(lvl) >= 2 and lvl[1] > 0]
    ask_prices = [lvl[0] for lvl in no_levels if len(lvl) >= 2 and lvl[1] > 0]

    best_bid = max(bid_prices) if bid_prices else None
    # NO price p means YES ask = 100 - p
    best_ask = (100 - min(ask_prices)) if ask_prices else None

    spread = (best_ask - best_bid) if (best_bid is not None and best_ask is not None) else None

    bid_vol = sum(lvl[1] for lvl in yes_levels if len(lvl) >= 2)
    ask_vol = sum(lvl[1] for lvl in no_levels if len(lvl) >= 2)
    total = bid_vol + ask_vol
    imbalance = (bid_vol - ask_vol) / total if total > 0 else 0.0

    return {
        "best_bid": best_bid,
        "best_ask": best_ask,
        "spread_cents": spread,
        "imbalance": imbalance,
        "bid_vol": bid_vol,
        "ask_vol": ask_vol,
    }


def analyze_spread_dynamics(snapshots: list[dict]) -> dict:
    """Classify spread regime from 3 orderbook snapshots taken seconds apart.

    Returns:
      signal: 'withdrawal' | 'widening' | 'directional' | 'normal'
      spread_cents: latest spread
      spread_trend: pct change from first to last snapshot
      imbalance: latest bid/ask imbalance
    """
    valid = [s for s in snapshots if s.get("spread_cents") is not None]
    if not valid:
        return {"signal": "normal", "spread_cents": None, "spread_trend": 0.0, "imbalance": 0.0}

    latest = valid[-1]
    spread = latest["spread_cents"]
    imbalance = latest["imbalance"]

    # Spread trend: % change from first to last valid snapshot
    if len(valid) >= 2 and valid[0]["spread_cents"] and valid[0]["spread_cents"] > 0:
        trend = (spread - valid[0]["spread_cents"]) / valid[0]["spread_cents"]
    else:
        trend = 0.0

    # Classify
    if spread is None:
        signal = "normal"
    elif spread > 15 or latest["best_bid"] is None or latest["best_ask"] is None:
        signal = "withdrawal"
    elif trend > 0.30 and abs(imbalance) > 0.60:
        signal = "directional"
    elif trend > 0.30:
        signal = "widening"
    else:
        signal = "normal"

    return {
        "signal": signal,
        "spread_cents": spread,
        "spread_trend": round(trend, 4),
        "imbalance": round(imbalance, 4),
    }


class MarketMakerAgent:
    """Detects market maker withdrawal and spread dynamics via REST orderbook snapshots."""

    def __init__(self, client: Any, snapshot_count: int = 3, snapshot_delay: float = 2.0) -> None:
        self._client = client
        self._snapshot_count = snapshot_count
        self._snapshot_delay = snapshot_delay

    async def run(self, ticker: str, title: str) -> list[SignalEstimate]:
        snapshots = []
        for i in range(self._snapshot_count):
            try:
                raw = await self._client.get_orderbook(ticker)
                snapshots.append(_parse_orderbook(raw))
            except Exception:
                pass
            if i < self._snapshot_count - 1:
                await asyncio.sleep(self._snapshot_delay)

        if not snapshots:
            return []

        dynamics = analyze_spread_dynamics(snapshots)

        if dynamics["signal"] == "normal":
            return []

        signal = dynamics["signal"]
        spread = dynamics["spread_cents"] or 0
        imbalance = dynamics["imbalance"]
        trend = dynamics["spread_trend"]

        # Probability: derive directional signal from imbalance
        # Positive imbalance (more bid volume) = YES pressure
        if signal == "withdrawal":
            # High uncertainty — skip directional bet
            return []

        # directional or widening with imbalance
        if abs(imbalance) < 0.20:
            return []

        prob = 0.5 + imbalance * 0.25
        prob = max(0.10, min(0.90, prob))

        if signal == "directional":
            uncertainty = 0.10
            weight = 0.65
        else:
            uncertainty = 0.15
            weight = 0.55

        narrative = (
            f"Spread={spread:.0f}¢ (trend {trend:+.0%}), "
            f"imbalance={imbalance:+.2f} ({'bid' if imbalance > 0 else 'ask'}-heavy). "
            f"Signal: {signal}."
        )

        return [SignalEstimate(
            source="kalshi_mm_spread",
            probability=round(prob, 4),
            uncertainty=uncertainty,
            weight=weight,
            data_issued_at=datetime.now(tz=timezone.utc),
            metadata={
                "ticker": ticker,
                "narrative": narrative,
                "data_quality": "fresh",
                "signal_type": signal,
                "spread_cents": spread,
                "spread_trend": trend,
                "imbalance": imbalance,
            },
        )]
