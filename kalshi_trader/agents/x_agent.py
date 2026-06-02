from __future__ import annotations
import asyncio
from pathlib import Path
from kalshi_trader import config
from kalshi_trader.models import SignalEstimate
from kalshi_trader.external.x_client import XClient
from kalshi_trader.external.x_strategies import (
    CATEGORY_STRATEGIES,
    STRATEGY_NAME_MAP,
    FALLBACK_STRATEGIES,
)
from kalshi_trader.agents.base import BaseAgent
from kalshi_trader.agents.parsing import parse_signal_estimates, estimate_to_dict
from kalshi_trader.signals.x import build_x_signal

_PROMPTS_DIR = Path(__file__).parent / "prompts"

_SCHEMAS: list[dict] = [
    {
        "name": "search_x_signal",
        "description": "Search X for social signal on a Kalshi market using default strategies for the category. Returns a list of raw SignalEstimate dicts.",
        "input_schema": {
            "type": "object",
            "properties": {
                "ticker": {"type": "string"},
                "category": {"type": "string"},
                "market_title": {"type": "string"},
            },
            "required": ["ticker", "category", "market_title"],
        },
    },
    {
        "name": "override_x_strategies",
        "description": "Run specific X search strategies instead of category defaults. Use when all estimates have uncertainty > 0.15.",
        "input_schema": {
            "type": "object",
            "properties": {
                "ticker": {"type": "string"},
                "market_title": {"type": "string"},
                "strategies": {
                    "type": "array",
                    "items": {"type": "string", "enum": ["sentiment", "news", "experts", "buzz"]},
                },
            },
            "required": ["ticker", "market_title", "strategies"],
        },
    },
    {
        "name": "build_x_signal",
        "description": "Attach your qualitative sentiment assessment to a raw X signal estimate. Call once per raw signal. Provide your narrative, sentiment_direction, and sentiment_reasoning.",
        "input_schema": {
            "type": "object",
            "properties": {
                "ticker": {"type": "string"},
                "raw_signal": {"type": "object", "description": "One element from search_x_signal or override_x_strategies"},
                "narrative": {"type": "string"},
                "sentiment_direction": {"type": "string"},
                "sentiment_reasoning": {"type": "string"},
                "strategies_used": {"type": "string", "description": "Comma-separated strategy names"},
                "post_count": {"type": "integer"},
            },
            "required": ["ticker", "raw_signal", "narrative", "sentiment_direction",
                         "sentiment_reasoning", "strategies_used", "post_count"],
        },
    },
]


class XAgent:
    def __init__(self) -> None:
        self._client = XClient()
        self._semaphore = asyncio.Semaphore(config.X_MAX_CONCURRENT_SEARCHES)
        system_prompt = (_PROMPTS_DIR / "x.md").read_text()
        self._agent = BaseAgent(
            tools=_SCHEMAS,
            handlers={
                "search_x_signal": self._search_x_signal,
                "override_x_strategies": self._override_x_strategies,
                "build_x_signal": self._build_x_signal,
            },
            system_prompt=system_prompt,
        )

    async def run(self, ticker: str, category: str, title: str) -> list[SignalEstimate]:
        prompt = (
            f"Analyze this Kalshi market for X social signal:\n"
            f"ticker: {ticker}\n"
            f"category: {category}\n"
            f"title: {title}"
        )
        raw = await self._agent.run(prompt)
        return self._parse_estimates(raw)

    def _parse_estimates(self, raw: str) -> list[SignalEstimate]:
        return parse_signal_estimates(raw)

    async def _run_strategy(self, strategy_cls: type, market_title: str) -> dict | None:
        async with self._semaphore:
            strategy = strategy_cls()
            result = await strategy.run(market_title, self._client)
        return estimate_to_dict(strategy.to_signal_estimate(result))

    async def _search_x_signal(
        self, ticker: str, category: str, market_title: str
    ) -> list[dict]:
        strategy_classes = CATEGORY_STRATEGIES.get(category, list(FALLBACK_STRATEGIES))
        results = await asyncio.gather(
            *[self._run_strategy(cls, market_title) for cls in strategy_classes],
            return_exceptions=True,
        )
        return [r for r in results if isinstance(r, dict)]

    async def _override_x_strategies(
        self, ticker: str, market_title: str, strategies: list[str]
    ) -> list[dict]:
        classes = [STRATEGY_NAME_MAP[n] for n in strategies if n in STRATEGY_NAME_MAP]
        if not classes:
            classes = list(FALLBACK_STRATEGIES)
        results = await asyncio.gather(
            *[self._run_strategy(cls, market_title) for cls in classes],
            return_exceptions=True,
        )
        return [r for r in results if isinstance(r, dict)]

    async def _build_x_signal(
        self,
        ticker: str,
        raw_signal: dict,
        narrative: str,
        sentiment_direction: str,
        sentiment_reasoning: str,
        strategies_used: str,
        post_count: int,
    ) -> dict:
        strategies_list = [s.strip() for s in strategies_used.split(",") if s.strip()]
        estimate = build_x_signal(
            ticker, raw_signal, narrative, sentiment_direction,
            sentiment_reasoning, strategies_list, post_count,
        )
        return estimate_to_dict(estimate)

    async def close(self) -> None:
        await self._client.close()
