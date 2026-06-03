"""Resolve and validate Kalshi website links.

Kalshi's public API exposes stable tickers, but not the human-readable slug used
in website deep links. This module tries plausible slugs, fetches candidate
pages, validates that the page is for the intended event, and returns only
verified URLs.
"""
from __future__ import annotations

from dataclasses import dataclass
import html
import json
import re
import ssl
import time
from typing import Callable, Iterable, Optional
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from kalshi_trader.web_links import KALSHI_MARKETS_BASE_URL, kalshi_market_url

KALSHI_API_BASE_URL = "https://external-api.kalshi.com/trade-api/v2"
_GENERIC_TITLES = {"kalshi", "kalshi - prediction market for trading the future"}


@dataclass(frozen=True)
class CandidateResult:
    url: str
    slug: str
    valid: bool
    reason: str
    page_title: Optional[str] = None


@dataclass(frozen=True)
class ResolvedLink:
    ticker: str
    event_ticker: str
    series_ticker: str
    url: str
    series_slug: str
    page_title: str
    attempts: tuple[CandidateResult, ...]


@dataclass(frozen=True)
class EventInput:
    ticker: str
    event_ticker: str
    series_ticker: str
    title: str = ""
    series_title: str = ""


FetchHTML = Callable[[str], str]


def slugify_title(title: str) -> str:
    """Convert a title into a Kalshi-style URL slug candidate."""
    text = html.unescape(title).lower()
    text = text.replace("&", " and ")
    text = re.sub(r"[^a-z0-9]+", "-", text)
    return re.sub(r"-+", "-", text).strip("-")


def slug_candidates(series_title: str = "", event_title: str = "", extra: Iterable[str] = ()) -> list[str]:
    """Return plausible slugs, with strongest candidates first."""
    candidates: list[str] = []

    def add(value: str) -> None:
        slug = slugify_title(value)
        if slug and slug not in candidates:
            candidates.append(slug)

    for value in extra:
        add(value)

    if series_title:
        add(series_title)
        for suffix in (" today", " tomorrow", " this week", " this month", " this year"):
            normalized = re.sub(r"[?!.]+$", "", series_title.strip(), flags=re.I)
            if normalized.lower().endswith(suffix):
                add(normalized[: -len(suffix)])

    if event_title:
        add(event_title)
        stripped = re.sub(r"\s+on\s+[a-z]+ \d{1,2},? \d{4}\??$", "", event_title.strip(), flags=re.I)
        add(stripped)

    return candidates


def validate_event_page(html_text: str, event_ticker: str, expected_title: str = "") -> tuple[bool, str, Optional[str]]:
    """Validate that fetched Kalshi HTML is the intended event page."""
    page_title = _extract_title(html_text)
    normalized_title = html.unescape(page_title or "").strip()
    if not normalized_title or normalized_title.lower() in _GENERIC_TITLES:
        return False, "generic page title", normalized_title or None

    ticker_pattern = re.escape(event_ticker).replace("\\-", "[-_]")
    if not re.search(ticker_pattern, html_text, flags=re.I):
        return False, "event ticker not embedded in page", normalized_title

    if expected_title and not _shares_meaningful_title_terms(normalized_title, expected_title):
        return False, "page title does not match expected market text", normalized_title

    return True, "validated", normalized_title


def resolve_event_link(
    event: EventInput,
    *,
    known_slug: Optional[str] = None,
    fetch_html: Optional[FetchHTML] = None,
    delay_seconds: float = 0.0,
    strict_title: bool = False,
) -> ResolvedLink | None:
    """Try candidate Kalshi URLs until one validates for the event."""
    fetch = fetch_html or fetch_url
    candidates = slug_candidates(event.series_title, event.title, extra=[known_slug] if known_slug else [])
    attempts: list[CandidateResult] = []

    for slug in candidates:
        url = kalshi_market_url(event.event_ticker, series_slug=slug, deep_link=True)
        try:
            html_text = fetch(url)
        except (HTTPError, URLError, TimeoutError, OSError) as exc:
            attempts.append(CandidateResult(url=url, slug=slug, valid=False, reason=type(exc).__name__))
            continue

        expected_title = event.title if strict_title else ""
        valid, reason, page_title = validate_event_page(html_text, event.event_ticker, expected_title)
        attempts.append(CandidateResult(url=url, slug=slug, valid=valid, reason=reason, page_title=page_title))
        if valid and page_title:
            return ResolvedLink(
                ticker=event.ticker,
                event_ticker=event.event_ticker,
                series_ticker=event.series_ticker,
                url=url,
                series_slug=slug,
                page_title=page_title,
                attempts=tuple(attempts),
            )
        if delay_seconds:
            time.sleep(delay_seconds)

    return None


def fetch_url(url: str, timeout_seconds: int = 15) -> str:
    """Fetch a Kalshi website page as text."""
    req = Request(url, headers={"User-Agent": "Mozilla/5.0"})
    context = ssl._create_unverified_context()
    with urlopen(req, timeout=timeout_seconds, context=context) as response:
        return response.read().decode("utf-8", "replace")


def fetch_series_title(series_ticker: str, timeout_seconds: int = 15) -> str:
    """Fetch a series title from the public Kalshi API."""
    if not series_ticker:
        return ""
    url = f"{KALSHI_API_BASE_URL}/series/{series_ticker.upper()}?{urlencode({'include_volume': 'false'})}"
    req = Request(url, headers={"User-Agent": "Mozilla/5.0"})
    context = ssl._create_unverified_context()
    with urlopen(req, timeout=timeout_seconds, context=context) as response:
        payload = json.loads(response.read().decode("utf-8"))
    return str(payload.get("series", {}).get("title") or "")


def _extract_title(html_text: str) -> Optional[str]:
    match = re.search(r"<title>(.*?)</title>", html_text, flags=re.I | re.S)
    if not match:
        return None
    return re.sub(r"\s+", " ", match.group(1)).strip()


def _shares_meaningful_title_terms(page_title: str, expected_title: str) -> bool:
    expected_terms = _meaningful_terms(expected_title)
    if not expected_terms:
        return True
    page_terms = _meaningful_terms(page_title)
    overlap = expected_terms & page_terms
    return len(overlap) >= min(2, len(expected_terms))


def _meaningful_terms(value: str) -> set[str]:
    stopwords = {
        "a", "an", "and", "as", "at", "before", "by", "for", "from", "in", "is",
        "of", "on", "or", "the", "this", "to", "will", "with", "yes", "no",
        "odds", "predictions",
    }
    terms = re.findall(r"[a-z0-9]{3,}", html.unescape(value).lower())
    return {term for term in terms if term not in stopwords}
