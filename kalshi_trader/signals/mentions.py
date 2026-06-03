"""Converter: speaker-attributed corpus + GDELT TV base rate → SignalEstimate.

For a "Will <person> say <word> in <hearing/briefing>" market, the best
unconditional prior blends two things:

* a **speaker-attributed** transcript count — how often *this person* has actually
  said the phrase in the relevant venue (preferred; the registry/archive supply it);
* the **GDELT TV** base rate — how often the phrase appeared in the speaker's TV
  coverage at all (the fallback, and corroborator when corpus evidence is thin).

The two are fused by evidence weight into a single ``mentions_base`` estimate. When
the archive is empty (no transcripts pulled yet) the signal degrades cleanly to the
GDELT-only behavior, at a lower weight/higher uncertainty, so the pipeline keeps
working before the corpus is populated.
"""
from __future__ import annotations

from datetime import datetime, timezone

from kalshi_trader.config import X_GROK_SIGNAL_WEIGHT
from kalshi_trader.external.congress_gov import DISRUPTED_STATUSES, STATUS_SCHEDULED
from kalshi_trader.external.mentions_parser import latest_mention_point, parse_point_datetime
from kalshi_trader.models import SignalEstimate

# Hardcoded constants — config_manager.py is a shared file we must not modify, so
# these are not wired into runtime_config.json. Tune via the paper-trade loop.
SOURCE_MENTIONS_BASE = "mentions_base"

# Corpus-backed tier: enough speaker-attributed documents to trust the attribution.
WEIGHT_CORPUS_BACKED = 0.55
UNCERTAINTY_CORPUS_BACKED = 0.18
# GDELT-only fallback tier: a working signal off TV coverage alone, but it cannot
# attribute the phrase to the speaker, so it carries less weight, more uncertainty,
# and is flagged non-independent of any other GDELT-derived estimate.
WEIGHT_GDELT_ONLY = 0.40
UNCERTAINTY_GDELT_ONLY = 0.22

# Evidence-weight shrinkage when fusing corpus with GDELT.
CORPUS_SHRINKAGE_K = 5.0
# At/above this many attributed documents the estimate counts as corpus-backed.
CORPUS_BACKED_DOC_THRESHOLD = 12
# Below this much total evidence with no GDELT coverage, emit nothing.
MIN_EFFECTIVE_FOR_SIGNAL = 4.0


# Near-real-time live detector: a recent caption match on the speaker's stations.
SOURCE_MENTIONS_LIVE = "mentions_live"
PROBABILITY_MENTIONS_LIVE = 0.92   # caption ASR errors + no speaker attribution
UNCERTAINTY_MENTIONS_LIVE = 0.08
WEIGHT_MENTIONS_LIVE = 0.85


def build_mentions_live_signal(
    ticker: str,
    phrase: str,
    stations: list[str],
    *,
    live_points: list[dict],
    speaker: str | None = None,
) -> SignalEstimate | None:
    """Near-real-time detection of a phrase in the last day's captions.

    Emits only on a match — absence in a feed that lags a few hours is not evidence,
    so a no-match while the window is open returns None. ``data_issued_at`` is the
    matching clip's own timestamp (not now), so the scorer's staleness discount
    reflects true freshness — the most important correctness detail in the live path.
    ``metadata["independent"]=False`` because it shares GDELT lineage with
    ``mentions_base`` and must not fake corroboration with it.
    """
    latest = latest_mention_point(live_points or [])
    if latest is None:
        return None
    clip_datetime = parse_point_datetime(latest.get("date", "")) or datetime.now(tz=timezone.utc)
    station_label = "+".join(stations) if stations else "CSPAN"
    narrative = (
        f"Live detection: \"{phrase}\" matched {station_label} captions around "
        f"{clip_datetime.isoformat()} while the {speaker or 'speaker'} window is open. "
        f"P≈{PROBABILITY_MENTIONS_LIVE:.0%} (caption ASR; unattributed)."
    )
    metadata = {
        "ticker": ticker,
        "narrative": narrative,
        "data_quality": "fresh",
        "source_model": "gdelt_live",
        "phrase": phrase,
        "stations": list(stations),
        "station": station_label,
        "clip_date": latest.get("date", ""),
        # Shares GDELT lineage with mentions_base → not independent corroboration.
        "independent": False,
    }
    if speaker:
        metadata["speaker"] = speaker
    return SignalEstimate(
        source=SOURCE_MENTIONS_LIVE,
        probability=PROBABILITY_MENTIONS_LIVE,
        uncertainty=UNCERTAINTY_MENTIONS_LIVE,
        weight=WEIGHT_MENTIONS_LIVE,
        data_issued_at=clip_datetime,
        metadata=metadata,
    )


