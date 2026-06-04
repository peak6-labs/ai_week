from datetime import datetime, timezone

import pytest

from kalshi_trader.external.gdelt import parse_timeline_json, extract_points, merge_station_points
from kalshi_trader.signals.mentions import (
    PROBABILITY_MENTIONS_LIVE,
    SOURCE_HEARING_SCHEDULE,
    SOURCE_MENTIONS_LIVE,
    WEIGHT_CORPUS_BACKED,
    WEIGHT_GDELT_ONLY,
    WEIGHT_MENTIONS_LIVE,
    build_hearing_schedule_signal,
    build_mentions_base_signal,
    build_mentions_live_signal,
)

_NOW = datetime(2026, 6, 3, tzinfo=timezone.utc)


def _meeting(committee, status, event_date, meeting_id="m1", chamber="House"):
    return {"meeting_id": meeting_id, "committee": committee, "chamber": chamber,
            "event_date": event_date, "status": status, "title": ""}


# --- gdelt response parsing (no network) -----------------------------------

_SAMPLE_TIMELINE = """
{"query_details": {"title": "shutdown station:CSPAN"},
 "timeline": [ { "series": "CSPAN", "data": [
   { "date": "20240101T120000Z", "value": 0.5 },
   { "date": "20240201T120000Z", "value": 0.0 },
   { "date": "20240301T120000Z", "value": 1.2 } ] } ] }
"""


def test_parse_timeline_json_valid():
    parsed = parse_timeline_json(_SAMPLE_TIMELINE)
    assert "timeline" in parsed
    points = extract_points(parsed, "CSPAN")
    assert len(points) == 3
    assert points[0]["value"] == pytest.approx(0.5)


def test_parse_timeline_json_empty_object():
    assert parse_timeline_json("{}") == {}
    assert extract_points({}, "CSPAN") == []


def test_parse_timeline_json_error_string():
    # GDELT returns plain text on a malformed query.
    assert parse_timeline_json("Your query must contain at least one station.") == {}


def test_extract_points_single_unlabeled_series():
    timeline = {"timeline": [{"data": [{"date": "20240101T120000Z", "value": 2.0}]}]}
    points = extract_points(timeline, "CSPAN")
    assert len(points) == 1
    assert points[0]["value"] == pytest.approx(2.0)


# --- multi-station merge (national-news routing) ---------------------------

def test_merge_station_points_takes_max_per_date_across_series():
    timeline = {"timeline": [
        {"series": "CNN", "data": [
            {"date": "20240101T120000Z", "value": 0.5},
            {"date": "20240201T120000Z", "value": 0.0},
        ]},
        {"series": "FOXNEWS", "data": [
            {"date": "20240101T120000Z", "value": 0.2},   # same date, lower → CNN wins
            {"date": "20240201T120000Z", "value": 1.1},   # FOX has it this period
        ]},
    ]}
    points = merge_station_points(timeline)
    assert points == [
        {"date": "20240101T120000Z", "value": pytest.approx(0.5)},
        {"date": "20240201T120000Z", "value": pytest.approx(1.1)},
    ]


def test_merge_station_points_empty():
    assert merge_station_points({}) == []


# --- mentions_base signal builder ------------------------------------------

def _gdelt(fraction, period_count=30, **extra):
    base = {
        "period_count": period_count,
        "periods_with_mention": int(round(fraction * period_count)),
        "fraction_with_mention": fraction,
        "n_effective": float(period_count),
        "mean_match_percent": 0.4,
        "max_match_percent": 2.1,
    }
    base.update(extra)
    return base


def test_gdelt_only_fallback_tier():
    sig = build_mentions_base_signal(
        "KXMENTION-POWELL-RECESSION", "recession", ["CSPAN"],
        gdelt_base_rate=_gdelt(0.7), corpus=None, speaker="Jerome Powell",
        speaker_key="powell",
    )
    assert sig is not None
    assert sig.source == "mentions_base"
    assert sig.probability == pytest.approx(0.7)
    assert sig.weight == pytest.approx(WEIGHT_GDELT_ONLY)
    assert sig.uncertainty == pytest.approx(0.22)
    assert sig.metadata["data_quality"] == "stale"
    assert sig.metadata["independent"] is False
    assert sig.metadata["speaker_key"] == "powell"
    assert sig.data_issued_at.tzinfo is not None


def test_corpus_backed_tier_fuses_corpus_and_gdelt():
    # 14/20 attributed (p_corpus=0.7) blended with GDELT 0.3:
    # weight_corpus = 20/(20+5) = 0.8 → 0.8*0.7 + 0.2*0.3 = 0.62
    sig = build_mentions_base_signal(
        "T", "recession", ["CSPAN"],
        gdelt_base_rate=_gdelt(0.3),
        corpus={"document_count": 20, "match_count": 14},
        speaker="Jerome Powell",
    )
    assert sig.weight == pytest.approx(WEIGHT_CORPUS_BACKED)
    assert sig.uncertainty == pytest.approx(0.18)
    assert sig.metadata["data_quality"] == "fresh"
    assert sig.metadata["independent"] is True
    assert sig.probability == pytest.approx(0.62, abs=1e-6)
    assert sig.metadata["corpus_document_count"] == 20
    assert sig.metadata["p_corpus"] == pytest.approx(0.7)


