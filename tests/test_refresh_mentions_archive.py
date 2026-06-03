"""Tests for kalshi_trader/refresh_mentions_archive.py — orchestration + discovery."""
from __future__ import annotations

import json

import pytest

from kalshi_trader import refresh_mentions_archive as refresh
from kalshi_trader.mentions.store import MentionsArchiveStore


@pytest.fixture
def store():
    archive = MentionsArchiveStore(db_path=":memory:")
    yield archive
    archive.close()


# ---------------------------------------------------------------------------
# Per-source isolation + refresh_log only on success
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_failed_source_left_stale_others_commit(store):
    async def good(store_, clients):
        store_.upsert_transcripts([{
            "source": "fed", "speaker_key": "powell", "venue_type": "fed_speech",
            "event_date": "2024-01-15", "url": "u", "full_text": "talk of recession",
        }])
        return 1, 1

    async def bad(store_, clients):
        raise RuntimeError("network boom")

    sources = {"good": good, "bad": bad}
    ttls = {"good": 3600, "bad": 3600}
    summary = await refresh.run_refresh(store, clients=None, sources=sources, ttls=ttls)

    assert summary["good"][0] == "ok"
    assert summary["bad"][0] == "failed"
    # good committed its rows and is no longer stale; bad stays stale for retry.
    assert store.is_stale("good", 3600) is False
    assert store.is_stale("bad", 3600) is True
    assert store.count_phrase("powell", None, "recession")["document_count"] == 1


@pytest.mark.asyncio
async def test_if_stale_skips_fresh_sources(store):
    calls = {"n": 0}

    async def counting(store_, clients):
        calls["n"] += 1
        return 0, 0

    store.mark_refreshed("counting")  # pretend it just ran
    summary = await refresh.run_refresh(
        store, clients=None, if_stale=True,
        sources={"counting": counting}, ttls={"counting": 3600},
    )
    assert summary["counting"][0] == "skipped"
    assert calls["n"] == 0  # the refresher was never invoked


@pytest.mark.asyncio
async def test_full_run_marks_refreshed(store):
    async def ok(store_, clients):
        return 2, 2

    await refresh.run_refresh(store, clients=None, sources={"src": ok}, ttls={"src": 3600})
    assert store.is_stale("src", 3600) is False


# ---------------------------------------------------------------------------
# Target discovery
# ---------------------------------------------------------------------------

def test_discover_targets_parses_mention_markets():
    markets = [
        {"ticker": "KX1", "title": "Will Jerome Powell say 'recession' in his next hearing?"},
        {"ticker": "KX2", "title": "Will it rain in Chicago tomorrow?"},  # not a mention
        {"ticker": "KX3", "title": "Will Donald Trump say 'tariffs' this week?"},
    ]
    targets = refresh.discover_targets(markets)
    keys = {t["speaker_key"] for t in targets}
    assert "powell" in keys
    assert "trump" in keys
    # Powell resolves to fed venues; one of his rows carries a fed venue.
    powell_venues = {t["venue_type"] for t in targets if t["speaker_key"] == "powell"}
    assert "fed_speech" in powell_venues or "fed_presser" in powell_venues


def test_discover_targets_dedupes_speaker_venue():
    markets = [
        {"ticker": "A", "title": "Will Powell say 'recession' this week?"},
        {"ticker": "B", "title": "Will Powell say 'inflation' this week?"},
    ]
    targets = refresh.discover_targets(markets)
    pairs = [(t["speaker_key"], t["venue_type"]) for t in targets]
    assert len(pairs) == len(set(pairs))  # no duplicate (speaker, venue) rows


def test_discover_targets_then_upsert(store):
    targets = refresh.discover_targets(
        [{"ticker": "KX1", "title": "Will Jerome Powell say 'recession' this week?"}]
    )
    store.upsert_targets(targets)
    stored = {t["speaker_key"] for t in store.get_targets()}
    assert "powell" in stored


def test_load_markets_tolerates_shapes(tmp_path):
    path = tmp_path / "snap.json"
    path.write_text(json.dumps({"markets": [
        {"ticker": "KX1", "title": "Will Powell say 'recession' this week?"},
        {"nope": 1},
    ]}))
    markets = refresh.load_markets(str(path))
    assert len(markets) == 1
    assert markets[0]["ticker"] == "KX1"


def test_load_markets_missing_file_returns_empty():
    assert refresh.load_markets("/no/such/file.json") == []
