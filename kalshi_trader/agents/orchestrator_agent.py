from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from kalshi_trader import config
from kalshi_trader.agents.base import BaseAgent
from kalshi_trader.models import Market, OrderAction, Side, SignalEstimate, TradeIdea

_PROMPTS_DIR = Path(__file__).parent / "prompts"


def _estimate_to_input(e: SignalEstimate) -> dict:
    return {
        "source": e.source,
        "probability": e.probability,
        "uncertainty": e.uncertainty,
        "weight": e.weight,
        "narrative": e.metadata.get("narrative", ""),
    }


def _build_trade_idea(
    ticker: str,
    side: str,
    confidence: float,
    reasoning: str,
    signal_sources: list[str],
    suggested_size_dollars: float,
    market_price: float = 0.0,
    category: str = "",
) -> dict:
    return {
        "agent_id": "orchestrator",
        "ticker": ticker,
        "side": side,
        "action": "buy",
        "confidence": confidence,
        "market_price": market_price,
        "reasoning": reasoning,
        "signal_sources": signal_sources,
        "suggested_size_dollars": suggested_size_dollars,
        "category": category,
    }


def _parse_trade_ideas(raw: str) -> list[TradeIdea]:
    match = re.search(r"```json\s*(.*?)\s*```", raw, re.DOTALL)
    if not match:
        return []
    try:
        data = json.loads(match.group(1))
    except json.JSONDecodeError:
        return []
    if not isinstance(data, list):
        return []
    ideas = []
    for item in data:
        try:
            ideas.append(TradeIdea(
                agent_id="orchestrator",
                ticker=item["ticker"],
                side=Side(item.get("side", "yes")),
                action=OrderAction(item.get("action", "buy")),
                confidence=float(item["confidence"]),
                market_price=float(item.get("market_price", 0)),
                reasoning=item.get("reasoning", ""),
                signal_sources=item.get("signal_sources", []),
                suggested_size_dollars=float(item.get("suggested_size_dollars", 10.0)),
                category=item.get("category", ""),
            ))
        except (KeyError, ValueError, TypeError):
            continue
    return ideas


_SCHEMAS: list[dict] = [
    {
        "name": "get_market_signals",
        "description": "Retrieve all collected SignalEstimate dicts for a market ticker.",
        "input_schema": {
            "type": "object",
            "properties": {
                "ticker": {"type": "string", "description": "Kalshi market ticker"},
            },
            "required": ["ticker"],
        },
    },
    {
        "name": "build_trade_idea",
        "description": "Record a trade idea that has survived adversarial challenge.",
        "input_schema": {
            "type": "object",
            "properties": {
                "ticker": {"type": "string"},
                "side": {"type": "string", "enum": ["yes", "no"]},
                "confidence": {"type": "number", "description": "0.0–1.0"},
                "reasoning": {"type": "string", "description": "Full adversarial challenge reasoning"},
                "signal_sources": {"type": "array", "items": {"type": "string"}},
                "suggested_size_dollars": {"type": "number"},
                "market_price": {"type": "number", "description": "Current YES ask in cents"},
                "category": {"type": "string"},
            },
            "required": ["ticker", "side", "confidence", "reasoning", "signal_sources", "suggested_size_dollars"],
        },
    },
]


class OrchestratorAgent:
    """Coordinates all signal agents and produces a ranked trade slate.

    Receives pre-collected SignalEstimates per market, synthesizes them
    using Claude with adversarial challenge, and returns TradeIdeas.
    """

    def __init__(self) -> None:
        self._signals_by_ticker: dict[str, list[SignalEstimate]] = {}
        self._markets_by_ticker: dict[str, Market] = {}
        system_prompt = (_PROMPTS_DIR / "orchestrator.md").read_text()
        self._agent = BaseAgent(
            tools=_SCHEMAS,
            handlers={
                "get_market_signals": self._get_market_signals,
                "build_trade_idea": self._build_trade_idea_handler,
            },
            system_prompt=system_prompt,
            model=config.COORDINATOR_MODEL,
        )

    async def run(
        self,
        markets: list[Market],
        signals: dict[str, list[SignalEstimate]],
    ) -> list[TradeIdea]:
        """Synthesize signals for the given markets into a trade slate.

        Args:
            markets: Markets to consider. Only markets with at least one signal
                     are passed to Claude.
            signals: Map of ticker → list[SignalEstimate] from all agents.
        """
        self._signals_by_ticker = signals
        self._markets_by_ticker = {m.ticker: m for m in markets}

        # Only pass markets that have signals
        actionable = [m for m in markets if signals.get(m.ticker)]
        if not actionable:
            return []

        market_list = [
            {
                "ticker": m.ticker,
                "title": m.title,
                "yes_ask": m.yes_ask,
                "category": m.category,
                "hours_to_close": round(
                    max(0, (m.close_time.replace(tzinfo=timezone.utc)
                            if m.close_time.tzinfo is None
                            else m.close_time
                            - datetime.now(tz=timezone.utc)).total_seconds() / 3600), 1
                ),
                "signal_count": len(signals.get(m.ticker, [])),
            }
            for m in actionable
        ]

        prompt = (
            f"Analyze these {len(actionable)} Kalshi markets and produce a trade slate.\n\n"
            f"Markets:\n{json.dumps(market_list, indent=2)}\n\n"
            f"For each market, call get_market_signals(ticker) to retrieve the signals, "
            f"apply your adversarial challenge, and call build_trade_idea for any that survive."
        )

        raw = await self._agent.run(prompt)
        return _parse_trade_ideas(raw)

    async def _get_market_signals(self, ticker: str) -> list[dict]:
        estimates = self._signals_by_ticker.get(ticker, [])
        market = self._markets_by_ticker.get(ticker)
        result = [_estimate_to_input(e) for e in estimates]
        if market:
            # Include current market price for context
            for item in result:
                item["market_yes_ask"] = market.yes_ask
        return result

    async def _build_trade_idea_handler(
        self,
        ticker: str,
        side: str,
        confidence: float,
        reasoning: str,
        signal_sources: list[str],
        suggested_size_dollars: float,
        market_price: float = 0.0,
        category: str = "",
    ) -> dict:
        market = self._markets_by_ticker.get(ticker)
        if market and market_price == 0.0:
            market_price = float(market.yes_ask)
        if market and not category:
            category = market.category
        return _build_trade_idea(
            ticker, side, confidence, reasoning,
            signal_sources, suggested_size_dollars,
            market_price, category,
        )
