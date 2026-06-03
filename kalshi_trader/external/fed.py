"""Federal Reserve speech/testimony + FOMC press-conference clients.

Two free, no-key, speaker-attributed corpora for the mentions archive:

* :func:`parse_speech_feed` — the Fed's speeches & testimony RSS feeds, parsed with
  ``feedparser``. ``venue_type=fed_speech``; the speaker is named per item.
* :func:`parse_presser_text` / :func:`parse_presser_pdf` — FOMC post-meeting press
  conferences, published as PDFs at a predictable URL and parsed with ``pypdf``.
  ``venue_type=fed_presser``; only the *Chair's* turns are kept, so a reporter's
  question that happens to contain the tracked phrase is not mis-attributed to the
  Chair.

Network fetch (:class:`FedClient`) and parsing are kept separate so the parsers are
pure and testable on committed fixtures; the network methods are exercised by the
nightly refresh CLI.
"""
from __future__ import annotations

import asyncio
import html as html_lib
import io
import re
import ssl
from datetime import datetime

import aiohttp

from kalshi_trader.external.speaker_registry import (
    VENUE_FED_PRESSER,
    VENUE_FED_SPEECH,
    normalize_speaker_key,
    resolve_speaker,
)

FED_SPEECHES_RSS = "https://www.federalreserve.gov/feeds/speeches.xml"
FED_TESTIMONY_RSS = "https://www.federalreserve.gov/feeds/testimony.xml"
FOMC_PRESSER_URL_TEMPLATE = (
    "https://www.federalreserve.gov/mediacenter/files/FOMCpresconf{yyyymmdd}.pdf"
)

# The FOMC press conference is led by the Fed Chair; update on a chair change.
# The transcript labels the Chair's turns "CHAIR <SURNAME>." — we keep only those.
FOMC_CHAIR_SURNAME = "POWELL"
FOMC_CHAIR_KEY = "powell"

# Maintained seed of recent FOMC press-conference dates (the second day of each
# two-day meeting, when the presser is held). The refresh fetches these PDFs;
# unknown/future dates 404 and are skipped fail-soft, so an out-of-date list never
# breaks a refresh — it just yields fewer documents. Extend as new meetings occur.
RECENT_FOMC_PRESSER_DATES: list[str] = [
    "2023-02-01", "2023-03-22", "2023-05-03", "2023-06-14",
    "2023-07-26", "2023-09-20", "2023-11-01", "2023-12-13",
    "2024-01-31", "2024-03-20", "2024-05-01", "2024-06-12",
    "2024-07-31", "2024-09-18", "2024-11-07", "2024-12-18",
    "2025-01-29", "2025-03-19", "2025-05-07", "2025-06-18",
    "2025-07-30", "2025-09-17", "2025-10-29", "2025-12-10",
    "2026-01-28", "2026-03-18", "2026-04-29",
]

# Broad Accept: the client fetches RSS (xml), speech pages (html) and presser PDFs
# through one session, so it must not advertise a type that excludes HTML.
_HEADERS = {"User-Agent": "kalshi-trader/1.0", "Accept": "text/html, application/xml, application/pdf, */*"}

# Max concurrent HTML fetches when enriching speech records with full text.
_SPEECH_FETCH_CONCURRENCY = 4

# The speech body lives in <div id="article">…</div>. These markers bound where the
# readable body ends (footnotes/last-update/footer), so boilerplate nav is dropped.
_ARTICLE_START = 'id="article"'
_ARTICLE_END_MARKERS = ('id="lastUpdate"', "Back to Top", 'class="footer', "References<")
_SCRIPT_STYLE = re.compile(r"<(script|style)\b.*?</\1>", re.DOTALL | re.IGNORECASE)

# A speaker label in an FOMC transcript: a run of 1–4 ALL-CAPS words ending in a
# period (e.g. "CHAIR POWELL.", "MICHELLE SMITH.", "STEVE LIESMAN."). Used to split
# the transcript into turns so non-Chair turns can be dropped.
_SPEAKER_LABEL = re.compile(r"([A-Z][A-Z'\-]+(?:\s+[A-Z][A-Z'\-]+){0,3})\.")

# "by <Name> ..." in a speech/testimony title, used when the feed item carries no
# explicit author element.
_TITLE_SPEAKER = re.compile(r"\bby\s+([A-Z][A-Za-z.'\-]+(?:\s+[A-Z][A-Za-z.'\-]+){0,3})")

_HTML_TAG = re.compile(r"<[^>]+>")


def _build_ssl_context() -> ssl.SSLContext:
    """SSL context that trusts the OS store (corporate-proxy safe; see gdelt.py)."""
    try:
        import truststore
        return truststore.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    except Exception:  # pragma: no cover - truststore optional
        return ssl.create_default_context()


