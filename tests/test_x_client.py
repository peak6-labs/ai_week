from __future__ import annotations
import json
import pytest
import aiohttp
from unittest.mock import AsyncMock, MagicMock
from kalshi_trader.external.x_client import XClient, GrokSearchResult, _empty_result


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


@pytest.mark.asyncio
async def test_returns_empty_result_when_api_key_missing(monkeypatch):
    monkeypatch.setattr("kalshi_trader.config.XAI_API_KEY", "")
    client = XClient()
    result = await client.live_search("test query", "test market")
    assert result["probability"] == 0.5
    assert result["uncertainty"] == 1.0


@pytest.mark.asyncio
async def test_parses_json_from_grok_response(monkeypatch):
    monkeypatch.setattr("kalshi_trader.config.XAI_API_KEY", "test-key")

    payload = {
        "probability": 0.72,
        "uncertainty": 0.09,
        "summary": "Bulls are favoured on X.",
        "key_quotes": ["Bulls will win", "Easy sweep"],
        "sentiment_breakdown": {"positive": 0.7, "negative": 0.1, "neutral": 0.2},
        "source_quality": {"high_follower": 0.5, "general": 0.5},
        "velocity": {"1h": 10, "6h": 40, "24h": 120},
        "key_entities": ["Bulls", "Heat"],
        "contrarian_signal": "",
        "issued_at": "2026-06-01T12:00:00",
    }
    api_response = {"choices": [{"message": {"content": json.dumps(payload)}}]}
    mock_resp = _MockResponse(api_response)

    mock_session = MagicMock()
    mock_session.post = MagicMock(return_value=mock_resp)

    client = XClient()
    client._session = mock_session

    result = await client.live_search("Bulls win series", "Will Bulls win the series?")
    assert result["probability"] == 0.72
    assert result["uncertainty"] == 0.09
    assert result["summary"] == "Bulls are favoured on X."


@pytest.mark.asyncio
async def test_parses_json_wrapped_in_markdown_code_block(monkeypatch):
    monkeypatch.setattr("kalshi_trader.config.XAI_API_KEY", "test-key")

    payload = {
        "probability": 0.5, "uncertainty": 0.2, "summary": "Mixed views.",
        "key_quotes": [], "sentiment_breakdown": {"positive": 0.5, "negative": 0.3, "neutral": 0.2},
        "source_quality": {"high_follower": 0.3, "general": 0.7},
        "velocity": {"1h": 1, "6h": 5, "24h": 20},
        "key_entities": [], "contrarian_signal": "", "issued_at": "2026-06-01T10:00:00",
    }
    wrapped = f"```json\n{json.dumps(payload)}\n```"
    api_response = {"choices": [{"message": {"content": wrapped}}]}
    mock_resp = _MockResponse(api_response)
    mock_session = MagicMock()
    mock_session.post = MagicMock(return_value=mock_resp)

    client = XClient()
    client._session = mock_session

    result = await client.live_search("query", "market")
    assert result["probability"] == 0.5


@pytest.mark.asyncio
async def test_returns_empty_result_on_network_error(monkeypatch):
    monkeypatch.setattr("kalshi_trader.config.XAI_API_KEY", "test-key")

    mock_session = MagicMock()
    mock_session.post = MagicMock(side_effect=aiohttp.ClientConnectionError("Network error"))

    client = XClient()
    client._session = mock_session

    result = await client.live_search("query", "market")
    assert result["uncertainty"] == 1.0


@pytest.mark.asyncio
async def test_returns_empty_result_on_invalid_json(monkeypatch):
    monkeypatch.setattr("kalshi_trader.config.XAI_API_KEY", "test-key")

    api_response = {"choices": [{"message": {"content": "This is not JSON at all."}}]}
    mock_resp = _MockResponse(api_response)
    mock_session = MagicMock()
    mock_session.post = MagicMock(return_value=mock_resp)

    client = XClient()
    client._session = mock_session

    result = await client.live_search("query", "market")
    assert result["uncertainty"] == 1.0


@pytest.mark.asyncio
async def test_returns_empty_result_on_partial_json(monkeypatch):
    """Partial response missing required keys must fall through to _empty_result."""
    monkeypatch.setattr("kalshi_trader.config.XAI_API_KEY", "test-key")

    partial = {"probability": 0.8}  # missing 9 required keys
    api_response = {"choices": [{"message": {"content": json.dumps(partial)}}]}
    mock_resp = _MockResponse(api_response)
    mock_session = MagicMock()
    mock_session.post = MagicMock(return_value=mock_resp)

    client = XClient()
    client._session = mock_session

    result = await client.live_search("query", "market")
    assert result["probability"] == 0.5
    assert result["uncertainty"] == 1.0
    assert result["summary"] == ""
