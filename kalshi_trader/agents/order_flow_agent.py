from __future__ import annotations
import asyncio
from collections import deque
from datetime import datetime, timezone
from typing import Any

from kalshi_trader.models import SignalEstimate


def _parse_trade_time(t: dict) -> datetime:
    ts = t.get("created_time") or t.get("ts") or ""
    if not ts:
        return datetime.now(tz=timezone.utc)
    ts = ts.replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(ts)
    except ValueError:
        return datetime.now(tz=timezone.utc)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def compute_ofi(trades: list[dict], window_minutes: int = 30) -> float:
    """Order Flow Imbalance: (buy_vol - sell_vol) / total_vol in [-1, 1].

    A Kalshi trade with taker_side='yes' means someone bought YES contracts
    (bullish pressure). taker_side='no' means bought NO (bearish pressure).
    Returns 0.0 if no trades.
    """
    now = datetime.now(tz=timezone.utc)
    cutoff = now.timestamp() - window_minutes * 60

    buy_vol = 0.0
    sell_vol = 0.0
    for t in trades:
        if _parse_trade_time(t).timestamp() < cutoff:
            continue
        count = float(t.get("count", t.get("size", 0)))
        price = float(t.get("yes_price", t.get("price", 50))) / 100.0
        dollar_vol = count * price
        side = t.get("taker_side", t.get("side", "yes"))
        if side == "yes":
            buy_vol += dollar_vol
        else:
            sell_vol += dollar_vol

    total = buy_vol + sell_vol
    if total == 0:
        return 0.0
    return (buy_vol - sell_vol) / total


def compute_vpin(trades: list[dict], bucket_size_usd: float = 5000.0) -> float:
    """VPIN: average order flow toxicity across equal-volume buckets.

    Groups trades into buckets of `bucket_size_usd` dollars each. For each
    bucket, toxicity = |buy_vol - sell_vol| / bucket_size. VPIN is the mean
    toxicity across up to 50 buckets. Values > 1.5 indicate elevated informed
    trading probability.
    """
    if not trades:
        return 0.0

    toxicities: deque = deque(maxlen=50)
    bucket_buy = 0.0
    bucket_sell = 0.0
    bucket_total = 0.0

    for t in trades:
        count = float(t.get("count", t.get("size", 0)))
        price = float(t.get("yes_price", t.get("price", 50))) / 100.0
        dollar_vol = count * price
        side = t.get("taker_side", t.get("side", "yes"))

        if side == "yes":
            bucket_buy += dollar_vol
        else:
            bucket_sell += dollar_vol
        bucket_total += dollar_vol

        if bucket_total >= bucket_size_usd:
            tau = abs(bucket_buy - bucket_sell) / bucket_size_usd
            toxicities.append(tau)
            bucket_buy = bucket_sell = bucket_total = 0.0

    if not toxicities:
        return 0.0
    return sum(toxicities) / len(toxicities)


def _recent_trade_count(trades: list[dict], window_minutes: int = 60) -> int:
    now = datetime.now(tz=timezone.utc)
    cutoff = now.timestamp() - window_minutes * 60
    return sum(1 for t in trades if _parse_trade_time(t).timestamp() >= cutoff)


class OrderFlowAgent:
    """Detects elevated informed trading via VPIN and OFI from Kalshi trade history."""

    def __init__(self, client: Any) -> None:
        self._client = client

    async def run(self, ticker: str, title: str) -> list[SignalEstimate]:
        try:
            trades = await self._client.get_trades(ticker, limit=200)
        except Exception:
            return []

        trade_list = trades if isinstance(trades, list) else trades.get("trades", [])

        recent_count = _recent_trade_count(trade_list, window_minutes=60)
        if recent_count < 20:
            return []

        ofi = compute_ofi(trade_list, window_minutes=30)
        vpin = compute_vpin(trade_list, bucket_size_usd=5000.0)

        if vpin < 1.0 or abs(ofi) < 0.20:
            return []

        # Direction: positive OFI → buy pressure → YES more likely
        base_prob = 0.5 + ofi * 0.25
        prob = max(0.05, min(0.95, base_prob))

        # Uncertainty scales with VPIN level: higher VPIN = more confident signal
        uncertainty = max(0.05, 0.25 - (vpin - 1.0) * 0.08)

        return [SignalEstimate(
            source="kalshi_ofi",
            probability=round(prob, 4),
            uncertainty=round(uncertainty, 4),
            weight=0.70,
            data_issued_at=datetime.now(tz=timezone.utc),
            metadata={
                "ticker": ticker,
                "narrative": (
                    f"VPIN={vpin:.2f} (>1.0 = elevated informed trading). "
                    f"OFI={ofi:+.3f} ({'buy' if ofi > 0 else 'sell'} pressure). "
                    f"{recent_count} trades in last 60 min."
                ),
                "data_quality": "fresh",
                "vpin": round(vpin, 4),
                "ofi": round(ofi, 4),
                "recent_trade_count": recent_count,
            },
        )]