# X-profile leading indicator. The source tag starts with "x_grok" so the scorer
# collapses it into the single X family (no double-count with the broad x-signal).
# It is a predictor, not a measurement, so it carries a deliberately modest weight.
SOURCE_X_PROFILE = "x_grok_profile"
WEIGHT_X_PROFILE = round(X_GROK_SIGNAL_WEIGHT * 0.7, 4)


def _clamp_probability(probability: float) -> float:
    return min(max(probability, 0.01), 0.99)


def _parse_iso_timestamp(value: object) -> datetime | None:
    """Parse an ISO 8601 string to an aware UTC datetime, or None on failure."""
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def build_x_profile_signal(
    ticker: str,
    phrase: str,
    scan: dict,
    handles: list[str],
    speaker: str | None = None,
) -> SignalEstimate | None:
    """Build the ``x_grok_profile`` SignalEstimate from a profile-topic scan.

    Emits only when the speaker's own accounts actually posted about the topic
    (``post_count > 0``); a quiet timeline returns None and never counts against
    the market. ``data_issued_at`` is the most-recent relevant post so the scorer's
    staleness discount reflects true freshness.
    """
    post_count = int(scan.get("post_count", 0) or 0)
    if post_count <= 0:
        return None

    probability = _clamp_probability(float(scan.get("probability", 0.5) or 0.5))
    uncertainty = float(scan.get("uncertainty", 0.3) or 0.3)
    issued_at = _parse_iso_timestamp(scan.get("issued_at")) or datetime.now(tz=timezone.utc)

    handle_list = ", ".join(f"@{handle}" for handle in handles)
    narrative = (
        f"X-profile leading indicator: {speaker or 'the speaker'}'s accounts "
        f"({handle_list}) posted about \"{phrase}\" {post_count} time(s) recently → "
        f"P={probability:.0%}. {scan.get('summary') or ''}".strip()
    )
    metadata = {
        "ticker": ticker,
        "narrative": narrative,
        "data_quality": "fresh",
        "source_model": "x_grok_profile",
        "phrase": phrase,
        "post_count": post_count,
        "handles": list(handles),
        "summary": scan.get("summary", ""),
        # Independent of the GDELT-derived mentions estimates (different source).
        "independent": True,
    }
    if speaker:
        metadata["speaker"] = speaker

    return SignalEstimate(
        source=SOURCE_X_PROFILE,
        probability=probability,
        uncertainty=uncertainty,
        weight=WEIGHT_X_PROFILE,
        data_issued_at=issued_at,
        metadata=metadata,
    )


# Hearing-schedule veto: a near-deterministic NO when the relevant hearing can't
# happen before the market closes. Genuinely independent of the GDELT/corpus prior.
SOURCE_HEARING_SCHEDULE = "hearing_schedule"
WEIGHT_HEARING_SCHEDULE = 0.95
PROBABILITY_HEARING_VETO = 0.03
UNCERTAINTY_HEARING_VETO = 0.05
# rapidfuzz partial_ratio score (0-100) required to call a schedule committee a
# match for the title's committee hint.
COMMITTEE_MATCH_THRESHOLD = 80


