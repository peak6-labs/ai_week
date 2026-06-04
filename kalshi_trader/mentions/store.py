"""SQLite archive for the mentions pipeline.

Modeled on :mod:`kalshi_trader.actionability.store`: a local WAL database, one per
machine, that caches the slow-moving inputs the mentions signal needs:

* ``transcripts``  — speaker-attributed transcript text (Fed/CREC/White House/…),
  keyed by a content hash so re-fetching the same document is idempotent. The
  ``norm_text`` column is the punctuation-folded form a phrase is counted against,
  and ``speaker_key`` is the registry-normalized attribution key — get that wrong
  and "how often does *Powell* say recession" silently becomes "how often does
  recession appear on TV at all".
* ``targets``      — speakers/venues discovered from live Kalshi mentions markets,
  so the nightly refresh knows what to pull.
* ``gdelt_cache``  — pre-computed GDELT base rates (7-day TTL), so a scan does not
  re-hit the TV API for every market every cycle.
* ``refresh_log``  — last successful refresh per source, for staleness checks.

The database file (``kalshi_trader/mentions_archive.db``) is gitignored.
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
import time
from datetime import datetime, timedelta, timezone

from kalshi_trader.external.mentions_parser import normalize_for_match

_CREATE_TRANSCRIPTS = """
CREATE TABLE IF NOT EXISTS transcripts (
    doc_id       TEXT    PRIMARY KEY,
    source       TEXT    NOT NULL,
    speaker_raw  TEXT,
    speaker_key  TEXT    NOT NULL,
    venue_type   TEXT,
    event_date   TEXT,
    url          TEXT,
    full_text    TEXT,
    norm_text    TEXT,
    fetched_at   INTEGER NOT NULL
);
"""

# Phrase counting filters on (speaker_key, venue_type), so index them.
_CREATE_TRANSCRIPTS_INDEX = """
CREATE INDEX IF NOT EXISTS idx_transcripts_speaker
    ON transcripts (speaker_key, venue_type);
