from __future__ import annotations
import json
from datetime import datetime
from typing import TypedDict
import aiohttp
from kalshi_trader import config


class GrokSearchResult(TypedDict):
    probability: float
    uncertainty: float
    summary: str
    key_quotes: list
    sentiment_breakdown: dict
    source_quality: dict
    velocity: dict
    key_entities: list
    contrarian_signal: str
    issued_at: str


def _empty_result() -> GrokSearchResult:
    return GrokSearchResult(
        probability=0.5,
        uncertainty=1.0,
        summary="",
        key_quotes=[],
        sentiment_breakdown={"positive": 0.33, "negative": 0.33, "neutral": 0.34},
        source_quality={"high_follower": 0.0, "general": 1.0},
        velocity={"1h": 0, "6h": 0, "24h": 0},
        key_entities=[],
        contrarian_signal="",
        issued_at=datetime.utcnow().isoformat(),
    )


def _parse_grok_response(text: str) -> GrokSearchResult:
    text = text.strip()
    if "```" in text:
        parts = text.split("```")
        for part in parts:
            part = part.strip()
            if part.startswith("json"):
                part = part[4:].strip()
            if part.startswith("{"):
                text = part
                break
    try:
        data = json.loads(text)
        if not data.get("issued_at"):
            data["issued_at"] = datetime.utcnow().isoformat()
        return data  # type: ignore[return-value]
    except (json.JSONDecodeError, ValueError):
        return _empty_result()


_SEARCH_PROMPT = (
    "Search X (Twitter) for posts about: {query}\n\n"
    "You are helping analyse a Kalshi prediction market: \"{market_title}\"\n\n"
    "Return ONLY a JSON object with exactly these fields:\n"
    '{{\n'
    '  "probability": <float 0.0-1.0, your estimate of YES resolution probability>,\n'
    '  "uncertainty": <float 0.0-1.0, where 0.05=very confident, 0.4=very uncertain>,\n'
    '  "summary": "<2-3 sentence summary of what X posts are saying>",\n'
    '  "key_quotes": ["<post 1>", "<post 2>"],\n'
    '  "sentiment_breakdown": {{"positive": <float>, "negative": <float>, "neutral": <float>}},\n'
    '  "source_quality": {{"high_follower": <float>, "general": <float>}},\n'
    '  "velocity": {{"1h": <int>, "6h": <int>, "24h": <int>}},\n'
    '  "key_entities": ["<entity1>"],\n'
    '  "contrarian_signal": "<notable minority view, or empty string>",\n'
    '  "issued_at": "<ISO 8601 timestamp>"\n'
    '}}'
)


class XClient:
    def __init__(self) -> None:
        self._session: aiohttp.ClientSession | None = None

    async def live_search(self, query: str, market_title: str = "") -> GrokSearchResult:
        if not config.XAI_API_KEY:
            return _empty_result()

        if self._session is None:
            self._session = aiohttp.ClientSession()

        payload = {
            "model": config.XAI_MODEL,
            "messages": [{
                "role": "user",
                "content": _SEARCH_PROMPT.format(query=query, market_title=market_title),
            }],
            "search_parameters": {"mode": "on", "sources": [{"type": "x"}]},
        }
        headers = {
            "Authorization": f"Bearer {config.XAI_API_KEY}",
            "Content-Type": "application/json",
        }

        try:
            async with self._session.post(
                f"{config.XAI_BASE_URL}/chat/completions",
                json=payload,
                headers=headers,
                timeout=aiohttp.ClientTimeout(total=30),
            ) as resp:
                resp.raise_for_status()
                data = await resp.json()
                text = data["choices"][0]["message"]["content"]
                return _parse_grok_response(text)
        except Exception:
            return _empty_result()

    async def close(self) -> None:
        if self._session:
            await self._session.close()
            self._session = None
