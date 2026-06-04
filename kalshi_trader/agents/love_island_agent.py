"""Love Island signal agent — fuses YouTube teasers + Grok X sentiment.

Mirrors :class:`kalshi_trader.agents.x_agent.XAgent`: a :class:`BaseAgent` with
data-fetch tools (YouTube teaser search, best-effort transcript, Grok x_search,
the curated mentions catchphrase prior) plus a ``build_love_island_signal`` tool
whose weight/uncertainty are fixed per evidence tier so the model controls only
the probability. ``run`` returns a ``list[SignalEstimate]``.
"""
from __future__ import annotations

from pathlib import Path

from kalshi_trader.models import SignalEstimate
from kalshi_trader.external.youtube_client import (
    LOVE_ISLAND_UK_CHANNEL_ID,
    LOVE_ISLAND_USA_CHANNEL_ID,
    YouTubeClient,
)
from kalshi_trader.external.x_client import XClient
from kalshi_trader.agents.base import BaseAgent
from kalshi_trader.agents.parsing import parse_signal_estimates, estimate_to_dict
from kalshi_trader.signals.love_island import build_love_island_signal, mentions_transcript_gate
from kalshi_trader.signals.love_island_lexicon import lookup_catchphrase_prior
from kalshi_trader.signals.love_island_x_accounts import love_island_x_query_focus

_PROMPTS_DIR = Path(__file__).parent / "prompts"

_SCHEMAS: list[dict] = [
    {
        "name": "search_youtube_teasers",
        "description": (
            "Search YouTube for official pre-episode teaser / 'First Look' videos. "
            "Returns normalized video records (video_id, title, description, "
            "published_at). To honor the same-day rule, pass published_after = start "
            "of the settlement day and published_before = the settlement time (RFC "
            "3339) so only that day's teaser is returned. Empty list means nothing "
            "was found."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {"type": "string"},
                "channel_id": {"type": "string", "description": "Optional official channel id to scope the search."},
                "published_after": {"type": "string", "description": "Optional RFC 3339 lower bound — set to the start of the settlement day."},
                "published_before": {"type": "string", "description": "Optional RFC 3339 upper bound — set to the settlement time."},
                "max_results": {"type": "integer"},
            },
            "required": ["query"],
        },
    },
    {
        "name": "fetch_youtube_transcript",
        "description": (
            "Best-effort public caption text for a video id. Often empty (third-party "
            "captions are not openly served) — empty means 'no transcript', not evidence."
        ),
        "input_schema": {
            "type": "object",
            "properties": {"video_id": {"type": "string"}},
            "required": ["video_id"],
        },
    },
    {
        "name": "search_x_sentiment",
        "description": (
            "Search X (Twitter) via Grok for fan sentiment / news about the market. "
            "Returns a probability + summary + sentiment breakdown. Best for public-vote "
            "markets (winners, couples). uncertainty=1.0 with empty summary means no signal."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {"type": "string"},
                "market_title": {"type": "string"},
            },
            "required": ["query", "market_title"],
        },
    },
    {
        "name": "lookup_catchphrase_prior",
        "description": (
            "For mentions markets only: look up the curated per-episode base rate that a "
            "Love Island catchphrase is said. matched=false means no franchise staple "
            "matched — treat as 'no prior', not a low probability."
        ),
        "input_schema": {
            "type": "object",
            "properties": {"phrase": {"type": "string"}},
            "required": ["phrase"],
        },
    },
    {
        "name": "build_love_island_signal",
        "description": (
            "Record one final SignalEstimate. weight/uncertainty are set automatically "
            "by evidence_strength; you supply probability, evidence_strength, "
            "market_bucket, narrative, and sources."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "ticker": {"type": "string"},
                "probability": {"type": "number"},
                "evidence_strength": {
                    "type": "string",
                    "enum": ["teaser_confirmed", "teaser_hinted", "sentiment_only", "prior_only"],
                },
                "market_bucket": {
                    "type": "string",
                    "enum": ["binary_event", "elimination", "winner", "mentions"],
                },
                "narrative": {"type": "string"},
                "sources": {"type": "array", "items": {"type": "string"}},
            },
            "required": ["ticker", "probability", "evidence_strength", "market_bucket", "narrative"],
        },
    },
]


