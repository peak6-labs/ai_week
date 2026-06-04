"""YouTube Data API v3 client — Peacock teaser discovery for the Love Island signal.

A Love Island market ("will a bombshell enter tonight", "who gets dumped") is
often given away by the official **pre-episode teaser** ("First Look") that the
network posts the day before. This client finds those teasers and reads their
title/description/publish-time so the signal can react to what the teaser reveals.

One free Google Cloud key (:data:`kalshi_trader.config.YOUTUBE_API_KEY`)
authorizes it; without the key the client returns ``[]``/``{}`` and the Love
Island signal degrades gracefully (X-only, or no signal).

Network fetch and parsing are separated: :func:`parse_search_results` and
:func:`parse_video_list` are pure and tested on fixtures; the client orchestrates
the GETs fail-soft.

Transcript caveat: the Data API only lets you DOWNLOAD caption tracks you own
(OAuth-as-owner), so a network's caption text is not fetchable with an API key.
:meth:`YouTubeClient.fetch_transcript` therefore uses the optional, **unofficial**
``youtube-transcript-api`` library (which scrapes YouTube's internal transcript
endpoint — no OAuth needed). It is best-effort and returns ``""`` whenever the
library is absent, a track is unavailable, or the fetch fails — callers fall back
to the title + description. This is an unofficial path (TOS-adjacent); it is the
only no-OAuth way to read a third party's captions.

API docs: https://developers.google.com/youtube/v3/docs
"""
from __future__ import annotations

import asyncio
import ssl
from typing import Any
from urllib.parse import urlencode

import aiohttp

from kalshi_trader import config

YOUTUBE_DATA_API_BASE = "https://www.googleapis.com/youtube/v3"
_HEADERS = {"User-Agent": "kalshi-trader/1.0", "Accept": "application/json"}

# Official channels that carry Love Island "First Look" teasers. Pass one of these
# as ``channel_id`` to scope a search to the network's own uploads (filters out the
# flood of fan re-uploads, whose clickbait titles falsely match event keywords). An
# empty channel_id means "search all of YouTube for the query". Resolved live via
# the Data API (search.list type=channel) on 2026-06-04.
LOVE_ISLAND_USA_CHANNEL_ID = "UCVV9-BZ_8EybNWtbvnF8DHw"   # "Love Island USA" (official)
LOVE_ISLAND_UK_CHANNEL_ID = "UCgM5P6QGHmrvu5fDPx79mug"    # "Love Island" (ITV, official)
PEACOCK_CHANNEL_ID = "UCPgMAS8woHJ_o_OZdTR7kcQ"           # "Peacock" (official; carries LI USA)


def _build_ssl_context() -> ssl.SSLContext:
    """SSL context trusting the OS store (corporate-proxy safe; see x_client.py)."""
    try:
        import truststore
        return truststore.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    except Exception:  # pragma: no cover - truststore optional
        return ssl.create_default_context()


def _normalize_video(video_id: str, snippet: dict) -> dict[str, str]:
    """Flatten a YouTube ``snippet`` into the record shape the signal consumes."""
    snippet = snippet or {}
    return {
        "video_id": str(video_id or ""),
        "title": str(snippet.get("title") or ""),
        "description": str(snippet.get("description") or ""),
        "published_at": str(snippet.get("publishedAt") or ""),
        "channel_title": str(snippet.get("channelTitle") or ""),
        "channel_id": str(snippet.get("channelId") or ""),
    }


def parse_search_results(search_json: dict) -> list[dict[str, str]]:
    """Pull normalized video records from a ``search.list`` response.

    ``search.list`` items carry the video id under ``id.videoId``; channel/playlist
    results (which have no ``videoId``) are skipped.
    """
    records: list[dict[str, str]] = []
    for item in (search_json or {}).get("items", []) or []:
        if not isinstance(item, dict):
            continue
        identifier = item.get("id")
        video_id = identifier.get("videoId") if isinstance(identifier, dict) else None
        if not video_id:
            continue
        records.append(_normalize_video(video_id, item.get("snippet") or {}))
    return records


def parse_video_list(videos_json: dict) -> list[dict[str, str]]:
    """Pull normalized video records from a ``videos.list`` response.

    ``videos.list`` items carry the video id as a top-level string ``id``.
    """
    records: list[dict[str, str]] = []
    for item in (videos_json or {}).get("items", []) or []:
        if not isinstance(item, dict):
            continue
        video_id = item.get("id")
        if not video_id:
            continue
        records.append(_normalize_video(str(video_id), item.get("snippet") or {}))
    return records


