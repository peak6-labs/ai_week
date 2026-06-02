"""Polymarket specialist agent for Kalshi trading.

Combines two signals from the LunarResearcher strategy:
1. Price gap — Polymarket prices the same event differently from Kalshi
2. Whale copy — a known high-performing wallet is entering the same direction

Entry logic: gap >= 7¢ AND market passes quality filter (depth/hours).
Confidence boost: target whale is entering in the same direction.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone

from kalshi_trader.external.market_scorer import score_market
from kalshi_trader.external.polymarket import PolymarketClient
from kalshi_trader.models import Market, OrderAction, Side, TradeIdea
from kalshi_trader.ui.config_manager import cfg

_AGENT_ID = "polymarket"


class PolymarketAgent:
    def __init__(
        self,
        target_wallets: list[str],
        client: PolymarketClient | None = None,
    ):
        self._targets = set(target_wallets)
        self._client = client or PolymarketClient()

    async def run(self, markets: list[Market]) -> list[TradeIdea]:
        if not markets:
            return []

        whale_boost = cfg.get("poly_whale_confidence_boost")
        max_conf = cfg.get("poly_max_confidence")
        gap_scale = cfg.get("poly_gap_scale")

        poly_markets = await self._client.get_markets_cached()
        ideas: list[TradeIdea] = []

        for km in markets:
            match = self._client.match_market(km.title, poly_markets)
            if not match:
                continue

            poly_prob = float(json.loads(match["outcomePrices"])[0])
            score = score_market(km, poly_prob)
            if score is None:
                continue

            kalshi_midpoint = (km.yes_bid + km.yes_ask) / 2.0  # cents
            gap = poly_prob - kalshi_midpoint / 100.0           # signed, probability units
            side = Side.YES if gap > 0 else Side.NO

            # Base confidence from gap size: 7¢ → ~0.35, 20¢ → 1.0 (capped)
            base_conf = min(abs(gap) / gap_scale, max_conf)

            # Check for agreeing target-whale entries
            trades = await self._client.get_large_trades(match["conditionId"])
            whale_signals = self._client.detect_whale_entries(trades)
            target_entries = [
                s for s in whale_signals
                if s.wallet_address in self._targets and s.side.lower() == side.value
            ]

            confidence = base_conf
            sources = ["polymarket_price"]
            if target_entries:
                confidence = min(confidence + whale_boost, max_conf)
                sources.append(f"whale_copy:{len(target_entries)}_targets")

            ideas.append(TradeIdea(
                agent_id=_AGENT_ID,
                ticker=km.ticker,
                side=side,
                action=OrderAction.BUY,
                confidence=round(confidence, 4),
                market_price=kalshi_midpoint,
                reasoning=(
                    f"Polymarket: {poly_prob:.0%}  Kalshi: {kalshi_midpoint:.0f}¢  "
                    f"Gap: {gap:+.0%}"
                    + (f"  {len(target_entries)} whale(s) agree" if target_entries else "")
                ),
                signal_sources=sources,
            ))

        return ideas
