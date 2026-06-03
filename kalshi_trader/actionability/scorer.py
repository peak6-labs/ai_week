from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from kalshi_trader.models import Market, ScoredMarket

_log = logging.getLogger(__name__)
from kalshi_trader.actionability.signals import (
    hl_position_score,
    momentum_score,
    ofi_score,
    oi_change_score,
    orderbook_skew_score,
    live_top_of_book,
    relative_historical_volume_score,
    spread_penalty_multiplier,
    volume_oi_ratio_score,
    volume_spike_short_term_score,
)
from kalshi_trader.signals.microstructure import (
    range_position,
    signed_momentum_cents,
    signed_ofi,
    signed_orderbook_skew,
)

if TYPE_CHECKING:
    from kalshi_trader.actionability.store import SnapshotStore


class MarketScorer:
    """Scores and ranks Kalshi markets by actionability.

    Weights reflect how strongly each signal indicates that something
    unusual is happening in a specific market right now.
    """

    MIN_COVERAGE: float = 0.30  # fraction of total weight that must be present

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
        scored = [self._score_one(market, store) for market in markets]
        scored.sort(key=lambda s: s.composite_score, reverse=True)
        return scored

    def _score_one(self, market: Market, store: "SnapshotStore") -> ScoredMarket:
        daily_candles = store.get_daily(market.ticker)
        hourly_candles = store.get_hourly(market.ticker)
        _log.debug("%s  daily=%d  hourly=%d", market.ticker, len(daily_candles), len(hourly_candles))
        midpoint_price = (market.yes_bid + market.yes_ask) / 2.0

        scores: dict[str, float | None] = {
            "volume_oi_ratio":            volume_oi_ratio_score(market),
            "relative_historical_volume": relative_historical_volume_score(daily_candles, market.volume_24h),
            "volume_spike_short_term":    volume_spike_short_term_score(hourly_candles),
            "oi_change":                  oi_change_score(hourly_candles),
            "price_momentum":             momentum_score(hourly_candles),
            "intraday_hl":                hl_position_score(hourly_candles, midpoint_price),
            "weekly_hl":                  hl_position_score(daily_candles[-7:] if len(daily_candles) >= 7 else daily_candles, midpoint_price),
            "ofi":                        None,
            "orderbook_skew":             None,
        }
        raw_composite_score = self._composite(scores)
        spread_multiplier = spread_penalty_multiplier(market)

        return ScoredMarket(
            market=market,
            composite_score=raw_composite_score * spread_multiplier,
            volume_oi_ratio_score=scores["volume_oi_ratio"],  # type: ignore[arg-type]
            raw_composite_score=raw_composite_score,
            spread_penalty_multiplier=spread_multiplier,
            relative_historical_volume_score=scores["relative_historical_volume"],
            volume_spike_short_term_score=scores["volume_spike_short_term"],
            oi_change_score=scores["oi_change"],
            momentum_score=scores["price_momentum"],
            intraday_hl_score=scores["intraday_hl"],
            weekly_hl_score=scores["weekly_hl"],
            ofi_score=None,
            orderbook_skew_score=None,
            # Signed components for the directional microstructure signal.
            signed_momentum_cents=signed_momentum_cents(hourly_candles),
            range_position=range_position(hourly_candles, midpoint_price),
        )

    def enrich_with_live(
        self,
        scored: list[ScoredMarket],
        trade_data: dict[str, list[dict]],
        orderbook_data: dict[str, dict],
    ) -> list[ScoredMarket]:
        """Add OFI and orderbook skew for markets that have live data, then re-sort."""
        trade_hits = 0
        orderbook_hits = 0
        for scored_market in scored:
            ticker = scored_market.market.ticker
            if ticker in trade_data:
                scored_market.ofi_score = ofi_score(trade_data[ticker])
                scored_market.signed_ofi = signed_ofi(trade_data[ticker])
                trade_hits += 1
            if ticker in orderbook_data:
                scored_market.orderbook_skew_score = orderbook_skew_score(orderbook_data[ticker])
                scored_market.signed_orderbook_skew = signed_orderbook_skew(orderbook_data[ticker])
                # Replace stale snapshot prices with the live top-of-book so the
                # entry price we score/record matches the live market.
                live_yes_bid, live_yes_ask = live_top_of_book(orderbook_data[ticker])
                if live_yes_bid is not None:
                    scored_market.market.yes_bid = live_yes_bid
                if live_yes_ask is not None:
                    scored_market.market.yes_ask = live_yes_ask
                orderbook_hits += 1
            raw_composite_score = self._composite(self._scores_dict(scored_market))
            spread_multiplier = spread_penalty_multiplier(scored_market.market)
            scored_market.raw_composite_score = raw_composite_score
            scored_market.spread_penalty_multiplier = spread_multiplier
            scored_market.composite_score = raw_composite_score * spread_multiplier
        _log.debug(
            "live enrichment: %d/%d trade hits, %d/%d orderbook hits",
            trade_hits, len(scored), orderbook_hits, len(scored),
        )

        scored.sort(key=lambda s: s.composite_score, reverse=True)
        return scored

    @staticmethod
    def _scores_dict(scored_market: ScoredMarket) -> dict[str, float | None]:
        return {
            "volume_oi_ratio":            scored_market.volume_oi_ratio_score,
            "relative_historical_volume": scored_market.relative_historical_volume_score,
            "volume_spike_short_term":    scored_market.volume_spike_short_term_score,
            "oi_change":                  scored_market.oi_change_score,
            "price_momentum":             scored_market.momentum_score,
            "intraday_hl":                scored_market.intraday_hl_score,
            "weekly_hl":                  scored_market.weekly_hl_score,
            "ofi":                        scored_market.ofi_score,
            "orderbook_skew":             scored_market.orderbook_skew_score,
        }

    def _composite(self, scores: dict[str, float | None]) -> float:
        """Weighted average over non-None signals. Re-normalizes when signals are absent.

        Returns 0.0 if fewer than MIN_COVERAGE of total weights are present, so
        markets with no candle history don't crowd out well-covered markets.
        """
        total_weight = 0.0
        weighted_sum = 0.0
        for signal_name, weight in self.WEIGHTS.items():
            signal_value = scores.get(signal_name)
            if signal_value is not None:
                weighted_sum += signal_value * weight
                total_weight += weight
        full_weight = sum(self.WEIGHTS.values())
        if total_weight <= 0 or (total_weight / full_weight) < self.MIN_COVERAGE:
            return 0.0
        return weighted_sum / total_weight
