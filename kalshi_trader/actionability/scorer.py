from __future__ import annotations

from typing import TYPE_CHECKING

from kalshi_trader.models import Market, ScoredMarket
from kalshi_trader.actionability.signals import (
    hl_position_score,
    momentum_score,
    ofi_score,
    oi_change_score,
    orderbook_skew_score,
    relative_historical_volume_score,
    volume_oi_ratio_score,
    volume_spike_short_term_score,
)

if TYPE_CHECKING:
    from kalshi_trader.actionability.store import SnapshotStore


class MarketScorer:
    """Scores and ranks Kalshi markets by actionability.

    Weights reflect how strongly each signal indicates that something
    unusual is happening in a specific market right now.
    """

    WEIGHTS: dict[str, float] = {
        "relative_historical_volume": 0.25,
        "volume_spike_short_term":    0.20,
        "price_momentum":             0.15,
        "volume_oi_ratio":            0.10,
        "oi_change":                  0.10,
        "intraday_hl":                0.08,
        "ofi":                        0.07,
        "weekly_hl":                  0.04,
        "orderbook_skew":             0.01,
    }

    def score_all(
        self,
        markets: list[Market],
        store: "SnapshotStore",
    ) -> list[ScoredMarket]:
        """Score all markets using cached candles. Returns sorted descending."""
        scored = [self._score_one(m, store) for m in markets]
        scored.sort(key=lambda s: s.composite_score, reverse=True)
        return scored

    def _score_one(self, market: Market, store: "SnapshotStore") -> ScoredMarket:
        daily = store.get_daily(market.ticker)
        hourly = store.get_hourly(market.ticker)
        mid = (market.yes_bid + market.yes_ask) / 2.0

        scores: dict[str, float | None] = {
            "volume_oi_ratio":            volume_oi_ratio_score(market),
            "relative_historical_volume": relative_historical_volume_score(daily, market.volume_24h),
            "volume_spike_short_term":    volume_spike_short_term_score(hourly),
            "oi_change":                  oi_change_score(hourly),
            "price_momentum":             momentum_score(hourly),
            "intraday_hl":                hl_position_score(hourly, mid),
            "weekly_hl":                  hl_position_score(daily[-7:] if len(daily) >= 7 else daily, mid),
            "ofi":                        None,
            "orderbook_skew":             None,
        }

        return ScoredMarket(
            market=market,
            composite_score=self._composite(scores),
            volume_oi_ratio_score=scores["volume_oi_ratio"],  # type: ignore[arg-type]
            relative_historical_volume_score=scores["relative_historical_volume"],
            volume_spike_short_term_score=scores["volume_spike_short_term"],
            oi_change_score=scores["oi_change"],
            momentum_score=scores["price_momentum"],
            intraday_hl_score=scores["intraday_hl"],
            weekly_hl_score=scores["weekly_hl"],
            ofi_score=None,
            orderbook_skew_score=None,
        )

    def enrich_with_live(
        self,
        scored: list[ScoredMarket],
        trade_data: dict[str, list[dict]],
        orderbook_data: dict[str, dict],
    ) -> list[ScoredMarket]:
        """Add OFI and orderbook skew for markets that have live data, then re-sort."""
        for s in scored:
            ticker = s.market.ticker
            if ticker in trade_data:
                s.ofi_score = ofi_score(trade_data[ticker])
            if ticker in orderbook_data:
                s.orderbook_skew_score = orderbook_skew_score(orderbook_data[ticker])
            all_scores: dict[str, float | None] = {
                "volume_oi_ratio":            s.volume_oi_ratio_score,
                "relative_historical_volume": s.relative_historical_volume_score,
                "volume_spike_short_term":    s.volume_spike_short_term_score,
                "oi_change":                  s.oi_change_score,
                "price_momentum":             s.momentum_score,
                "intraday_hl":                s.intraday_hl_score,
                "weekly_hl":                  s.weekly_hl_score,
                "ofi":                        s.ofi_score,
                "orderbook_skew":             s.orderbook_skew_score,
            }
            s.composite_score = self._composite(all_scores)

        scored.sort(key=lambda s: s.composite_score, reverse=True)
        return scored

    def _composite(self, scores: dict[str, float | None]) -> float:
        """Weighted average over non-None signals. Re-normalizes when signals are absent."""
        total_weight = 0.0
        weighted_sum = 0.0
        for key, weight in self.WEIGHTS.items():
            val = scores.get(key)
            if val is not None:
                weighted_sum += val * weight
                total_weight += weight
        if total_weight <= 0:
            return 0.0
        return weighted_sum / total_weight
