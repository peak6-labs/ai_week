"""Converter: GDELT TV base-rate summary → SignalEstimate.

For a "Will <person> say <word> in <hearing/briefing>" market, the historical
fraction of comparable broadcast periods in which the phrase appeared is an
unconditional base rate for whether it gets said at all. We use that fraction as
the probability and treat the breadth of historical coverage as the data-quality
signal.
"""
from __future__ import annotations

from datetime import datetime, timezone

from kalshi_trader.models import SignalEstimate

# Hardcoded constants — config_manager.py is a shared file we must not modify, so
# these are not wired into runtime_config.json. Tune via the paper-trade loop.
WEIGHT_GDELT_MENTIONS = 0.55
UNCERTAINTY_GDELT_MENTIONS = 0.18


def build_mentions_signal(
    ticker: str,
    phrase: str,
    station: str,
    base_rate: dict,
    speaker: str | None = None,
) -> SignalEstimate:
    """Build a SignalEstimate from a GDELT mention base-rate summary.

    Args:
        ticker: Kalshi market ticker.
        phrase: Word/phrase the market tracks.
        station: TV station queried (e.g. "CSPAN").
        base_rate: Dict from mentions_parser.base_rate_from_points with keys
            period_count, periods_with_mention, fraction_with_mention,
            mean_match_percent, max_match_percent.
        speaker: Optional named speaker for the narrative.

    Returns:
        SignalEstimate with source="gdelt_mentions".
    """
    fraction_with_mention = float(base_rate.get("fraction_with_mention", 0.0) or 0.0)
    period_count = int(base_rate.get("period_count", 0) or 0)

    probability = min(max(fraction_with_mention, 0.01), 0.99)

    # Data quality scales with how much historical coverage backs the base rate.
    if period_count >= 24:
        data_quality = "fresh"
    elif period_count >= 6:
        data_quality = "stale"
    else:
        data_quality = "unavailable"

    speaker_clause = f"{speaker} saying " if speaker else ""
    narrative = (
        f"GDELT TV ({station}) base rate: \"{phrase}\" appeared in "
        f"{base_rate.get('periods_with_mention', 0)}/{period_count} historical periods "
        f"({fraction_with_mention:.1%}). P({speaker_clause}\"{phrase}\") = {probability:.2%}. "
        f"Data is {data_quality}."
    )

    metadata: dict = {
        "ticker": ticker,
        "narrative": narrative,
        "data_quality": data_quality,
        "source_model": "gdelt_tv",
        "phrase": phrase,
        "station": station,
        "period_count": period_count,
        "mean_match_percent": base_rate.get("mean_match_percent", 0.0),
        "max_match_percent": base_rate.get("max_match_percent", 0.0),
    }
    if speaker:
        metadata["speaker"] = speaker

    return SignalEstimate(
        source="gdelt_mentions",
        probability=probability,
        uncertainty=UNCERTAINTY_GDELT_MENTIONS,
        weight=WEIGHT_GDELT_MENTIONS,
        data_issued_at=datetime.now(tz=timezone.utc),
        metadata=metadata,
    )
