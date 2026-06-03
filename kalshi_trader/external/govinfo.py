"""GovInfo Congressional Record (CREC) client — speaker-attributed floor speeches.

The Government Publishing Office's GovInfo API publishes the daily Congressional
Record (CREC collection). Each day's package contains granules — individual floor
statements — and a granule's ``members`` array attributes it to the speaking
member(s). That attribution is what turns "how often does *this senator* say
<phrase> on the floor" into a real base rate (``venue_type=congress_floor``).

Authorized by the same free ``api.data.gov`` key as congress.gov
(:data:`kalshi_trader.config.DATA_GOV_API_KEY`); without it the client returns
``[]``. Fetch and parsing are separated: the ``parse_*`` helpers are pure and
tested on fixtures; the client walks packages → granules → summary → text.

NOTE: the exact GovInfo field names are coded to the published API shape; the
parsers are deliberately tolerant (``.get`` with fallbacks). Verify against a live
response once a key is configured — see the refresh CLI.
API docs: https://api.govinfo.gov/docs/
"""
from __future__ import annotations

import asyncio
import html as html_lib
import re
import ssl
from urllib.parse import urlencode

import aiohttp

from kalshi_trader.external.speaker_registry import normalize_speaker_key, resolve_speaker

GOVINFO_BASE = "https://api.govinfo.gov"
_HEADERS = {"User-Agent": "kalshi-trader/1.0", "Accept": "application/json"}
_FETCH_CONCURRENCY = 4

# Granule classes that are actual floor speech (skip procedural/front-matter).
FLOOR_GRANULE_CLASSES = frozenset({"HOUSE", "SENATE"})

_HTML_TAG = re.compile(r"<[^>]+>")


def _build_ssl_context() -> ssl.SSLContext:
    try:
        import truststore
        return truststore.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    except Exception:  # pragma: no cover - truststore optional
        return ssl.create_default_context()


def parse_crec_packages(collection_json: dict) -> list[dict]:
    """Pull ``{packageId, dateIssued}`` from a CREC collection response."""
    packages: list[dict] = []
    for package in (collection_json or {}).get("packages", []) or []:
        package_id = str(package.get("packageId") or "")
        if package_id:
            packages.append({
                "packageId": package_id,
                "dateIssued": str(package.get("dateIssued") or "")[:10],
            })
    return packages


def parse_crec_granules(granules_json: dict) -> list[dict]:
    """Pull floor-speech granule stubs from a package's granules response.

    Keeps only HOUSE/SENATE granules (skips Daily Digest / front matter).
    """
    stubs: list[dict] = []
    for granule in (granules_json or {}).get("granules", []) or []:
        granule_id = str(granule.get("granuleId") or "")
        granule_class = str(granule.get("granuleClass") or "")
        if granule_id and granule_class in FLOOR_GRANULE_CLASSES:
            stubs.append({
                "granuleId": granule_id,
                "granuleClass": granule_class,
                "title": str(granule.get("title") or ""),
            })
    return stubs


def _member_speaker(member: dict) -> str:
    """Extract a usable speaker name from a granule member object.

    GovInfo names members "Last, First"; reduce to the surname (before the comma)
    so the registry resolves it the same way a market's parsed speaker does.
    """
    name = str(member.get("memberName") or member.get("name") or "").strip()
    if "," in name:
        return name.split(",", 1)[0].strip()
    return name


def parse_crec_granule_summary(summary_json: dict) -> dict | None:
    """Parse a granule summary → ``{speaker_raw, date, txt_link, title}`` or None.

    Returns None when the granule names no member (procedural text we can't
    attribute), so unattributed floor text never pollutes the corpus.
    """
    if not isinstance(summary_json, dict):
        return None
    members = summary_json.get("members") or []
    speaker_raw = ""
    for member in members:
        if isinstance(member, dict):
            speaker_raw = _member_speaker(member)
            if speaker_raw:
                break
    if not speaker_raw:
        return None
    download = summary_json.get("download") or {}
    return {
        "speaker_raw": speaker_raw,
        "date": str(summary_json.get("dateIssued") or "")[:10],
        "txt_link": str(download.get("txtLink") or ""),
        "title": str(summary_json.get("title") or ""),
    }


def crec_text_to_plain(raw_text: str) -> str:
    """Reduce CREC granule text (HTML or pre-formatted) to plain text."""
    if not raw_text:
        return ""
    text = html_lib.unescape(_HTML_TAG.sub(" ", raw_text))
    return re.sub(r"\s+", " ", text).strip()


