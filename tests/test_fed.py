"""Tests for kalshi_trader/external/fed.py — pure parsers on fixtures."""
from __future__ import annotations

from kalshi_trader.external.fed import (
    parse_presser_pdf,
    parse_presser_text,
    parse_speech_feed,
    parse_speech_html,
    presser_pdf_url,
)
from kalshi_trader.external.mentions_parser import normalize_for_match

# A Fed speeches RSS body in the REAL format: no <author>, the speaker is the
# surname before the first comma in the title, and <description> is only the venue
# line (the full speech text lives in the linked HTML page, parsed separately).
_SPEECH_FEED = """<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0">
<channel>
  <title>FRB: Speeches</title>
  <item>
    <title>Powell, The Economic Outlook</title>
    <link>https://www.federalreserve.gov/newsevents/speech/powell20240115a.htm</link>
    <description>Speech At the Economic Club, Washington, D.C.</description>
    <pubDate>Mon, 15 Jan 2024 12:00:00 GMT</pubDate>
  </item>
  <item>
    <title>Waller, Bank Capital Requirements</title>
    <link>https://www.federalreserve.gov/newsevents/testimony/waller20240220a.htm</link>
    <description>Testimony Before the Committee on Banking</description>
    <pubDate>Tue, 20 Feb 2024 14:00:00 GMT</pubDate>
  </item>
</channel>
</rss>
"""

# A Fed speech HTML page: the body lives in <div id="article">, ending at the
# last-update marker. Mirrors the real page structure.
_SPEECH_HTML = """<html><body>
<div class="col-xs-12">
<div id="article">
  January 15, 2024 The Economic Outlook Governor Jerome H. Powell
  Thank you. Today I want to discuss the risk of recession and our policy path.
  <div id="lastUpdate">Last Update: January 15, 2024</div>
</div>
</div>
<footer>Board of Governors</footer>
</body></html>"""

# An FOMC press-conference transcript with speaker labels. The Chair's turns are
# attributed; the reporter's question is not.
_PRESSER_TEXT = (
    "Transcript of Chair Powell's Press Conference June 12, 2024 "
    "CHAIR POWELL. Good afternoon. The economy still faces some recession risk. "
    "MICHELLE SMITH. We will now take questions. "
    "STEVE LIESMAN. Are you worried about runaway inflation right now? "
    "CHAIR POWELL. We are monitoring inflation closely and remain data dependent."
)


# ---------------------------------------------------------------------------
# Speech / testimony RSS
# ---------------------------------------------------------------------------

def test_parse_speech_feed_yields_one_record_per_item():
    records = parse_speech_feed(_SPEECH_FEED)
    assert len(records) == 2


def test_parse_speech_feed_powell_record_fields():
    powell = parse_speech_feed(_SPEECH_FEED)[0]
    assert powell["source"] == "fed"
    assert powell["venue_type"] == "fed_speech"
    assert powell["speaker_key"] == "powell"          # surname-from-title → registry
    assert powell["event_date"] == "2024-01-15"
    assert powell["url"].endswith("powell20240115a.htm")
    # The RSS body is only the venue line; the speech title is carried through.
    assert "economic outlook" in normalize_for_match(powell["full_text"])


def test_parse_speech_feed_unregistered_speaker_gets_stable_key():
    waller = parse_speech_feed(_SPEECH_FEED)[1]
    assert waller["speaker_key"] == "waller"
    assert waller["venue_type"] == "fed_speech"


def test_parse_speech_feed_empty_body_returns_empty():
    assert parse_speech_feed("") == []
    assert parse_speech_feed("<rss></rss>") == []


# ---------------------------------------------------------------------------
# Speech HTML body extraction (the full attributed text)
# ---------------------------------------------------------------------------

def test_parse_speech_html_extracts_article_body():
    body = parse_speech_html(_SPEECH_HTML)
    norm = normalize_for_match(body)
    assert "recession" in norm
    assert "economic outlook" in norm
    # Stops at the last-update marker; footer nav is excluded.
    assert "board of governors" not in norm
    assert "last update" not in norm


def test_parse_speech_html_no_article_returns_empty():
    assert parse_speech_html("<html><body>no article div here</body></html>") == ""
    assert parse_speech_html("") == ""


# ---------------------------------------------------------------------------
# FOMC press-conference transcript
# ---------------------------------------------------------------------------

def test_parse_presser_keeps_only_chair_turns():
    records = parse_presser_text(_PRESSER_TEXT, "2024-06-12")
    assert len(records) == 1
    record = records[0]
    assert record["venue_type"] == "fed_presser"
    assert record["speaker_key"] == "powell"
    assert record["event_date"] == "2024-06-12"
    norm = normalize_for_match(record["full_text"])
    # Chair's own words are present.
    assert "recession" in norm
    assert "monitoring inflation closely" in norm
    # The reporter's question is NOT attributed to the Chair.
    assert "worried about runaway inflation" not in norm


def test_parse_presser_empty_text_returns_empty():
    assert parse_presser_text("", "2024-06-12") == []
    assert parse_presser_text("   ", "2024-06-12") == []


def test_parse_presser_without_chair_label_falls_back_to_full_text():
    records = parse_presser_text("Some unlabelled remarks about recession.", "2024-06-12")
    assert len(records) == 1
    assert "recession" in normalize_for_match(records[0]["full_text"])


def test_parse_presser_pdf_fail_soft_on_garbage_bytes():
    # Not a PDF → pypdf raises internally → fail-soft to [].
    assert parse_presser_pdf(b"this is not a pdf", "2024-06-12") == []


def test_presser_pdf_url_formatting():
    assert presser_pdf_url("2024-06-12").endswith("/FOMCpresconf20240612.pdf")
