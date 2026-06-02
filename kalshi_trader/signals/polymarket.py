"""Converter: raw Polymarket data → SignalEstimate objects."""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from kalshi_trader.models import SignalEstimate


def build_price_signal(
    ticker: str,
    poly_prob: float,
    gap_cents: float,
    match_score: float,
    fetched_at: datetime | None = None,
) -> SignalEstimate:
    """Build a price-gap SignalEstimate from Polymarket.

    Args:
        ticker: Kalshi market ticker.
        poly_prob: Polymarket implied probability (0.0–1.0).
        gap_cents: Signed gap in cents (Polymarket - Kalshi midpoint).
        match_score: Title match quality (0.0–1.0).
        fetched_at: When the data was fetched (defaults to now UTC).

    Returns:
        SignalEstimate with source="polymarket_price".
    """
    if fetched_at is None:
        fetched_at = datetime.now(tz=timezone.utc)

    if gap_cents > 0:
        direction_str = "higher"
    elif gap_cents < 0:
        direction_str = "lower"
    else:
        direction_str = "equal"
    narrative = (
        f"Polymarket prices {ticker} at {poly_prob:.0%} "
        f"({abs(gap_cents):.1f}¢ {direction_str} than Kalshi). "
        f"Market match confidence: {match_score:.0%}."
    )

    return SignalEstimate(
        source="polymarket_price",
        probability=poly_prob,
        uncertainty=0.03,
        weight=0.75,
        data_issued_at=fetched_at,
        metadata={
            "ticker": ticker,
            "narrative": narrative,
            "data_quality": "fresh",
            "gap_cents": round(gap_cents, 2),
            "match_score": round(match_score, 4),
        },
    )


def build_whale_signal(
    ticker: str,
    whale_entries: list[dict[str, Any]],
    fetched_at: datetime | None = None,
) -> SignalEstimate | None:
    """Build a whale-copy SignalEstimate from Polymarket large trades.

    Args:
        ticker: Kalshi market ticker.
        whale_entries: List of dicts with wallet_address, side, entry_price,
                       size_usd, and timestamp (datetime or ISO str).
        fetched_at: Fallback data_issued_at if no entries (unused when entries
                    present — most recent timestamp is used instead).

    Returns:
        SignalEstimate with source="polymarket_whale", or None if empty.
    """
    if not whale_entries:
        return None

    # Filter out malformed entries missing required keys
    valid_entries = []
    for e in whale_entries:
        try:
            _ = e["wallet_address"], e["side"], e["entry_price"], e["size_usd"]
            valid_entries.append(e)
        except KeyError:
            continue

    if not valid_entries:
        return None

    # Parse timestamps and compute size-weighted implied YES probability
    total_size = 0.0
    weighted_prob = 0.0
    timestamps: list[datetime] = []

    for entry in valid_entries:
        size = float(entry["size_usd"])
        side = entry["side"].upper()
        entry_price = float(entry["entry_price"])
        implied_yes = entry_price if side == "YES" else 1.0 - entry_price

        total_size += size
        weighted_prob += implied_yes * size

        # Parse timestamp
        ts = entry.get("timestamp")
        if isinstance(ts, str):
            parsed = datetime.fromisoformat(ts)
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            timestamps.append(parsed)
        elif isinstance(ts, datetime):
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=timezone.utc)
            timestamps.append(ts)

    if total_size == 0:
        return None

    probability = weighted_prob / total_size

    # Distinct wallets determine uncertainty
    whale_count = len({e["wallet_address"] for e in valid_entries})
    uncertainty = 0.15 if whale_count == 1 else 0.10

    # data_issued_at = most recent timestamp among entries
    data_issued_at = max(timestamps) if timestamps else (
        fetched_at if fetched_at is not None else datetime.now(tz=timezone.utc)
    )

    narrative = (
        f"{whale_count} whale(s) entered {ticker} on Polymarket with a "
        f"size-weighted implied YES probability of {probability:.0%}."
    )

    return SignalEstimate(
        source="polymarket_whale",
        probability=probability,
        uncertainty=uncertainty,
        weight=0.60,
        data_issued_at=data_issued_at,
        metadata={
            "ticker": ticker,
            "narrative": narrative,
            "data_quality": "fresh",
            "whale_count": whale_count,
        },
    )
