"""Tests for the X-profile leading indicator: XClient.profile_topic_scan +
build_x_profile_signal."""
from __future__ import annotations

import json

import aiohttp
import pytest
from unittest.mock import MagicMock

from kalshi_trader.external.x_client import XClient, _parse_profile_response
from kalshi_trader.signals.mentions import (
    SOURCE_X_PROFILE,
    WEIGHT_X_PROFILE,
    build_x_profile_signal,
)


class _MockResponse:
    def __init__(self, data: dict):
        self._data = data

    def raise_for_status(self) -> None:
        pass

    async def json(self) -> dict:
        return self._data

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_):
        pass


def _responses_payload(text: str) -> dict:
    return {
        "output": [
            {"type": "reasoning", "summary": [], "id": "r1"},
            {"type": "custom_tool_call", "name": "x_search", "id": "t1"},
            {
                "type": "message",
                "role": "assistant",
                "content": [{"type": "output_text", "text": text, "annotations": []}],
            },
        ]
    }


_VALID_PROFILE_PAYLOAD = {
    "post_count": 4,
    "probability": 0.72,
    "uncertainty": 0.2,
    "issued_at": "2026-06-02T09:30:00",
    "summary": "The accounts have posted repeatedly about uranium this week.",
    "key_quotes": ["We need more uranium.", "Uranium deal soon."],
}


# ---------------------------------------------------------------------------
# profile_topic_scan
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_profile_scan_empty_when_api_key_missing(monkeypatch):
    monkeypatch.setattr("kalshi_trader.config.XAI_API_KEY", "")
    client = XClient()
    result = await client.profile_topic_scan(["realdonaldtrump"], "uranium", "this week")
    assert result["post_count"] == 0


@pytest.mark.asyncio
async def test_profile_scan_empty_when_no_handles_does_not_call_grok(monkeypatch):
    monkeypatch.setattr("kalshi_trader.config.XAI_API_KEY", "test-key")
    mock_session = MagicMock()
    mock_session.post = MagicMock()
    client = XClient()
    client._session = mock_session

    result = await client.profile_topic_scan([], "uranium", "this week")
    assert result["post_count"] == 0
    mock_session.post.assert_not_called()


@pytest.mark.asyncio
async def test_profile_scan_parses_valid_json(monkeypatch):
    monkeypatch.setattr("kalshi_trader.config.XAI_API_KEY", "test-key")
    mock_session = MagicMock()
    mock_session.post = MagicMock(return_value=_MockResponse(
        _responses_payload(json.dumps(_VALID_PROFILE_PAYLOAD))))
    client = XClient()
    client._session = mock_session

    result = await client.profile_topic_scan(["realdonaldtrump", "potus"], "uranium", "this week")
    assert result["post_count"] == 4
    assert result["probability"] == pytest.approx(0.72)


@pytest.mark.asyncio
async def test_profile_scan_passes_handles_to_allowed_x_handles(monkeypatch):
    monkeypatch.setattr("kalshi_trader.config.XAI_API_KEY", "test-key")
    mock_session = MagicMock()
    mock_session.post = MagicMock(return_value=_MockResponse(
        _responses_payload(json.dumps(_VALID_PROFILE_PAYLOAD))))
    client = XClient()
    client._session = mock_session

    handles = ["realdonaldtrump", "potus", "trumpdailyposts", "donaldjtrumpjr", "jdvance"]
    await client.profile_topic_scan(handles, "uranium", "this week")
    posted_json = mock_session.post.call_args.kwargs["json"]
    tool = posted_json["tools"][0]
    assert tool["type"] == "x_search"
    assert tool["allowed_x_handles"] == handles


@pytest.mark.asyncio
async def test_profile_scan_caps_handles_at_twenty(monkeypatch):
    monkeypatch.setattr("kalshi_trader.config.XAI_API_KEY", "test-key")
    mock_session = MagicMock()
    mock_session.post = MagicMock(return_value=_MockResponse(
        _responses_payload(json.dumps(_VALID_PROFILE_PAYLOAD))))
    client = XClient()
    client._session = mock_session

    await client.profile_topic_scan([f"h{i}" for i in range(30)], "x", "this week")
    tool = mock_session.post.call_args.kwargs["json"]["tools"][0]
    assert len(tool["allowed_x_handles"]) == 20


@pytest.mark.asyncio
async def test_profile_scan_network_error_returns_empty(monkeypatch):
    monkeypatch.setattr("kalshi_trader.config.XAI_API_KEY", "test-key")
    mock_session = MagicMock()
    mock_session.post = MagicMock(side_effect=aiohttp.ClientConnectionError("boom"))
    client = XClient()
    client._session = mock_session

    result = await client.profile_topic_scan(["realdonaldtrump"], "uranium", "this week")
    assert result["post_count"] == 0


def test_parse_profile_partial_json_returns_empty():
    # Missing required keys → empty sentinel.
    assert _parse_profile_response(json.dumps({"post_count": 3}))["post_count"] == 0


# ---------------------------------------------------------------------------
# build_x_profile_signal
# ---------------------------------------------------------------------------

def test_build_x_profile_signal_emits_for_active_timeline():
    sig = build_x_profile_signal(
        "KXMENTION-TRUMP-URANIUM", "uranium", _VALID_PROFILE_PAYLOAD,
        handles=["realdonaldtrump", "potus"], speaker="Donald Trump",
    )
    assert sig is not None
    assert sig.source == SOURCE_X_PROFILE
    assert sig.source.startswith("x_grok")          # folds into the X family
    assert sig.probability == pytest.approx(0.72)
    assert sig.weight == pytest.approx(WEIGHT_X_PROFILE)
    assert sig.weight < 0.6                          # modest (predictor, not measurement)
    assert sig.metadata["post_count"] == 4
    assert sig.metadata["independent"] is True
    # data_issued_at is the most-recent post timestamp (recency), not now().
    assert sig.data_issued_at.isoformat().startswith("2026-06-02T09:30:00")


def test_build_x_profile_signal_quiet_timeline_returns_none():
    quiet = {"post_count": 0, "probability": 0.5, "uncertainty": 1.0,
             "issued_at": "2026-06-02T09:30:00", "summary": "", "key_quotes": []}
    assert build_x_profile_signal("T", "uranium", quiet, handles=["realdonaldtrump"]) is None


def test_build_x_profile_signal_probability_clamped():
    payload = {**_VALID_PROFILE_PAYLOAD, "probability": 1.5}
    sig = build_x_profile_signal("T", "x", payload, handles=["a"])
    assert sig.probability <= 0.99