def test_window_fraction_is_preferred_over_plain_fraction():
    base_rate = _gdelt(0.9, window_fraction=0.2)
    sig = build_mentions_base_signal("T", "recession", ["CSPAN"], gdelt_base_rate=base_rate)
    assert sig.probability == pytest.approx(0.2)


def test_corpus_only_above_threshold_emits():
    sig = build_mentions_base_signal(
        "T", "recession", ["CSPAN"],
        gdelt_base_rate={},  # no GDELT coverage
        corpus={"document_count": 8, "match_count": 4},
    )
    assert sig is not None
    assert sig.probability == pytest.approx(0.5)


def test_no_evidence_returns_none():
    assert build_mentions_base_signal("T", "x", ["CSPAN"], gdelt_base_rate={}, corpus=None) is None
    assert build_mentions_base_signal(
        "T", "x", ["CSPAN"], gdelt_base_rate=None,
        corpus={"document_count": 2, "match_count": 1},  # too thin, no GDELT
    ) is None


def test_probability_clamped():
    low = build_mentions_base_signal("T", "x", ["CSPAN"], gdelt_base_rate=_gdelt(0.0))
    high = build_mentions_base_signal("T", "x", ["CSPAN"], gdelt_base_rate=_gdelt(1.0))
    assert low.probability >= 0.01
    assert high.probability <= 0.99


def test_stations_recorded_in_metadata():
    sig = build_mentions_base_signal(
        "T", "uranium", ["CNN", "FOXNEWS", "MSNBC"], gdelt_base_rate=_gdelt(0.4),
        speaker="Donald Trump",
    )
    assert sig.metadata["stations"] == ["CNN", "FOXNEWS", "MSNBC"]
    assert sig.metadata["station"] == "CNN+FOXNEWS+MSNBC"


# --- hearing-schedule veto -------------------------------------------------

def _schedule_signal(records, committee_hint="financial services", close_date="2026-06-30"):
    return build_hearing_schedule_signal(
        "KXMENTION-POWELL-RECESSION", "recession", records,
        committee_hint=committee_hint, close_date=close_date, now=_NOW, speaker="Jerome Powell",
    )


def test_hearing_canceled_in_window_is_near_veto():
    records = [_meeting("Committee on Financial Services", "Canceled", "2026-06-10")]
    sig = _schedule_signal(records)
    assert sig is not None
    assert sig.source == SOURCE_HEARING_SCHEDULE
    assert sig.probability == pytest.approx(0.03)
    assert sig.weight == pytest.approx(0.95)
    assert sig.metadata["veto_reason"] == "disrupted"
    assert sig.metadata["independent"] is True


def test_hearing_scheduled_in_window_emits_nothing():
    records = [_meeting("Committee on Financial Services", "Scheduled", "2026-06-10")]
    assert _schedule_signal(records) is None


def test_tracked_committee_with_no_meeting_in_window_is_near_veto():
    # We have this committee in the schedule, but its only meeting is AFTER close.
    records = [_meeting("Committee on Financial Services", "Scheduled", "2026-08-01")]
    sig = _schedule_signal(records, close_date="2026-06-30")
    assert sig is not None
    assert sig.metadata["veto_reason"] == "not_scheduled"


def test_no_committee_hint_emits_nothing():
    records = [_meeting("Committee on Financial Services", "Canceled", "2026-06-10")]
    assert _schedule_signal(records, committee_hint=None) is None


def test_committee_not_in_schedule_emits_nothing():
    # Title hints "judiciary" but the schedule only has Financial Services → no info.
    records = [_meeting("Committee on Financial Services", "Canceled", "2026-06-10")]
    assert _schedule_signal(records, committee_hint="judiciary") is None


def test_no_close_date_emits_nothing():
    records = [_meeting("Committee on Financial Services", "Canceled", "2026-06-10")]
    assert _schedule_signal(records, close_date=None) is None


# --- near-real-time live detector ------------------------------------------

def test_live_match_emits_092_stamped_with_clip_timestamp():
    points = [
        {"date": "20260603T080000Z", "value": 0.0},
        {"date": "20260603T140000Z", "value": 0.6},   # the match (most recent > 0)
        {"date": "20260603T120000Z", "value": 0.3},
    ]
    sig = build_mentions_live_signal(
        "T", "recession", ["CSPAN"], live_points=points, speaker="Jerome Powell",
    )
    assert sig is not None
    assert sig.source == SOURCE_MENTIONS_LIVE
    assert sig.probability == pytest.approx(PROBABILITY_MENTIONS_LIVE)
    assert sig.weight == pytest.approx(WEIGHT_MENTIONS_LIVE)
    assert sig.metadata["independent"] is False
    # data_issued_at is the matching clip's own timestamp (14:00), not now().
    assert sig.data_issued_at == datetime(2026, 6, 3, 14, 0, 0, tzinfo=timezone.utc)


def test_live_no_match_emits_nothing():
    # All-zero last-24h points (no mention) → absence in a lagged feed isn't evidence.
    points = [{"date": "20260603T080000Z", "value": 0.0},
              {"date": "20260603T140000Z", "value": 0.0}]
    assert build_mentions_live_signal("T", "recession", ["CSPAN"], live_points=points) is None
    assert build_mentions_live_signal("T", "recession", ["CSPAN"], live_points=[]) is None