"""

_CREATE_TARGETS = """
CREATE TABLE IF NOT EXISTS targets (
    speaker_key          TEXT    NOT NULL,
    venue_type           TEXT    NOT NULL,
    speaker_aliases_json TEXT,
    last_seen_ticker     TEXT,
    discovered_at        INTEGER NOT NULL,
    PRIMARY KEY (speaker_key, venue_type)
);
"""

_CREATE_GDELT_CACHE = """
CREATE TABLE IF NOT EXISTS gdelt_cache (
    phrase         TEXT    NOT NULL,
    station        TEXT    NOT NULL,
    base_rate_json TEXT    NOT NULL,
    fetched_at     INTEGER NOT NULL,
    PRIMARY KEY (phrase, station)
);
"""

_CREATE_SCHEDULE = """
CREATE TABLE IF NOT EXISTS schedule (
    meeting_id   TEXT    PRIMARY KEY,
    committee    TEXT,
    chamber      TEXT,
    event_date   TEXT,
    status       TEXT,
    title        TEXT,
    fetched_at   INTEGER NOT NULL
);
"""

_CREATE_REFRESH_LOG = """
CREATE TABLE IF NOT EXISTS refresh_log (
    source       TEXT    PRIMARY KEY,
    refreshed_at INTEGER NOT NULL
);
"""


def make_doc_id(source: str, url: str, speaker_key: str) -> str:
    """Stable content hash for a transcript document → idempotent upsert key."""
    return hashlib.sha256(f"{source}|{url}|{speaker_key}".encode("utf-8")).hexdigest()


class MentionsArchiveStore:
    """SQLite-backed archive of transcripts, targets, GDELT base rates, refreshes.

    Each machine keeps its own local copy at ``db_path``. Pass ``":memory:"`` for
    tests. Idempotent on transcripts (``INSERT OR IGNORE`` by ``doc_id``) so the
    nightly refresh can re-list overlapping windows without duplicating rows.
    """

    DEFAULT_DB_PATH: str = "kalshi_trader/mentions_archive.db"
    GDELT_TTL_SECONDS: int = 7 * 86400          # base rates change slowly
    DEFAULT_RETENTION_DAYS: int = 3 * 365       # prune transcripts older than this

    def __init__(self, db_path: str = DEFAULT_DB_PATH) -> None:
        self._conn = sqlite3.connect(db_path, check_same_thread=False)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=NORMAL")
        self._conn.execute(_CREATE_TRANSCRIPTS)
        self._conn.execute(_CREATE_TRANSCRIPTS_INDEX)
        self._conn.execute(_CREATE_TARGETS)
        self._conn.execute(_CREATE_GDELT_CACHE)
        self._conn.execute(_CREATE_SCHEDULE)
        self._conn.execute(_CREATE_REFRESH_LOG)
        self._conn.commit()

    # ------------------------------------------------------------------
    # Transcripts
    # ------------------------------------------------------------------

    def upsert_transcripts(self, records: list[dict], commit: bool = True) -> int:
        """Insert transcript records idempotently, keeping the fullest text.

        Each record needs at least ``source`` and ``speaker_key``. ``doc_id`` is
        derived from ``(source, url, speaker_key)`` when absent; ``norm_text`` is
        derived from ``full_text`` when absent. De-dupes by ``doc_id``: a repeat of
        the same document is a no-op, **except** when the incoming ``full_text`` is
        longer than what's stored — that updates the row, so a document first saved
        with a thin RSS line self-heals once full text is fetched. Returns the
        number of rows offered.
        """
        rows: list[tuple] = []
        fetched_at = int(time.time())
        for record in records:
            speaker_key = record.get("speaker_key") or ""
            source = record.get("source") or ""
            url = record.get("url") or ""
            full_text = record.get("full_text") or ""
            doc_id = record.get("doc_id") or make_doc_id(source, url, speaker_key)
            norm_text = record.get("norm_text") or normalize_for_match(full_text)
            rows.append((
                doc_id,
                source,
                record.get("speaker_raw") or "",
                speaker_key,
                record.get("venue_type") or "",
                record.get("event_date") or "",
                url,
                full_text,
                norm_text,
                fetched_at,
            ))
        self._conn.executemany(
            "INSERT INTO transcripts VALUES (?,?,?,?,?,?,?,?,?,?) "
            "ON CONFLICT(doc_id) DO UPDATE SET "
            "  full_text=excluded.full_text, "
            "  norm_text=excluded.norm_text, "
            "  fetched_at=excluded.fetched_at "
            "WHERE length(excluded.full_text) > length(transcripts.full_text)",
            rows,
        )
        if commit:
            self._conn.commit()
        return len(rows)

    def count_phrase(
        self,
        speaker_key: str,
        venue_type: str | None,
        phrase: str,
        since: str | None = None,
        until: str | None = None,
    ) -> dict:
        """Count how often a speaker's transcripts contain a phrase.

        Args:
            speaker_key: Registry-normalized attribution key.
            venue_type: Restrict to one corpus (e.g. ``fed_presser``), or None for
                every venue attributed to this speaker.
            phrase: The search phrase; normalized the same way as ``norm_text``.
            since: Optional inclusive ``YYYY-MM-DD`` lower bound on ``event_date``.
            until: Optional **exclusive** ``YYYY-MM-DD`` upper bound on ``event_date``
                (the walk-forward as-of cutoff). Transcripts dated on or after this
                date are excluded, so a backtest predicting an event that occurs on
                ``until`` never counts that event (or anything later) — no look-ahead.

        Returns ``{"document_count": int, "match_count": int}`` — ``document_count``
        is the number of attributed transcripts considered (the base-rate
        denominator) and ``match_count`` is how many contained the phrase.
        """
        normalized_phrase = normalize_for_match(phrase)
        if not speaker_key or not normalized_phrase:
            return {"document_count": 0, "match_count": 0}

        clauses = ["speaker_key = ?"]
        params: list = [speaker_key]
        if venue_type:
            clauses.append("venue_type = ?")
            params.append(venue_type)
        if since:
            clauses.append("event_date >= ?")
            params.append(since)
        if until:
            clauses.append("event_date < ?")  # strict: walk-forward as-of cutoff
            params.append(until)
        where = " AND ".join(clauses)
        result_rows = self._conn.execute(
            f"SELECT norm_text FROM transcripts WHERE {where}", params
        ).fetchall()

        document_count = len(result_rows)
        match_count = sum(
            1 for (norm_text,) in result_rows if normalized_phrase in (norm_text or "")
        )
        return {"document_count": document_count, "match_count": match_count}

    def count_phrase_global(
        self,
        venue_type: str | None,
        phrase: str,
        since: str | None = None,
        until: str | None = None,
    ) -> dict:
        """Count phrase occurrences across **all speakers** (the global denominator).

        Identical to :meth:`count_phrase` but without the ``speaker_key`` filter, so
        it yields the unconditional base rate the speaker-attributed rate is tested
        against in the corpus-premise backtest (global base rate ⊂ speaker base rate).
        ``until`` is the same exclusive walk-forward as-of cutoff.

        Returns ``{"document_count": int, "match_count": int}``.
        """
        normalized_phrase = normalize_for_match(phrase)
        if not normalized_phrase:
            return {"document_count": 0, "match_count": 0}

        clauses: list[str] = []
        params: list = []
        if venue_type:
            clauses.append("venue_type = ?")
            params.append(venue_type)
        if since:
            clauses.append("event_date >= ?")
            params.append(since)
        if until:
            clauses.append("event_date < ?")  # strict: walk-forward as-of cutoff
            params.append(until)
        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        result_rows = self._conn.execute(
            f"SELECT norm_text FROM transcripts{where}", params
        ).fetchall()

        document_count = len(result_rows)
        match_count = sum(
            1 for (norm_text,) in result_rows if normalized_phrase in (norm_text or "")
        )
        return {"document_count": document_count, "match_count": match_count}

    def list_transcript_events(self, venue_type: str | None = None) -> list[dict]:
        """Return every transcript as a lightweight event dict (read-only).

        Used by the corpus-premise backtest, which needs the full attributed
        timeline (``speaker_key``, ``event_date``, ``norm_text``) to run its own
        strict walk-forward scoring. Restrict to one ``venue_type`` to pool within
        a venue (so document-length differences across venues do not bias the rate).
        """
        clauses: list[str] = []
        params: list = []
        if venue_type:
            clauses.append("venue_type = ?")
            params.append(venue_type)
        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        result_rows = self._conn.execute(
            f"SELECT speaker_key, venue_type, event_date, norm_text FROM transcripts{where}",
            params,
        ).fetchall()
        return [
            {"speaker_key": speaker_key, "venue_type": venue_type_value,
             "event_date": event_date, "norm_text": norm_text}
            for (speaker_key, venue_type_value, event_date, norm_text) in result_rows
        ]

    def distinct_venue_types(self) -> list[str]:
        """Non-empty distinct ``venue_type`` values present in the archive."""
        result_rows = self._conn.execute(
            "SELECT DISTINCT venue_type FROM transcripts"
        ).fetchall()
        return sorted(venue_type for (venue_type,) in result_rows if venue_type)

    def distinct_speaker_count(self, venue_type: str | None = None) -> int:
        """How many distinct speakers the archive holds (optionally within a venue)."""
        if venue_type:
            result_rows = self._conn.execute(
                "SELECT COUNT(DISTINCT speaker_key) FROM transcripts WHERE venue_type = ?",
                (venue_type,),
            ).fetchone()
        else:
            result_rows = self._conn.execute(
                "SELECT COUNT(DISTINCT speaker_key) FROM transcripts"
            ).fetchone()
        return int(result_rows[0] or 0)

    # ------------------------------------------------------------------
    # Targets
    # ------------------------------------------------------------------

    def upsert_targets(self, targets: list[dict], commit: bool = True) -> None:
        """Record speakers/venues discovered from live markets (idempotent)."""
        discovered_at = int(time.time())
        rows = [
            (
                target.get("speaker_key") or "",
                target.get("venue_type") or "",
                json.dumps(target.get("aliases", [])),
                target.get("last_seen_ticker") or "",
                discovered_at,
            )
            for target in targets
        ]
        self._conn.executemany(
            "INSERT OR REPLACE INTO targets VALUES (?,?,?,?,?)", rows
        )
        if commit:
            self._conn.commit()

    def get_targets(self) -> list[dict]:
        result_rows = self._conn.execute(
            "SELECT speaker_key, venue_type, speaker_aliases_json, last_seen_ticker, "
            "discovered_at FROM targets"
        ).fetchall()
        return [
            {
                "speaker_key": row[0],
                "venue_type": row[1],
                "aliases": json.loads(row[2]) if row[2] else [],
                "last_seen_ticker": row[3],
                "discovered_at": int(row[4]),
            }
            for row in result_rows
        ]

    # ------------------------------------------------------------------
    # GDELT base-rate cache (7-day TTL)
    # ------------------------------------------------------------------

    def put_gdelt_base_rate(
        self, phrase: str, station: str, base_rate: dict, commit: bool = True
    ) -> None:
        self._conn.execute(
            "INSERT OR REPLACE INTO gdelt_cache VALUES (?,?,?,?)",
            (normalize_for_match(phrase), station, json.dumps(base_rate), int(time.time())),
        )
        if commit:
            self._conn.commit()

    def get_gdelt_base_rate(self, phrase: str, station: str) -> dict | None:
        """Return a cached base rate, or None when missing or past the 7-day TTL."""
        row = self._conn.execute(
            "SELECT base_rate_json, fetched_at FROM gdelt_cache WHERE phrase=? AND station=?",
            (normalize_for_match(phrase), station),
        ).fetchone()
        if not row:
            return None
        base_rate_json, fetched_at = row
        if (time.time() - int(fetched_at)) > self.GDELT_TTL_SECONDS:
            return None
        try:
            return json.loads(base_rate_json)
        except (json.JSONDecodeError, TypeError):
            return None

    # ------------------------------------------------------------------
    # Hearing schedule (congress.gov committee meetings)
    # ------------------------------------------------------------------

    def upsert_schedule(self, meetings: list[dict], commit: bool = True) -> int:
        """Upsert committee-meeting schedule records (idempotent by meeting_id)."""
        fetched_at = int(time.time())
        rows = [
            (
                meeting.get("meeting_id") or "",
                meeting.get("committee") or "",
                meeting.get("chamber") or "",
                meeting.get("event_date") or "",
                meeting.get("status") or "",
                meeting.get("title") or "",
                fetched_at,
            )
            for meeting in meetings
            if meeting.get("meeting_id")
        ]
        self._conn.executemany(
            "INSERT OR REPLACE INTO schedule VALUES (?,?,?,?,?,?,?)", rows
        )
        if commit:
            self._conn.commit()
        return len(rows)

    def get_schedule(self, chamber: str | None = None) -> list[dict]:
        """Return schedule records, optionally filtered to one chamber."""
        if chamber:
            result_rows = self._conn.execute(
                "SELECT meeting_id, committee, chamber, event_date, status, title "
                "FROM schedule WHERE lower(chamber)=?",
                (chamber.lower(),),
            ).fetchall()
        else:
            result_rows = self._conn.execute(
                "SELECT meeting_id, committee, chamber, event_date, status, title FROM schedule"
            ).fetchall()
        return [
            {
                "meeting_id": row[0],
                "committee": row[1],
                "chamber": row[2],
                "event_date": row[3],
                "status": row[4],
                "title": row[5],
            }
            for row in result_rows
        ]

    # ------------------------------------------------------------------
    # Refresh bookkeeping + retention
    # ------------------------------------------------------------------

    def _last_refreshed(self, source: str) -> int:
        row = self._conn.execute(
            "SELECT refreshed_at FROM refresh_log WHERE source=?", (source,)
        ).fetchone()
        return int(row[0]) if row else 0

    def is_stale(self, source: str, ttl_seconds: int) -> bool:
        """True when ``source`` has not had a successful refresh within ``ttl_seconds``."""
        return (time.time() - self._last_refreshed(source)) > ttl_seconds

    def mark_refreshed(self, source: str, commit: bool = True) -> None:
        """Record a *successful* refresh of ``source``.

        Only call this after a source actually committed rows, so a failed source
        stays stale and is retried on the next run.
        """
        self._conn.execute(
            "INSERT OR REPLACE INTO refresh_log VALUES (?,?)", (source, int(time.time()))
        )
        if commit:
            self._conn.commit()

    def prune(self, max_age_days: int | None = None, commit: bool = True) -> int:
        """Delete transcripts whose ``event_date`` is older than the retention window.

        ``event_date`` is stored as ``YYYY-MM-DD`` so a lexicographic comparison is
        a date comparison. Rows with a blank ``event_date`` are left untouched.
        Returns the number of rows deleted.
        """
        retention_days = self.DEFAULT_RETENTION_DAYS if max_age_days is None else max_age_days
        cutoff = datetime.now(tz=timezone.utc) - timedelta(days=retention_days)
        cutoff_date = cutoff.strftime("%Y-%m-%d")
        cursor = self._conn.execute(
            "DELETE FROM transcripts WHERE event_date != '' AND event_date < ?",
            (cutoff_date,),
        )
        if commit:
            self._conn.commit()
        return cursor.rowcount

    def close(self) -> None:
        self._conn.close()