def build_hearing_schedule_signal(
    ticker: str,
    phrase: str,
    schedule_records: list[dict],
    *,
    committee_hint: str | None,
    close_date: str | None,
    now: datetime | None = None,
    speaker: str | None = None,
) -> SignalEstimate | None:
    """Near-veto SignalEstimate when the relevant hearing can't happen before close.

    Matches the title's ``committee_hint`` against the schedule's committee names
    (rapidfuzz), restricted to meetings in the resolution window ``[today, close_date]``.

    - A scheduled meeting still on the calendar in the window → returns None (the
      hearing will happen; the saying-probability is left to the base/profile signals).
    - The matched committee's window meeting is Canceled/Postponed/Rescheduled, **or**
      a tracked committee has nothing on the calendar before close → near-veto
      (``probability=0.03, uncertainty=0.05, weight=0.95``).
    - No ``committee_hint`` / no ``close_date`` / committee not in our schedule →
      returns None (we know nothing — never fabricate a veto).
    """
    if not committee_hint or not close_date or not schedule_records:
        return None

    from rapidfuzz import fuzz

    committee_hint_lower = committee_hint.lower()
    matched = [
        record for record in schedule_records
        if fuzz.partial_ratio(committee_hint_lower, (record.get("committee") or "").lower())
        >= COMMITTEE_MATCH_THRESHOLD
    ]
    if not matched:
        return None  # committee not in our schedule → no information, no veto

    today = (now or datetime.now(tz=timezone.utc)).strftime("%Y-%m-%d")
    in_window = [
        record for record in matched
        if today <= (record.get("event_date") or "") <= close_date
    ]
    if any(record.get("status") == STATUS_SCHEDULED for record in in_window):
        return None  # a hearing is still on the calendar before close → no veto

    disrupted = [record for record in in_window if record.get("status") in DISRUPTED_STATUSES]
    if disrupted:
        reason = f"the {committee_hint} hearing was {disrupted[0]['status'].lower()}"
    else:
        reason = f"no {committee_hint} hearing is scheduled before the market closes"

    narrative = (
        f"Hearing-schedule veto: {reason} (by {close_date}), so "
        f"{speaker or 'the speaker'} cannot say \"{phrase}\" there. P≈{PROBABILITY_HEARING_VETO:.0%}."
    )
    metadata = {
        "ticker": ticker,
        "narrative": narrative,
        "data_quality": "fresh",
        "source_model": "congress_gov_schedule",
        "phrase": phrase,
        "committee_hint": committee_hint,
        "close_date": close_date,
        "matched_meetings": [record.get("meeting_id") for record in matched],
        "veto_reason": "disrupted" if disrupted else "not_scheduled",
        # Independent of the GDELT/corpus prior (a different question: does the
        # event even occur), so its agreement/disagreement is real corroboration.
        "independent": True,
    }
    if speaker:
        metadata["speaker"] = speaker

    return SignalEstimate(
        source=SOURCE_HEARING_SCHEDULE,
        probability=PROBABILITY_HEARING_VETO,
        uncertainty=UNCERTAINTY_HEARING_VETO,
        weight=WEIGHT_HEARING_SCHEDULE,
        data_issued_at=now or datetime.now(tz=timezone.utc),
        metadata=metadata,
    )


