"""Tests for kalshi_trader/external/whitehouse.py — pure scraper helpers (fixtures)."""
from __future__ import annotations

from kalshi_trader.external.mentions_parser import normalize_for_match
from kalshi_trader.external.whitehouse import (
    build_record,
    extract_article_body,
    extract_article_date,
    extract_article_links,
    is_spoken_remarks,
    speaker_from_url,
)

_REMARKS_URL = "https://www.whitehouse.gov/briefings-statements/2026/06/remarks-by-president-trump-on-the-economy/"
_BRIEFING_URL = "https://www.whitehouse.gov/briefings-statements/2026/06/press-briefing-by-press-secretary-karoline-leavitt-on-tariffs/"
_MESSAGE_URL = "https://www.whitehouse.gov/releases/2026/06/presidential-message-on-pentecost/"

_LISTING_HTML = f"""
<ul>
  <li><a href="{_REMARKS_URL}">Remarks</a></li>
  <li><a href="{_MESSAGE_URL}">Message</a></li>
  <li><a href="{_BRIEFING_URL}">Briefing</a></li>
  <li><a href="{_REMARKS_URL}">Remarks dup</a></li>
</ul>
"""

_ARTICLE_HTML = """<html><head>
<meta property="article:published_time" content="2026-06-02T15:00:00Z">
</head><body>
<main>
<div class="entry-content wp-block-post-content">
  THE PRESIDENT: Thank you all. Today we discuss the economy and the risk of recession.
  <div id="comments">comment form here</div>
</div>
</main>
<footer>The White House</footer>
</body></html>"""


# --- listing + classification ----------------------------------------------

def test_extract_article_links_dedupes_dated_links():
    links = extract_article_links(_LISTING_HTML)
    assert links == [_REMARKS_URL, _MESSAGE_URL, _BRIEFING_URL]


def test_is_spoken_remarks():
    assert is_spoken_remarks(_REMARKS_URL) is True
    assert is_spoken_remarks(_BRIEFING_URL) is True
    assert is_spoken_remarks(_MESSAGE_URL) is False


# --- speaker from slug ------------------------------------------------------

def test_speaker_from_url_president():
    assert speaker_from_url(_REMARKS_URL) == "president trump"


def test_speaker_from_url_press_secretary():
    assert speaker_from_url(_BRIEFING_URL) == "press secretary karoline leavitt"


def test_speaker_from_url_no_match():
    assert speaker_from_url(_MESSAGE_URL) == ""


# --- body + date ------------------------------------------------------------

def test_extract_article_body():
    body = normalize_for_match(extract_article_body(_ARTICLE_HTML))
    assert "risk of recession" in body
    assert "comment form" not in body          # cut at id="comments"
    assert "the white house" not in body       # footer excluded


def test_extract_article_body_no_container():
    assert extract_article_body("<html><body>nothing</body></html>") == ""


def test_extract_article_date_prefers_published_time():
    assert extract_article_date(_ARTICLE_HTML, _REMARKS_URL) == "2026-06-02"


def test_extract_article_date_falls_back_to_url_month():
    assert extract_article_date("<html></html>", _REMARKS_URL) == "2026-06-01"


# --- record assembly --------------------------------------------------------

def test_build_record_spoken_remarks():
    record = build_record(_REMARKS_URL, _ARTICLE_HTML)
    assert record is not None
    assert record["source"] == "whitehouse"
    assert record["venue_type"] == "wh_briefing"
    assert record["speaker_key"] == "trump"         # president trump → registry
    assert record["event_date"] == "2026-06-02"
    assert "recession" in normalize_for_match(record["full_text"])


def test_build_record_skips_written_message():
    assert build_record(_MESSAGE_URL, _ARTICLE_HTML) is None


def test_build_record_broken_markup_returns_none():
    # Spoken URL but no usable body (layout change) → fail-soft to None.
    assert build_record(_REMARKS_URL, "<html><body>no entry content</body></html>") is None
