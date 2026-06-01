"""Polymarket Gamma API client.

Provides price signals for Kalshi markets by cross-referencing matching
Polymarket markets. When Polymarket (larger, more liquid) prices an event
differently than Kalshi, the gap is a trading edge.

LunarResearcher strategy adaptation:
- Polymarket prices as probability signal (weight 0.75)
- Volume-spike detection for exit triggers
- Title-overlap matching to find the same event across platforms
"""
from __future__ import annotations

import json
from datetime import datetime, timezone

import ssl

import aiohttp
import truststore

from kalshi_trader.models import SignalEstimate

def _ssl_context() -> ssl.SSLContext:
    """Build an SSL context that trusts the corporate proxy (Zscaler)."""
    ctx = truststore.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    return ctx

_GAMMA_BASE = "https://gamma-api.polymarket.com"
_POLYMARKET_WEIGHT = 0.75


class PolymarketClient:
    def to_signal_estimate(self, market: dict) -> SignalEstimate:
        prices = json.loads(market["outcomePrices"])
        yes_price = float(prices[0])

        # Uncertainty: markets near 50¢ are harder to call than near-resolved ones.
        # Use distance from 0.5: max at 50¢ (0.10), near zero at extremes.
        uncertainty = 0.10 * (1.0 - abs(yes_price - 0.5) / 0.5)

        issued = datetime.fromisoformat(
            market["updatedAt"].replace("Z", "+00:00")
        )

        return SignalEstimate(
            source="polymarket",
            probability=yes_price,
            uncertainty=round(uncertainty, 4),
            weight=_POLYMARKET_WEIGHT,
            data_issued_at=issued,
            metadata={"volume_24h": int(float(market.get("volume24hr", 0)))},
        )

    def match_market(self, kalshi_title: str, poly_markets: list[dict]) -> dict | None:
        """Find the best-matching Polymarket market by token overlap (Jaccard)."""
        kalshi_tokens = set(kalshi_title.lower().split())
        best_score = 0.0
        best_market = None
        for market in poly_markets:
            poly_tokens = set(market["question"].lower().split())
            intersection = len(kalshi_tokens & poly_tokens)
            union = len(kalshi_tokens | poly_tokens)
            score = intersection / union if union else 0.0
            if score > best_score:
                best_score = score
                best_market = market
        # Require at least 2 shared tokens AND >20% Jaccard
        if best_score < 0.20 or not best_market:
            return None
        kalshi_tokens_list = kalshi_title.lower().split()
        poly_tokens_list = best_market["question"].lower().split()
        shared = set(kalshi_tokens_list) & set(poly_tokens_list)
        if len(shared) < 2:
            return None
        return best_market

    def detect_volume_spike(self, current: float, recent_volumes: list[float]) -> bool:
        """Return True if current volume exceeds 2× the recent average."""
        if not recent_volumes:
            return False
        avg = sum(recent_volumes) / len(recent_volumes)
        return current > 2 * avg

    async def get_markets(self, limit: int = 500) -> list[dict]:
        """Fetch active markets from Polymarket Gamma API."""
        params = {"active": "true", "closed": "false", "limit": str(limit)}
        connector = aiohttp.TCPConnector(ssl=_ssl_context())
        async with aiohttp.ClientSession(connector=connector) as session:
            async with session.get(f"{_GAMMA_BASE}/markets", params=params) as resp:
                raw: list[dict] = await resp.json()
        return [m for m in raw if m.get("active") and not m.get("closed")]
