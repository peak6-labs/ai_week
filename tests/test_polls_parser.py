import pytest

from kalshi_trader.external.polls_parser import parse_election_title, recent_margin
from kalshi_trader.external.fivethirtyeight import parse_polls_csv


# --- title parsing ---------------------------------------------------------

def test_parse_senate_state_candidate():
    result = parse_election_title(
        "KXSENATEGA",
        "Will Jon Ossoff win the Georgia Senate race?",
    )
    assert result is not None
    assert result["poll_type"] == "senate"
    assert result["state"] == "georgia"
    assert result["candidate"] == "Jon Ossoff"


def test_parse_governor_gubernatorial_keyword():
    result = parse_election_title("TICKER", "Will the Republican win the Texas gubernatorial election?")
    assert result is not None
    assert result["poll_type"] == "governor"
    assert result["state"] == "texas"


def test_parse_president_nationwide():
    result = parse_election_title("TICKER", "Will the Democratic candidate win the presidential election?")
    assert result is not None
    assert result["poll_type"] == "president"
    assert result["state"] is None


def test_parse_generic_ballot():
    result = parse_election_title("TICKER", "Will Democrats lead the generic ballot?")
    assert result is not None
    assert result["poll_type"] == "generic_ballot"


def test_parse_non_election_returns_none():
    result = parse_election_title("TICKER", "Will it rain in Chicago on June 4?")
    assert result is None


def test_parse_two_word_state_new_hampshire():
    result = parse_election_title("TICKER", "Will the incumbent win the New Hampshire Senate race?")
    assert result is not None
    assert result["state"] == "new hampshire"


# --- unsupported race families (538 polls general-election head-to-heads only) -

def test_parse_mayoral_race_returns_none():
    # FiveThirtyEight does not poll municipal/mayoral races.
    result = parse_election_title(
        "KXVOTEPRIMARY-MAYORLA26SPRA-65",
        "Will Spencer Pratt receive at least 30% of the popular vote in the "
        "first round of the 2026 Los Angeles mayoral election?",
    )
    assert result is None


def test_parse_primary_with_chamber_keyword_returns_none():
    # A Senate *primary* contains "Senate" but 538 has no primary polling, so the
    # general-election senate file would produce a bogus signal — reject it.
    result = parse_election_title(
        "TICKER", "Will Colin Allred win the Democratic primary for the Texas Senate race?"
    )
    assert result is None


def test_parse_vote_share_threshold_returns_none():
    # A vote-share *threshold* market ("at least 55%") asks a different question
    # than 538's head-to-head win margin — reject it even though it says "Senate".
    result = parse_election_title(
        "TICKER", "Will the Republican get at least 55% of the vote in the Ohio Senate race?"
    )
    assert result is None


def test_parse_runoff_returns_none():
    result = parse_election_title(
        "TICKER", "Will Raphael Warnock win the Georgia Senate runoff?"
    )
    assert result is None


# --- csv parsing -----------------------------------------------------------

_SAMPLE_CSV = (
    "poll_id,state,fte_grade,end_date,candidate_name,answer,pct\n"
    "1,Georgia,A+,11/01/24,Jon Ossoff,Ossoff,51.0\n"
    "1,Georgia,A+,11/01/24,Herschel Walker,Walker,47.0\n"
    "2,Georgia,B,10/15/24,Jon Ossoff,Ossoff,49.0\n"
    "2,Georgia,B,10/15/24,Herschel Walker,Walker,49.0\n"
    "3,Ohio,A,11/01/24,Some Other,Other,55.0\n"
    "3,Ohio,A,11/01/24,Rival Person,Rival,40.0\n"
)


def test_parse_polls_csv_rows():
    rows = parse_polls_csv(_SAMPLE_CSV)
    assert len(rows) == 6
    assert rows[0]["candidate_name"] == "Jon Ossoff"
    assert rows[0]["state"] == "Georgia"


# --- recent margin ---------------------------------------------------------

def test_recent_margin_state_filter_and_named_candidate():
    rows = parse_polls_csv(_SAMPLE_CSV)
    summary = recent_margin(rows, candidate="Jon Ossoff", state="georgia")
    assert summary is not None
    assert summary["candidate"] == "Jon Ossoff"
    assert summary["opponent"] == "Herschel Walker"
    # Ossoff leads on average → positive margin.
    assert summary["margin"] > 0
    assert summary["poll_count"] == 4


def test_recent_margin_defaults_to_leader_when_no_candidate():
    rows = parse_polls_csv(_SAMPLE_CSV)
    summary = recent_margin(rows, candidate=None, state="georgia")
    assert summary is not None
    assert summary["candidate"] == "Jon Ossoff"


def test_recent_margin_no_rows_for_state_returns_none():
    rows = parse_polls_csv(_SAMPLE_CSV)
    summary = recent_margin(rows, candidate=None, state="california")
    assert summary is None


def test_recent_margin_empty_rows_returns_none():
    assert recent_margin([], candidate=None, state=None) is None
