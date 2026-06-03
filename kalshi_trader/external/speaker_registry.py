"""Speaker → source-routing registry for the mentions pipeline.

A Kalshi "mentions" market asks whether ``<person>`` will say ``<phrase>`` in some
venue. Answering it well means counting how often *that specific person* says the
phrase, on the *right* channels: Powell is on CSPAN, the President is on every
national network, and a written Truth Social post is not spoken at all. This
registry maps a normalized ``speaker_key`` to the sources that actually cover that
speaker — which GDELT TV stations carry them, which speaker-attributed transcript
corpora to count, which X handles to watch as a leading indicator, and (optionally)
a YouTube channel for non-government voices.

Modeled on :mod:`kalshi_trader.external.weather_authorities`: an editable dict plus
a resolver that normalizes aliases ("Chair Powell" / "Mr. Powell" / "Powell" →
``powell``) and falls back to a generic, lower-confidence profile for unregistered
speakers. The ``speaker_key`` this module produces is the *same* key the archive
stores transcripts under, so a market's parsed speaker and the corpus line up — get
this wrong and the speaker-attribution bug the pipeline exists to fix returns.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

# ---------------------------------------------------------------------------
# Venue-type tags (the transcripts table's ``venue_type`` column). A speaker's
# ``transcript_venues`` lists the corpora whose attributed text we count for them.
# ---------------------------------------------------------------------------
VENUE_FED_SPEECH = "fed_speech"
VENUE_FED_PRESSER = "fed_presser"
VENUE_CONGRESS_FLOOR = "congress_floor"
VENUE_WH_BRIEFING = "wh_briefing"

# ---------------------------------------------------------------------------
# GDELT TV station sets. CSPAN carries gavel-to-gavel congressional and Fed
# proceedings; the national-news set is the right corroborator for executive
# voices who appear across every network rather than only on CSPAN.
# ---------------------------------------------------------------------------
GDELT_CSPAN: list[str] = ["CSPAN"]
GDELT_NATIONAL_NEWS: list[str] = ["CNN", "FOXNEWS", "MSNBC"]


@dataclass(frozen=True)
class SpeakerProfile:
    """Resolved routing for one speaker.

    ``is_known`` is False for the generic fallback returned when a speaker is not
    registered; callers should drop such a market to a lower-confidence tier and
    rely on broad GDELT coverage rather than speaker-attributed corpora.
    """

    speaker_key: str
    role: str
    gdelt_stations: list[str] = field(default_factory=list)
    transcript_venues: list[str] = field(default_factory=list)
    x_handles: list[str] = field(default_factory=list)
    youtube_channel: str | None = None
    is_known: bool = True


# ---------------------------------------------------------------------------
# The editable registry. Keys are normalized surnames (the key
# ``normalize_speaker_key`` produces for the bare surname). Seed with the
# high-frequency figures; add more by appending a row.
#
# X handle sets are tagged primary-vs-orbit in the comments: Trump's
# ``trumpdailyposts`` mirrors Truth Social and is often the best proxy for what he
# is about to say out loud.
# ---------------------------------------------------------------------------
SPEAKER_REGISTRY: dict[str, dict] = {
    "trump": {
        "role": "executive",
        "gdelt_stations": GDELT_NATIONAL_NEWS,
        "transcript_venues": [VENUE_WH_BRIEFING],
        # primary: realdonaldtrump, potus, whitehouse (the official @WhiteHouse
        # account is highly active and posts the administration's actual remarks —
        # the best X proxy now that he posts on Truth Social, not X); orbit:
        # trumpdailyposts (Truth Social mirror), donaldjtrumpjr, jdvance.
        "x_handles": ["realdonaldtrump", "potus", "whitehouse", "trumpdailyposts", "donaldjtrumpjr", "jdvance"],
        "youtube_channel": None,
    },
    "biden": {
        "role": "executive",
        "gdelt_stations": GDELT_NATIONAL_NEWS,
        "transcript_venues": [VENUE_WH_BRIEFING],
        "x_handles": ["joebiden", "potus46archive"],
        "youtube_channel": None,
    },
    "vance": {
        "role": "executive",
        "gdelt_stations": GDELT_NATIONAL_NEWS,
        "transcript_venues": [VENUE_WH_BRIEFING],
        "x_handles": ["jdvance", "vp", "whitehouse"],
        "youtube_channel": None,
    },
    "powell": {
        "role": "fed",
        "gdelt_stations": GDELT_CSPAN,
        "transcript_venues": [VENUE_FED_SPEECH, VENUE_FED_PRESSER],
        "x_handles": ["federalreserve"],
        "youtube_channel": None,
    },
    "yellen": {
        "role": "executive",
        "gdelt_stations": GDELT_CSPAN,
        "transcript_venues": [VENUE_WH_BRIEFING],
        "x_handles": ["ustreasury"],
        "youtube_channel": None,
    },
}

# Full-name aliases → registry key, for the cases the surname-fallback below would
# get wrong or ambiguously. Kept small on purpose: most names resolve via the
# title-strip + surname fallback, since registry keys are surnames.
_ALIASES: dict[str, str] = {
    "potus": "trump",
    "the president": "trump",
}

# Honorifics / titles stripped before matching, so "Chair Powell" and "Mr. Powell"
# both reduce to "powell".
_TITLES: frozenset[str] = frozenset({
    "mr", "mrs", "ms", "miss", "dr", "sir",
    "sen", "senator", "rep", "representative", "congressman", "congresswoman",
    "chair", "chairman", "chairwoman", "chairperson",
    "president", "vice", "vp", "secretary", "treasury", "press",
    "gov", "governor", "the", "hon", "honorable", "justice", "judge",
    "director", "administrator", "ambassador", "general",
})


def _name_tokens(name: str) -> list[str]:
    """Lowercase a name, drop punctuation and honorifics, return the word tokens."""
    cleaned = re.sub(r"[^a-z\s'-]", " ", name.lower())
    return [token for token in cleaned.split() if token and token not in _TITLES]


def normalize_speaker_key(name: str | None) -> str:
    """Stable ``speaker_key`` for any name (registered or not).

    Underscore-joined, honorific-stripped tokens — e.g.
    ``"Governor Christopher J. Waller"`` → ``"christopher_j_waller"``. Used as the
    archive's attribution key so an unregistered speaker still counts consistently.
    """
    return "_".join(_name_tokens(name or ""))


def _generic_profile(speaker_key: str) -> SpeakerProfile:
    """Lower-confidence fallback for an unregistered speaker.

    Routes to broad national-TV coverage (no speaker-attributed corpus available)
    and carries no X handle set. ``is_known`` is False so callers can down-weight.
    """
    return SpeakerProfile(
        speaker_key=speaker_key,
        role="unknown",
        gdelt_stations=list(GDELT_NATIONAL_NEWS),
        transcript_venues=[],
        x_handles=[],
        youtube_channel=None,
        is_known=False,
    )


def _profile_from_entry(speaker_key: str, entry: dict) -> SpeakerProfile:
    return SpeakerProfile(
        speaker_key=speaker_key,
        role=entry["role"],
        gdelt_stations=list(entry.get("gdelt_stations", [])),
        transcript_venues=list(entry.get("transcript_venues", [])),
        x_handles=list(entry.get("x_handles", [])),
        youtube_channel=entry.get("youtube_channel"),
        is_known=True,
    )


def resolve_speaker(parsed_speaker: str | None) -> SpeakerProfile:
    """Resolve a parsed speaker name to its routing profile.

    Resolution order: full-name alias → exact normalized key → surname fallback →
    generic fallback (``is_known=False``). Normalizes honorifics and punctuation so
    "Chair Powell", "Mr. Powell" and "Jerome Powell" all resolve to ``powell``.
    """
    if not parsed_speaker or not parsed_speaker.strip():
        return _generic_profile("")

    tokens = _name_tokens(parsed_speaker)
    normalized_name = " ".join(tokens)

    resolved_key: str | None = _ALIASES.get(normalized_name)
    if resolved_key is None:
        if normalized_name in SPEAKER_REGISTRY:
            resolved_key = normalized_name
        elif tokens and tokens[-1] in SPEAKER_REGISTRY:
            resolved_key = tokens[-1]

    if resolved_key and resolved_key in SPEAKER_REGISTRY:
        return _profile_from_entry(resolved_key, SPEAKER_REGISTRY[resolved_key])

    return _generic_profile(normalize_speaker_key(parsed_speaker))
