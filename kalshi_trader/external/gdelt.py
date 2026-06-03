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
        station: str = "CSPAN",
        timespan: str = "FULL",
    ) -> dict:
        """Fetch the per-period mention timeline for a phrase on a station.

        Args:
            phrase: Word or phrase to search the closed-caption stream for
                (exact-string match; phrases capped at 5 words by the API).
            station: TV station code (CSPAN / CSPAN2 / CSPAN3 carry hearings).
            timespan: GDELT timespan; "FULL" returns the full monthly history
                since 2009 — the base rate we want.

        Returns:
            {
              "station": str,
              "phrase": str,
              "points": [{"date": "YYYYMMDDTHHMMSSZ", "value": float}, ...],
              "fetched_at": datetime (UTC, aware),
            }
            ``value`` is the percent of 15-second clips matching the phrase in
            that period (datanorm=perc). ``points`` is empty when GDELT has no
            coverage for the query.
        """
        # GDELT requires every query to name a station; it rejects bare
        # parentheses, so the phrase is passed unwrapped alongside the filter.
        query = f"{phrase.strip()} station:{station}"
        timeline = await self._get_json(
            {
                "query": query,
                "mode": "timelinevol",
                "format": "json",
                "datanorm": "perc",
                "timespan": timespan,
            }
        )
        return {
            "station": station,
            "phrase": phrase.strip(),
            "points": extract_points(timeline, station),
            "fetched_at": datetime.now(tz=timezone.utc),
        }

    async def close(self) -> None:
        if self._session:
            await self._session.close()
            self._session = None


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
