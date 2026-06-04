"""Converter: FiveThirtyEight recent-margin summary → SignalEstimate.

Maps a polling margin to a win probability with a normal model centered on the
margin and a forecast-error std — the same scipy.stats.norm-around-a-margin
pattern used in signals/weather.py for temperature markets. The probability of
winning is P(true_margin > 0) = norm(margin, std).sf(0).
"""
from __future__ import annotations

from datetime import datetime, timezone

import scipy.stats

from kalshi_trader.models import SignalEstimate

# Hardcoded constants — config_manager.py is a shared file we must not modify, so
# these are not wired into runtime_config.json. Tune via the paper-trade loop.
WEIGHT_FIVETHIRTYEIGHT = 0.25
UNCERTAINTY_FIVETHIRTYEIGHT = 0.10
# Forecast-error std on the polling margin, in percentage points. ~5–6 pts is a
# common standing-poll-to-outcome error; 6.0 is deliberately conservative.
MARGIN_FORECAST_ERROR_STD = 6.0


def build_polls_signal(
    ticker: str,
    margin_summary: dict,
    poll_type: str,
    state: str | None = None,
) -> SignalEstimate:
    """Build a SignalEstimate from a 538 recent-margin summary.

    Args:
        ticker: Kalshi market ticker.
        margin_summary: Dict from polls_parser.recent_margin with keys
            candidate, candidate_pct, opponent, opponent_pct, margin,
            poll_count.
        poll_type: 538 poll-file type (president / senate / governor / ...).
        state: Optional state name for the narrative.

    Returns:
        SignalEstimate with source="fivethirtyeight".
    """
    margin = float(margin_summary["margin"])
    poll_count = int(margin_summary.get("poll_count", 0) or 0)
    candidate = margin_summary.get("candidate", "the leader")
    opponent = margin_summary.get("opponent", "the field")

    # Win probability = P(true margin > 0) under a normal centered on the
    # observed margin (identical mechanics to weather.py's temp model).
    distribution = scipy.stats.norm(margin, MARGIN_FORECAST_ERROR_STD)
    raw_probability = float(distribution.sf(0.0))
    probability = min(max(raw_probability, 0.01), 0.99)

    # Data quality scales with how many polls back the average.
    if poll_count >= 5:
        data_quality = "fresh"
    elif poll_count >= 2:
        data_quality = "stale"
    else:
        data_quality = "unavailable"

    location = f" in {state.title()}" if state else ""
    narrative = (
        f"538 polling average{location} ({poll_type}): {candidate} "
        f"{margin_summary.get('candidate_pct', 0):.1f}% vs {opponent} "
        f"{margin_summary.get('opponent_pct', 0):.1f}% (margin {margin:+.1f} pts, "
        f"{poll_count} polls). P({candidate} wins) = {probability:.2%}. "
        f"Data is {data_quality}."
    )

    metadata: dict = {
        "ticker": ticker,
        "narrative": narrative,
        "data_quality": data_quality,
        "source_model": "fivethirtyeight_polls",
        "poll_type": poll_type,
        "candidate": candidate,
        "opponent": opponent,
        "margin": round(margin, 2),
        "poll_count": poll_count,
    }
    if state:
        metadata["state"] = state

    return SignalEstimate(
        source="fivethirtyeight",
        probability=probability,
        uncertainty=UNCERTAINTY_FIVETHIRTYEIGHT,
        weight=WEIGHT_FIVETHIRTYEIGHT,
        data_issued_at=datetime.now(tz=timezone.utc),
        metadata=metadata,
    )
