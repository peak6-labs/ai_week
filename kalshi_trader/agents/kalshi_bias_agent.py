from __future__ import annotations
from datetime import datetime, timezone
from typing import Any

from kalshi_trader.models import SignalEstimate


_POLITICAL_KEYWORDS = {
    "election", "vote", "president", "senate", "congress", "governor",
    "mayor", "poll", "approval", "democrat", "republican", "party",
    "candidate", "primary", "ballot", "politics", "political",
}


def _is_political(category: str, title: str) -> bool:
    text = (category + " " + title).lower()
    return any(kw in text for kw in _POLITICAL_KEYWORDS)


def _horizon_factor(hours_to_resolution: float) -> float:
    """Scale down bias correction as market approaches resolution."""
    if hours_to_resolution < 12:
        return 0.30
    elif hours_to_resolution < 48:
        return 0.60
    else:
        return 1.0


def compute_bias_adjustment(
    price_prob: float,
    is_political: bool,
    hours_to_resolution: float,
) -> float:
    """Compute calibration bias adjustment.

    Returns the adjusted probability. If the adjustment is too small to trade
    (< 5 percentage points after horizon scaling), returns price_prob unchanged
    to signal 'no edge'.

    Args:
        price_prob: Market implied probability in [0, 1].
        is_political: Whether this is a political market.
        hours_to_resolution: Hours until market closes.

    Returns:
        Adjusted probability in [0, 1].
    """
    h = _horizon_factor(hours_to_resolution)

    if is_political:
        # Political underconfidence: push away from 0.5
        if price_prob > 0.5:
            raw_adj = 0.065 * h  # push toward YES
        elif price_prob < 0.5:
            raw_adj = -0.065 * h  # push toward NO
        else:
            raw_adj = 0.0
    else:
        # Favorite-longshot bias
        if price_prob < 0.15:
            # Longshot overpriced: true_prob ≈ market × 0.65
            raw_adj = price_prob * (0.65 - 1.0) * h  # negative: push down
        elif price_prob > 0.85:
            # Favorite underpriced: true_prob ≈ 1 - (1-p)*0.65
            raw_adj = (1.0 - price_prob) * (1.0 - 0.65) * h  # positive: push up
        else:
            raw_adj = 0.0

    adjusted = price_prob + raw_adj
    adjusted = max(0.01, min(0.99, adjusted))

    # Only return adjusted value if the move is meaningful (> 1pp)
    if abs(adjusted - price_prob) < 0.01:
        return price_prob  # signal "no edge"

    return adjusted


class KalshiBiasAgent:
    """Applies favorite-longshot and political underconfidence bias corrections."""

    def __init__(self, client: Any) -> None:
        self._client = client

    async def run(
        self,
        ticker: str,
        title: str,
        category: str = "",
        hours_to_resolution: float = 72.0,
    ) -> list[SignalEstimate]:
        # Get current market price
        try:
            market_data = await self._client.get_market(ticker)
        except Exception:
            return []

        market = market_data.get("market", market_data)
        yes_bid = market.get("yes_bid", 0) or 0
        yes_ask = market.get("yes_ask", 0) or 0
        if yes_bid == 0 and yes_ask == 0:
            return []

        midpoint_cents = (yes_bid + yes_ask) / 2.0
        price_prob = midpoint_cents / 100.0
        political = _is_political(category, title)

        # Apply threshold filter before computing adjustment
        if political:
            if 0.45 <= price_prob <= 0.55:
                return []
        else:
            if 0.20 <= price_prob <= 0.80:
                return []

        adjusted_prob = compute_bias_adjustment(price_prob, political, hours_to_resolution)

        # No meaningful edge
        if adjusted_prob == price_prob:
            return []

        edge = abs(adjusted_prob - price_prob)
        # Minimum edge after ~1.2% fee: at 50¢ fee = 0.3¢, at 10¢ fee ≈ 0.1¢
        fee = 0.035 * price_prob * (1 - price_prob)
        if edge <= fee:
            return []

        direction = "longshot_bias" if not political else "political_underconfidence"
        narrative = (
            f"Market price {midpoint_cents:.0f}¢ → bias-adjusted {adjusted_prob*100:.1f}¢ "
            f"({'political underconfidence' if political else 'favorite-longshot bias'}). "
            f"Horizon factor {_horizon_factor(hours_to_resolution):.1f}x."
        )

        return [SignalEstimate(
            source="kalshi_bias",
            probability=round(adjusted_prob, 4),
            uncertainty=0.12,
            weight=0.55,
            data_issued_at=datetime.now(tz=timezone.utc),
            metadata={
                "ticker": ticker,
                "narrative": narrative,
                "data_quality": "fresh",
                "market_price_cents": midpoint_cents,
                "adjusted_prob": round(adjusted_prob, 4),
                "bias_type": direction,
                "is_political": political,
                "hours_to_resolution": hours_to_resolution,
            },
        )]
