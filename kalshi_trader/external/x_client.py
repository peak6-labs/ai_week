from __future__ import annotations
import asyncio
import json
import ssl
from datetime import datetime, timezone
from typing import TypedDict
import aiohttp
from kalshi_trader import config


# The Agent Tools API (/v1/responses) runs a reasoning model that calls x_search
# and reflects before answering, so it is markedly slower than the old one-shot
# /chat/completions call. Give it room.
_RESPONSES_TIMEOUT_SECONDS = 90


def _build_ssl_context() -> ssl.SSLContext:
    """SSL context that trusts the OS trust store.

    Behind the corporate proxy (Zscaler) the x.ai cert chain ends in a
    self-signed root that only lives in the system trust store, not certifi's
    bundle — so a default aiohttp context fails verification (and the failure is
    silently swallowed into an empty result). truststore reads the OS store and
    fixes this, exactly as noaa.py / gdelt.py / fivethirtyeight.py do. Falls back
    to the default context if truststore is unavailable.
    """
    try:
        import truststore
        return truststore.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    except Exception:  # pragma: no cover - truststore optional
        return ssl.create_default_context()


def _extract_responses_text(data: dict) -> str:
    """Concatenate the assistant ``output_text`` blocks from a /v1/responses body.

    The Agent Tools API returns an ``output`` array interleaving ``reasoning`` and
    ``custom_tool_call`` items; the final assistant answer lives in a ``message``
    item whose ``content`` holds one or more ``output_text`` blocks. Returns ""
    when no such block is present.
    """
    parts: list[str] = []
    for item in data.get("output", []) or []:
        for block in item.get("content", []) or []:
            if isinstance(block, dict) and block.get("type") == "output_text" and block.get("text"):
                parts.append(str(block["text"]))
    return "\n".join(parts)


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


_REQUIRED_KEYS = {
    "probability", "uncertainty", "summary", "key_quotes",
    "sentiment_breakdown", "source_quality", "velocity",
    "key_entities", "contrarian_signal", "issued_at",
}


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
        issued_at=datetime.now(timezone.utc).replace(tzinfo=None).isoformat(),
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
        if not isinstance(data, dict) or not _REQUIRED_KEYS.issubset(data):
            return _empty_result()
        if not data.get("issued_at"):
            data["issued_at"] = datetime.now(timezone.utc).replace(tzinfo=None).isoformat()
        return data  # type: ignore[return-value]
    except (json.JSONDecodeError, ValueError):
        return _empty_result()


class AuthorityForecast(TypedDict):
    temp_high: float | None
    temp_low: float | None
    precip_pct: float | None
    confidence: str           # "high" / "medium" / "low"
    post_count: int           # number of relevant posts found (0 = nothing usable)
    issued_at: str            # ISO 8601 timestamp of the most recent relevant post
    summary: str
    key_quotes: list


_AUTHORITY_REQUIRED_KEYS = {
    "temp_high", "temp_low", "precip_pct", "confidence",
    "post_count", "issued_at", "summary", "key_quotes",
}


def _empty_authority_result() -> AuthorityForecast:
    """No-data sentinel. ``post_count=0`` is the flag the scorer drops on."""
    return AuthorityForecast(
        temp_high=None,
        temp_low=None,
        precip_pct=None,
        confidence="low",
        post_count=0,
        issued_at=datetime.now(timezone.utc).replace(tzinfo=None).isoformat(),
        summary="",
        key_quotes=[],
    )


def _coerce_optional_float(value: object) -> float | None:
    if value is None:
        return None
    try:
        return float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


def _parse_authority_response(text: str) -> AuthorityForecast:
    """Parse Grok's authority-forecast JSON, falling back to empty on any failure.

    Mirrors ``_parse_grok_response``: tolerates ```json fences, validates the
    required keys, and coerces the numeric fields to ``float | None`` /
    ``post_count`` to ``int``.
    """
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
        if not isinstance(data, dict) or not _AUTHORITY_REQUIRED_KEYS.issubset(data):
            return _empty_authority_result()
        try:
            post_count = int(data.get("post_count") or 0)
        except (TypeError, ValueError):
            post_count = 0
        if not data.get("issued_at"):
            data["issued_at"] = datetime.now(timezone.utc).replace(tzinfo=None).isoformat()
        return AuthorityForecast(
            temp_high=_coerce_optional_float(data.get("temp_high")),
            temp_low=_coerce_optional_float(data.get("temp_low")),
            precip_pct=_coerce_optional_float(data.get("precip_pct")),
            confidence=str(data.get("confidence") or "low"),
            post_count=max(0, post_count),
            issued_at=str(data["issued_at"]),
            summary=str(data.get("summary") or ""),
            key_quotes=list(data.get("key_quotes") or []),
        )
    except (json.JSONDecodeError, ValueError):
        return _empty_authority_result()


