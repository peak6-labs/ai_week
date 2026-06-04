"""Tests for the per-series settlement-terms cache.

Covers the load/save round-trip, the cache-first get_or_fetch (one API call on a
miss, zero on a hit), and the batch get_or_fetch_many that dedups a strike ladder
to a single series call and tolerates a failed lookup.
"""
from __future__ import annotations

import asyncio
from collections import Counter

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


class _RateLimitError(Exception):
    """Mimics a Kalshi 429: carries ``.response.status_code == 429`` so
    ``with_retry`` treats it as rate-limited and backs off rather than raising."""

    def __init__(self) -> None:
        super().__init__("rate limited")
        self.response = type("_Resp", (), {"status_code": 429})()


class RateLimitingClient:
    """429s the first ``fail_first`` calls for each series, then succeeds.

    Tracks the peak number of concurrently in-flight ``get_series_detail`` calls
    so a test can assert the batch honored the concurrency cap.
    """

    def __init__(self, by_series: dict[str, dict], *, fail_first: int) -> None:
        self._by_series = by_series
        self._remaining_failures = {ticker: fail_first for ticker in by_series}
        self.calls: list[str] = []
        self._active = 0
        self.max_concurrent = 0

    async def get_series_detail(self, series_ticker: str) -> dict:
        self._active += 1
        self.max_concurrent = max(self.max_concurrent, self._active)
        try:
            await asyncio.sleep(0)  # yield so queued tasks can interleave
            self.calls.append(series_ticker)
            if self._remaining_failures.get(series_ticker, 0) > 0:
                self._remaining_failures[series_ticker] -= 1
                raise _RateLimitError()
            return {"series": self._by_series[series_ticker]}
        finally:
            self._active -= 1


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


def test_get_or_fetch_many_retries_429_and_caps_concurrency(tmp_path, monkeypatch):
    # Make with_retry's exponential backoff instant so the test is fast, while
    # still yielding to the event loop so concurrent tasks interleave.
    real_sleep = asyncio.sleep

    async def _fast_sleep(_seconds):
        await real_sleep(0)

    monkeypatch.setattr(asyncio, "sleep", _fast_sleep)

    path = tmp_path / "series_contract_terms.json"
    by_series = {f"KXHIGH{index:02d}": dict(_KXLAX) for index in range(12)}
    client = RateLimitingClient(by_series, fail_first=2)  # each 429s twice, then succeeds

    result = asyncio.run(contract_terms.get_or_fetch_many(list(by_series), client, path=path))

    # Every series resolved despite the 429s — with_retry backed off and retried.
    assert set(result) == set(by_series)
    assert all("fetched_at" in entry for entry in result.values())
    # Each series: 2 failed attempts + 1 success = 3 calls.
    call_counts = Counter(client.calls)
    assert all(count == 3 for count in call_counts.values())
    # The fan-out never exceeded the concurrency cap (12 series, cap 6).
    assert client.max_concurrent <= contract_terms._FETCH_CONCURRENCY
    assert client.max_concurrent > 1  # but it WAS concurrent, not serialized
