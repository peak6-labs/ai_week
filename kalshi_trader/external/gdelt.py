"""Async client for the GDELT 2.0 Television API.

The TV API exposes the Internet Archive TV News Archive's closed-caption stream
for 150+ stations since 2009, including the full CSPAN / CSPAN2 / CSPAN3 archive
(the channels that carry congressional hearings and floor proceedings). For a
keyword/phrase query it reports the *percent of 15-second clips* that match,
which is the historical base rate we turn into a probability for a "mentions"
market ("Will <person> say <word> in a hearing/briefing").

Free, no API key, JSON. See docs/research/non_financial_sources.md (source #1).
"""
from __future__ import annotations

import ssl
from datetime import datetime, timezone
from urllib.parse import urlencode

import aiohttp

GDELT_TV_BASE = "https://api.gdeltproject.org/api/v2/tv/tv"
_HEADERS = {"User-Agent": "kalshi-trader/1.0 scorley@peak6.com", "Accept": "application/json"}

# Short recent window for near-real-time detection. GDELT's TV captions lag a few
# hours, so "1d" is the freshest free signal; the matching bucket's own timestamp
# (not now) is what stamps the live SignalEstimate.
LIVE_TIMESPAN = "1d"


def _build_ssl_context() -> ssl.SSLContext:
    """SSL context that trusts the OS trust store.

    Behind the corporate proxy (Zscaler) the upstream cert chain ends in a
    self-signed root that only lives in the system trust store, not certifi's
    bundle — so a default aiohttp context fails verification. truststore reads
    the OS store and fixes this (same reason db.py injects truststore for httpx).
    Falls back to the default context if truststore is unavailable.
    """
    try:
        import truststore
        return truststore.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    except Exception:  # pragma: no cover - truststore optional
        return ssl.create_default_context()


class GDELTClient:
    def __init__(self) -> None:
        self._session: aiohttp.ClientSession | None = None

    async def _get_json(self, params: dict) -> dict:
        if self._session is None:
            connector = aiohttp.TCPConnector(ssl=_build_ssl_context())
            self._session = aiohttp.ClientSession(headers=_HEADERS, connector=connector)
        url = f"{GDELT_TV_BASE}?{urlencode(params)}"
        async with self._session.get(url, timeout=aiohttp.ClientTimeout(total=30)) as api_response:
            api_response.raise_for_status()
            text_body = await api_response.text()
            return parse_timeline_json(text_body)

    async def get_mention_timeline(
        self,
        phrase: str,
        stations: list[str] | str = "CSPAN",
        timespan: str = "FULL",
        live: bool = False,
    ) -> dict:
        """Fetch the per-period mention timeline for a phrase on one or more stations.

        Args:
            phrase: Word or phrase to search the closed-caption stream for
                (exact-string match; phrases capped at 5 words by the API).
            stations: A single TV station code, or a list of them. CSPAN / CSPAN2 /
                CSPAN3 carry hearings; a national-news set (CNN/FOXNEWS/MSNBC) is
                the right corroborator for executive voices. The speaker registry
                supplies this set, so the query follows *who* is speaking rather
                than always defaulting to CSPAN.
            timespan: GDELT timespan; "FULL" returns the full monthly history
                since 2009 — the base rate we want.
            live: When True, override ``timespan`` with :data:`LIVE_TIMESPAN` (the
                last ~day) for near-real-time detection during an open market
                window. The freshest free option; combine with
                ``mentions_parser.latest_mention_point`` to find the clip time.

        Returns:
            {
              "station": str,             # the primary (first) station
              "stations": list[str],      # every station queried
              "phrase": str,
              "points": [{"date": "YYYYMMDDTHHMMSSZ", "value": float}, ...],
              "fetched_at": datetime (UTC, aware),
            }
            ``value`` is the percent of 15-second clips matching the phrase in
            that period (datanorm=perc). For a multi-station query the per-station
            series are merged by date (presence-oriented: the max value across
            stations), so ``points`` answers "did the phrase appear anywhere in the
            speaker's coverage this period". Empty when GDELT has no coverage.
        """
        station_list = _normalize_stations(stations)
        query = _build_station_query(phrase, station_list)
        timeline = await self._get_json(
            {
                "query": query,
                "mode": "timelinevol",
                "format": "json",
                "datanorm": "perc",
                "timespan": LIVE_TIMESPAN if live else timespan,
            }
        )
        if len(station_list) == 1:
            points = extract_points(timeline, station_list[0])
        else:
            points = merge_station_points(timeline)
        return {
            "station": station_list[0],
            "stations": station_list,
            "phrase": phrase.strip(),
            "points": points,
            "fetched_at": datetime.now(tz=timezone.utc),
        }

    async def close(self) -> None:
        if self._session:
            await self._session.close()
            self._session = None


def _normalize_stations(stations: list[str] | str) -> list[str]:
    """Coerce the ``stations`` argument to a non-empty list of station codes."""
    if isinstance(stations, str):
        station_list = [stations]
    else:
        station_list = [station for station in stations if station]
    return station_list or ["CSPAN"]


def _build_station_query(phrase: str, station_list: list[str]) -> str:
    """Build the GDELT query string for a phrase scoped to one or more stations.

    GDELT requires every query to name a station and rejects bare parentheses, so
    the phrase is passed unwrapped alongside the station filter. Multiple stations
    are OR-combined inside parentheses.
    """
    cleaned_phrase = phrase.strip()
    if len(station_list) == 1:
        return f"{cleaned_phrase} station:{station_list[0]}"
    station_clause = " OR ".join(f"station:{station}" for station in station_list)
    return f"{cleaned_phrase} ({station_clause})"


def merge_station_points(timeline: dict) -> list[dict]:
    """Merge every per-station series in a timeline into one date-keyed series.

    A multi-station query returns one series per station. For a base rate we only
    care whether the phrase appeared *anywhere* in the speaker's coverage in a
    period, so values are merged by date taking the max across stations. Returns
    points sorted ascending by date.
    """
    merged_by_date: dict[str, float] = {}
    for series in timeline.get("timeline", []) or []:
        for point in series.get("data", []):
            date = point.get("date", "")
            if not date:
                continue
            value = float(point.get("value", 0.0) or 0.0)
            merged_by_date[date] = max(merged_by_date.get(date, 0.0), value)
    return [{"date": date, "value": merged_by_date[date]} for date in sorted(merged_by_date)]


def parse_timeline_json(text_body: str) -> dict:
    """Parse a GDELT timeline response body into a dict.

    GDELT returns ``{}`` for a valid-but-empty query and a short plain-text
    error string for a malformed query. Both map to an empty timeline here so
    callers never raise on a content-level problem.
    """
    import json

    stripped = text_body.strip()
    if not stripped or not stripped.startswith("{"):
        return {}
    try:
        return json.loads(stripped)
    except json.JSONDecodeError:
        return {}


def extract_points(timeline: dict, station: str) -> list[dict]:
    """Pull the (date, value) series for ``station`` out of a timeline payload."""
    for series in timeline.get("timeline", []) or []:
        if series.get("series") == station or series.get("series") == station.upper():
            return [
                {"date": point.get("date", ""), "value": float(point.get("value", 0.0) or 0.0)}
                for point in series.get("data", [])
            ]
    # Single-station queries sometimes return one unlabeled series — use it.
    series_list = timeline.get("timeline", []) or []
    if len(series_list) == 1:
        return [
            {"date": point.get("date", ""), "value": float(point.get("value", 0.0) or 0.0)}
            for point in series_list[0].get("data", [])
        ]
    return []
