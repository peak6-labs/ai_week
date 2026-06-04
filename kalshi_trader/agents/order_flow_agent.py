from __future__ import annotations
from collections import deque
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from kalshi_trader.models import SignalEstimate
from kalshi_trader.agents.base import BaseAgent
from kalshi_trader.agents.parsing import parse_signal_estimates, estimate_to_dict
from kalshi_trader.ui.config_manager import cfg

_PROMPTS_DIR = Path(__file__).parent / "prompts"


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


def _trade_dollar_volume(t: dict) -> float:
    """Extract dollar volume from a trade dict.

    Kalshi v2 API uses count_fp (string) and yes_price_dollars (string, 0–1).
    Legacy/test dicts may use count (int) and yes_price (int, 0–100).
    """
    count_fp = t.get("count_fp")
    if count_fp is not None:
        count = float(count_fp)
    else:
        count = float(t.get("count", t.get("size", 0)))

    yes_price_dollars = t.get("yes_price_dollars")
    if yes_price_dollars is not None:
        price = float(yes_price_dollars)
    else:
        price = float(t.get("yes_price", t.get("price", 50))) / 100.0

    return count * price


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
        dollar_vol = _trade_dollar_volume(t)
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
        dollar_vol = _trade_dollar_volume(t)
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


_SCHEMAS: list[dict] = [
    {
        "name": "fetch_and_compute_metrics",
        "description": (
            "Fetch recent trades for a Kalshi market and compute all order flow metrics in one call. "
            "Returns vpin_score, high_informed_trading, ofi_score, direction, buying_fraction, "
            "recent_trade_count (last 60 min), and total_trades."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "ticker": {"type": "string"},
            },
            "required": ["ticker"],
        },
    },
    {
        "name": "build_order_flow_signal",
        "description": "Construct a SignalEstimate dict from VPIN and OFI results. Returns a SignalEstimate dict ready for JSON output.",
        "input_schema": {
            "type": "object",
            "properties": {
                "ticker": {"type": "string"},
                "vpin_result": {
                    "type": "object",
                    "description": "Result from compute_vpin: {vpin_score, high_informed_trading}",
                },
                "ofi_result": {
                    "type": "object",
                    "description": "Result from compute_ofi: {ofi_score, direction, buying_fraction}",
                },
            },
            "required": ["ticker", "vpin_result", "ofi_result"],
        },
    },
]


class OrderFlowAgent:
    """Detects elevated informed trading via VPIN and OFI from Kalshi trade history."""

    def __init__(self, client: Any) -> None:
        self._client = client
        system_prompt = (_PROMPTS_DIR / "order_flow.md").read_text()
        self._agent = BaseAgent(
            tools=_SCHEMAS,
            handlers={
                "fetch_and_compute_metrics": self._fetch_and_compute_metrics,
                "build_order_flow_signal": self._build_order_flow_signal,
            },
            system_prompt=system_prompt,
        )

    async def run(self, ticker: str, title: str) -> list[SignalEstimate]:
        prompt = f"Analyze order flow for this Kalshi market:\nticker: {ticker}\ntitle: {title}"
        raw = await self._agent.run(prompt)
        return parse_signal_estimates(raw)

    async def _get_market_trades(self, ticker: str, limit: int = 200) -> list[dict]:
        result = await self._client.get_trades(ticker, limit=limit)
        if isinstance(result, list):
            return result
        return result.get("trades", [])

    async def _fetch_and_compute_metrics(self, ticker: str) -> dict:
        """Fetch trades and compute all metrics in one call so no trade data passes through the LLM."""
        trades = await self._get_market_trades(ticker, limit=200)
        vpin_result = await self._compute_vpin(trades)
        ofi_result = await self._compute_ofi(trades)
        return {
            "vpin_score": vpin_result["vpin_score"],
            "high_informed_trading": vpin_result["high_informed_trading"],
            "ofi_score": ofi_result["ofi_score"],
            "direction": ofi_result["direction"],
            "buying_fraction": ofi_result["buying_fraction"],
            "recent_ofi_trades": ofi_result["recent_ofi_trades"],
            "recent_trade_count": _recent_trade_count(trades, window_minutes=cfg.get("trade_count_window_minutes")),
            "total_trades": len(trades),
        }

    async def _compute_vpin(self, trades: list[dict], n_buckets: int = 10) -> dict:
        # Derive bucket_size_usd from total volume / n_buckets so we get ~n_buckets buckets
        total_vol = sum(_trade_dollar_volume(t) for t in trades)
        bucket_size_usd = max(total_vol / n_buckets, 1.0) if trades else 1.0
        vpin_score = compute_vpin(trades, bucket_size_usd=bucket_size_usd)
        return {
            "vpin_score": round(vpin_score, 4),
            "high_informed_trading": vpin_score > 0.4,
        }

    async def _compute_ofi(self, trades: list[dict]) -> dict:
        window_minutes = cfg.get("ofi_window_minutes")
        ofi_score = compute_ofi(trades, window_minutes=window_minutes)

        # buying_fraction must use the same window as OFI so they can't contradict each other
        now = datetime.now(tz=timezone.utc)
        cutoff = now.timestamp() - window_minutes * 60
        windowed_trades = [t for t in trades if _parse_trade_time(t).timestamp() >= cutoff]

        if ofi_score > 0.1:
            direction = "YES"
        elif ofi_score < -0.1:
            direction = "NO"
        else:
            direction = "neutral"

        total_buy = sum(
            _trade_dollar_volume(t) for t in windowed_trades
            if t.get("taker_side", t.get("side", "yes")) == "yes"
        )
        total_all = sum(_trade_dollar_volume(t) for t in windowed_trades)
        buying_fraction = round(total_buy / total_all, 4) if total_all > 0 else 0.5
        return {
            "ofi_score": round(ofi_score, 4),
            "direction": direction,
            "buying_fraction": buying_fraction,
            "recent_ofi_trades": len(windowed_trades),
        }

    async def _build_order_flow_signal(
        self,
        ticker: str,
        vpin_result: dict,
        ofi_result: dict,
    ) -> dict:
        vpin_score = float(vpin_result.get("vpin_score", 0.0))
        ofi_score = float(ofi_result.get("ofi_score", 0.0))
        direction = ofi_result.get("direction", "neutral")

        # Probability: 0.5 base, shifted by OFI direction
        base_prob = 0.5 + ofi_score * cfg.get("ofi_prob_scale")
        prob = max(0.05, min(0.95, base_prob))

        # Uncertainty: higher VPIN → more confident (lower uncertainty)
        uncertainty = max(0.05, 0.25 - max(vpin_score - 0.4, 0.0) * 0.08)

        estimate = SignalEstimate(
            source="order_flow",
            probability=round(prob, 4),
            uncertainty=round(uncertainty, 4),
            weight=0.25,
            data_issued_at=datetime.now(tz=timezone.utc),
            metadata={
                "ticker": ticker,
                "narrative": (
                    f"VPIN of {vpin_score:.2f} indicates "
                    f"{'active' if vpin_result.get('high_informed_trading') else 'moderate'} informed trading. "
                    f"OFI {direction}-directional (buying_fraction={ofi_result.get('buying_fraction', 0.5):.2f})."
                ),
                "data_quality": "fresh",
                "vpin_score": vpin_score,
                "ofi_score": ofi_score,
                "ofi_direction": direction,
            },
        )
        return estimate_to_dict(estimate)
