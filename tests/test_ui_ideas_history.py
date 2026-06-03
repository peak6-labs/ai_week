"""Tests for the Ideas History endpoint and its data shaping.

Covers ``kalshi_trader.paper.recommendations_with_marks`` (recommendation +
marks join, elapsed-time computation, ordering) and the read-only
``GET /api/ideas/history`` endpoint.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from kalshi_trader.ui.config_manager import ConfigManager
from kalshi_trader.ui.state import TradingState


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def make_client(tmp_path: Path) -> TestClient:
    from kalshi_trader.ui.server import create_app

    state = TradingState()
    manager = ConfigManager(path=tmp_path / "cfg.json")
    return TestClient(create_app(trading_state=state, config_manager=manager))


def write_paper_store(monkeypatch, tmp_path: Path, recommendations: list[dict], marks: list[dict]) -> None:
    """Point the paper module at a temp JSONL store seeded with given rows."""
    from kalshi_trader import paper

    recs_file = tmp_path / "recommendations.jsonl"
    marks_file = tmp_path / "marks.jsonl"
    recs_file.write_text("".join(json.dumps(row) + "\n" for row in recommendations))
    marks_file.write_text("".join(json.dumps(row) + "\n" for row in marks))
    monkeypatch.setattr(paper, "_RECS_FILE", recs_file)
    monkeypatch.setattr(paper, "_MARKS_FILE", marks_file)


SAMPLE_RECS = [
    {
        "rec_id": "rec-a", "cycle_ts": "20260603T040000Z",
        "recorded_at": "2026-06-03T04:00:00+00:00", "ticker": "KXTEST-A",
        "side": "no", "entry_price_cents": 54.0, "predicted_prob": 0.76,
        "edge_cents": 22.0, "n_sources": 6, "sources": ["microstructure", "kalshi_bias"],
        "category": "elections", "status": "open", "disposition": "worth_trading",
    },
    {
        "rec_id": "rec-b", "cycle_ts": "20260603T050000Z",
        "recorded_at": "2026-06-03T05:00:00+00:00", "ticker": "KXTEST-B",
        "side": "yes", "entry_price_cents": 30.0, "predicted_prob": 0.40,
        "edge_cents": 5.0, "n_sources": 2, "sources": ["sportsbook"],
        "category": "sports", "status": "resolved", "disposition": "approved",
    },
]

SAMPLE_MARKS = [
    # rec-a: two snapshots, +0h and +7h
    {"rec_id": "rec-a", "ticker": "KXTEST-A", "checked_at": "2026-06-03T04:00:00+00:00",
     "current_value_cents": 54.0, "pnl_cents": 0.0, "would_profit": False, "resolved": False},
    {"rec_id": "rec-a", "ticker": "KXTEST-A", "checked_at": "2026-06-03T11:00:00+00:00",
     "current_value_cents": 60.0, "pnl_cents": 6.0, "would_profit": True, "resolved": False},
    # rec-b: one resolved snapshot
    {"rec_id": "rec-b", "ticker": "KXTEST-B", "checked_at": "2026-06-03T09:00:00+00:00",
     "current_value_cents": 100.0, "pnl_cents": 70.0, "would_profit": True, "resolved": True},
]


# ---------------------------------------------------------------------------
# paper.recommendations_with_marks
# ---------------------------------------------------------------------------

class TestRecommendationsWithMarks:
    def test_joins_marks_to_each_recommendation(self, monkeypatch, tmp_path):
        from kalshi_trader import paper
        write_paper_store(monkeypatch, tmp_path, SAMPLE_RECS, SAMPLE_MARKS)
        joined = paper.recommendations_with_marks()
        by_id = {idea["rec_id"]: idea for idea in joined}
        assert len(by_id["rec-a"]["marks"]) == 2
        assert len(by_id["rec-b"]["marks"]) == 1

    def test_marks_ordered_by_checked_at(self, monkeypatch, tmp_path):
        from kalshi_trader import paper
        write_paper_store(monkeypatch, tmp_path, SAMPLE_RECS, SAMPLE_MARKS)
        rec_a = next(i for i in paper.recommendations_with_marks() if i["rec_id"] == "rec-a")
        checked = [mark["checked_at"] for mark in rec_a["marks"]]
        assert checked == sorted(checked)

    def test_elapsed_seconds_computed(self, monkeypatch, tmp_path):
        from kalshi_trader import paper
        write_paper_store(monkeypatch, tmp_path, SAMPLE_RECS, SAMPLE_MARKS)
        rec_a = next(i for i in paper.recommendations_with_marks() if i["rec_id"] == "rec-a")
        assert rec_a["marks"][0]["elapsed_seconds"] == pytest.approx(0.0)
        assert rec_a["marks"][1]["elapsed_seconds"] == pytest.approx(7 * 3600)

    def test_resolved_flag_preserved(self, monkeypatch, tmp_path):
        from kalshi_trader import paper
        write_paper_store(monkeypatch, tmp_path, SAMPLE_RECS, SAMPLE_MARKS)
        rec_b = next(i for i in paper.recommendations_with_marks() if i["rec_id"] == "rec-b")
        assert rec_b["marks"][0]["resolved"] is True

    def test_recommendations_sorted_newest_first(self, monkeypatch, tmp_path):
        from kalshi_trader import paper
        write_paper_store(monkeypatch, tmp_path, SAMPLE_RECS, SAMPLE_MARKS)
        joined = paper.recommendations_with_marks()
        assert [idea["rec_id"] for idea in joined] == ["rec-b", "rec-a"]

    def test_recommendation_with_no_marks_gets_empty_list(self, monkeypatch, tmp_path):
        from kalshi_trader import paper
        write_paper_store(monkeypatch, tmp_path, SAMPLE_RECS, [])
        joined = paper.recommendations_with_marks()
        assert all(idea["marks"] == [] for idea in joined)

    def test_original_fields_preserved(self, monkeypatch, tmp_path):
        from kalshi_trader import paper
        write_paper_store(monkeypatch, tmp_path, SAMPLE_RECS, SAMPLE_MARKS)
        rec_a = next(i for i in paper.recommendations_with_marks() if i["rec_id"] == "rec-a")
        assert rec_a["ticker"] == "KXTEST-A"
        assert rec_a["disposition"] == "worth_trading"
        assert rec_a["edge_cents"] == 22.0
        assert rec_a["sources"] == ["microstructure", "kalshi_bias"]

    def test_empty_store_returns_empty(self, monkeypatch, tmp_path):
        from kalshi_trader import paper
        write_paper_store(monkeypatch, tmp_path, [], [])
        assert paper.recommendations_with_marks() == []


# ---------------------------------------------------------------------------
# GET /api/ideas/history
# ---------------------------------------------------------------------------

class TestIdeasHistoryEndpoint:
    """These verify local-store shaping through the endpoint, so they force the
    Supabase source unavailable and exercise the local fallback. Source-selection
    itself is covered by ``TestIdeasHistorySourcing``."""

    @pytest.fixture(autouse=True)
    def _force_local_source(self, monkeypatch):
        from kalshi_trader import db

        async def _unavailable():
            raise RuntimeError("Supabase disabled in this test")
        monkeypatch.setattr(db, "recommendations_with_marks", _unavailable)

    def test_returns_200(self, monkeypatch, tmp_path):
        write_paper_store(monkeypatch, tmp_path, SAMPLE_RECS, SAMPLE_MARKS)
        client = make_client(tmp_path)
        assert client.get("/api/ideas/history").status_code == 200

    def test_body_has_ideas_list(self, monkeypatch, tmp_path):
        write_paper_store(monkeypatch, tmp_path, SAMPLE_RECS, SAMPLE_MARKS)
        client = make_client(tmp_path)
        body = client.get("/api/ideas/history").json()
        assert "ideas" in body
        assert len(body["ideas"]) == 2

    def test_each_idea_carries_marks_timeline(self, monkeypatch, tmp_path):
        write_paper_store(monkeypatch, tmp_path, SAMPLE_RECS, SAMPLE_MARKS)
        client = make_client(tmp_path)
        ideas = client.get("/api/ideas/history").json()["ideas"]
        rec_a = next(i for i in ideas if i["rec_id"] == "rec-a")
        assert len(rec_a["marks"]) == 2
        first_mark = rec_a["marks"][0]
        for field in ("checked_at", "current_value_cents", "pnl_cents", "resolved", "elapsed_seconds"):
            assert field in first_mark

    def test_empty_store_returns_empty_list(self, monkeypatch, tmp_path):
        write_paper_store(monkeypatch, tmp_path, [], [])
        client = make_client(tmp_path)
        assert client.get("/api/ideas/history").json() == {"ideas": []}


# ---------------------------------------------------------------------------
# Endpoint sourcing: Supabase first, local JSONL fallback
# ---------------------------------------------------------------------------

class TestIdeasHistorySourcing:
    SUPABASE_IDEAS = [{"rec_id": "sb-1", "recorded_at": "2026-06-03T06:00:00+00:00",
                       "ticker": "KXSUPA-1", "marks": []}]

    def test_uses_supabase_when_available(self, monkeypatch, tmp_path):
        from kalshi_trader import db
        # Local store has the SAMPLE recs; Supabase returns something distinct.
        write_paper_store(monkeypatch, tmp_path, SAMPLE_RECS, SAMPLE_MARKS)

        async def fake_supabase():
            return self.SUPABASE_IDEAS
        monkeypatch.setattr(db, "recommendations_with_marks", fake_supabase)

        ideas = make_client(tmp_path).get("/api/ideas/history").json()["ideas"]
        assert [idea["rec_id"] for idea in ideas] == ["sb-1"]

    def test_falls_back_to_local_when_supabase_raises(self, monkeypatch, tmp_path):
        from kalshi_trader import db
        write_paper_store(monkeypatch, tmp_path, SAMPLE_RECS, SAMPLE_MARKS)

        async def boom():
            raise RuntimeError("supabase unreachable")
        monkeypatch.setattr(db, "recommendations_with_marks", boom)

        ideas = make_client(tmp_path).get("/api/ideas/history").json()["ideas"]
        # local data served instead — the SAMPLE recs
        assert {idea["rec_id"] for idea in ideas} == {"rec-a", "rec-b"}