def _strip_html(text: str) -> str:
    return _HTML_TAG.sub(" ", text or "").replace("&nbsp;", " ").strip()


def _feed_entry_date(entry) -> str:
    """``YYYY-MM-DD`` from a feedparser entry's published/updated date, else ""."""
    parsed_time = getattr(entry, "published_parsed", None) or getattr(entry, "updated_parsed", None)
    if not parsed_time:
        return ""
    return datetime(parsed_time.tm_year, parsed_time.tm_mon, parsed_time.tm_mday).strftime("%Y-%m-%d")


def _feed_entry_speaker(entry) -> str:
    """Best-effort speaker name from a feed entry.

    The live Fed feed has no ``<author>`` and titles the item "Surname, Speech
    Title" (e.g. "Powell, Acceptance Remarks"), so the surname before the first
    comma is the primary source. Falls back to an ``<author>`` element and then a
    "by <Name>" pattern (the shape older items and our fixtures use).
    """
    title = entry.get("title", "")
    if "," in title:
        candidate = title.split(",", 1)[0].strip()
        if candidate and len(candidate.split()) <= 3 and re.fullmatch(r"[A-Za-z.'\- ]{2,40}", candidate):
            return candidate
    author = (entry.get("author") or "").strip()
    if author:
        return author
    title_match = _TITLE_SPEAKER.search(title)
    if title_match:
        return title_match.group(1).strip()
    return ""


def parse_speech_html(html: str) -> str:
    """Extract the readable speech body from a Fed speech/testimony HTML page.

    The body is the text of ``<div id="article">``; this slices from that marker to
    the first footnote/footer boundary, drops script/style, strips tags, unescapes
    entities, and collapses whitespace. Returns "" when the container is absent so
    the caller can fall back to the RSS summary.
    """
    if not html:
        return ""
    start = html.find(_ARTICLE_START)
    if start == -1:
        return ""
    # Begin after the container tag closes so the literal id="article"> is dropped.
    tag_close = html.find(">", start)
    if tag_close != -1:
        start = tag_close + 1
    segment = html[start:]
    end = len(segment)
    for marker in _ARTICLE_END_MARKERS:
        position = segment.find(marker, 1)
        if position != -1:
            end = min(end, position)
    segment = segment[:end]
    segment = _SCRIPT_STYLE.sub(" ", segment)
    text = _HTML_TAG.sub(" ", segment)
    text = html_lib.unescape(text)
    return re.sub(r"\s+", " ", text).strip()


def parse_speech_feed(feed_text: str, venue_type: str = VENUE_FED_SPEECH) -> list[dict]:
    """Parse a Fed speeches/testimony RSS body into transcript records.

    Each ``<item>`` becomes one record. ``speaker_key`` is resolved through the
    speaker registry so a record attributed to "Chair Jerome H. Powell" counts
    under the same ``powell`` key the market's parsed speaker resolves to. The RSS
    description carries the speech summary (the freshest free attributed text);
    title + description form ``full_text``.
    """
    import feedparser  # imported lazily so the module loads even if absent

    parsed = feedparser.parse(feed_text)
    records: list[dict] = []
    for entry in parsed.entries:
        speaker_raw = _feed_entry_speaker(entry)
        if speaker_raw:
            speaker_key = resolve_speaker(speaker_raw).speaker_key or normalize_speaker_key(speaker_raw)
        else:
            speaker_key = ""
        title = entry.get("title", "")
        summary = _strip_html(entry.get("summary") or entry.get("description") or "")
        full_text = f"{title}. {summary}".strip()
        records.append({
            "source": "fed",
            "speaker_raw": speaker_raw,
            "speaker_key": speaker_key,
            "venue_type": venue_type,
            "event_date": _feed_entry_date(entry),
            "url": entry.get("link", ""),
            "full_text": full_text,
        })
    return records


def _extract_turns(text: str) -> list[tuple[str, str]]:
    """Split an FOMC transcript into (speaker, spoken_text) turns by speaker label."""
    labels = list(_SPEAKER_LABEL.finditer(text))
    turns: list[tuple[str, str]] = []
    for index, match in enumerate(labels):
        speaker = match.group(1).strip()
        start = match.end()
        end = labels[index + 1].start() if index + 1 < len(labels) else len(text)
        spoken = text[start:end].strip()
        if spoken:
            turns.append((speaker, spoken))
    return turns


