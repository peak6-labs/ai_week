"""Tests for the paper-track CLI's backtest recorder (scripts/paper_track.py).

Focuses on `record-scored`, which records EVERY scored 2+ source market — not
just the risk-approved slate — so rejected/insufficient-edge candidates can be
marked to market and the edge bar judged. Supabase mirroring is best-effort and
wrapped in try/except, so it no-ops cleanly when the DB is unreachable.
"""
from __future__ import annotations

import importlib.util
import json
from argparse import Namespace
from pathlib import Path

import pytest

from kalshi_trader import paper

_MODULE_PATH = Path(__file__).resolve().parent.parent / "scripts" / "paper_track.py"
_spec = importlib.util.spec_from_file_location("paper_track", _MODULE_PATH)
paper_track = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(paper_track)


@pytest.fixture
def isolated_store(tmp_path, monkeypatch):
    monkeypatch.setattr(paper, "_RECS_FILE", tmp_path / "recommendations.jsonl")
    monkeypatch.setattr(paper, "_MARKS_FILE", tmp_path / "marks.jsonl")
    # Neutralise the Supabase mirror so the test never reaches the network.
    monkeypatch.setattr(paper_track, "_mirror_recommendations", lambda rows: None)
    return tmp_path


def _write_scored(tmp_path, markets) -> str:
    path = tmp_path / "scored.json"
    path.write_text(json.dumps(markets))
    return str(path)


def test_record_scored_tags_disposition_and_prices_each_side(isolated_store, tmp_path) -> None:
    scored = [
        # Cleared the edge bar but not on the approved slate → worth_trading.
        {"ticker": "KX-WT", "side": "yes", "yes_ask": 40, "yes_bid": 38,
         "combined_probability": 0.6, "fee_adjusted_edge": 8.0, "worth_trading": True,
         "n_sources": 2, "sources": ["kalshi_bias", "x_grok"], "category": "politics"},
        # Below the edge bar → insufficient_edge, NO side priced off the bid.
        {"ticker": "KX-IE", "side": "no", "yes_ask": 60, "yes_bid": 58,
         "combined_probability": 0.55, "fee_adjusted_edge": 2.0, "worth_trading": False,
         "n_sources": 2, "sources": ["microstructure", "kalshi_bias"], "category": "econ"},
        # Single source — excluded by --min-sources 2.
        {"ticker": "KX-1SRC", "side": "yes", "yes_ask": 30, "yes_bid": 28,
         "combined_probability": 0.5, "fee_adjusted_edge": 1.0, "worth_trading": False,
         "n_sources": 1, "sources": ["kalshi_bias"], "category": "econ"},
    ]
    args = Namespace(scored_file=_write_scored(tmp_path, scored), cycle_ts="T",
                     exclude_file="", min_sources=2)
    paper_track._cmd_record_scored(args)

    rows = {r["ticker"]: r for r in paper.load_recommendations()}
    assert set(rows) == {"KX-WT", "KX-IE"}  # single-source one skipped
    assert rows["KX-WT"]["disposition"] == "worth_trading"
    assert rows["KX-WT"]["entry_price_cents"] == 40  # YES entry = yes_ask
    assert rows["KX-IE"]["disposition"] == "insufficient_edge"
    assert rows["KX-IE"]["entry_price_cents"] == 42  # NO entry = 100 - yes_bid(58)
    assert rows["KX-IE"]["predicted_prob"] == pytest.approx(0.45)  # 1 - combined_probability


def test_record_scored_excludes_approved_slate(isolated_store, tmp_path) -> None:
    scored = [
        {"ticker": "KX-APPROVED", "side": "yes", "yes_ask": 40, "yes_bid": 38,
         "combined_probability": 0.6, "fee_adjusted_edge": 9.0, "worth_trading": True,
         "n_sources": 2, "sources": ["x_grok", "kalshi_bias"], "category": "politics"},
        {"ticker": "KX-OTHER", "side": "yes", "yes_ask": 45, "yes_bid": 43,
         "combined_probability": 0.5, "fee_adjusted_edge": 1.0, "worth_trading": False,
         "n_sources": 2, "sources": ["microstructure", "kalshi_bias"], "category": "econ"},
    ]
    exclude = tmp_path / "approved.json"
    exclude.write_text(json.dumps([{"ticker": "KX-APPROVED"}]))
    args = Namespace(scored_file=_write_scored(tmp_path, scored), cycle_ts="T",
                     exclude_file=str(exclude), min_sources=2)
    paper_track._cmd_record_scored(args)

    tickers = {r["ticker"] for r in paper.load_recommendations()}
    assert tickers == {"KX-OTHER"}  # approved slate recorded separately, not here
