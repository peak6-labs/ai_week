"""Tests for the Love Island signal builder + catchphrase lexicon."""
from __future__ import annotations

import pytest

from kalshi_trader.signals.love_island import (
    EVIDENCE_PROFILES,
    build_love_island_signal,
    mentions_transcript_gate,
)
from kalshi_trader.signals.love_island_lexicon import lookup_catchphrase_prior


def test_teaser_confirmed_sets_high_weight_low_uncertainty():
    estimate = build_love_island_signal(
        ticker="KXLIUSABOMBSHELL-26JUN05",
        probability=0.82,
        evidence_strength="teaser_confirmed",
        market_bucket="binary_event",
        narrative="Teaser shows two bombshells arriving tonight.",
        sources=["youtube:abc"],
    )
    assert estimate.source == "love_island_teaser"
    assert estimate.weight == 0.90
    assert estimate.uncertainty == 0.05
    assert estimate.probability == 0.82
    assert estimate.metadata["narrative"]
    assert estimate.metadata["market_bucket"] == "binary_event"
    assert estimate.metadata["evidence_strength"] == "teaser_confirmed"
    assert estimate.metadata["sources"] == ["youtube:abc"]
    assert estimate.metadata["independent"] is True


def test_sentiment_only_is_low_weight():
    estimate = build_love_island_signal(
        ticker="KXLIUSAWINNERS-26-MEL",
        probability=0.3,
        evidence_strength="sentiment_only",
        market_bucket="winner",
        narrative="Diffuse fan sentiment.",
    )
    assert estimate.source == "love_island_sentiment"
    assert estimate.weight == 0.40
    assert estimate.weight < EVIDENCE_PROFILES["teaser_confirmed"][1]


@pytest.mark.parametrize("raw_probability,expected", [
    (1.5, 0.99),
    (-0.2, 0.01),
    (0.0, 0.01),
    (1.0, 0.99),
    (0.5, 0.5),
])
def test_probability_is_clamped(raw_probability, expected):
    estimate = build_love_island_signal(
        ticker="T", probability=raw_probability, evidence_strength="teaser_hinted",
        market_bucket="elimination", narrative="x",
    )
    assert estimate.probability == expected


def test_unknown_evidence_strength_raises():
    with pytest.raises(ValueError):
        build_love_island_signal(
            ticker="T", probability=0.5, evidence_strength="vibes",
            market_bucket="winner", narrative="x",
        )


def test_non_numeric_probability_raises():
    with pytest.raises(ValueError):
        build_love_island_signal(
            ticker="T", probability="high", evidence_strength="teaser_confirmed",
            market_bucket="binary_event", narrative="x",
        )


# ---------------------------------------------------------------------------
# catchphrase lexicon
# ---------------------------------------------------------------------------

def test_lexicon_matches_franchise_staple():
    result = lookup_catchphrase_prior("Bombshell")
    assert result["matched"] is True
    assert result["canonical"] == "bombshell"
    assert 0.0 < result["base_rate"] <= 1.0


def test_lexicon_matches_slash_separated_variants():
    # The contract subtitle lists variants; any alias should match.
    result = lookup_catchphrase_prior("Mog / Mogged / Mogging")
    assert result["matched"] is True
    assert result["canonical"] == "mugged off"


def test_lexicon_unknown_phrase_returns_no_prior():
    result = lookup_catchphrase_prior("quantum chromodynamics")
    assert result["matched"] is False
    assert result["base_rate"] is None


# ---------------------------------------------------------------------------
# mentions transcript gate
# ---------------------------------------------------------------------------

def test_mentions_gate_blocks_when_no_transcript():
    message = mentions_transcript_gate("mentions", 0)
    assert message is not None
    assert "transcript" in message.lower()


def test_mentions_gate_passes_once_transcript_seen():
    assert mentions_transcript_gate("mentions", 1) is None


@pytest.mark.parametrize("bucket", ["binary_event", "elimination", "winner"])
def test_gate_ignores_non_mentions_buckets(bucket):
    # Other buckets never require a transcript, even with none fetched.
    assert mentions_transcript_gate(bucket, 0) is None
