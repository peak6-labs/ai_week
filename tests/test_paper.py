"""Tests for paper-trade P&L math (kalshi_trader/paper.py)."""
from __future__ import annotations

import pytest

from kalshi_trader import paper


@pytest.fixture
def isolated_store(tmp_path, monkeypatch):
    """Point the JSONL store at a temp dir so tests never touch real data."""
    recs_file = tmp_path / "recommendations.jsonl"
    marks_file = tmp_path / "marks.jsonl"
    monkeypatch.setattr(paper, "_RECS_FILE", recs_file)
    monkeypatch.setattr(paper, "_MARKS_FILE", marks_file)
    return tmp_path


def test_entry_price_yes_is_ask_no_is_complement_of_bid() -> None:
    assert paper.entry_price_cents("yes", yes_bid=38, yes_ask=40) == 40
    assert paper.entry_price_cents("no", yes_bid=38, yes_ask=40) == 62  # 100-38


def test_mark_value_sells_into_the_book() -> None:
    # YES marked at the bid you could sell into; NO at 100-ask.
    assert paper.mark_value_cents("yes", yes_bid=44, yes_ask=46) == 44
    assert paper.mark_value_cents("no", yes_bid=44, yes_ask=46) == 54


def test_settle_value_pays_100_to_the_winning_side() -> None:
    assert paper.settle_value_cents("yes", resolved_yes=True) == 100
    assert paper.settle_value_cents("yes", resolved_yes=False) == 0
    assert paper.settle_value_cents("no", resolved_yes=False) == 100
    assert paper.settle_value_cents("no", resolved_yes=True) == 0


def test_compute_mark_unresolved_profit() -> None:
    # Bought YES at 40, market now 50/52 → sell into 50 bid → +10c.
    mark = paper.compute_mark("yes", entry_cents=40, yes_bid=50, yes_ask=52, resolved_yes=None)
    assert mark["resolved"] is False
    assert mark["pnl_cents"] == 10
    assert mark["would_profit"] is True


def test_compute_mark_resolved_winner() -> None:
    mark = paper.compute_mark("no", entry_cents=62, yes_bid=0, yes_ask=0, resolved_yes=False)
    assert mark["resolved"] is True
    assert mark["current_value_cents"] == 100
    assert mark["pnl_cents"] == 38


def test_compute_mark_no_price_unresolved_returns_none() -> None:
    mark = paper.compute_mark("yes", entry_cents=40, yes_bid=None, yes_ask=None, resolved_yes=None)
    assert mark["pnl_cents"] is None
    assert mark["resolved"] is False


# --- disposition + backtest scorecards --------------------------------------

def test_record_recommendation_defaults_disposition_to_candidate(isolated_store) -> None:
    paper.record_recommendation(
        cycle_ts="T", ticker="KX-A", side="yes", entry_cents=40,
        predicted_prob=0.6, edge_cents=8, n_sources=2, sources=["kalshi_bias", "x_grok"],
    )
    rows = paper.load_recommendations()
    assert len(rows) == 1
    # Lifecycle status stays "open"; disposition is the new classification axis.
    assert rows[0]["status"] == "open"
    assert rows[0]["disposition"] == "candidate"


def test_record_recommendation_stores_explicit_disposition(isolated_store) -> None:
    paper.record_recommendation(
        cycle_ts="T", ticker="KX-A", side="yes", entry_cents=40,
        predicted_prob=0.6, edge_cents=8, n_sources=2, sources=["kalshi_bias"],
        disposition="insufficient_edge",
    )
    assert paper.load_recommendations()[0]["disposition"] == "insufficient_edge"


