"""Parsers for FiveThirtyEight election markets and polling rows.

Turns a Kalshi election-market title into a structured query (poll type, state,
candidate of interest) and reduces a set of 538 poll rows into a recent average
margin for that candidate vs. the field.
"""
from __future__ import annotations

import re
from datetime import datetime

# Title keyword → 538 poll-file type.
_POLL_TYPE_KEYWORDS: list[tuple[str, str]] = [
    ("generic ballot", "generic_ballot"),
    ("senate", "senate"),
    ("governor", "governor"),
    ("gubernatorial", "governor"),
    ("house", "house"),
    ("president", "president"),
    ("presidential", "president"),
    ("white house", "president"),
]

# Full US state names (lowercased) for state-race detection.
_STATES = [
    "alabama", "alaska", "arizona", "arkansas", "california", "colorado",
    "connecticut", "delaware", "florida", "georgia", "hawaii", "idaho",
    "illinois", "indiana", "iowa", "kansas", "kentucky", "louisiana", "maine",
    "maryland", "massachusetts", "michigan", "minnesota", "mississippi",
    "missouri", "montana", "nebraska", "nevada", "new hampshire", "new jersey",
    "new mexico", "new york", "north carolina", "north dakota", "ohio",
    "oklahoma", "oregon", "pennsylvania", "rhode island", "south carolina",
    "south dakota", "tennessee", "texas", "utah", "vermont", "virginia",
    "washington", "west virginia", "wisconsin", "wyoming",
]


def parse_election_title(ticker: str, title: str) -> dict | None:
    """Parse a Kalshi election-market title → structured query.

    Returns a dict with:
        poll_type: 538 poll file type (president / senate / governor / house /
                   generic_ballot)
        state:     full state name (lowercased) if a state race, else None
        candidate: the candidate/party the YES side is about, if extractable
    or None if the title is not a recognizable election market.
    """
    lowered = title.lower()

    poll_type: str | None = None
    for keyword, mapped_type in _POLL_TYPE_KEYWORDS:
        if keyword in lowered:
            poll_type = mapped_type
            break
    if poll_type is None:
        return None

    state: str | None = None
    for state_name in sorted(_STATES, key=len, reverse=True):
        if state_name in lowered:
            state = state_name
            break

    # Candidate — the proper-noun run after "Will" and before the verb
    # (win/be elected/carry), e.g. "Will Josh Shapiro win ...".
    candidate: str | None = None
    candidate_match = re.search(
        r"(?:[Ww]ill|[Dd]oes|[Cc]an)\s+"
        r"([A-Z][a-zA-Z.'-]+(?:\s+[A-Z][a-zA-Z.'-]+){0,3})\s+"
        r"(?:win|be\b|beat|carry|defeat|take)",
        title,
    )
    if candidate_match:
        candidate = candidate_match.group(1).strip()

    return {
        "poll_type": poll_type,
        "state": state,
        "candidate": candidate,
    }


def _parse_end_date(raw: str) -> datetime | None:
    raw = (raw or "").strip()
    for fmt in ("%m/%d/%y", "%m/%d/%Y", "%Y-%m-%d"):
        try:
            return datetime.strptime(raw, fmt)
        except ValueError:
            continue
    return None


# 538 letter grades → recency-independent quality weight (A+ best). Unknown /
# ungraded pollsters get a neutral-low weight so they count but don't dominate.
_GRADE_WEIGHTS: dict[str, float] = {
    "A+": 1.0, "A": 0.95, "A-": 0.9, "A/B": 0.85,
    "B+": 0.8, "B": 0.75, "B-": 0.7, "B/C": 0.65,
    "C+": 0.6, "C": 0.55, "C-": 0.5, "C/D": 0.45,
    "D+": 0.4, "D": 0.35, "D-": 0.3, "F": 0.2,
}


def _grade_weight(grade: str) -> float:
    return _GRADE_WEIGHTS.get((grade or "").strip(), 0.5)


def recent_margin(
    rows: list[dict],
    candidate: str | None,
    state: str | None,
    recent_n: int = 20,
) -> dict | None:
    """Compute the candidate's quality-weighted average polling margin.

    Filters the poll rows to the requested state, takes the ``recent_n`` most
    recently completed polls, averages each candidate's ``pct`` weighted by
    pollster grade, then returns the margin between the named candidate (or the
    leader, if no candidate was named) and the runner-up.

    Returns {"candidate", "candidate_pct", "opponent", "opponent_pct",
             "margin", "poll_count"} or None if there isn't enough data.
    """
    if not rows:
        return None

    # State filter (538 leaves ``state`` blank for nationwide rows).
    if state is not None:
        selected = [row for row in rows if (row.get("state") or "").strip().lower() == state]
    else:
        selected = list(rows)
    if not selected:
        return None

    # Sort by poll end date, most recent first, and keep the freshest window.
    def _sort_key(row: dict) -> datetime:
        parsed = _parse_end_date(row.get("end_date", ""))
        return parsed or datetime.min

    selected.sort(key=_sort_key, reverse=True)
    window = selected[: max(recent_n, 1)]

    # Quality-weighted average pct per candidate over the window.
    weighted_pct_sum: dict[str, float] = {}
    weight_sum: dict[str, float] = {}
    for row in window:
        name = (row.get("candidate_name") or row.get("answer") or "").strip()
        if not name:
            continue
        try:
            pct = float(row.get("pct", "") or 0.0)
        except ValueError:
            continue
        weight = _grade_weight(row.get("fte_grade", ""))
        weighted_pct_sum[name] = weighted_pct_sum.get(name, 0.0) + pct * weight
        weight_sum[name] = weight_sum.get(name, 0.0) + weight

    averages = {
        name: weighted_pct_sum[name] / weight_sum[name]
        for name in weighted_pct_sum
        if weight_sum[name] > 0
    }
    if len(averages) < 2:
        return None

    ranked = sorted(averages.items(), key=lambda pair: pair[1], reverse=True)

    # Resolve which candidate the YES side names; default to the current leader.
    target_name = None
    if candidate:
        candidate_lower = candidate.lower()
        for name, _average in ranked:
            if candidate_lower in name.lower() or name.lower() in candidate_lower:
                target_name = name
                break
    if target_name is None:
        target_name = ranked[0][0]

    target_pct = averages[target_name]
    opponent_name, opponent_pct = next(
        ((name, average) for name, average in ranked if name != target_name),
        (ranked[0][0], ranked[0][1]),
    )

    return {
        "candidate": target_name,
        "candidate_pct": target_pct,
        "opponent": opponent_name,
        "opponent_pct": opponent_pct,
        "margin": target_pct - opponent_pct,
        "poll_count": len(window),
    }
