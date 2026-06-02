"""Converter: raw X/social data + Claude qualitative fields → SignalEstimate."""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from kalshi_trader.models import SignalEstimate


def build_x_signal(
    ticker: str,
    raw_signal: dict[str, Any],
    narrative: str,
    sentiment_direction: str,
    sentiment_reasoning: str,
    strategies_used: list[str],
    post_count: int,
) -> SignalEstimate:
    """Build a SignalEstimate from X/social signal data and Claude analysis.

    Args:
        ticker: Kalshi market ticker.
        raw_signal: Dict with source, probability, uncertainty, weight, and
                    optionally data_issued_at (datetime or ISO string).
        narrative: 1-2 sentence summary of the social signal.
        sentiment_direction: e.g. "bullish", "bearish", "neutral".
        sentiment_reasoning: Explanation of the sentiment call.
        strategies_used: List of X strategy names that contributed.
        post_count: Number of posts analysed.

    Returns:
        SignalEstimate populated from raw_signal + qualitative metadata.
    """
    # Parse data_issued_at
    raw_issued = raw_signal.get("data_issued_at")
    if raw_issued is None:
        data_issued_at = datetime.now(tz=timezone.utc)
    elif isinstance(raw_issued, datetime):
        data_issued_at = raw_issued
        if data_issued_at.tzinfo is None:
            data_issued_at = data_issued_at.replace(tzinfo=timezone.utc)
    else:
        # ISO string
        data_issued_at = datetime.fromisoformat(str(raw_issued))
        if data_issued_at.tzinfo is None:
            data_issued_at = data_issued_at.replace(tzinfo=timezone.utc)

    return SignalEstimate(
        source=raw_signal["source"],
        probability=float(raw_signal["probability"]),
        uncertainty=float(raw_signal["uncertainty"]),
        weight=float(raw_signal["weight"]),
        data_issued_at=data_issued_at,
        metadata={
            "ticker": ticker,
            "narrative": narrative,
            "data_quality": "fresh",
            "post_count": post_count,
            "sentiment_direction": sentiment_direction,
            "sentiment_reasoning": sentiment_reasoning,
            "strategies_used": strategies_used,
        },
    )
