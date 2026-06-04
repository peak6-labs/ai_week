"""Tests for the walk-forward corpus-premise scorer (kalshi_trader/mentions/premise_backtest.py).

Inline events, no fixtures or network — matching the repo's test style. Each event is a
dict ``{"speaker_key", "event_date", "norm_text"}`` exactly as the store yields it.
"""
from __future__ import annotations

import pytest

from kalshi_trader.mentions.premise_backtest import (
    DEFAULT_SHRINKAGE_STRENGTH,
    bootstrap_skill_interval,
    summarize_premise,
    walk_forward_predictions_for_phrase,
)

PHRASE = "recession"


def _event(speaker_key: str, day: int, contains: bool) -> dict:
    """An event on 2020-01-<day>; norm_text contains the phrase iff ``contains``."""
    return {
        "speaker_key": speaker_key,
        "event_date": f"2020-01-{day:02d}",
        "norm_text": "talk of recession ahead" if contains else "no mention here",
    }


def test_speaker_rate_beats_global_when_speakers_differ():
    # Speaker "always" says the phrase every time; speaker "never" says it never.
    # The global rate hovers near 0.5, so the speaker-attributed model should win.
    events = []
    day = 1
    for _ in range(30):
        events.append(_event("always", day, True))
        day += 1
        events.append(_event("never", day, False))
        day += 1
    summary = summarize_premise(walk_forward_predictions_for_phrase(events, PHRASE))

    assert summary["brier_speaker"] < summary["brier_global"]
    assert summary["brier_skill_score"] > 0.0
    # Strong, clean separation → the credible interval sits clearly above zero.
    assert summary["skill_credible_interval"]["percentile_2_5"] > 0.0
    assert summary["premise_supported"] is True


def test_skill_near_zero_when_speakers_match_the_global_rate():
    # Both speakers alternate yes/no identically, so each speaker's rate equals the
    # global rate → the speaker model carries no extra information.
    events = []
    day = 1
    for index in range(30):
        contains = index % 2 == 0
        events.append(_event("alpha", day, contains))
        day += 1
        events.append(_event("beta", day, contains))
        day += 1
    summary = summarize_premise(walk_forward_predictions_for_phrase(events, PHRASE))

    # No real edge: skill near zero, lower credible bound not above zero, so the
    # premise does not clear its bar.
    assert abs(summary["brier_skill_score"]) < 0.15
    assert summary["skill_credible_interval"]["percentile_2_5"] <= 0.0
    assert summary["premise_supported"] is False


def test_shrinkage_pulls_a_one_document_speaker_toward_the_global_rate():
    events = [
        _event("speaker_x", 1, True),    # earliest group — unscored
        _event("speaker_y", 2, False),   # so global before day 3 is 1 match / 2 docs = 0.5
        _event("speaker_x", 3, True),    # scored: speaker_x has exactly 1 prior doc (a match)
    ]
    predictions = walk_forward_predictions_for_phrase(events, PHRASE)
    day_three = [p for p in predictions if p.event_date == "2020-01-03"]
    assert len(day_three) == 1
    prediction = day_three[0]

    expected_global = 0.5
    expected_speaker = (1 + DEFAULT_SHRINKAGE_STRENGTH * expected_global) / (1 + DEFAULT_SHRINKAGE_STRENGTH)
    assert prediction.global_prediction == pytest.approx(expected_global)
    assert prediction.speaker_prediction == pytest.approx(expected_speaker)
    assert prediction.speaker_had_prior_history is True


def test_look_ahead_guard_same_date_events_do_not_see_each_other():
    # Two events share a date; neither may count the other (strict before), and the
    # very first date group is unscored (nothing earlier to predict from).
    events = [
        _event("speaker_a", 1, True),
        _event("speaker_a", 1, True),   # same date as above
        _event("speaker_a", 2, False),  # only this one is scored
    ]
    predictions = walk_forward_predictions_for_phrase(events, PHRASE)
    assert len(predictions) == 1
    prediction = predictions[0]
    assert prediction.event_date == "2020-01-02"
    assert prediction.speaker_had_prior_history is True
    # Prior global rate is 2 matches / 2 docs = 1.0, clamped to 0.99.
    assert prediction.global_prediction == pytest.approx(0.99)
    assert prediction.realized_outcome == 0


def test_empty_and_unattributed_events_are_dropped():
    events = [
        {"speaker_key": "", "event_date": "2020-01-01", "norm_text": "recession"},  # no speaker
        {"speaker_key": "x", "event_date": "", "norm_text": "recession"},           # no date
    ]
    assert walk_forward_predictions_for_phrase(events, PHRASE) == []
    assert bootstrap_skill_interval([])["draws"] == 0