class LoveIslandAgent:
    def __init__(self) -> None:
        self._youtube = YouTubeClient()
        self._x_client = XClient()
        self._current_ticker = ""
        # Count of non-empty transcripts fetched this run — gates mentions signals,
        # which must be transcript-backed (not built from the prior alone).
        self._nonempty_transcript_count = 0
        system_prompt = (_PROMPTS_DIR / "love_island.md").read_text()
        self._agent = BaseAgent(
            tools=_SCHEMAS,
            handlers={
                "search_youtube_teasers": self._search_youtube_teasers,
                "fetch_youtube_transcript": self._fetch_youtube_transcript,
                "search_x_sentiment": self._search_x_sentiment,
                "lookup_catchphrase_prior": self._lookup_catchphrase_prior,
                "build_love_island_signal": self._build_love_island_signal,
            },
            system_prompt=system_prompt,
        )

    async def run(self, ticker: str, title: str, category: str) -> list[SignalEstimate]:
        self._current_ticker = ticker
        self._nonempty_transcript_count = 0
        prompt = (
            f"Analyze this Love Island Kalshi market for a signal:\n"
            f"ticker: {ticker}\n"
            f"category: {category}\n"
            f"title: {title}"
        )
        raw = await self._agent.run(prompt)
        return parse_signal_estimates(raw)

    def _default_channel(self) -> str:
        """Official channel for the current market's region — scopes out fan re-uploads."""
        if "LIUK" in self._current_ticker.upper():
            return LOVE_ISLAND_UK_CHANNEL_ID
        return LOVE_ISLAND_USA_CHANNEL_ID

    async def _search_youtube_teasers(
        self,
        query: str,
        channel_id: str | None = None,
        published_after: str | None = None,
        published_before: str | None = None,
        max_results: int = 10,
    ) -> list[dict]:
        # Default to the official channel so fan re-uploads (whose clickbait titles
        # falsely match event keywords) don't pollute the teaser evidence, and to
        # newest-first so the upcoming episode's teaser leads — not a relevance
        # match against old seasons/spin-offs on the same channel. published_after /
        # published_before let the agent enforce the same-day-as-settlement rule.
        return await self._youtube.search_videos(
            query,
            channel_id=channel_id or self._default_channel(),
            published_after=published_after,
            published_before=published_before,
            order="date",
            max_results=max_results,
        )

    async def _fetch_youtube_transcript(self, video_id: str) -> dict:
        text = await self._youtube.fetch_transcript(video_id)
        if text:
            self._nonempty_transcript_count += 1
        return {"video_id": video_id, "available": bool(text), "text": text}

    async def _search_x_sentiment(self, query: str, market_title: str) -> dict:
        # Focus Grok's x_search on the curated Love Island accounts + hashtags.
        focused_query = f"{query}{love_island_x_query_focus()}"
        return dict(await self._x_client.live_search(focused_query, market_title))

    async def _lookup_catchphrase_prior(self, phrase: str) -> dict:
        return lookup_catchphrase_prior(phrase)

    async def _build_love_island_signal(
        self,
        ticker: str,
        probability: float,
        evidence_strength: str,
        market_bucket: str,
        narrative: str,
        sources: list[str] | None = None,
    ) -> dict:
        # Mentions markets must be transcript-backed: reject the build (sending the
        # agent back to fetch a transcript) until one has been read this run.
        gate_message = mentions_transcript_gate(market_bucket, self._nonempty_transcript_count)
        if gate_message:
            return {"error": gate_message}
        estimate = build_love_island_signal(
            ticker=ticker,
            probability=probability,
            evidence_strength=evidence_strength,
            market_bucket=market_bucket,
            narrative=narrative,
            sources=sources,
        )
        return estimate_to_dict(estimate)

    async def close(self) -> None:
        await self._youtube.close()
        await self._x_client.close()
