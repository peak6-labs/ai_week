"""Tests for kalshi_trader/external/weather_authorities.py"""
from __future__ import annotations

from kalshi_trader.external.weather_authorities import (
    NWS_OFFICE_HANDLES,
    WEATHER_AUTHORITIES,
    get_authorities,
    is_independent_authority,
)
from kalshi_trader.external.weather_parser import CITY_COORDS


# ---------------------------------------------------------------------------
# Map consistency
# ---------------------------------------------------------------------------

def test_every_authority_city_exists_in_city_coords():
    for city in WEATHER_AUTHORITIES:
        assert city in CITY_COORDS, f"{city!r} is not a CITY_COORDS key"


def test_every_authority_list_is_non_empty():
    for city, handles in WEATHER_AUTHORITIES.items():
        assert handles, f"{city!r} has an empty authority list"


def test_nws_office_handles_appear_in_the_map():
    all_handles = {handle for handles in WEATHER_AUTHORITIES.values() for handle in handles}
    for office_handle in NWS_OFFICE_HANDLES:
        assert office_handle in all_handles, f"{office_handle!r} is unused in the map"


# ---------------------------------------------------------------------------
# get_authorities
# ---------------------------------------------------------------------------

def test_get_authorities_returns_handles_for_mapped_city():
    assert get_authorities("dallas") == ["wfaaweather"]


def test_get_authorities_nyc_aliases_resolve_to_same_list():
    canonical = get_authorities("new york")
    assert canonical == ["NWSNewYorkNY"]
    assert get_authorities("nyc") == canonical
    assert get_authorities("new york city") == canonical
    assert get_authorities("New York") == canonical  # case-insensitive


def test_get_authorities_unmapped_city_returns_empty():
    assert get_authorities("san diego") == []
    assert get_authorities("atlantis") == []
    assert get_authorities("") == []


def test_get_authorities_returns_a_copy_not_the_underlying_list():
    handles = get_authorities("houston")
    handles.append("intruder")
    assert "intruder" not in WEATHER_AUTHORITIES["houston"]


# ---------------------------------------------------------------------------
# is_independent_authority
# ---------------------------------------------------------------------------

def test_broadcast_meteorologist_is_independent():
    assert is_independent_authority(["wfaaweather"]) is True
    assert is_independent_authority(["BradNitzWSB", "JoanneFOX5"]) is True


def test_nws_office_only_is_not_independent():
    assert is_independent_authority(["NWSBoston"]) is False
    assert is_independent_authority(["NWSNewYorkNY"]) is False


def test_mixed_list_with_one_broadcast_met_is_independent():
    assert is_independent_authority(["NWSBoston", "wfaaweather"]) is True


def test_independence_check_is_case_insensitive():
    assert is_independent_authority(["nwsboston"]) is False


def test_empty_handle_list_is_not_independent():
    assert is_independent_authority([]) is False
