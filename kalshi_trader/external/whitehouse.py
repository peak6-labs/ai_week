"""White House briefing-room scraper — speaker-attributed spoken remarks.

RISKIEST SOURCE — flagged on purpose. whitehouse.gov is a WordPress site whose
markup and URL scheme change without notice; every selector here is best-effort
and the whole client **fails soft** (any miss → ``[]``), so a layout change
degrades the mentions corpus to government-only rather than breaking a refresh.

It harvests only *spoken* transcripts ("Remarks by …", "Press Briefing by …") —
written messages/releases are skipped — so it contributes a modest corpus
(``venue_type=wh_briefing``). The speaker is parsed from the article slug and
normalized through the speaker registry.

ALL the brittle, layout-coupled logic lives in the pure helpers below
(:func:`extract_article_links`, :func:`is_spoken_remarks`, :func:`speaker_from_url`,
:func:`extract_article_body`, :func:`extract_article_date`); the async client just
fetches and stitches them together.
"""
from __future__ import annotations

import asyncio
import html as html_lib
import re
from datetime import datetime

import aiohttp

from kalshi_trader.external.speaker_registry import normalize_speaker_key, resolve_speaker

WH_BASE = "https://www.whitehouse.gov"
WH_NEWS_PATH = "/news/page/{page}/"
_HEADERS = {"User-Agent": "Mozilla/5.0 kalshi-trader/1.0", "Accept": "text/html"}
_FETCH_CONCURRENCY = 4

# Editable reference list of recent White House press secretaries — the speaker is
# normally parsed straight from the slug, but this documents who "press secretary"
# resolves to and is a place to pin attribution if a slug omits the name.
WH_PRESS_SECRETARIES: tuple[str, ...] = ("Karoline Leavitt",)

# A dated article URL: /<section>/<YYYY>/<MM>/<slug>/
_ARTICLE_LINK = re.compile(r'href="(https://www\.whitehouse\.gov/[a-z-]+/\d{4}/\d{2}/[^"]+/)"')

# Slugs that denote a spoken transcript (vs a written message/release).
_SPOKEN_SLUG = re.compile(r"/(remarks|press-briefing|press-gaggle|press-conference|interview)[a-z-]*-by-")

# "…-by-<who>-(on|at|before|…)-<topic>" → capture <who>.
_SPEAKER_FROM_SLUG = re.compile(
    r"-by-(.+?)-(?:on|at|before|during|after|following|regarding|in|to|with|and|of)-"
)

_ARTICLE_BODY_START = 'class="entry-content'
_ARTICLE_BODY_END_MARKERS = ('id="comments', 'class="wp-block-post-comments', 'class="footer', "</main>")
_SCRIPT_STYLE = re.compile(r"<(script|style)\b.*?</\1>", re.DOTALL | re.IGNORECASE)
_HTML_TAG = re.compile(r"<[^>]+>")
_PUBLISHED_TIME = re.compile(r'property="article:published_time"\s+content="([^"]+)"')
_TIME_TAG = re.compile(r'<time[^>]*datetime="([^"]+)"')


def _build_ssl_context():
    import ssl
    try:
        import truststore
        return truststore.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    except Exception:  # pragma: no cover - truststore optional
        return ssl.create_default_context()


def extract_article_links(listing_html: str) -> list[str]:
    """All dated article URLs on a briefing-room listing page, de-duplicated."""
    return list(dict.fromkeys(_ARTICLE_LINK.findall(listing_html or "")))


def is_spoken_remarks(url: str) -> bool:
    """True when the URL slug denotes a spoken transcript (remarks / briefing)."""
    return bool(_SPOKEN_SLUG.search(url or ""))


def speaker_from_url(url: str) -> str:
    """Parse the speaker name out of a "remarks-by-<who>-on-…" slug, else ""."""
    match = _SPEAKER_FROM_SLUG.search(url or "")
    if not match:
        return ""
    return match.group(1).replace("-", " ").strip()


