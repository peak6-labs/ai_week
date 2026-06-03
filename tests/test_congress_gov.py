"""Tests for kalshi_trader/external/congress_gov.py — pure parsers + fail-soft client."""
from __future__ import annotations

import pytest

from kalshi_trader.external.congress_gov import (
    CongressGovClient,
    normalize_meeting_status,
    parse_committee_meeting,
    parse_meeting_list,
)


def _detail(status: str, date: str = "2026-06-10T14:00:00Z") -> dict:
    return {
        "committeeMeeting": {
            "eventId": "115538",
            "chamber": "House",
            "meetingStatus": status,
            "date": date,
            "title": "Monetary Policy and the State of the Economy",
            "committees": [
                {"name": "Committee on Financial Services", "systemCode": "hsba00", "chamber": "House"}
            ],
        }
    }


# ---------------------------------------------------------------------------
# status normalization
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("raw,expected", [
    ("Scheduled", "Scheduled"),
    ("Canceled", "Canceled"),
    ("Cancelled", "Canceled"),     # both spellings fold to one
    ("Postponed", "Postponed"),
    ("Rescheduled", "Rescheduled"),
    ("  postponed ", "Postponed"),
    ("", "Scheduled"),             # blank/unknown never fabricates a veto
    (None, "Scheduled"),
    ("Something Odd", "Scheduled"),
])
def test_normalize_meeting_status(raw, expected):
    assert normalize_meeting_status(raw) == expected


# ---------------------------------------------------------------------------
# meeting detail parsing
# ---------------------------------------------------------------------------

def test_parse_committee_meeting_fields():
    record = parse_committee_meeting(_detail("Scheduled"))
    assert record == {
        "meeting_id": "115538",
        "committee": "Committee on Financial Services",
        "chamber": "House",
        "event_date": "2026-06-10",
        "status": "Scheduled",
        "title": "Monetary Policy and the State of the Economy",
    }


def test_parse_committee_meeting_status_variants():
    assert parse_committee_meeting(_detail("Cancelled"))["status"] == "Canceled"
    assert parse_committee_meeting(_detail("Postponed"))["status"] == "Postponed"


def test_parse_committee_meeting_no_meeting_returns_none():
    assert parse_committee_meeting({}) is None
    assert parse_committee_meeting({"committeeMeeting": None}) is None


def test_parse_meeting_list_extracts_stubs_and_skips_missing_ids():
    stubs = parse_meeting_list({"committeeMeetings": [
        {"eventId": "1", "chamber": "House"},
        {"eventId": "2", "chamber": "Senate"},
        {"chamber": "House"},  # no eventId → skipped
    ]})
    assert stubs == [
        {"eventId": "1", "chamber": "House"},
        {"eventId": "2", "chamber": "Senate"},
    ]


def test_parse_meeting_list_empty():
    assert parse_meeting_list({}) == []


# ---------------------------------------------------------------------------
# client: no key → [] (graceful degradation), never calls the network
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_get_committee_meetings_no_key_returns_empty():
    client = CongressGovClient(api_key="")
    result = await client.get_committee_meetings(119, "house")
    assert result == []
    # No session was ever created (no network attempted).
    assert client._session is None