def test_performance_by_disposition_groups_marks(isolated_store) -> None:
    approved = paper.record_recommendation(
        cycle_ts="T", ticker="KX-WIN", side="yes", entry_cents=40,
        predicted_prob=0.6, edge_cents=8, n_sources=2, sources=["x_grok"],
        disposition="approved")
    rejected = paper.record_recommendation(
        cycle_ts="T", ticker="KX-LOSE", side="yes", entry_cents=40,
        predicted_prob=0.52, edge_cents=2, n_sources=2, sources=["x_grok"],
        disposition="insufficient_edge")
    paper.append_mark(approved, "KX-WIN", {"current_value_cents": 55, "pnl_cents": 15,
                                           "would_profit": True, "resolved": False})
    paper.append_mark(rejected, "KX-LOSE", {"current_value_cents": 30, "pnl_cents": -10,
                                            "would_profit": False, "resolved": False})
    by_disposition = paper.performance_by_disposition()
    assert by_disposition["approved"]["wins"] == 1
    assert by_disposition["approved"]["avg_pnl_cents"] == 15
    assert by_disposition["insufficient_edge"]["wins"] == 0
    assert by_disposition["insufficient_edge"]["avg_pnl_cents"] == -10


def test_performance_by_edge_bucket_brackets_at_the_threshold(isolated_store) -> None:
    # Two recs: one just under the 5c fee-adjusted bar, one well over it.
    under = paper.record_recommendation(
        cycle_ts="T", ticker="KX-UNDER", side="yes", entry_cents=48,
        predicted_prob=0.51, edge_cents=3.0, n_sources=2, sources=["x_grok"],
        disposition="insufficient_edge")
    over = paper.record_recommendation(
        cycle_ts="T", ticker="KX-OVER", side="yes", entry_cents=40,
        predicted_prob=0.60, edge_cents=12.0, n_sources=2, sources=["x_grok"],
        disposition="approved")
    paper.append_mark(under, "KX-UNDER", {"pnl_cents": -2, "would_profit": False, "resolved": False})
    paper.append_mark(over, "KX-OVER", {"pnl_cents": 9, "would_profit": True, "resolved": False})
    by_bucket = paper.performance_by_edge_bucket()
    # The 3c rec lands in a sub-5c bucket; the 12c rec lands in a 10c+ bucket.
    assert by_bucket["[2.5,5)"]["marked"] == 1
    assert by_bucket["[2.5,5)"]["wins"] == 0
    assert by_bucket["[10,inf)"]["marked"] == 1
    assert by_bucket["[10,inf)"]["wins"] == 1


def test_append_mark_stamps_checked_at_into_the_mark_dict(isolated_store) -> None:
    """append_mark must stamp checked_at into the passed dict so the same value
    can be mirrored to Supabase, and return it."""
    rec_id = paper.record_recommendation(
        cycle_ts="c1", ticker="KX-STAMP", side="yes", entry_cents=40.0,
        predicted_prob=0.6, edge_cents=8, n_sources=2, sources=["kalshi_bias", "x_grok"])
    mark = {"current_value_cents": 45.0, "pnl_cents": 5.0, "would_profit": True, "resolved": False}
    returned = paper.append_mark(rec_id, "KX-STAMP", mark)
    # stamped into the caller's dict + returned
    assert "checked_at" in mark
    assert returned["checked_at"] == mark["checked_at"]
    # and persisted with the same value
    stored = next(m for m in paper.recommendations_with_marks() if m["rec_id"] == rec_id)["marks"][0]
    assert stored["checked_at"] == mark["checked_at"]


def test_append_mark_preserves_explicit_checked_at(isolated_store) -> None:
    """If the caller already set checked_at, append_mark keeps it."""
    rec_id = paper.record_recommendation(
        cycle_ts="c1", ticker="KX-EXPL", side="no", entry_cents=60.0,
        predicted_prob=0.4, edge_cents=8, n_sources=2, sources=["kalshi_bias", "x_grok"])
    mark = {"checked_at": "2026-06-03T12:00:00+00:00", "pnl_cents": 1.0,
            "would_profit": True, "resolved": False}
    paper.append_mark(rec_id, "KX-EXPL", mark)
    assert mark["checked_at"] == "2026-06-03T12:00:00+00:00"
