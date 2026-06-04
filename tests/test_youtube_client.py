"""Tests for kalshi_trader/external/youtube_client.py — pure parsers + fail-soft client."""
from __future__ import annotations

import pytest

from kalshi_trader.external.youtube_client import (
    YouTubeClient,
    parse_search_results,
    parse_video_list,
)


def test_parse_search_results_extracts_video_records():
    search_json = {
        "items": [
            {
                "id": {"kind": "youtube#video", "videoId": "abc123"},
                "snippet": {
                    "title": "Love Island USA First Look",
                    "description": "Bombshells arrive tonight",
                    "publishedAt": "2026-06-04T18:00:00Z",
                    "channelTitle": "Love Island USA",
                    "channelId": "chan1",
                },
            },
            # A channel result has no videoId and must be skipped.
            {"id": {"kind": "youtube#channel", "channelId": "chan2"}, "snippet": {"title": "Peacock"}},
        ]
    }
    records = parse_search_results(search_json)
    assert len(records) == 1
    assert records[0]["video_id"] == "abc123"
    assert records[0]["title"] == "Love Island USA First Look"
    assert records[0]["published_at"] == "2026-06-04T18:00:00Z"


def test_parse_video_list_uses_top_level_id():
    videos_json = {
        "items": [
            {"id": "vid9", "snippet": {"title": "First Look", "description": "teaser"}},
            {"snippet": {"title": "no id, skipped"}},
        ]
    }
    records = parse_video_list(videos_json)
    assert len(records) == 1
    assert records[0]["video_id"] == "vid9"
    assert records[0]["title"] == "First Look"


def test_parsers_tolerate_empty_or_garbage():
    assert parse_search_results({}) == []
    assert parse_search_results({"items": [None, "x"]}) == []
    assert parse_video_list({}) == []


@pytest.mark.asyncio
async def test_search_videos_no_key_returns_empty_no_session():
    client = YouTubeClient(api_key="")
    assert await client.search_videos("Love Island First Look") == []
    # No session was ever created (no network attempted).
    assert client._session is None


@pytest.mark.asyncio
async def test_list_videos_empty_inputs_return_empty():
    client = YouTubeClient(api_key="")
    assert await client.list_videos(["x"]) == []          # no key
    other = YouTubeClient(api_key="present")
    assert await other.list_videos([]) == []              # no ids → no call
    assert other._session is None


@pytest.mark.asyncio
async def test_fetch_transcript_empty_video_id_returns_empty():
    client = YouTubeClient(api_key="present")
    assert await client.fetch_transcript("") == ""
    assert client._session is None