class ProfileScan(TypedDict):
    post_count: int           # relevant on-topic posts found (0 = nothing usable)
    probability: float        # inferred P(speaker says the phrase aloud in the window)
    uncertainty: float
    issued_at: str            # ISO 8601 timestamp of the MOST RECENT relevant post
    summary: str
    key_quotes: list


_PROFILE_REQUIRED_KEYS = {
    "post_count", "probability", "uncertainty", "issued_at", "summary", "key_quotes",
}


def _empty_profile_result() -> ProfileScan:
    """No-data sentinel. ``post_count=0`` is the flag the scorer/pipeline drops on."""
    return ProfileScan(
        post_count=0,
        probability=0.5,
        uncertainty=1.0,
        issued_at=datetime.now(timezone.utc).replace(tzinfo=None).isoformat(),
        summary="",
        key_quotes=[],
    )


def _parse_profile_response(text: str) -> ProfileScan:
    """Parse Grok's profile-scan JSON, falling back to empty on any failure.

    Mirrors ``_parse_authority_response``: tolerates ```json fences, validates the
    required keys, clamps ``probability``/``uncertainty`` to [0,1] and coerces
    ``post_count`` to a non-negative int.
    """
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
        if not isinstance(data, dict) or not _PROFILE_REQUIRED_KEYS.issubset(data):
            return _empty_profile_result()
        try:
            post_count = max(0, int(data.get("post_count") or 0))
        except (TypeError, ValueError):
            post_count = 0
        probability = _coerce_optional_float(data.get("probability"))
        uncertainty = _coerce_optional_float(data.get("uncertainty"))
        if not data.get("issued_at"):
            data["issued_at"] = datetime.now(timezone.utc).replace(tzinfo=None).isoformat()
        return ProfileScan(
            post_count=post_count,
            probability=min(max(probability if probability is not None else 0.5, 0.0), 1.0),
            uncertainty=min(max(uncertainty if uncertainty is not None else 0.3, 0.0), 1.0),
            issued_at=str(data["issued_at"]),
            summary=str(data.get("summary") or ""),
            key_quotes=list(data.get("key_quotes") or []),
        )
    except (json.JSONDecodeError, ValueError):
        return _empty_profile_result()


_PROFILE_PROMPT = (
    "Search X for recent posts from these accounts: {handles}\n\n"
    "You are gauging whether the person behind these accounts is likely to SAY the "
    "word/phrase \"{phrase}\" out loud in public during {window}, judging by how much "
    "and how recently these accounts have posted about that topic. Someone hammering a "
    "subject online is more likely to say it out loud that week.\n\n"
    "Rules:\n"
    "- Count only posts from {window} that are genuinely about \"{phrase}\" or its topic.\n"
    "- If none of these accounts posted about it in {window}, set post_count to 0 and "
    "probability to 0.5. Do not infer or guess — a quiet timeline is NOT evidence against.\n"
    "- Heavier and more recent on-topic posting → higher probability.\n"
    "- issued_at must be the ISO 8601 timestamp of the MOST RECENT relevant post.\n\n"
    "Return ONLY a JSON object with exactly these fields:\n"
    '{{\n'
    '  "post_count": <int, number of relevant posts found>,\n'
    '  "probability": <float 0.0-1.0, P(they say it aloud in the window)>,\n'
    '  "uncertainty": <float 0.0-1.0, where 0.1=confident, 0.4=uncertain>,\n'
    '  "issued_at": "<ISO 8601 timestamp of the most recent relevant post>",\n'
    '  "summary": "<1-2 sentence summary of what these accounts are posting>",\n'
    '  "key_quotes": ["<post 1>", "<post 2>"]\n'
    '}}'
)


