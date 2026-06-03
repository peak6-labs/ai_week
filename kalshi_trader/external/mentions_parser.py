"""Parsers for GDELT "mentions" markets and TV-API timelines.

A Kalshi mention market asks "Will <speaker> say <word/phrase> in <venue>"
(a hearing, briefing, floor speech, press conference). We parse the title into
the phrase to search and the TV station whose closed-caption archive covers the
venue, then turn the historical per-period match-percent timeline into a base
rate.
"""
from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone

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
# specific pattern wins. The captured group is the quoted/target phrase. Both
# straight and curly quotes, single and double, are accepted ("recession",
# 'recession', “recession”, ‘recession’).
_OPEN_QUOTE = "\"“'‘"
_CLOSE_QUOTE = "\"”'’"
_SAY_PATTERNS = [
    rf'say\s+the\s+(?:word|phrase|words)\s+[{_OPEN_QUOTE}]([^{_CLOSE_QUOTE}]+)[{_CLOSE_QUOTE}]',
    rf'say\s+[{_OPEN_QUOTE}]([^{_CLOSE_QUOTE}]+)[{_CLOSE_QUOTE}]',
    rf'mention\s+[{_OPEN_QUOTE}]([^{_CLOSE_QUOTE}]+)[{_CLOSE_QUOTE}]',
    rf'utter\s+[{_OPEN_QUOTE}]([^{_CLOSE_QUOTE}]+)[{_CLOSE_QUOTE}]',
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
            phrase = match.group(1).strip().strip('.,?"“”\'‘’')
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


# ---------------------------------------------------------------------------
# Text normalization (shared by the archive store and corpus phrase-counting)
# ---------------------------------------------------------------------------

# Smart quotes and dashes folded to their ASCII equivalents before lowercasing,
# so "don't" in a transcript matches "don't" in a market title regardless of the
# curly-quote a source used.
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
    never cause a missed match.
    """
    if not text:
        return ""
    folded = text.translate(_SMART_CHAR_FOLD).lower()
    return re.sub(r"[^a-z0-9]+", " ", folded).strip()


# ---------------------------------------------------------------------------
# Recency-, window-, and context-aware base rates
# ---------------------------------------------------------------------------

def _parse_point_date(date_str: str) -> datetime | None:
    """Parse a GDELT point date ("YYYYMMDDTHHMMSSZ") into an aware UTC datetime."""
    if not date_str or len(date_str) < 8:
        return None
    try:
        return datetime.strptime(date_str[:8], "%Y%m%d").replace(tzinfo=timezone.utc)
    except (ValueError, TypeError):
        return None


def parse_point_datetime(date_str: str) -> datetime | None:
    """Parse a GDELT point timestamp at full precision ("YYYYMMDDTHHMMSSZ").

    Unlike :func:`_parse_point_date` (which keeps only the day), this preserves the
    hour/minute so the live detector can stamp a signal with the *clip's* time.
    Falls back to day precision if the time portion is malformed.
    """
    if not date_str:
        return None
    try:
        return datetime.strptime(date_str, "%Y%m%dT%H%M%SZ").replace(tzinfo=timezone.utc)
    except (ValueError, TypeError):
        return _parse_point_date(date_str)


def latest_mention_point(points: list[dict]) -> dict | None:
    """Return the most recent point whose value > 0 (a real mention), else None.

    Used by the live detector: a non-zero recent bucket means the phrase was said
    on the speaker's stations in that window; the point's timestamp is the clip time.
    """
    dated: list[tuple[datetime, dict]] = []
    for point in points:
        if float(point.get("value", 0.0) or 0.0) <= 0.0:
            continue
        point_datetime = parse_point_datetime(point.get("date", ""))
        if point_datetime is not None:
            dated.append((point_datetime, point))
    if not dated:
        return None
    return max(dated, key=lambda pair: pair[0])[1]


def recency_weighted_base_rate(
    points: list[dict],
    half_life_days: float = 365.0,
    now: datetime | None = None,
) -> dict:
    """Reduce a GDELT timeline to a *recency-weighted* base-rate summary.

    Each period contributes a binary "did the phrase appear" outcome weighted by
    ``w_i = 0.5 ** (age_days / half_life_days)`` (a one-year half-life by default),
    so a mention last month counts for far more than one in 2011::

        fraction_with_mention = Σ(w_i · mention_i) / Σ(w_i)
        n_effective           = Σ(w_i)

    ``n_effective`` is the recency-discounted count of periods backing the rate —
    the evidence weight the signal builder uses to decide its confidence tier.
    Returns the same keys as :func:`base_rate_from_points` plus ``n_effective``.
    """
    reference_time = now or datetime.now(tz=timezone.utc)
    period_count = len(points)
    if period_count == 0:
        return {
            "period_count": 0,
            "periods_with_mention": 0,
            "fraction_with_mention": 0.0,
            "n_effective": 0.0,
            "mean_match_percent": 0.0,
            "max_match_percent": 0.0,
        }

    weighted_mention_sum = 0.0
    weight_total = 0.0
    values: list[float] = []
    periods_with_mention = 0
    for point in points:
        value = float(point.get("value", 0.0) or 0.0)
        values.append(value)
        mention = 1.0 if value > 0.0 else 0.0
        if mention:
            periods_with_mention += 1
        point_date = _parse_point_date(point.get("date", ""))
        if point_date is None:
            weight = 1.0
        else:
            age_days = max(0.0, (reference_time - point_date).total_seconds() / 86400.0)
            weight = 0.5 ** (age_days / half_life_days)
        weighted_mention_sum += weight * mention
        weight_total += weight

    fraction = (weighted_mention_sum / weight_total) if weight_total > 0 else 0.0
    return {
        "period_count": period_count,
        "periods_with_mention": periods_with_mention,
        "fraction_with_mention": fraction,
        "n_effective": weight_total,
        "mean_match_percent": sum(values) / period_count,
        "max_match_percent": max(values),
    }


def window_aligned_fraction(
    points: list[dict],
    window_days: float,
    now: datetime | None = None,
) -> dict:
    """Fraction of resolution-window-length buckets that contained a mention.

    GDELT's FULL timeline is ~monthly; a market that resolves over "this week" or
    "this year" bets on a different horizon. Grouping the points into consecutive
    buckets ``window_days`` wide (a bucket "has a mention" if any point in it does)
    yields a per-window base rate that matches the bet's horizon. Returns
    ``{"fraction", "bucket_count", "buckets_with_mention"}``.
    """
    if not points or window_days <= 0:
        return {"fraction": 0.0, "bucket_count": 0, "buckets_with_mention": 0}

    dated: list[tuple[datetime, float]] = []
    for point in points:
        point_date = _parse_point_date(point.get("date", ""))
        if point_date is None:
            continue
        dated.append((point_date, float(point.get("value", 0.0) or 0.0)))
    if not dated:
        return {"fraction": 0.0, "bucket_count": 0, "buckets_with_mention": 0}

    dated.sort(key=lambda pair: pair[0])
    earliest = dated[0][0]
    window = timedelta(days=window_days)
    bucket_max: dict[int, float] = {}
    for point_date, value in dated:
        bucket_index = int((point_date - earliest) / window)
        bucket_max[bucket_index] = max(bucket_max.get(bucket_index, 0.0), value)

    bucket_count = len(bucket_max)
    buckets_with_mention = sum(1 for value in bucket_max.values() if value > 0.0)
    return {
        "fraction": buckets_with_mention / bucket_count if bucket_count else 0.0,
        "bucket_count": bucket_count,
        "buckets_with_mention": buckets_with_mention,
    }


def shrink_estimate(
    p_narrow: float,
    n_narrow: float,
    p_broad: float,
    shrinkage_k: float = 5.0,
) -> float:
    """Shrink a sparse narrow estimate toward a broader prior.

    Empirical-Bayes shrinkage::

        weight_narrow = n_narrow / (n_narrow + K)
        p = weight_narrow · p_narrow + (1 - weight_narrow) · p_broad

    With little narrow evidence the result leans on ``p_broad``; as ``n_narrow``
    grows it trusts ``p_narrow``. Used to walk the context ladder
    (speaker+venue → speaker → venue → GDELT-only).
    """
    n_narrow = max(0.0, float(n_narrow))
    denominator = n_narrow + shrinkage_k
    weight_narrow = (n_narrow / denominator) if denominator > 0 else 0.0
    return weight_narrow * p_narrow + (1.0 - weight_narrow) * p_broad


# ---------------------------------------------------------------------------
# Market-window parsing and the written-post wrong-signal guard
# ---------------------------------------------------------------------------

# Title phrase → resolution-window length in days. Ordered most-specific first.
_WINDOW_PATTERNS: list[tuple[str, int]] = [
    (r"\btoday\b", 1),
    (r"\btomorrow\b", 1),
    (r"\bthis week\b", 7),
    (r"\bnext week\b", 7),
    (r"\bthis month\b", 30),
    (r"\bthis year\b", 365),
    (r"\bin 20\d{2}\b", 365),
]


def parse_window_days(title: str | None) -> int | None:
    """Infer the market's resolution window length in days from its title.

    Returns None when no window phrase is recognized, in which case the caller
    keeps the recency-weighted (un-bucketed) base rate.
    """
    lowered = (title or "").lower()
    for pattern, days in _WINDOW_PATTERNS:
        if re.search(pattern, lowered):
            return days
    return None


# Indicators that the contract settles on a *written* post, not a spoken remark.
# A transcript/TV base rate measures the wrong thing for these — the X signal owns
# them instead — so the mentions pipeline returns nothing.
_WRITTEN_POST_KEYWORDS: tuple[str, ...] = (
    "truth social",
    "tweet",
    "retweet",
    "post on x",
    "post to x",
    "posts on x",
    "x post",
    "social media post",
    "post on truth",
    "written post",
    "in writing",
)


# Known congressional committee keywords, most-specific first so "financial
# services" wins over "finance" and "energy and commerce" over "commerce". Used to
# pull a committee hint out of a market title for fuzzy-matching the hearing schedule.
_COMMITTEE_KEYWORDS: tuple[str, ...] = (
    "financial services", "armed services", "ways and means", "energy and commerce",
    "foreign relations", "foreign affairs", "homeland security", "natural resources",
    "veterans affairs", "small business", "intelligence", "appropriations",
    "agriculture", "judiciary", "oversight", "banking", "commerce", "finance",
    "budget", "ethics", "rules", "education", "health", "labor",
)


def extract_committee_hint(title: str | None) -> str | None:
    """Best-effort congressional committee name from a market title.

    Returns a keyword like ``"banking"`` / ``"financial services"`` (to fuzzy-match
    a schedule committee such as "Committee on Banking, Housing, and Urban Affairs"),
    or the phrase before the word "committee", else None when the title names none.
    """
    lowered = (title or "").lower()
    for keyword in _COMMITTEE_KEYWORDS:
        if keyword in lowered:
            return keyword
    committee_match = re.search(r"([a-z][a-z& ]{2,40}?)\s+committee\b", lowered)
    if committee_match:
        return committee_match.group(1).strip()
    return None


def extract_chamber_hint(title: str | None) -> str | None:
    """Return ``"Senate"`` / ``"House"`` if the title names a chamber, else None."""
    lowered = (title or "").lower()
    if "senate" in lowered:
        return "Senate"
    if "house" in lowered:
        return "House"
    return None


def is_written_post_market(title: str | None, settlement: dict | None = None) -> bool:
    """True if the market settles on a written post rather than a spoken remark.

    Scans the title and any settlement rule text for written-post indicators
    (tweet / Truth Social / "post on X"). The transcript and TV paths measure
    *spoken* mentions, so when this is True the pipeline emits nothing and leaves
    the market to the X signal.
    """
    haystack = (title or "").lower()
    if settlement:
        for field_name in ("rules_primary", "rules_secondary", "subtitle"):
            field_text = settlement.get(field_name)
            if field_text:
                haystack += " " + str(field_text).lower()
    return any(keyword in haystack for keyword in _WRITTEN_POST_KEYWORDS)
