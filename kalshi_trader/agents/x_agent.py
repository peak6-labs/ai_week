from __future__ import annotations
import asyncio
import json
from datetime import datetime
import anthropic
from kalshi_trader import config
from kalshi_trader.models import SignalEstimate
from kalshi_trader.external.x_client import XClient
from kalshi_trader.external.x_strategies import (
    CATEGORY_STRATEGIES,
    STRATEGY_NAME_MAP,
    FALLBACK_STRATEGIES,
    BaseXStrategy,
)


_SEARCH_X_SIGNAL_SCHEMA = {
    "name": "search_x_signal",
    "description": (
        "Search X for social signal on a Kalshi market. "
        "Returns a list of SignalEstimate dicts from all default strategies for the market category."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "ticker":       {"type": "string", "description": "Kalshi market ticker"},
            "category":     {"type": "string", "description": "e.g. weather, politics, sports, mentions"},
            "market_title": {"type": "string", "description": "Full market question title"},
        },
        "required": ["ticker", "category", "market_title"],
    },
}

_OVERRIDE_X_STRATEGIES_SCHEMA = {
    "name": "override_x_strategies",
    "description": (
        "Run specific X search strategies instead of the category defaults. "
        "Use when the market is ambiguous or you want additional signal types."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "ticker":       {"type": "string"},
            "market_title": {"type": "string"},
            "strategies": {
                "type": "array",
                "items": {"type": "string", "enum": ["sentiment", "news", "experts", "buzz"]},
                "description": "Strategy names to run",
            },
        },
        "required": ["ticker", "market_title", "strategies"],
    },
}

X_AGENT_TOOLS = [_SEARCH_X_SIGNAL_SCHEMA, _OVERRIDE_X_STRATEGIES_SCHEMA]


def _estimate_to_dict(e: SignalEstimate) -> dict:
    return {
        "source": e.source,
        "probability": e.probability,
        "uncertainty": e.uncertainty,
        "weight": e.weight,
        "data_issued_at": e.data_issued_at.isoformat(),
        "metadata": e.metadata,
    }


class XAgent:
    def __init__(self) -> None:
        self._client = XClient()
        self._semaphore = asyncio.Semaphore(config.X_MAX_CONCURRENT_SEARCHES)
        self._anthropic: anthropic.AsyncAnthropic | None = (
            anthropic.AsyncAnthropic(api_key=config.ANTHROPIC_API_KEY)
            if config.ANTHROPIC_API_KEY
            else None
        )

    async def _run_one(
        self, strategy: BaseXStrategy, market_title: str
    ) -> list[SignalEstimate]:
        async with self._semaphore:
            result = await strategy.run(market_title, self._client)

        estimates = [strategy.to_signal_estimate(result)]

        if result["uncertainty"] > config.X_GROK_UNCERTAINTY_THRESHOLD:
            second = await self._claude_second_pass(
                summary=result["summary"],
                market_title=market_title,
                source_tag=strategy.source_tag,
                issued_at=estimates[0].data_issued_at,
            )
            if second is not None:
                estimates.append(second)

        return estimates

    async def _claude_second_pass(
        self,
        summary: str,
        market_title: str,
        source_tag: str,
        issued_at: datetime,
    ) -> SignalEstimate | None:
        if not summary or self._anthropic is None:
            return None
        try:
            resp = await self._anthropic.messages.create(
                model=config.SPECIALIST_MODEL,
                max_tokens=256,
                messages=[{
                    "role": "user",
                    "content": (
                        f"Market: {market_title}\n\n"
                        f"X social signal summary: {summary}\n\n"
                        "Based on this X posts summary, estimate the probability this market resolves YES. "
                        'Return only a JSON object: {"probability": float, "uncertainty": float, "reasoning": string}'
                    ),
                }],
            )
            data = json.loads(resp.content[0].text)
            return SignalEstimate(
                source=f"x_claude_{source_tag.split('_')[-1]}",
                probability=float(data["probability"]),
                uncertainty=float(data["uncertainty"]),
                weight=config.X_CLAUDE_SIGNAL_WEIGHT,
                data_issued_at=issued_at,
                metadata={"reasoning": data.get("reasoning", "")},
            )
        except Exception:
            return None

    async def search_x_signal(
        self, ticker: str, category: str, market_title: str
    ) -> list[dict]:
        strategy_classes = CATEGORY_STRATEGIES.get(category, FALLBACK_STRATEGIES)
        strategies = [cls() for cls in strategy_classes]
        all_estimates = await asyncio.gather(*[self._run_one(s, market_title) for s in strategies])
        flat = [e for group in all_estimates for e in group]
        return [_estimate_to_dict(e) for e in flat]

    async def override_x_strategies(
        self, ticker: str, market_title: str, strategies: list[str]
    ) -> list[dict]:
        classes = [STRATEGY_NAME_MAP[n] for n in strategies if n in STRATEGY_NAME_MAP]
        if not classes:
            classes = list(FALLBACK_STRATEGIES)
        strats = [cls() for cls in classes]
        all_estimates = await asyncio.gather(*[self._run_one(s, market_title) for s in strats])
        flat = [e for group in all_estimates for e in group]
        return [_estimate_to_dict(e) for e in flat]

    async def close(self) -> None:
        await self._client.close()
