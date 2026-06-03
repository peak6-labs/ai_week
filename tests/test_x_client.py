from __future__ import annotations
import json
import pytest
import aiohttp
from unittest.mock import AsyncMock, MagicMock
from kalshi_trader.external.x_client import (
    XClient,
    GrokSearchResult,
    _empty_result,
    _parse_authority_response,
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
    """Build a /v1/responses body whose assistant message carries ``text``.

    Mirrors the Agent Tools API: an ``output`` array with reasoning/tool-call
    items followed by a ``message`` item holding ``output_text`` content.
    """
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
    mock_resp = _MockResponse(_responses_payload(json.dumps(payload)))

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
    mock_resp = _MockResponse(_responses_payload(wrapped))
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

    mock_resp = _MockResponse(_responses_payload("This is not JSON at all."))
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
    mock_resp = _MockResponse(_responses_payload(json.dumps(partial)))
    mock_session = MagicMock()
    mock_session.post = MagicMock(return_value=mock_resp)

    client = XClient()
    client._session = mock_session

    result = await client.live_search("query", "market")
    assert result["probability"] == 0.5
    assert result["uncertainty"] == 1.0
    assert result["summary"] == ""


# ---------------------------------------------------------------------------
# forecast_search (authority meteorologist polling)
# ---------------------------------------------------------------------------

_VALID_AUTHORITY_PAYLOAD = {
    "temp_high": 88,
    "temp_low": 71,
    "precip_pct": 20,
    "confidence": "high",
    "post_count": 2,
    "issued_at": "2026-06-05T13:30:00",
    "summary": "WFAA forecasts a high near 88F for Friday.",
    "key_quotes": ["High 88, low 71, 20% rain chance Friday."],
}


@pytest.mark.asyncio
async def test_forecast_search_empty_when_api_key_missing(monkeypatch):
    monkeypatch.setattr("kalshi_trader.config.XAI_API_KEY", "")
    client = XClient()
    result = await client.forecast_search(["wfaaweather"], "dallas", "2026-06-05", "temp_high")
    assert result["post_count"] == 0
    assert result["temp_high"] is None


@pytest.mark.asyncio
async def test_forecast_search_empty_when_no_handles_does_not_call_grok(monkeypatch):
    monkeypatch.setattr("kalshi_trader.config.XAI_API_KEY", "test-key")
    mock_session = MagicMock()
    mock_session.post = MagicMock()
    client = XClient()
    client._session = mock_session

    result = await client.forecast_search([], "san diego", "2026-06-05", "temp_high")
    assert result["post_count"] == 0
    mock_session.post.assert_not_called()


@pytest.mark.asyncio
async def test_forecast_search_parses_valid_json(monkeypatch):
    monkeypatch.setattr("kalshi_trader.config.XAI_API_KEY", "test-key")
    mock_session = MagicMock()
    mock_session.post = MagicMock(return_value=_MockResponse(
        _responses_payload(json.dumps(_VALID_AUTHORITY_PAYLOAD))))
    client = XClient()
    client._session = mock_session

    result = await client.forecast_search(["wfaaweather"], "dallas", "2026-06-05", "temp_high")
    assert result["temp_high"] == 88.0
    assert result["temp_low"] == 71.0
    assert result["precip_pct"] == 20.0
    assert result["post_count"] == 2
    assert result["confidence"] == "high"


@pytest.mark.asyncio
async def test_forecast_search_parses_markdown_fenced_json(monkeypatch):
    monkeypatch.setattr("kalshi_trader.config.XAI_API_KEY", "test-key")
    wrapped = f"```json\n{json.dumps(_VALID_AUTHORITY_PAYLOAD)}\n```"
    mock_session = MagicMock()
    mock_session.post = MagicMock(return_value=_MockResponse(_responses_payload(wrapped)))
    client = XClient()
    client._session = mock_session

    result = await client.forecast_search(["wfaaweather"], "dallas", "2026-06-05", "temp_high")
    assert result["temp_high"] == 88.0
    assert result["post_count"] == 2


@pytest.mark.asyncio
async def test_forecast_search_network_error_returns_empty(monkeypatch):
    monkeypatch.setattr("kalshi_trader.config.XAI_API_KEY", "test-key")
    mock_session = MagicMock()
    mock_session.post = MagicMock(side_effect=aiohttp.ClientConnectionError("Network error"))
    client = XClient()
    client._session = mock_session

    result = await client.forecast_search(["wfaaweather"], "dallas", "2026-06-05", "temp_high")
    assert result["post_count"] == 0
    assert result["temp_high"] is None


@pytest.mark.asyncio
async def test_forecast_search_invalid_json_returns_empty(monkeypatch):
    monkeypatch.setattr("kalshi_trader.config.XAI_API_KEY", "test-key")
    mock_session = MagicMock()
    mock_session.post = MagicMock(return_value=_MockResponse(_responses_payload("no json here")))
    client = XClient()
    client._session = mock_session

    result = await client.forecast_search(["wfaaweather"], "dallas", "2026-06-05", "temp_high")
    assert result["post_count"] == 0


@pytest.mark.asyncio
async def test_forecast_search_partial_json_returns_empty(monkeypatch):
    monkeypatch.setattr("kalshi_trader.config.XAI_API_KEY", "test-key")
    partial = {"temp_high": 88}  # missing the rest of the required keys
    mock_session = MagicMock()
    mock_session.post = MagicMock(return_value=_MockResponse(_responses_payload(json.dumps(partial))))
    client = XClient()
    client._session = mock_session

    result = await client.forecast_search(["wfaaweather"], "dallas", "2026-06-05", "temp_high")
    assert result["post_count"] == 0
    assert result["temp_high"] is None


@pytest.mark.asyncio
async def test_forecast_search_restricts_to_allowed_handles(monkeypatch):
    monkeypatch.setattr("kalshi_trader.config.XAI_API_KEY", "test-key")
    mock_session = MagicMock()
    mock_session.post = MagicMock(return_value=_MockResponse(
        _responses_payload(json.dumps(_VALID_AUTHORITY_PAYLOAD))))
    client = XClient()
    client._session = mock_session

    await client.forecast_search(["h1", "h2"], "dallas", "2026-06-05", "temp_high")

    posted_json = mock_session.post.call_args.kwargs["json"]
    # The x_search tool is restricted to exactly the city's authority handles.
    assert posted_json["tools"] == [{"type": "x_search", "allowed_x_handles": ["h1", "h2"]}]
    # The handles are also named in the prompt for the model's context.
    assert "@h1" in posted_json["input"][0]["content"]


@pytest.mark.asyncio
async def test_forecast_search_posts_to_responses_endpoint(monkeypatch):
    monkeypatch.setattr("kalshi_trader.config.XAI_API_KEY", "test-key")
    mock_session = MagicMock()
    mock_session.post = MagicMock(return_value=_MockResponse(
        _responses_payload(json.dumps(_VALID_AUTHORITY_PAYLOAD))))
    client = XClient()
    client._session = mock_session

    await client.forecast_search(["wfaaweather"], "dallas", "2026-06-05", "temp_high")

    posted_url = mock_session.post.call_args.args[0]
    assert posted_url.endswith("/responses")


def test_parse_authority_response_coerces_post_count_zero_on_garbage():
    result = _parse_authority_response("not even close to json")
    assert result["post_count"] == 0
    assert result["temp_high"] is None
    assert result["confidence"] == "low"
