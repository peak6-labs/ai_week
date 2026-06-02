from __future__ import annotations

import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from kalshi_trader.agents.base import BaseAgent
from kalshi_trader.agents.parsing import estimate_to_dict, parse_signal_estimates
from kalshi_trader.models import Market, SignalEstimate

_PROMPTS_DIR = Path(__file__).parent / "prompts"


def find_chain_violations(markets: list[dict]) -> list[dict]:
    """Detect markets where a later-closing market has a higher price than
    an earlier-closing one within the same event family.

    Markets must be sorted by close_time ascending (earliest first).
    Returns list of violation dicts:
      {early_ticker, early_price, late_ticker, late_price, price_gap_cents}

    A violation means the early market (which resolves sooner and is a
    prerequisite) is cheaper than the later one — structurally impossible
    if they represent sequential stages of the same outcome.
    """
    violations = []
    n = len(markets)
    for i in range(n):
        for j in range(i + 1, n):
            early = markets[i]
            late = markets[j]
            early_price = early.get("yes_ask", 0) or 0
            late_price = late.get("yes_ask", 0) or 0
            if early_price <= 0 or late_price <= 0:
                continue
            # Violation: later market priced higher than earlier one
            if late_price > early_price:
                gap = late_price - early_price
                violations.append({
                    "early_ticker": early["ticker"],
                    "early_price": early_price,
                    "late_ticker": late["ticker"],
                    "late_price": late_price,
                    "price_gap_cents": gap,
                })
    return violations


_SCHEMAS: list[dict] = [
    {
        "name": "get_event_markets",
        "description": "Fetch all open markets in this Kalshi event family, sorted by close time (earliest first).",
        "input_schema": {
            "type": "object",
            "properties": {
                "event_ticker": {"type": "string", "description": "Kalshi event ticker"},
            },
            "required": ["event_ticker"],
        },
    },
    {
        "name": "find_chain_violations",
        "description": "Given a list of event markets sorted by close time, return all pairs where a later-closing market is priced higher than an earlier-closing one — a conditional probability violation.",
        "input_schema": {
            "type": "object",
            "properties": {
                "markets": {
                    "type": "array",
                    "items": {"type": "object"},
                    "description": "Markets from get_event_markets",
                },
            },
            "required": ["markets"],
        },
    },
    {
        "name": "build_conditional_signal",
        "description": "Build a SignalEstimate for an underpriced early-stage market. Only call this when price_gap_cents >= 5.",
        "input_schema": {
            "type": "object",
            "properties": {
                "ticker": {"type": "string", "description": "The underpriced early-stage market ticker"},
                "chain_violation": {
                    "type": "object",
                    "description": "Violation dict from find_chain_violations",
                },
            },
            "required": ["ticker", "chain_violation"],
        },
    },
]


class ConditionalEventAgent:
    """Detects conditional probability violations in Kalshi multi-leg event chains."""

    def __init__(self, client: Any) -> None:
        self._client = client
        system_prompt = (_PROMPTS_DIR / "conditional_event.md").read_text()
        self._agent = BaseAgent(
            tools=_SCHEMAS,
            handlers={
                "get_event_markets": self._get_event_markets,
                "find_chain_violations": self._find_chain_violations,
                "build_conditional_signal": self._build_conditional_signal,
            },
            system_prompt=system_prompt,
        )

    async def run(self, ticker: str, title: str, event_ticker: str = "") -> list[SignalEstimate]:
        if not event_ticker:
            # Try to extract event_ticker from the market
            try:
                data = await self._client.get_market(ticker)
                market = data.get("market", data)
                event_ticker = market.get("event_ticker", "")
            except Exception:
                return []
        if not event_ticker:
            return []

        prompt = (
            f"Analyze this Kalshi event chain for conditional probability violations:\n"
            f"ticker: {ticker}\ntitle: {title}\nevent_ticker: {event_ticker}"
        )
        raw = await self._agent.run(prompt)
        return parse_signal_estimates(raw)

    async def _get_event_markets(self, event_ticker: str) -> list[dict]:
        """Fetch all markets in this event family, sorted by close time."""
        try:
            data = await self._client.get(
                "/markets", {"event_ticker": event_ticker, "status": "open", "limit": 50}
            )
            markets = data.get("markets", [])
        except Exception:
            return []

        result = []
        for m in markets:
            try:
                close_time = m.get("close_time", "")
                result.append({
                    "ticker": m["ticker"],
                    "title": m.get("title", ""),
                    "yes_bid": int(m.get("yes_bid") or 0),
                    "yes_ask": int(m.get("yes_ask") or 0),
                    "close_time": close_time,
                })
            except (KeyError, TypeError):
                continue

        result.sort(key=lambda x: x.get("close_time", ""))
        return result

    async def _find_chain_violations(self, markets: list[dict]) -> list[dict]:
        return find_chain_violations(markets)

    async def _build_conditional_signal(
        self, ticker: str, chain_violation: dict
    ) -> dict:
        gap = chain_violation.get("price_gap_cents", 0)
        early_price = chain_violation.get("early_price", 0)
        late_price = chain_violation.get("late_price", 0)
        late_ticker = chain_violation.get("late_ticker", "")

        # The early market is underpriced — its true prob >= the late market's price
        implied_prob = min(0.95, (late_price + gap * 0.5) / 100.0)
        uncertainty = max(0.05, 0.15 - gap * 0.005)

        estimate = SignalEstimate(
            source="conditional_event",
            probability=round(implied_prob, 4),
            uncertainty=round(uncertainty, 4),
            weight=0.80,
            data_issued_at=datetime.now(tz=timezone.utc),
            metadata={
                "ticker": ticker,
                "narrative": (
                    f"Conditional chain violation: early market ({ticker}) at {early_price}¢ "
                    f"is cheaper than later-stage market ({late_ticker}) at {late_price}¢. "
                    f"Gap: {gap}¢. The early market must resolve YES before the later one can — "
                    f"it is structurally underpriced."
                ),
                "data_quality": "fresh",
                "price_gap_cents": gap,
                "late_ticker": late_ticker,
                "early_price": early_price,
                "late_price": late_price,
            },
        )
        return estimate_to_dict(estimate)
