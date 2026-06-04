"""Tests for kalshi_trader/mentions/store.py (in-memory / tmp_path SQLite)."""
from __future__ import annotations

import time

import pytest

from kalshi_trader.mentions.store import MentionsArchiveStore, make_doc_id


@pytest.fixture
def store():
    archive = MentionsArchiveStore(db_path=":memory:")
    yield archive
    archive.close()


def _transcript(**overrides) -> dict:
    record = {
        "source": "fed",
        "speaker_raw": "Chair Jerome Powell",
        "speaker_key": "powell",
        "venue_type": "fed_speech",
        "event_date": "2024-01-15",
        "url": "https://example.gov/powell20240115.htm",
        "full_text": "Today I want to discuss the risk of recession.",
    }
    record.update(overrides)
    return record


# ---------------------------------------------------------------------------
# Transcript upsert + dedup
# ---------------------------------------------------------------------------

def test_upsert_then_count_phrase_match(store):
    store.upsert_transcripts([_transcript()])
    result = store.count_phrase("powell", "fed_speech", "recession")
    assert result == {"document_count": 1, "match_count": 1}


def test_upsert_is_idempotent_by_doc_id(store):
    record = _transcript()
    store.upsert_transcripts([record])
    store.upsert_transcripts([record])  # same source+url+speaker_key → same doc_id
    result = store.count_phrase("powell", None, "recession")
    assert result["document_count"] == 1


def test_upsert_self_heals_thin_text_with_longer_text(store):
    # First stored with only a venue line (enrichment failed)...
    store.upsert_transcripts([_transcript(full_text="Speech. At the Economic Club.")])
    assert store.count_phrase("powell", None, "recession")["match_count"] == 0
    # ...then re-stored with the full body (same doc_id) → row updates.
    store.upsert_transcripts([_transcript(full_text="Today I discuss the risk of recession at length.")])
    result = store.count_phrase("powell", None, "recession")
    assert result == {"document_count": 1, "match_count": 1}


def test_upsert_does_not_replace_full_text_with_thinner(store):
    store.upsert_transcripts([_transcript(full_text="Today I discuss the risk of recession at length.")])
    # A later thin re-fetch must NOT clobber the good text.
    store.upsert_transcripts([_transcript(full_text="Speech.")])
    assert store.count_phrase("powell", None, "recession")["match_count"] == 1


def test_make_doc_id_is_deterministic_and_distinct():
    a = make_doc_id("fed", "u1", "powell")
    b = make_doc_id("fed", "u1", "powell")
    c = make_doc_id("fed", "u2", "powell")
    assert a == b
    assert a != c


# ---------------------------------------------------------------------------
# count_phrase: normalization, venue filter, since filter, denominator
# ---------------------------------------------------------------------------

def test_count_phrase_is_punctuation_and_case_insensitive(store):
    store.upsert_transcripts([_transcript(full_text="The word: RECESSION, in caps.")])
    assert store.count_phrase("powell", None, "recession")["match_count"] == 1


def test_count_phrase_counts_documents_as_denominator(store):
    store.upsert_transcripts([
        _transcript(url="u1", full_text="talk of recession ahead"),
        _transcript(url="u2", full_text="no mention of the r-word here"),
        _transcript(url="u3", full_text="recession again"),
    ])
    result = store.count_phrase("powell", None, "recession")
    assert result == {"document_count": 3, "match_count": 2}


def test_count_phrase_venue_filter(store):
    store.upsert_transcripts([
        _transcript(url="u1", venue_type="fed_speech", full_text="recession one"),
        _transcript(url="u2", venue_type="fed_presser", full_text="recession two"),
    ])
    assert store.count_phrase("powell", "fed_presser", "recession")["document_count"] == 1
    assert store.count_phrase("powell", None, "recession")["document_count"] == 2


def test_count_phrase_since_filter(store):
    store.upsert_transcripts([
        _transcript(url="u1", event_date="2022-01-01", full_text="old recession"),
        _transcript(url="u2", event_date="2024-05-01", full_text="recent recession"),
    ])
    result = store.count_phrase("powell", None, "recession", since="2023-01-01")
    assert result["document_count"] == 1


def test_count_phrase_until_excludes_on_or_after_cutoff(store):
    # The as-of cutoff is strict (event_date < until): a transcript dated exactly on
    # the cutoff date must be excluded, so a backtest never sees the event it predicts.
    store.upsert_transcripts([
        _transcript(url="u1", event_date="2024-05-01", full_text="earlier recession"),
        _transcript(url="u2", event_date="2024-05-10", full_text="on-cutoff recession"),
        _transcript(url="u3", event_date="2024-05-20", full_text="later recession"),
    ])
    result = store.count_phrase("powell", None, "recession", until="2024-05-10")
    assert result == {"document_count": 1, "match_count": 1}  # only the 2024-05-01 row


