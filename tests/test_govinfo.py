"""Tests for kalshi_trader/external/govinfo.py — pure CREC parsers + fail-soft client."""
from __future__ import annotations

import pytest

from kalshi_trader.external.govinfo import (
    GovInfoClient,
    build_crec_record,
    crec_text_to_plain,
    parse_crec_granule_summary,
    parse_crec_granules,
    parse_crec_packages,
)


# ---------------------------------------------------------------------------
# collection / granule list parsing
# ---------------------------------------------------------------------------

def test_parse_crec_packages():
    collection = {"packages": [
        {"packageId": "CREC-2024-01-15", "dateIssued": "2024-01-15"},
        {"packageId": "CREC-2024-01-16", "dateIssued": "2024-01-16T00:00:00Z"},
        {"dateIssued": "2024-01-17"},  # no packageId → skipped
    ]}
    packages = parse_crec_packages(collection)
    assert packages == [
        {"packageId": "CREC-2024-01-15", "dateIssued": "2024-01-15"},
        {"packageId": "CREC-2024-01-16", "dateIssued": "2024-01-16"},
    ]


def test_parse_crec_granules_keeps_only_floor_classes():
    granules_json = {"granules": [
        {"granuleId": "G1", "granuleClass": "SENATE", "title": "RECESSION RISKS"},
        {"granuleId": "G2", "granuleClass": "HOUSE", "title": "TARIFFS"},
        {"granuleId": "G3", "granuleClass": "DAILYDIGEST", "title": "Digest"},  # skipped
        {"granuleClass": "SENATE"},  # no id → skipped
    ]}
    stubs = parse_crec_granules(granules_json)
    assert [s["granuleId"] for s in stubs] == ["G1", "G2"]


# ---------------------------------------------------------------------------
# granule summary → speaker attribution
# ---------------------------------------------------------------------------

def _summary(members, txt="https://api.govinfo.gov/.../htm"):
    return {
        "title": "RECESSION RISKS",
        "dateIssued": "2024-01-15",
        "granuleClass": "SENATE",
        "members": members,
        "download": {"txtLink": txt},
    }


def test_parse_granule_summary_extracts_surname_from_last_first():
    summary = parse_crec_granule_summary(_summary(
        [{"memberName": "Mullin, Markwayne", "role": "SPEAKING", "bioGuideId": "M001190"}]
    ))
    assert summary["speaker_raw"] == "Mullin"
    assert summary["date"] == "2024-01-15"
    assert summary["txt_link"].endswith("/htm")


def test_parse_granule_summary_no_members_returns_none():
    assert parse_crec_granule_summary(_summary([])) is None
    assert parse_crec_granule_summary({}) is None


# ---------------------------------------------------------------------------
# text cleaning + record assembly
# ---------------------------------------------------------------------------

def test_crec_text_to_plain_strips_html_and_unescapes():
    assert crec_text_to_plain("<p>Mr.&nbsp;President, recession &amp; growth</p>") == \
        "Mr. President, recession & growth"


def test_build_crec_record_fields_and_attribution():
    summary = {"speaker_raw": "Mullin", "date": "2024-01-15", "txt_link": "x", "title": "t"}
    record = build_crec_record("CREC-2024-01-15-pt1-PgS1", summary,
                               "<html>I rise to discuss the risk of recession.</html>")
    assert record["source"] == "govinfo_crec"
    assert record["venue_type"] == "congress_floor"
    assert record["speaker_key"] == "mullin"
    assert record["event_date"] == "2024-01-15"
    assert record["doc_id"] == "govinfo_crec|CREC-2024-01-15-pt1-PgS1"
    assert "recession" in record["full_text"]


def test_build_crec_record_empty_text_returns_none():
    summary = {"speaker_raw": "Mullin", "date": "2024-01-15", "txt_link": "x", "title": "t"}
    assert build_crec_record("G1", summary, "") is None
    assert build_crec_record("G1", summary, "<html>   </html>") is None


# ---------------------------------------------------------------------------
# client: no key → [] (graceful), no network
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_get_crec_records_no_key_returns_empty():
    client = GovInfoClient(api_key="")
    assert await client.get_crec_records("2024-01-01") == []
    assert client._session is None
