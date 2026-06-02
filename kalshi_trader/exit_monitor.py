"""Exit monitoring loop for open Kalshi positions.

Checks three exit conditions derived from the LunarResearcher strategy:
1. take_profit  — price converged 85% of the entry gap
2. volume_spike — Polymarket volume jumped 2× the entry-time baseline
3. stale_thesis — held 24h+ with <2% price move toward thesis
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone

from kalshi_trader.external.market_scorer import is_stale_thesis, should_take_profit
from kalshi_trader.external.polymarket import PolymarketClient
from kalshi_trader.models import Market, Side


@dataclass
class TradeEntry:
    ticker: str
    condition_id: str       # Polymarket condition ID for volume checks
    side: Side
    entry_price_prob: float  # 0.0–1.0 at time of entry
    entry_gap: float         # edge gap that motivated entry
    entry_time: datetime
    entry_volume_24h: float = 0.0  # Polymarket volume_24hr at entry; 0 = skip spike check


class ExitMonitor:
    def __init__(self, poly_client: PolymarketClient | None = None):
        self._client = poly_client or PolymarketClient()

    async def check_exits(
        self,
        open_trades: list[TradeEntry],
        kalshi_markets: list[Market],
    ) -> list[tuple[TradeEntry, str]]:
        """Return (trade, reason) for every position that should be closed.

        Checks are applied in order; the first matching condition wins.
        'volume_spike' is skipped when entry_volume_24h == 0.
        """
        if not open_trades:
            return []

        poly_markets = await self._client.get_markets()
        market_by_ticker = {m.ticker: m for m in kalshi_markets}
        exits: list[tuple[TradeEntry, str]] = []
        now = datetime.now(tz=timezone.utc)

        for trade in open_trades:
            km = market_by_ticker.get(trade.ticker)
            if km is None:
                continue

            # Current probability from Kalshi midpoint
            mid_prob = (km.yes_bid + km.yes_ask) / 2.0 / 100.0
            current_prob = mid_prob if trade.side == Side.YES else 1.0 - mid_prob

            # 1. Take profit
            if should_take_profit(trade.entry_price_prob, current_prob, trade.entry_gap):
                exits.append((trade, "take_profit"))
                continue

            # 2. Volume spike (only when we have an entry baseline)
            if trade.entry_volume_24h > 0:
                pm = self._client.match_market(km.title, poly_markets)
                if pm:
                    current_vol = float(pm.get("volume24hr", 0))
                    if self._client.detect_volume_spike(current_vol, [trade.entry_volume_24h]):
                        exits.append((trade, "volume_spike"))
                        continue

            # 3. Stale thesis
            hours_held = (now - trade.entry_time).total_seconds() / 3600.0
            price_change = abs(current_prob - trade.entry_price_prob)
            if is_stale_thesis(hours_held, price_change):
                exits.append((trade, "stale_thesis"))

        return exits
