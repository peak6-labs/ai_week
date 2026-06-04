"""Converter: Love Island evidence + Claude qualitative read → SignalEstimate.

The agent supplies a *probability* and a categorical *evidence_strength*; this
builder maps that category to a deterministic ``weight``/``uncertainty`` and a
lineage-bearing ``source`` so the LLM cannot inflate its own confidence. A teaser
that explicitly shows tonight's bombshell is near-certain; diffuse fan sentiment
on a winner market is weak and directional only.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from kalshi_trader.models import SignalEstimate

# evidence_strength → (source, weight, uncertainty). Weight/uncertainty are fixed
# per evidence tier so the agent's qualitative call drives only the probability,
# never how heavily that probability is trusted downstream.
EVIDENCE_PROFILES: dict[str, tuple[str, float, float]] = {
    # The pre-episode teaser explicitly shows the outcome (e.g. "Bombshells arrive
    # tonight"). Highest trust.
    "teaser_confirmed": ("love_island_teaser", 0.90, 0.05),
    # The teaser strongly implies but does not state the outcome.
    "teaser_hinted": ("love_island_teaser", 0.60, 0.15),
    # No teaser evidence; only X fan sentiment (public-vote markets). Directional.
    "sentiment_only": ("love_island_sentiment", 0.40, 0.25),
    # Mentions markets resting on the curated franchise-catchphrase base rate only.
    "prior_only": ("love_island_prior", 0.35, 0.30),
}

VALID_BUCKETS = frozenset({"binary_event", "elimination", "winner", "mentions"})


def mentions_transcript_gate(market_bucket: str, nonempty_transcript_count: int) -> str | None:
    """Require transcript evidence before a mentions signal may be built.

    A "what will the cast say" market is about words actually spoken on screen, so a
    read resting only on the static catchphrase prior is not tradeable — the same
    posture as ``MENTIONS_REQUIRE_CORPUS_BACKED`` for the GDELT mentions signal.

    Returns an instruction string to hand back to the agent when a ``mentions``
    signal is attempted with no transcript text fetched yet (so it fetches one or
    returns ``[]``), or ``None`` when the gate is satisfied / not a mentions market.
    """
    if market_bucket == "mentions" and nonempty_transcript_count <= 0:
        return (
            "Mentions markets require transcript evidence. Call "
            "fetch_youtube_transcript on the episode's First Look / clip videos "
            "first, then base the estimate on whether the phrase is spoken. If no "
            "transcript text is obtainable for this episode, return [] — do not "
            "estimate from the catchphrase prior alone."
        )
    return None


def build_love_island_signal(
    ticker: str,
    probability: float,
    evidence_strength: str,
    market_bucket: str,
    narrative: str,
    sources: list[str] | None = None,
    data_issued_at: datetime | None = None,
) -> SignalEstimate:
    """Build a SignalEstimate from a Love Island evidence read.

    Args:
        ticker: Kalshi market ticker.
        probability: The agent's YES probability estimate (clamped to [0.01, 0.99]).
        evidence_strength: One of ``EVIDENCE_PROFILES`` — sets weight/uncertainty.
        market_bucket: One of ``VALID_BUCKETS`` (recorded in metadata).
        narrative: 1-3 sentence summary citing what drove the estimate.
        sources: Evidence pointers (e.g. video ids, "x_search").
        data_issued_at: When the evidence was valid; defaults to now (UTC).

    Returns:
        A populated SignalEstimate.

    Raises:
        ValueError: on an unknown evidence_strength or a non-numeric probability.
    """
    if evidence_strength not in EVIDENCE_PROFILES:
        raise ValueError(
            f"unknown evidence_strength {evidence_strength!r} for ticker {ticker}; "
            f"expected one of {sorted(EVIDENCE_PROFILES)}"
        )
    try:
        probability_value = float(probability)
    except (TypeError, ValueError) as caught_exception:
        raise ValueError(
            f"non-numeric probability for ticker {ticker}: {probability!r}"
        ) from caught_exception

    # Clamp away from 0/1 — a prediction-market signal never claims certainty.
    probability_value = min(max(probability_value, 0.01), 0.99)

    source, weight, uncertainty = EVIDENCE_PROFILES[evidence_strength]
    issued_at = data_issued_at or datetime.now(tz=timezone.utc)
    if issued_at.tzinfo is None:
        issued_at = issued_at.replace(tzinfo=timezone.utc)

    metadata: dict[str, Any] = {
        "ticker": ticker,
        "narrative": narrative,
        "data_quality": "fresh",
        "source_model": "love_island_signal",
        "market_bucket": market_bucket,
        "evidence_strength": evidence_strength,
        "sources": sources or [],
        # YouTube teaser evidence is independent of the price-derived scout
        # signals; X sentiment shares no lineage with them either.
        "independent": True,
    }
    return SignalEstimate(
        source=source,
        probability=probability_value,
        uncertainty=uncertainty,
        weight=weight,
        data_issued_at=issued_at,
        metadata=metadata,
    )