def parse_presser_text(
    text: str,
    event_date: str,
    url: str = "",
    chair_surname: str = FOMC_CHAIR_SURNAME,
    chair_key: str = FOMC_CHAIR_KEY,
) -> list[dict]:
    """Build a ``fed_presser`` record from an FOMC press-conference transcript.

    Keeps only the Chair's turns (labelled "CHAIR <SURNAME>.") so reporters'
    questions are excluded from the attributed text. Falls back to the full text
    when no Chair label is found (e.g. a layout the splitter doesn't recognize).
    Returns a one-element list, or ``[]`` when there is no usable text.
    """
    if not text or not text.strip():
        return []

    turns = _extract_turns(text)
    chair_segments = [spoken for speaker, spoken in turns if chair_surname in speaker.upper()]
    chair_text = " ".join(chair_segments).strip() if chair_segments else text.strip()
    if not chair_text:
        return []

    return [{
        "source": "fed",
        "speaker_raw": f"Chair {chair_surname.title()}",
        "speaker_key": chair_key,
        "venue_type": VENUE_FED_PRESSER,
        "event_date": event_date,
        "url": url,
        "full_text": chair_text,
    }]


def _pdf_to_text(pdf_bytes: bytes) -> str:
    """Extract text from a PDF, returning "" on any parse failure."""
    try:
        from pypdf import PdfReader
        reader = PdfReader(io.BytesIO(pdf_bytes))
        return "\n".join((page.extract_text() or "") for page in reader.pages)
    except Exception:
        return ""


def parse_presser_pdf(pdf_bytes: bytes, event_date: str, url: str = "") -> list[dict]:
    """Parse an FOMC press-conference PDF into a ``fed_presser`` record (fail-soft)."""
    return parse_presser_text(_pdf_to_text(pdf_bytes), event_date, url=url)


def presser_pdf_url(meeting_date: str) -> str:
    """URL of the FOMC press-conference PDF for a ``YYYY-MM-DD`` meeting date."""
    return FOMC_PRESSER_URL_TEMPLATE.format(yyyymmdd=meeting_date.replace("-", ""))


class FedClient:
    """Async fetcher for the Fed speech/testimony feeds and FOMC presser PDFs.

    Used by the nightly refresh. Every fetch is fail-soft: a transport error
    yields an empty result rather than raising, so one bad source never aborts a
    refresh.
    """

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

    async def _get_bytes(self, url: str) -> bytes:
        session = self._ensure_session()
        async with session.get(url, timeout=aiohttp.ClientTimeout(total=30)) as api_response:
            api_response.raise_for_status()
            return await api_response.read()

    async def _enrich_full_text(self, record: dict) -> dict:
        """Replace a record's thin RSS summary with the full speech HTML body.

        Fail-soft: any fetch/parse problem leaves the RSS-derived ``full_text`` in
        place, so a speech still contributes its title/venue line if the page is
        unreachable.
        """
        url = record.get("url") or ""
        if not url:
            return record
        try:
            html = await self._get_text(url)
        except (aiohttp.ClientError, OSError):
            return record
        body = parse_speech_html(html)
        if body:
            record["full_text"] = body
        return record

    async def get_speeches(self, since: str | None = None) -> list[dict]:
        """Fetch speeches + testimony as full-text records, filtered to ``event_date >= since``.

        The RSS feed only indexes recent items and carries a one-line summary, so
        each record's body is enriched from its HTML page (concurrency-limited).
        """
        records: list[dict] = []
        for feed_url, venue_type in (
            (FED_SPEECHES_RSS, VENUE_FED_SPEECH),
            (FED_TESTIMONY_RSS, VENUE_FED_SPEECH),
        ):
            try:
                feed_text = await self._get_text(feed_url)
            except (aiohttp.ClientError, OSError):
                continue
            records.extend(parse_speech_feed(feed_text, venue_type=venue_type))
        if since:
            records = [record for record in records if (record.get("event_date") or "") >= since]

        concurrency_semaphore = asyncio.Semaphore(_SPEECH_FETCH_CONCURRENCY)

        async def _enrich(record: dict) -> dict:
            async with concurrency_semaphore:
                return await self._enrich_full_text(record)

        return list(await asyncio.gather(*[_enrich(record) for record in records]))

    async def get_presser_transcripts(self, meeting_dates: list[str] | None = None) -> list[dict]:
        """Fetch and parse the FOMC presser PDF for each ``YYYY-MM-DD`` meeting date.

        Defaults to :data:`RECENT_FOMC_PRESSER_DATES`. Each date is fetched
        fail-soft, so an unknown/future date simply contributes nothing.
        """
        if meeting_dates is None:
            meeting_dates = RECENT_FOMC_PRESSER_DATES
        records: list[dict] = []
        for meeting_date in meeting_dates:
            try:
                pdf_bytes = await self._get_bytes(presser_pdf_url(meeting_date))
            except (aiohttp.ClientError, OSError):
                continue
            records.extend(parse_presser_pdf(pdf_bytes, meeting_date, url=presser_pdf_url(meeting_date)))
        return records

    async def close(self) -> None:
        if self._session:
            await self._session.close()
            self._session = None
