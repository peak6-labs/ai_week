"""Tests for kalshi_trader.ideas_history — the pure transform shared by the
local (paper JSONL) and Supabase readers of the Ideas History view.

No I/O: given already-normalized recommendation dicts (carrying ``rec_id`` and
``recorded_at``) and mark dicts (carrying ``rec_id`` and ``checked_at``), the
helper produces the exact shape the dashboard consumes.
"""

from __future__ import annotations

import pytest

from kalshi_trader.ideas_history import join_recommendations_and_marks


RECS = [
    {"rec_id": "rec-a", "recorded_at": "2026-06-03T04:00:00+00:00",
     "ticker": "KXTEST-A", "disposition": "worth_trading", "edge_cents": 22.0},
    {"rec_id": "rec-b", "recorded_at": "2026-06-03T05:00:00+00:00",
     "ticker": "KXTEST-B", "disposition": "approved", "edge_cents": 5.0},
]

MARKS = [
    {"rec_id": "rec-a", "checked_at": "2026-06-03T11:00:00+00:00",
     "current_value_cents": 60.0, "pnl_cents": 6.0, "would_profit": True, "resolved": False},
    {"rec_id": "rec-a", "checked_at": "2026-06-03T04:00:00+00:00",
     "current_value_cents": 54.0, "pnl_cents": 0.0, "would_profit": False, "resolved": False},
    {"rec_id": "rec-b", "checked_at": "2026-06-03T09:00:00+00:00",
     "current_value_cents": 100.0, "pnl_cents": 70.0, "would_profit": True, "resolved": True},
]


def test_joins_marks_to_each_recommendation():
    joined = join_recommendations_and_marks(RECS, MARKS)
    by_id = {idea["rec_id"]: idea for idea in joined}
    assert len(by_id["rec-a"]["marks"]) == 2
    assert len(by_id["rec-b"]["marks"]) == 1


def test_marks_ordered_oldest_first_by_checked_at():
    rec_a = next(i for i in join_recommendations_and_marks(RECS, MARKS) if i["rec_id"] == "rec-a")
    checked = [mark["checked_at"] for mark in rec_a["marks"]]
    assert checked == sorted(checked)


def test_elapsed_seconds_computed_from_recorded_at():
    rec_a = next(i for i in join_recommendations_and_marks(RECS, MARKS) if i["rec_id"] == "rec-a")
    assert rec_a["marks"][0]["elapsed_seconds"] == pytest.approx(0.0)
    assert rec_a["marks"][1]["elapsed_seconds"] == pytest.approx(7 * 3600)


def test_recommendations_sorted_newest_first():
    joined = join_recommendations_and_marks(RECS, MARKS)
    assert [idea["rec_id"] for idea in joined] == ["rec-b", "rec-a"]


def test_original_recommendation_fields_preserved():
    rec_a = next(i for i in join_recommendations_and_marks(RECS, MARKS) if i["rec_id"] == "rec-a")
    assert rec_a["ticker"] == "KXTEST-A"
    assert rec_a["disposition"] == "worth_trading"
    assert rec_a["edge_cents"] == 22.0


def test_each_mark_carries_expected_fields():
    rec_b = next(i for i in join_recommendations_and_marks(RECS, MARKS) if i["rec_id"] == "rec-b")
    mark = rec_b["marks"][0]
    for field in ("checked_at", "current_value_cents", "pnl_cents", "would_profit",
                  "resolved", "elapsed_seconds"):
        assert field in mark
    assert mark["resolved"] is True


def test_recommendation_with_no_marks_gets_empty_list():
    joined = join_recommendations_and_marks(RECS, [])
    assert all(idea["marks"] == [] for idea in joined)


def test_empty_recommendations_returns_empty():
    assert join_recommendations_and_marks([], MARKS) == []


def test_missing_timestamps_yield_none_elapsed():
    recs = [{"rec_id": "rec-x", "recorded_at": None, "ticker": "KXTEST-X"}]
    marks = [{"rec_id": "rec-x", "checked_at": None, "current_value_cents": None,
              "pnl_cents": None, "would_profit": None, "resolved": False}]
    rec_x = join_recommendations_and_marks(recs, marks)[0]
    assert rec_x["marks"][0]["elapsed_seconds"] is None
