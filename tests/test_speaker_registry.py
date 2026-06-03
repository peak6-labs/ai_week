"""Tests for kalshi_trader/external/speaker_registry.py"""
from __future__ import annotations

from kalshi_trader.external.speaker_registry import (
    GDELT_CSPAN,
    GDELT_NATIONAL_NEWS,
    SPEAKER_REGISTRY,
    SpeakerProfile,
    normalize_speaker_key,
    resolve_speaker,
)


# ---------------------------------------------------------------------------
# Alias / normalization resolution
# ---------------------------------------------------------------------------

def test_resolve_powell_aliases_all_collapse_to_one_key():
    for alias in ("Powell", "Chair Powell", "Mr. Powell", "Jerome Powell", "Chairman Powell"):
        profile = resolve_speaker(alias)
        assert profile.speaker_key == "powell", alias
        assert profile.is_known is True


def test_resolve_trump_aliases():
    for alias in ("Trump", "Donald Trump", "President Trump", "Donald J. Trump"):
        assert resolve_speaker(alias).speaker_key == "trump", alias


def test_resolve_explicit_alias_potus_maps_to_trump():
    assert resolve_speaker("POTUS").speaker_key == "trump"


# ---------------------------------------------------------------------------
# Station routing (the core "wrong station" fix)
# ---------------------------------------------------------------------------

def test_trump_routes_to_national_news_not_cspan():
    profile = resolve_speaker("Donald Trump")
    assert profile.gdelt_stations == GDELT_NATIONAL_NEWS
    assert "CSPAN" not in profile.gdelt_stations


def test_powell_routes_to_cspan():
    profile = resolve_speaker("Jerome Powell")
    assert profile.gdelt_stations == GDELT_CSPAN
    assert profile.role == "fed"


def test_powell_has_attributed_transcript_venues_and_handle():
    profile = resolve_speaker("Powell")
    assert "fed_speech" in profile.transcript_venues
    assert "fed_presser" in profile.transcript_venues
    assert profile.x_handles == ["federalreserve"]


# ---------------------------------------------------------------------------
# Unknown speaker → generic fallback
# ---------------------------------------------------------------------------

def test_unknown_speaker_returns_generic_fallback():
    profile = resolve_speaker("Jane Q. Public")
    assert profile.is_known is False
    assert profile.role == "unknown"
    assert profile.transcript_venues == []
    assert profile.x_handles == []
    # Stable attribution key even for an unregistered speaker.
    assert profile.speaker_key == "jane_q_public"
    # Falls back to broad national coverage.
    assert profile.gdelt_stations == GDELT_NATIONAL_NEWS


def test_none_and_blank_speaker_return_generic_with_empty_key():
    for value in (None, "", "   "):
        profile = resolve_speaker(value)
        assert profile.is_known is False
        assert profile.speaker_key == ""


# ---------------------------------------------------------------------------
# normalize_speaker_key
# ---------------------------------------------------------------------------

def test_normalize_speaker_key_strips_titles_and_punctuation():
    assert normalize_speaker_key("Governor Christopher J. Waller") == "christopher_j_waller"
    assert normalize_speaker_key("Chair Powell") == "powell"
    assert normalize_speaker_key("Sen. Markwayne Mullin") == "markwayne_mullin"


# ---------------------------------------------------------------------------
# Registry integrity
# ---------------------------------------------------------------------------

def test_every_registry_entry_has_required_fields():
    for speaker_key, entry in SPEAKER_REGISTRY.items():
        assert entry.get("role"), speaker_key
        assert entry.get("gdelt_stations"), speaker_key
        # Resolving the bare key returns a fully-formed, known profile.
        profile = resolve_speaker(speaker_key)
        assert isinstance(profile, SpeakerProfile)
        assert profile.is_known is True
        assert profile.speaker_key == speaker_key


def test_resolved_profile_lists_are_copies_not_shared_with_registry():
    profile = resolve_speaker("trump")
    profile.x_handles.append("intruder")
    assert "intruder" not in SPEAKER_REGISTRY["trump"]["x_handles"]