class YouTubeClient:
    """Async YouTube Data API v3 client (fail-soft).

    Returns ``[]``/``{}``/``""`` immediately when no ``YOUTUBE_API_KEY`` is set.
    """

    def __init__(self, api_key: str | None = None) -> None:
        self._api_key = api_key if api_key is not None else config.YOUTUBE_API_KEY
        self._session: aiohttp.ClientSession | None = None
        # Lazily-built requests.Session for the (synchronous) transcript library,
        # with a truststore SSL adapter so it works behind the corporate proxy.
        self._transcript_http = None

    def _ensure_session(self) -> aiohttp.ClientSession:
        if self._session is None:
            connector = aiohttp.TCPConnector(ssl=_build_ssl_context())
            self._session = aiohttp.ClientSession(headers=_HEADERS, connector=connector)
        return self._session

    async def _get_json(self, url: str, params: dict) -> dict:
        session = self._ensure_session()
        query = {**params, "key": self._api_key}
        async with session.get(
            f"{url}?{urlencode(query)}", timeout=aiohttp.ClientTimeout(total=30)
        ) as api_response:
            api_response.raise_for_status()
            return await api_response.json()

    async def search_videos(
        self,
        query: str,
        channel_id: str | None = None,
        published_before: str | None = None,
        published_after: str | None = None,
        order: str = "relevance",
        max_results: int = 10,
    ) -> list[dict[str, str]]:
        """Search YouTube for videos matching ``query``.

        ``published_before`` (an RFC 3339 timestamp, e.g. ``2026-06-04T00:00:00Z``)
        is the no-look-ahead lever for the backtest: set it to the episode's air
        time so only the pre-episode teaser is returned, never a post-episode recap.
        Returns ``[]`` when no API key is configured or on any error.
        """
        if not self._api_key:
            return []
        params: dict[str, Any] = {
            "part": "snippet",
            "q": query,
            "type": "video",
            "order": order,
            "maxResults": max_results,
        }
        if channel_id:
            params["channelId"] = channel_id
        if published_before:
            params["publishedBefore"] = published_before
        if published_after:
            params["publishedAfter"] = published_after
        try:
            search_json = await self._get_json(f"{YOUTUBE_DATA_API_BASE}/search", params)
        except (aiohttp.ClientError, OSError):
            return []
        return parse_search_results(search_json)

    async def list_videos(self, video_ids: list[str]) -> list[dict[str, str]]:
        """Fetch full snippet metadata for specific video ids (cheaper than search).

        Returns ``[]`` when no API key is configured, when ``video_ids`` is empty,
        or on any error.
        """
        if not self._api_key or not video_ids:
            return []
        params = {"part": "snippet", "id": ",".join(video_ids)}
        try:
            videos_json = await self._get_json(f"{YOUTUBE_DATA_API_BASE}/videos", params)
        except (aiohttp.ClientError, OSError):
            return []
        return parse_video_list(videos_json)

    def _ensure_transcript_http(self):
        """Lazily build the requests.Session for the transcript library.

        ``youtube-transcript-api`` uses ``requests``, which behind the corporate
        proxy needs the OS trust store (see the SSL note in x_client.py).
        ``truststore.inject_into_ssl()`` is truststore's idiomatic way to make
        stdlib-ssl-based libraries (requests/urllib3) verify against the OS store —
        it keeps full certificate verification (unlike a misconfigured ssl_context
        adapter, which silently downgrades to unverified) and leaves the aiohttp
        clients (which pass their own context) untouched.
        """
        if self._transcript_http is None:
            import requests
            try:
                import truststore
                truststore.inject_into_ssl()
            except Exception:  # pragma: no cover - truststore optional
                pass
            self._transcript_http = requests.Session()
        return self._transcript_http

    def _fetch_transcript_sync(self, video_id: str, language: str) -> str:
        """Blocking transcript fetch via the optional youtube-transcript-api lib."""
        try:
            from youtube_transcript_api import YouTubeTranscriptApi
        except ImportError:
            return ""  # optional dependency not installed → degrade gracefully
        try:
            transcript_api = YouTubeTranscriptApi(http_client=self._ensure_transcript_http())
            fetched = transcript_api.fetch(video_id, languages=[language, "en", "en-US"])
            return " ".join(snippet.text for snippet in fetched).strip()
        except Exception:
            # No transcript, transcripts disabled, region block, network — all soft.
            return ""

    async def fetch_transcript(self, video_id: str, language: str = "en") -> str:
        """Best-effort caption text for a video, or ``""`` when unavailable.

        Uses the unofficial ``youtube-transcript-api`` (no OAuth) run in an executor
        so the synchronous library does not block the event loop. Returns ``""`` when
        the library is absent or the fetch fails — callers must treat that as "no
        transcript" and fall back to title + description, never as evidence.
        """
        if not video_id:
            return ""
        return await asyncio.get_running_loop().run_in_executor(
            None, self._fetch_transcript_sync, video_id, language
        )

    async def __aenter__(self) -> "YouTubeClient":
        return self

    async def __aexit__(self, *_: object) -> None:
        await self.close()

    async def close(self) -> None:
        if self._session:
            await self._session.close()
            self._session = None
        if self._transcript_http is not None:
            self._transcript_http.close()
            self._transcript_http = None