def test_count_phrase_since_and_until_window(store):
    store.upsert_transcripts([
        _transcript(url="u1", event_date="2022-01-01", full_text="too old recession"),
        _transcript(url="u2", event_date="2024-05-05", full_text="in window recession"),
        _transcript(url="u3", event_date="2024-09-01", full_text="too new recession"),
    ])
    result = store.count_phrase(
        "powell", None, "recession", since="2024-01-01", until="2024-08-01"
    )
    assert result == {"document_count": 1, "match_count": 1}


def test_count_phrase_global_ignores_speaker(store):
    # Two different speakers, one phrase: the global count spans both speakers, while
    # the speaker-scoped count sees only that speaker's transcripts.
    store.upsert_transcripts([
        _transcript(speaker_key="powell", url="p1", full_text="powell on recession"),
        _transcript(speaker_key="yellen", url="y1", full_text="yellen on recession"),
        _transcript(speaker_key="yellen", url="y2", full_text="yellen on growth"),
    ])
    global_count = store.count_phrase_global(None, "recession")
    assert global_count == {"document_count": 3, "match_count": 2}
    assert store.count_phrase("powell", None, "recession")["document_count"] == 1


def test_count_phrase_global_respects_until(store):
    store.upsert_transcripts([
        _transcript(speaker_key="powell", url="p1", event_date="2024-01-01", full_text="recession early"),
        _transcript(speaker_key="yellen", url="y1", event_date="2024-06-01", full_text="recession late"),
    ])
    result = store.count_phrase_global(None, "recession", until="2024-03-01")
    assert result == {"document_count": 1, "match_count": 1}


def test_count_phrase_unknown_speaker_or_blank_phrase_is_zero(store):
    store.upsert_transcripts([_transcript()])
    assert store.count_phrase("nobody", None, "recession")["document_count"] == 0
    assert store.count_phrase("powell", None, "")["document_count"] == 0


# ---------------------------------------------------------------------------
# GDELT base-rate cache (7-day TTL)
# ---------------------------------------------------------------------------

def test_gdelt_cache_round_trip(store):
    base_rate = {"fraction_with_mention": 0.42, "period_count": 30, "n_effective": 12.5}
    store.put_gdelt_base_rate("recession", "CSPAN", base_rate)
    assert store.get_gdelt_base_rate("recession", "CSPAN") == base_rate


def test_gdelt_cache_miss_returns_none(store):
    assert store.get_gdelt_base_rate("nope", "CSPAN") is None


def test_gdelt_cache_respects_ttl(store):
    store.put_gdelt_base_rate("recession", "CSPAN", {"fraction_with_mention": 0.5})
    # Backdate the cache row beyond the 7-day TTL.
    stale_ts = int(time.time()) - (MentionsArchiveStore.GDELT_TTL_SECONDS + 10)
    store._conn.execute("UPDATE gdelt_cache SET fetched_at=?", (stale_ts,))
    store._conn.commit()
    assert store.get_gdelt_base_rate("recession", "CSPAN") is None


# ---------------------------------------------------------------------------
# Refresh bookkeeping
# ---------------------------------------------------------------------------

def test_is_stale_before_first_refresh(store):
    assert store.is_stale("fed", ttl_seconds=3600) is True


def test_mark_refreshed_clears_staleness(store):
    store.mark_refreshed("fed")
    assert store.is_stale("fed", ttl_seconds=3600) is False


# ---------------------------------------------------------------------------
# Targets
# ---------------------------------------------------------------------------

def test_targets_upsert_and_read(store):
    store.upsert_targets([
        {"speaker_key": "powell", "venue_type": "fed_speech",
         "aliases": ["Chair Powell"], "last_seen_ticker": "KXMENTION-1"},
    ])
    targets = store.get_targets()
    assert len(targets) == 1
    assert targets[0]["speaker_key"] == "powell"
    assert targets[0]["aliases"] == ["Chair Powell"]


def test_targets_upsert_is_idempotent_on_key(store):
    target = {"speaker_key": "powell", "venue_type": "fed_speech", "last_seen_ticker": "A"}
    store.upsert_targets([target])
    store.upsert_targets([{**target, "last_seen_ticker": "B"}])
    targets = store.get_targets()
    assert len(targets) == 1
    assert targets[0]["last_seen_ticker"] == "B"


# ---------------------------------------------------------------------------
# Retention prune
# ---------------------------------------------------------------------------

def test_prune_removes_old_transcripts_only(store):
    store.upsert_transcripts([
        _transcript(url="old", event_date="2010-01-01"),
        _transcript(url="new", event_date="2024-01-01"),
        _transcript(url="undated", event_date=""),
    ])
    # ~10-year window brackets the two dated rows: drops 2010, keeps 2024.
    deleted = store.prune(max_age_days=3650)
    assert deleted == 1  # only the 2010 row
    # The undated row is left untouched; the recent row remains.
    assert store.count_phrase("powell", None, "recession")["document_count"] == 2
