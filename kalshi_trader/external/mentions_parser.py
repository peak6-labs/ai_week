"""Parsers for GDELT "mentions" markets and TV-API timelines.

A Kalshi mention market asks "Will <speaker> say <word/phrase> in <venue>"
(a hearing, briefing, floor speech, press conference). We parse the title into
the phrase to search and the TV station whose closed-caption archive covers the
venue, then turn the historical per-period match-percent timeline into a base
rate.
"""
from __future__ import annotations

import re
from datetime import datetime, timezone

_SMART_CHAR_FOLD = str.maketrans({
    "‘": "'", "’": "'", "“": '"', "”": '"',
    "–": "-", "—": "-", "…": " ",
})


def normalize_for_match(text: str | None) -> str:
    """Fold a string to a canonical form for substring phrase matching.

    Folds smart quotes/dashes to ASCII, lowercases, replaces every run of
    non-alphanumeric characters with a single space, and trims. The result is the
    ``norm_text`` stored alongside each transcript and the form a search phrase is
    reduced to before ``phrase in norm_text`` counting — so punctuation and casing
    never cause a missed match. Used by ``kalshi_trader.mentions.store``.
    """
    if not text:
        return ""
    folded = text.translate(_SMART_CHAR_FOLD).lower()
    return re.sub(r"[^a-z0-9]+", " ", folded).strip()


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
    r'say\s+the\s+(?:word|phrase|words)\s+[""]([^""]+)[""]',
    r'say\s+[""]([^""]+)[""]',
    r'mention\s+[""]([^""]+)[""]',
    r'utter\s+[""]([^""]+)[""]',
    r'say\s+the\s+(?:word|phrase|words)\s+([a-z][a-z\s-]+?)(?:\s+(?:in|during|at|on)\b|\?|$)',
    r'\bsay\s+([a-z][a-z-]+)\b',
    r'\bmention\s+([a-z][a-z-]+)\b',
]

# Prepositions/articles/fillers that can appear directly after "say"/"mention"
# in generic titles like "what will they say during X" — never the target phrase.
_STOPWORD_PHRASES = frozenset([
    "a", "an", "the", "in", "on", "at", "to", "of", "for", "by", "with",
    "during", "about", "after", "before", "between", "through", "into",
    "something", "anything", "this", "that", "what", "which",
    "any", "some", "their", "its", "his", "her", "our", "your", "my",
])


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
            phrase = match.group(1).strip().strip('.,?"""')
            break
    if not phrase:
        return None
    # Reject stopwords extracted by the loose last-resort patterns.
    if phrase.strip().lower() in _STOPWORD_PHRASES:
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


_SPORTS_SOURCE_KEYWORDS = ("nba.com", "mlb.com", "nfl.com", "nhl.com", "ufc.com", "espn.com")

# Regex to pull the phrase from rules_primary: 'says <phrase> as part of'
_RULES_PHRASE_PATTERN = re.compile(
    r'says\s+([^\n]+?)\s+as\s+part\s+of', re.IGNORECASE
)


def extract_phrase_from_settlement(settlement: dict) -> dict | None:
    """Extract the trackable phrase from settlement context when the title lacks it.

    Returns {"phrase": str, "station": str, "speaker": str|None,
             "is_sports_venue": bool} or None if no phrase can be found.

    Priority:
    1. ``yes_sub_title`` — the clearest source (e.g. "Alley-oop", "China / Chinese")
    2. ``rules_primary`` — parse 'says <phrase> as part of'

    Station: congressional/hearing venues → CSPAN; sports venues → "CNN" (broadest
    GDELT news coverage as a proxy — explicitly noted in signal narrative).
    """
    if not settlement:
        return None

    phrase: str | None = None

    # 1. yes_sub_title is the canonical keyword field on Kalshi mentions contracts.
    raw_sub_title = (settlement.get("yes_sub_title") or "").strip()
    # Skip placeholder values that just echo back the event-does-not-qualify stub.
    if raw_sub_title and "does not qualify" not in raw_sub_title.lower():
        phrase = raw_sub_title.split("/")[0].strip()  # "China / Chinese" → "China"

    # 2. Parse rules_primary as fallback.
    if not phrase:
        rules = (settlement.get("rules_primary") or "")
        match = _RULES_PHRASE_PATTERN.search(rules)
        if match:
            phrase = match.group(1).strip().strip('"""')

    if not phrase:
        return None

    # Cap at 5 words — GDELT phrase limit.
    words = phrase.split()
    if not words or len(words) > 5:
        phrase = " ".join(words[:5])

    # Determine venue type from settlement_sources URLs.
    sources = settlement.get("settlement_sources") or []
    source_urls = " ".join((s.get("url") or "").lower() for s in sources)
    is_sports_venue = any(kw in source_urls for kw in _SPORTS_SOURCE_KEYWORDS)

    # Station: hearing/government → CSPAN; sports/other → CNN (broadest GDELT coverage).
    if is_sports_venue:
        station = "CNN"
    else:
        station = "CSPAN"

    # Speaker from rules_primary: 'If <Name> says <phrase>'
    speaker: str | None = None
    rules = settlement.get("rules_primary") or ""
    speaker_match = re.search(r"If\s+([A-Z][a-zA-Z.\s'-]+?)\s+says\s", rules)
    if speaker_match:
        speaker = speaker_match.group(1).strip()

    return {
        "phrase": phrase.lower(),
        "station": station,
        "speaker": speaker,
        "is_sports_venue": is_sports_venue,
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


def latest_mention_point(points: list[dict]) -> dict | None:
    """Return the chronologically most-recent point with a non-zero value, or None.

    Selects by parsed timestamp rather than list position — GDELT usually returns
    points in order, but we must not depend on it (an out-of-order feed would
    otherwise stamp a live signal with the wrong, older clip time).
    """
    matching_points = [point for point in points if float(point.get("value", 0.0) or 0.0) > 0.0]
    if not matching_points:
        return None
    epoch = datetime.min.replace(tzinfo=timezone.utc)
    return max(matching_points, key=lambda point: parse_point_datetime(point.get("date", "")) or epoch)


def parse_point_datetime(date_string: str) -> "datetime | None":
    """Parse a GDELT point date string into a UTC datetime, or None.

    Accepts both compact ``YYYYMMDDHHMMSS`` and ISO-ish ``YYYYMMDDTHHMMSSZ`` (the
    TV API returns the latter), by reducing to the leading 14 digits before parsing.
    """
    from datetime import datetime, timezone
    digits = "".join(character for character in (date_string or "") if character.isdigit())
    if len(digits) < 14:
        return None
    try:
        return datetime.strptime(digits[:14], "%Y%m%d%H%M%S").replace(tzinfo=timezone.utc)
    except ValueError:
        return None