def build_mentions_base_signal(
    ticker: str,
    phrase: str,
    stations: list[str],
    *,
    gdelt_base_rate: dict | None,
    corpus: dict | None = None,
    speaker: str | None = None,
    speaker_key: str | None = None,
) -> SignalEstimate | None:
    """Build the fused ``mentions_base`` SignalEstimate.

    Args:
        ticker: Kalshi market ticker.
        phrase: Word/phrase the market tracks.
        stations: TV stations the GDELT base rate was queried on (from the registry).
        gdelt_base_rate: Dict from ``recency_weighted_base_rate`` — has
            ``fraction_with_mention``, ``n_effective``, ``period_count`` and may
            carry ``window_fraction`` (the window-aligned rate, preferred when
            present). Empty/None when GDELT had no coverage.
        corpus: Optional ``{"document_count", "match_count"}`` from the archive.
        speaker: Named speaker for the narrative.
        speaker_key: Registry-normalized attribution key (recorded in metadata).

    Returns:
        One ``mentions_base`` SignalEstimate, or None when there is too little
        evidence to say anything (mirrors the empty-array sentinel upstream).
    """
    gdelt_base_rate = gdelt_base_rate or {}
    has_gdelt = int(gdelt_base_rate.get("period_count", 0) or 0) > 0
    # Window-aligned rate matches the bet's horizon; fall back to the plain rate.
    if "window_fraction" in gdelt_base_rate:
        p_gdelt = float(gdelt_base_rate.get("window_fraction", 0.0) or 0.0)
    else:
        p_gdelt = float(gdelt_base_rate.get("fraction_with_mention", 0.0) or 0.0)

    corpus = corpus or {}
    document_count = float(corpus.get("document_count", 0) or 0)
    match_count = float(corpus.get("match_count", 0) or 0)
    p_corpus = (match_count / document_count) if document_count > 0 else 0.0

    # Not enough to say anything: no GDELT coverage and a too-thin corpus.
    if not has_gdelt and document_count < MIN_EFFECTIVE_FOR_SIGNAL:
        return None

    # Evidence-weighted fusion of corpus with GDELT.
    if document_count > 0 and has_gdelt:
        weight_corpus = document_count / (document_count + CORPUS_SHRINKAGE_K)
        probability = weight_corpus * p_corpus + (1.0 - weight_corpus) * p_gdelt
    elif document_count > 0:
        probability = p_corpus
    else:
        probability = p_gdelt
    probability = _clamp_probability(probability)

    corpus_backed = document_count >= CORPUS_BACKED_DOC_THRESHOLD
    if corpus_backed:
        weight = WEIGHT_CORPUS_BACKED
        uncertainty = UNCERTAINTY_CORPUS_BACKED
        data_quality = "fresh"
        independent = True
    else:
        weight = WEIGHT_GDELT_ONLY
        uncertainty = UNCERTAINTY_GDELT_ONLY
        data_quality = "stale"
        independent = False

    station_label = "+".join(stations) if stations else "CSPAN"
    speaker_clause = f"{speaker} saying " if speaker else ""
    if corpus_backed:
        evidence_clause = (
            f"speaker-attributed corpus: \"{phrase}\" in {int(match_count)}/{int(document_count)} "
            f"of {speaker or 'the speaker'}'s transcripts (P={p_corpus:.1%}), "
            f"blended with GDELT TV ({station_label}, P={p_gdelt:.1%})"
        )
    elif document_count > 0:
        evidence_clause = (
            f"thin corpus ({int(match_count)}/{int(document_count)}) blended with "
            f"GDELT TV ({station_label}, P={p_gdelt:.1%})"
        )
    else:
        evidence_clause = f"GDELT TV ({station_label}) base rate only, P={p_gdelt:.1%}"
    narrative = (
        f"P({speaker_clause}\"{phrase}\") = {probability:.2%} — {evidence_clause}. "
        f"Data is {data_quality}."
    )

    metadata: dict = {
        "ticker": ticker,
        "narrative": narrative,
        "data_quality": data_quality,
        "source_model": "mentions_base",
        "phrase": phrase,
        "stations": list(stations),
        "station": station_label,
        "period_count": int(gdelt_base_rate.get("period_count", 0) or 0),
        "n_effective": float(gdelt_base_rate.get("n_effective", 0.0) or 0.0),
        "corpus_document_count": int(document_count),
        "corpus_match_count": int(match_count),
        "p_corpus": round(p_corpus, 4),
        "p_gdelt": round(p_gdelt, 4),
        "mean_match_percent": gdelt_base_rate.get("mean_match_percent", 0.0),
        "max_match_percent": gdelt_base_rate.get("max_match_percent", 0.0),
        # Domain-neutral independence flag read by the scorer's agreement boost.
        # GDELT-only estimates share lineage with any other GDELT-derived signal,
        # so they must not corroborate one another.
        "independent": independent,
    }
    if "window_fraction" in gdelt_base_rate:
        metadata["window_fraction"] = round(float(gdelt_base_rate["window_fraction"]), 4)
    if "window_days" in gdelt_base_rate:
        metadata["window_days"] = gdelt_base_rate["window_days"]
    if speaker:
        metadata["speaker"] = speaker
    if speaker_key:
        metadata["speaker_key"] = speaker_key

    return SignalEstimate(
        source=SOURCE_MENTIONS_BASE,
        probability=probability,
        uncertainty=uncertainty,
        weight=weight,
        data_issued_at=datetime.now(tz=timezone.utc),
        metadata=metadata,
    )
