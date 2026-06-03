"""Parsers for GDELT "mentions" markets and TV-API timelines.

A Kalshi mention market asks "Will <speaker> say <word/phrase> in <venue>"
(a hearing, briefing, floor speech, press conference). We parse the title into
the phrase to search and the TV station whose closed-caption archive covers the
venue, then turn the historical per-period match-percent timeline into a base
rate.
"""
from __future__ import annotations

import re

# Venue keyword → TV station whose Internet-Archive caption stream covers it.
# CSPAN carries House/Senate hearings and floor proceedings; the White House
# press briefings and presidential pressers are also carried on CSPAN.
_VENUE_STATIONS: list[tuple[str, str]] = [
    ("hearing", "CSPAN"),
    ("testimony", "CSPAN"),
    ("testify", "CSPAN"),
    ("committee", "CSPAN"),
    ("congress", "CSPAN"),
    ("senate", "CSPAN"),
    ("house floor", "CSPAN"),
    ("floor", "CSPAN"),
    ("briefing", "CSPAN"),
    ("press conference", "CSPAN"),
    ("presser", "CSPAN"),
    ("white house", "CSPAN"),
    ("debate", "CSPAN"),
]

# Phrases that introduce the word being tracked, longest first so the more
# specific pattern wins. The captured group is the quoted/target phrase.
_SAY_PATTERNS = [
    r'say\s+the\s+(?:word|phrase|words)\s+["“]([^"”]+)["”]',
    r'say\s+["“]([^"”]+)["”]',
    r'mention\s+["“]([^"”]+)["”]',
    r'utter\s+["“]([^"”]+)["”]',
    r'say\s+the\s+(?:word|phrase|words)\s+([a-z][a-z\s-]+?)(?:\s+(?:in|during|at|on)\b|\?|$)',
    r'\bsay\s+([a-z][a-z-]+)\b',
    r'\bmention\s+([a-z][a-z-]+)\b',
]


def parse_mention_title(ticker: str, title: str) -> dict | None:
    """Parse a Kalshi mention-market title → structured question.

    Returns a dict with:
        phrase:  the word/phrase to search the caption stream for
        station: the TV station code to query (CSPAN by default)
        speaker: the named speaker if one can be extracted, else None
    or None if the title is not a recognizable "will X say Y" mention market.
    """
    lowered = title.lower()

    # Must look like a mention market at all.
    if not any(keyword in lowered for keyword in ("say", "mention", "utter")):
        return None

    # Phrase being tracked — try quoted forms first, then bare verbs.
    phrase: str | None = None
    for pattern in _SAY_PATTERNS:
        match = re.search(pattern, lowered)
        if match:
            phrase = match.group(1).strip().strip('.,?"“”')
            break
    if not phrase:
        return None
    # GDELT caps phrases at 5 words; keep it tractable.
    words = phrase.split()
    if not words or len(words) > 5:
        return None

    # Venue → station.
    station = "CSPAN"
    for keyword, mapped_station in _VENUE_STATIONS:
        if keyword in lowered:
            station = mapped_station
            break

    # Speaker — the proper-noun run before the say/mention verb, if any.
    speaker: str | None = None
    speaker_match = re.search(
        r"(?:[Ww]ill|[Ww]ould|[Dd]oes|[Dd]id)\s+"
        r"([A-Z][a-zA-Z.'-]+(?:\s+[A-Z][a-zA-Z.'-]+){0,3})\s+"
        r"(?:say|mention|utter)",
        title,
    )
    if speaker_match:
        speaker = speaker_match.group(1).strip()

    return {
        "phrase": phrase,
        "station": station,
        "speaker": speaker,
    }


def base_rate_from_points(points: list[dict]) -> dict:
    """Reduce a GDELT match-percent timeline to a base-rate summary.

    The TV API's ``value`` is the percent of 15-second clips in a period that
    matched the phrase. We treat the fraction of periods in which the phrase
    appeared at all (value > 0) as the unconditional probability the phrase gets
    said in a comparable broadcast period, and also keep the mean match percent
    as a continuous intensity feature.

    Returns {"period_count", "periods_with_mention", "fraction_with_mention",
             "mean_match_percent", "max_match_percent"}.
    """
    period_count = len(points)
    if period_count == 0:
        return {
            "period_count": 0,
            "periods_with_mention": 0,
            "fraction_with_mention": 0.0,
            "mean_match_percent": 0.0,
            "max_match_percent": 0.0,
        }
    values = [float(point.get("value", 0.0) or 0.0) for point in points]
    periods_with_mention = sum(1 for value in values if value > 0.0)
    return {
        "period_count": period_count,
        "periods_with_mention": periods_with_mention,
        "fraction_with_mention": periods_with_mention / period_count,
        "mean_match_percent": sum(values) / period_count,
        "max_match_percent": max(values),
    }
