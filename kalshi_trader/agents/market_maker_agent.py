from __future__ import annotations
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from kalshi_trader.models import SignalEstimate
from kalshi_trader.agents.base import BaseAgent
from kalshi_trader.agents.parsing import parse_signal_estimates, estimate_to_dict

_PROMPTS_DIR = Path(__file__).parent / "prompts"


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
    """Classify spread regime from orderbook snapshots.

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


_SCHEMAS: list[dict] = [
    {
        "name": "get_orderbook",
        "description": "Fetch the current Kalshi orderbook for a ticker. Returns yes_bid, yes_ask, spread_cents, bid_depth, ask_depth, and timestamp.",
        "input_schema": {
            "type": "object",
            "properties": {
                "ticker": {"type": "string"},
            },
            "required": ["ticker"],
        },
    },
    {
        "name": "analyze_spread_dynamics",
        "description": "Analyze spread and depth imbalance from an orderbook snapshot. Returns spread_cents, spread_anomaly (bool), depth_imbalance (float), direction (YES/NO/neutral), and maker_withdrawal_score.",
        "input_schema": {
            "type": "object",
            "properties": {
                "ticker": {"type": "string"},
                "orderbook": {"type": "object"},
            },
            "required": ["ticker", "orderbook"],
        },
    },
    {
        "name": "build_market_maker_signal",
        "description": "Build a SignalEstimate dict from spread analysis. Returns the signal dict if spread_anomaly or abs(depth_imbalance) > 0.4, otherwise returns {\"signal\": null}.",
        "input_schema": {
            "type": "object",
            "properties": {
                "ticker": {"type": "string"},
                "analysis": {"type": "object"},
            },
            "required": ["ticker", "analysis"],
        },
    },
]


class MarketMakerAgent:
    """Detects market maker withdrawal and spread dynamics via a Claude tool-use loop."""

    def __init__(self, client: Any) -> None:
        self._client = client
        system_prompt = (_PROMPTS_DIR / "market_maker.md").read_text()
        self._agent = BaseAgent(
            tools=_SCHEMAS,
            handlers={
                "get_orderbook": self._get_orderbook,
                "analyze_spread_dynamics": self._analyze_spread_dynamics,
                "build_market_maker_signal": self._build_market_maker_signal,
            },
            system_prompt=system_prompt,
        )

    async def run(self, ticker: str, title: str) -> list[SignalEstimate]:
        prompt = f"Analyze market maker dynamics for this Kalshi market:\nticker: {ticker}\ntitle: {title}"
        raw = await self._agent.run(prompt)
        return parse_signal_estimates(raw)

    async def _get_orderbook(self, ticker: str) -> dict:
        raw = await self._client.get_orderbook(ticker)
        parsed = _parse_orderbook(raw)
        return {
            "yes_bid": parsed["best_bid"],
            "yes_ask": parsed["best_ask"],
            "spread_cents": parsed["spread_cents"],
            "bid_depth": float(parsed["bid_vol"]),
            "ask_depth": float(parsed["ask_vol"]),
            "timestamp": datetime.now(tz=timezone.utc).isoformat(),
        }

    async def _analyze_spread_dynamics(self, ticker: str, orderbook: dict) -> dict:
        # Reconstruct a snapshot compatible with the math function
        snapshot = {
            "spread_cents": orderbook.get("spread_cents"),
            "imbalance": _compute_imbalance(orderbook),
            "best_bid": orderbook.get("yes_bid"),
            "best_ask": orderbook.get("yes_ask"),
        }
        dynamics = analyze_spread_dynamics([snapshot])

        spread_cents = dynamics["spread_cents"]
        imbalance = dynamics["imbalance"]
        trend = dynamics["spread_trend"]

        spread_anomaly = spread_cents is not None and spread_cents > 8
        if imbalance > 0.1:
            direction = "YES"
        elif imbalance < -0.1:
            direction = "NO"
        else:
            direction = "neutral"

        # maker_withdrawal_score = spread_trend clamped to [0, 1]
        maker_withdrawal_score = max(0.0, min(1.0, trend))

        return {
            "spread_cents": spread_cents,
            "spread_anomaly": spread_anomaly,
            "depth_imbalance": imbalance,
            "direction": direction,
            "maker_withdrawal_score": maker_withdrawal_score,
        }

    async def _build_market_maker_signal(self, ticker: str, analysis: dict) -> dict:
        spread_anomaly = analysis.get("spread_anomaly", False)
        depth_imbalance = analysis.get("depth_imbalance", 0.0)

        if not spread_anomaly and abs(depth_imbalance) <= 0.4:
            return {"signal": None}

        spread_cents = analysis.get("spread_cents") or 0.0
        direction = analysis.get("direction", "neutral")
        maker_withdrawal_score = analysis.get("maker_withdrawal_score", 0.0)

        # Derive probability from depth imbalance
        prob = 0.5 + depth_imbalance * 0.25
        prob = max(0.10, min(0.90, prob))

        if spread_anomaly and abs(depth_imbalance) > 0.4:
            uncertainty = 0.10
            weight = 0.65
        elif spread_anomaly:
            uncertainty = 0.15
            weight = 0.55
        else:
            uncertainty = 0.12
            weight = 0.60

        narrative = (
            f"Spread {spread_cents:.0f}¢ (anomaly={spread_anomaly}). "
            f"Depth imbalance={depth_imbalance:+.2f} ({'bid' if depth_imbalance > 0 else 'ask'}-heavy). "
            f"Direction: {direction}."
        )

        estimate = SignalEstimate(
            source="market_maker",
            probability=round(prob, 4),
            uncertainty=uncertainty,
            weight=weight,
            data_issued_at=datetime.now(tz=timezone.utc),
            metadata={
                "ticker": ticker,
                "narrative": narrative,
                "data_quality": "fresh",
                "spread_cents": spread_cents,
                "depth_imbalance": depth_imbalance,
                "direction": direction,
                "maker_withdrawal_score": maker_withdrawal_score,
            },
        )
        return estimate_to_dict(estimate)


def _compute_imbalance(orderbook: dict) -> float:
    """Compute depth imbalance from an orderbook dict returned by _get_orderbook."""
    bid_depth = orderbook.get("bid_depth", 0.0)
    ask_depth = orderbook.get("ask_depth", 0.0)
    total = bid_depth + ask_depth
    if total == 0:
        return 0.0
    return (bid_depth - ask_depth) / total