_AUTHORITY_PROMPT = (
    "Search X for the most recent forecast posts from these meteorologist accounts: {handles}\n\n"
    "You are extracting the forecast these named meteorologist authorities give for "
    "{city} on the specific date {target_date}. The market cares about: {metric}.\n\n"
    "Rules:\n"
    "- Only report a value an account EXPLICITLY forecasts for {target_date}. If none "
    "has, set that field to null.\n"
    "- If no account has posted a relevant forecast for {target_date}, set post_count to "
    "0 and all numeric fields to null. Do not infer or guess.\n"
    "- Report a single integer or null for each numeric field — never a range "
    "(if a range is given, use its midpoint rounded to the nearest integer).\n"
    "- temp_high / temp_low are degrees Fahrenheit; precip_pct is 0-100.\n"
    "- issued_at must be the ISO 8601 timestamp of the MOST RECENT relevant post.\n\n"
    "Return ONLY a JSON object with exactly these fields:\n"
    '{{\n'
    '  "temp_high": <int or null>,\n'
    '  "temp_low": <int or null>,\n'
    '  "precip_pct": <int 0-100 or null>,\n'
    '  "confidence": "<high|medium|low>",\n'
    '  "post_count": <int, number of relevant posts found>,\n'
    '  "issued_at": "<ISO 8601 timestamp of the most recent relevant post>",\n'
    '  "summary": "<1-2 sentence summary of what the authorities are forecasting>",\n'
    '  "key_quotes": ["<post 1>", "<post 2>"]\n'
    '}}'
)


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

    def _ensure_session(self) -> aiohttp.ClientSession:
        """Lazily create the session with a truststore SSL connector.

        A bare ClientSession uses certifi's bundle and fails TLS verification
        behind the corporate proxy, silently degrading every X search to an
        empty result. Mirrors the other external clients.
        """
        if self._session is None:
            connector = aiohttp.TCPConnector(ssl=_build_ssl_context())
            self._session = aiohttp.ClientSession(connector=connector)
        return self._session

    async def _responses_text(self, content: str, tools: list[dict]) -> str:
        """POST to the Agent Tools ``/v1/responses`` endpoint, return the answer text.

        Raises on transport/HTTP error so callers can fall back to their sentinel.
        """
        session = self._ensure_session()
        payload = {
            "model": config.XAI_MODEL,
            "input": [{"role": "user", "content": content}],
            "tools": tools,
        }
        headers = {
            "Authorization": f"Bearer {config.XAI_API_KEY}",
            "Content-Type": "application/json",
        }
        async with session.post(
            f"{config.XAI_BASE_URL}/responses",
            json=payload,
            headers=headers,
            timeout=aiohttp.ClientTimeout(total=_RESPONSES_TIMEOUT_SECONDS),
        ) as response:
            response.raise_for_status()
            data = await response.json()
        return _extract_responses_text(data)

    async def live_search(self, query: str, market_title: str = "") -> GrokSearchResult:
        if not config.XAI_API_KEY:
            return _empty_result()

        content = _SEARCH_PROMPT.format(query=query, market_title=market_title)
        try:
            text = await self._responses_text(content, [{"type": "x_search"}])
            return _parse_grok_response(text)
        except (aiohttp.ClientError, asyncio.TimeoutError, OSError, KeyError, IndexError):
            return _empty_result()

    async def forecast_search(
        self,
        handles: list[str],
        city: str,
        target_date: str,
        metric: str,
    ) -> AuthorityForecast:
        """Poll named meteorologist authorities on X for their forecast.

        Restricts the ``x_search`` tool to ``handles`` via ``allowed_x_handles`` and
        asks Grok to extract the high/low/precip the authorities explicitly
        forecast for ``target_date``. Returns ``_empty_authority_result()``
        (``post_count=0``) immediately when there are no handles or no API key, and
        on any error.
        """
        if not handles or not config.XAI_API_KEY:
            return _empty_authority_result()

        content = _AUTHORITY_PROMPT.format(
            handles=", ".join(f"@{handle}" for handle in handles),
            city=city, target_date=target_date, metric=metric,
        )
        # allowed_x_handles caps at 20; the city authority lists are far shorter.
        tools = [{"type": "x_search", "allowed_x_handles": handles}]
        try:
            text = await self._responses_text(content, tools)
            return _parse_authority_response(text)
        except (aiohttp.ClientError, asyncio.TimeoutError, OSError, KeyError, IndexError):
            return _empty_authority_result()

    async def profile_topic_scan(
        self,
        handles: list[str],
        phrase: str,
        window: str,
    ) -> ProfileScan:
        """Scan a speaker's own X accounts for how much they're posting about a topic.

        A *leading indicator* for a "Will <person> say <phrase>" market: someone
        hammering a subject on their timeline is more likely to say it out loud in
        the window. Restricts ``x_search`` to ``handles`` via ``allowed_x_handles``
        (capped at 20) and asks Grok for a ``post_count`` + an inferred probability +
        the most-recent relevant-post timestamp. Returns ``_empty_profile_result()``
        (``post_count=0``) immediately when there are no handles or no API key, and
        on any error — a quiet timeline never counts against the market.
        """
        handles = handles[:20]
        if not handles or not config.XAI_API_KEY:
            return _empty_profile_result()

        content = _PROFILE_PROMPT.format(
            handles=", ".join(f"@{handle}" for handle in handles),
            phrase=phrase, window=window,
        )
        tools = [{"type": "x_search", "allowed_x_handles": handles}]
        try:
            text = await self._responses_text(content, tools)
            return _parse_profile_response(text)
        except (aiohttp.ClientError, asyncio.TimeoutError, OSError, KeyError, IndexError):
            return _empty_profile_result()

    async def __aenter__(self) -> "XClient":
        return self

    async def __aexit__(self, *_) -> None:
        await self.close()

    async def close(self) -> None:
        if self._session:
            await self._session.close()
            self._session = None