def build_crec_record(granule_id: str, summary: dict, text: str) -> dict | None:
    """Assemble a congress_floor transcript record, or None when there's no text."""
    plain_text = crec_text_to_plain(text)
    if not plain_text:
        return None
    speaker_raw = summary.get("speaker_raw") or ""
    speaker_key = resolve_speaker(speaker_raw).speaker_key if speaker_raw else ""
    if not speaker_key:
        speaker_key = normalize_speaker_key(speaker_raw)
    return {
        "source": "govinfo_crec",
        "speaker_raw": speaker_raw,
        "speaker_key": speaker_key,
        "venue_type": "congress_floor",
        "event_date": summary.get("date") or "",
        "url": f"{GOVINFO_BASE}/packages/granules/{granule_id}",
        "full_text": plain_text,
        "doc_id": f"govinfo_crec|{granule_id}",
    }


class GovInfoClient:
    """Async client for GovInfo CREC floor speeches (fail-soft, [] without key)."""

    def __init__(self, api_key: str | None = None) -> None:
        from kalshi_trader import config
        self._api_key = api_key if api_key is not None else config.DATA_GOV_API_KEY
        self._session: aiohttp.ClientSession | None = None

    def _ensure_session(self) -> aiohttp.ClientSession:
        if self._session is None:
            connector = aiohttp.TCPConnector(ssl=_build_ssl_context())
            self._session = aiohttp.ClientSession(headers=_HEADERS, connector=connector)
        return self._session

    async def _get_json(self, url: str, params: dict) -> dict:
        session = self._ensure_session()
        query = {**params, "api_key": self._api_key}
        async with session.get(
            f"{url}?{urlencode(query)}", timeout=aiohttp.ClientTimeout(total=30)
        ) as api_response:
            api_response.raise_for_status()
            return await api_response.json()

    async def _get_text(self, url: str) -> str:
        session = self._ensure_session()
        joiner = "&" if "?" in url else "?"
        async with session.get(
            f"{url}{joiner}{urlencode({'api_key': self._api_key})}",
            timeout=aiohttp.ClientTimeout(total=30),
        ) as api_response:
            api_response.raise_for_status()
            return await api_response.text()

    async def get_crec_records(
        self, since: str, max_packages: int = 3, max_granules_per_package: int = 60
    ) -> list[dict]:
        """Fetch speaker-attributed CREC floor-speech records since ``since`` (ISO date).

        Walks packages → granules → granule summary → granule text, fail-soft at
        every level. ``since`` is a ``YYYY-MM-DD``; bounded by ``max_packages`` /
        ``max_granules_per_package`` to keep the call volume sane.
        """
        if not self._api_key:
            return []
        start = f"{since}T00:00:00Z"
        try:
            collection = await self._get_json(
                f"{GOVINFO_BASE}/collections/CREC/{start}", {"offset": 0, "pageSize": max_packages}
            )
        except (aiohttp.ClientError, OSError):
            return []

        records: list[dict] = []
        for package in parse_crec_packages(collection)[:max_packages]:
            try:
                granules_json = await self._get_json(
                    f"{GOVINFO_BASE}/packages/{package['packageId']}/granules",
                    {"offset": 0, "pageSize": max_granules_per_package},
                )
            except (aiohttp.ClientError, OSError):
                continue
            granules = parse_crec_granules(granules_json)[:max_granules_per_package]
            records.extend(await self._fetch_package_records(package["packageId"], granules))
        return records

    async def _fetch_package_records(self, package_id: str, granules: list[dict]) -> list[dict]:
        concurrency_semaphore = asyncio.Semaphore(_FETCH_CONCURRENCY)

        async def _one(granule: dict) -> dict | None:
            async with concurrency_semaphore:
                try:
                    summary_json = await self._get_json(
                        f"{GOVINFO_BASE}/packages/{package_id}/granules/{granule['granuleId']}/summary",
                        {},
                    )
                except (aiohttp.ClientError, OSError):
                    return None
                summary = parse_crec_granule_summary(summary_json)
                if not summary or not summary.get("txt_link"):
                    return None
                try:
                    text = await self._get_text(summary["txt_link"])
                except (aiohttp.ClientError, OSError):
                    return None
            return build_crec_record(granule["granuleId"], summary, text)

        results = await asyncio.gather(*[_one(granule) for granule in granules])
        return [record for record in results if record]

    async def close(self) -> None:
        if self._session:
            await self._session.close()
            self._session = None


__all__ = [
    "GovInfoClient", "parse_crec_packages", "parse_crec_granules",
    "parse_crec_granule_summary", "build_crec_record", "crec_text_to_plain",
]
