"""Tests for the per-series settlement-terms cache.

Covers the load/save round-trip, the cache-first get_or_fetch (one API call on a
miss, zero on a hit), and the batch get_or_fetch_many that dedups a strike ladder
to a single series call and tolerates a failed lookup.
"""
from __future__ import annotations

import asyncio

from kalshi_trader import contract_terms


class FakeClient:
    """Stand-in KalshiClient that counts get_series_detail calls."""

    def __init__(self, by_series: dict[str, dict], *, fail: set[str] | None = None) -> None:
        self._by_series = by_series
        self._fail = fail or set()
        self.calls: list[str] = []

    async def get_series_detail(self, series_ticker: str) -> dict:
        self.calls.append(series_ticker)
        if series_ticker in self._fail:
            raise RuntimeError("series detail unavailable")
        return {"series": self._by_series[series_ticker]}


_KXTEMP = {
    "settlement_sources": [{"name": "AccuWeather", "url": "https://www.accuweather.com"}],
    "contract_terms_url": "https://example.s3/NHIGHD.pdf",
    "contract_url": "https://example.s3/NHIGHD-cert.pdf",
}
_KXLAX = {
    "settlement_sources": [{"name": "NWS Climatological Report", "url": "https://forecast.weather.gov"}],
    "contract_terms_url": "https://example.s3/LAXHIGH.pdf",
    "contract_url": "https://example.s3/LAXHIGH-cert.pdf",
}


def test_save_load_round_trip(tmp_path):
    path = tmp_path / "series_contract_terms.json"
    terms = {"KXTEMPNYCH": dict(_KXTEMP, fetched_at="2026-06-03T00:00:00+00:00")}
    contract_terms.save_contract_terms(terms, path)
    loaded = contract_terms.load_contract_terms(path)
    assert loaded["KXTEMPNYCH"]["settlement_sources"][0]["name"] == "AccuWeather"
    assert loaded["KXTEMPNYCH"]["contract_terms_url"] == "https://example.s3/NHIGHD.pdf"


def test_load_missing_file_returns_empty(tmp_path):
    assert contract_terms.load_contract_terms(tmp_path / "nope.json") == {}


def test_save_is_sorted_and_uppercased(tmp_path):
    path = tmp_path / "series_contract_terms.json"
    contract_terms.save_contract_terms({"kxlax": _KXLAX, "KXTEMPNYCH": _KXTEMP}, path)
    text = path.read_text()
    # Keys uppercased and sorted: KXLAX before KXTEMPNYCH.
    assert text.index('"KXLAX"') < text.index('"KXTEMPNYCH"')
    assert "kxlax" not in text


def test_get_or_fetch_miss_then_hit(tmp_path):
    path = tmp_path / "series_contract_terms.json"
    client = FakeClient({"KXTEMPNYCH": _KXTEMP})

    async def scenario():
        first = await contract_terms.get_or_fetch("KXTEMPNYCH", client, path=path)
        # Second call (and even lowercase) is a cache hit — no new API call.
        second = await contract_terms.get_or_fetch("kxtempnych", client, path=path)
        return first, second

    first, second = asyncio.run(scenario())
    assert first["contract_terms_url"] == "https://example.s3/NHIGHD.pdf"
    assert "fetched_at" in first
    assert second == first
    assert client.calls == ["KXTEMPNYCH"]  # exactly one fetch


def test_get_or_fetch_many_dedups_ladder_to_one_call(tmp_path):
    path = tmp_path / "series_contract_terms.json"
    client = FakeClient({"KXTEMPNYCH": _KXTEMP, "KXHIGHLAX": _KXLAX})
    ladder = ["KXTEMPNYCH", "KXTEMPNYCH", "KXTEMPNYCH", "KXHIGHLAX"]

    result = asyncio.run(contract_terms.get_or_fetch_many(ladder, client, path=path))

    assert set(result) == {"KXTEMPNYCH", "KXHIGHLAX"}
    # Distinct series only: two fetches, not four.
    assert sorted(client.calls) == ["KXHIGHLAX", "KXTEMPNYCH"]


def test_get_or_fetch_many_second_cycle_is_free(tmp_path):
    path = tmp_path / "series_contract_terms.json"
    client = FakeClient({"KXTEMPNYCH": _KXTEMP})

    async def scenario():
        await contract_terms.get_or_fetch_many(["KXTEMPNYCH"], client, path=path)
        await contract_terms.get_or_fetch_many(["KXTEMPNYCH"], client, path=path)

    asyncio.run(scenario())
    assert client.calls == ["KXTEMPNYCH"]  # cached after the first cycle


def test_get_or_fetch_many_tolerates_failure(tmp_path):
    path = tmp_path / "series_contract_terms.json"
    client = FakeClient({"KXTEMPNYCH": _KXTEMP}, fail={"KXBROKEN"})

    result = asyncio.run(
        contract_terms.get_or_fetch_many(["KXTEMPNYCH", "KXBROKEN"], client, path=path)
    )

    # Good series resolves; broken one is omitted and not cached.
    assert "KXTEMPNYCH" in result
    assert "KXBROKEN" not in result
    assert "KXBROKEN" not in contract_terms.load_contract_terms(path)