def extract_article_date(html: str, url: str) -> str:
    """``YYYY-MM-DD`` for an article, from its metadata, falling back to the URL month."""
    for pattern in (_PUBLISHED_TIME, _TIME_TAG):
        match = pattern.search(html or "")
        if match:
            try:
                return datetime.fromisoformat(match.group(1).replace("Z", "+00:00")).strftime("%Y-%m-%d")
            except (ValueError, TypeError):
                pass
    url_match = re.search(r"/(\d{4})/(\d{2})/", url or "")
    if url_match:
        return f"{url_match.group(1)}-{url_match.group(2)}-01"
    return ""


def extract_article_body(html: str) -> str:
    """Extract the readable transcript text from a WH article's ``entry-content`` div."""
    if not html:
        return ""
    start = html.find(_ARTICLE_BODY_START)
    if start == -1:
        return ""
    tag_close = html.find(">", start)
    if tag_close != -1:
        start = tag_close + 1
    segment = html[start:]
    end = len(segment)
    for marker in _ARTICLE_BODY_END_MARKERS:
        position = segment.find(marker, 1)
        if position != -1:
            end = min(end, position)
    segment = _SCRIPT_STYLE.sub(" ", segment[:end])
    text = html_lib.unescape(_HTML_TAG.sub(" ", segment))
    return re.sub(r"\s+", " ", text).strip()


def build_record(url: str, html: str) -> dict | None:
    """Assemble a wh_briefing transcript record from an article page, or None.

    Returns None when the article isn't a spoken transcript or has no usable body.
    """
    if not is_spoken_remarks(url):
        return None
    body = extract_article_body(html)
    if not body:
        return None
    speaker_raw = speaker_from_url(url)
    speaker_key = resolve_speaker(speaker_raw).speaker_key if speaker_raw else ""
    if not speaker_key:
        speaker_key = normalize_speaker_key(speaker_raw)
    return {
        "source": "whitehouse",
        "speaker_raw": speaker_raw,
        "speaker_key": speaker_key,
        "venue_type": "wh_briefing",
        "event_date": extract_article_date(html, url),
        "url": url,
        "full_text": body,
    }


class WhiteHouseClient:
    """Async best-effort scraper for White House spoken transcripts (fail-soft)."""

    def __init__(self) -> None:
        self._session: aiohttp.ClientSession | None = None

    def _ensure_session(self) -> aiohttp.ClientSession:
        if self._session is None:
            connector = aiohttp.TCPConnector(ssl=_build_ssl_context())
            self._session = aiohttp.ClientSession(headers=_HEADERS, connector=connector)
        return self._session

    async def _get_text(self, url: str) -> str:
        session = self._ensure_session()
        async with session.get(url, timeout=aiohttp.ClientTimeout(total=30)) as api_response:
            api_response.raise_for_status()
            return await api_response.text()

    async def get_briefings(self, max_pages: int = 3) -> list[dict]:
        """Scrape recent spoken transcripts from the briefing-room listing pages.

        Walks up to ``max_pages`` listing pages, keeps the spoken-transcript links,
        fetches each article, and builds records. Every fetch is fail-soft.
        """
        links: list[str] = []
        for page in range(1, max_pages + 1):
            try:
                listing_html = await self._get_text(f"{WH_BASE}{WH_NEWS_PATH.format(page=page)}")
            except (aiohttp.ClientError, OSError):
                continue
            links.extend(url for url in extract_article_links(listing_html) if is_spoken_remarks(url))
        links = list(dict.fromkeys(links))

        concurrency_semaphore = asyncio.Semaphore(_FETCH_CONCURRENCY)

        async def _fetch_record(url: str) -> dict | None:
            async with concurrency_semaphore:
                try:
                    html = await self._get_text(url)
                except (aiohttp.ClientError, OSError):
                    return None
            return build_record(url, html)

        results = await asyncio.gather(*[_fetch_record(url) for url in links])
        return [record for record in results if record]

    async def close(self) -> None:
        if self._session:
            await self._session.close()
            self._session = None
