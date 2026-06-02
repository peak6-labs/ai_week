"""OrchestratorAgent — autonomous trading cycle coordinator.

Wraps signal-gathering and synthesis so Claude can drive the full trading
cycle from scratch via tool calls:
  scan markets → run specialist agents → synthesise signals →
  adversarial challenge → build trade ideas.

All orchestration intelligence lives in data_orchestrator.md.
Python is plumbing only.
"""
from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from kalshi_trader import config
from kalshi_trader.agents.base import BaseAgent
from kalshi_trader.agents.parsing import estimate_to_dict
from kalshi_trader.models import Market, OrderAction, Side, SignalEstimate, TradeIdea

_PROMPTS_DIR = Path(__file__).parent / "prompts"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

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


# ---------------------------------------------------------------------------
# Tool schemas
# ---------------------------------------------------------------------------

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
    {
        "name": "scan_open_markets",
        "description": "Scan Kalshi for open markets passing basic quality filters. Returns a list of market dicts with ticker, title, category, yes_ask, open_interest, hours_to_close.",
        "input_schema": {"type": "object", "properties": {}, "required": []},
    },
    {
        "name": "run_weather_agent",
        "description": "Run the weather specialist agent on a single market. Only useful for weather-category markets. Returns list of SignalEstimate dicts.",
        "input_schema": {
            "type": "object",
            "properties": {
                "ticker": {"type": "string"},
                "title": {"type": "string"},
            },
            "required": ["ticker", "title"],
        },
    },
    {
        "name": "run_order_flow_agent",
        "description": "Run the order flow specialist agent (VPIN + OFI analysis). Returns list of SignalEstimate dicts.",
        "input_schema": {
            "type": "object",
            "properties": {
                "ticker": {"type": "string"},
                "title": {"type": "string"},
            },
            "required": ["ticker", "title"],
        },
    },
    {
        "name": "run_market_maker_agent",
        "description": "Run the market maker agent (spread dynamics, orderbook withdrawal detection). Returns list of SignalEstimate dicts.",
        "input_schema": {
            "type": "object",
            "properties": {
                "ticker": {"type": "string"},
                "title": {"type": "string"},
            },
            "required": ["ticker", "title"],
        },
    },
    {
        "name": "run_kalshi_bias_agent",
        "description": "Run the Kalshi bias agent (favorite-longshot + political underconfidence corrections). Returns list of SignalEstimate dicts.",
        "input_schema": {
            "type": "object",
            "properties": {
                "ticker": {"type": "string"},
                "title": {"type": "string"},
                "category": {"type": "string"},
                "hours_to_resolution": {"type": "number"},
            },
            "required": ["ticker", "title"],
        },
    },
    {
        "name": "run_x_agent",
        "description": "Run the X/social media agent (Grok + Claude signal synthesis from X). Only call for categories with social signal value (politics, crypto, sports). Returns list of SignalEstimate dicts.",
        "input_schema": {
            "type": "object",
            "properties": {
                "ticker": {"type": "string"},
                "category": {"type": "string"},
                "title": {"type": "string"},
            },
            "required": ["ticker", "category", "title"],
        },
    },
    {
        "name": "run_polymarket_agent",
        "description": "Run the Polymarket agent across all scanned markets. Finds price gaps and whale signals between Kalshi and Polymarket. Returns list of trade idea dicts (not signals — these are ready-to-evaluate ideas).",
        "input_schema": {"type": "object", "properties": {}, "required": []},
    },
]


# ---------------------------------------------------------------------------
# OrchestratorAgent
# ---------------------------------------------------------------------------

class OrchestratorAgent:
    """Autonomous trading cycle coordinator.

    Claude drives the full pipeline via tool calls:
      scan markets → run specialist agents → synthesise signals →
      adversarial challenge → build trade ideas.

    All orchestration intelligence lives in data_orchestrator.md.
    """

    def __init__(
        self,
        kalshi_client: Any,
        weather_agent: Any,
        order_flow_agent: Any,
        market_maker_agent: Any,
        kalshi_bias_agent: Any,
        x_agent: Any,
        polymarket_agent: Any,
    ) -> None:
        self._kalshi_client = kalshi_client
        self._weather_agent = weather_agent
        self._order_flow_agent = order_flow_agent
        self._market_maker_agent = market_maker_agent
        self._kalshi_bias_agent = kalshi_bias_agent
        self._x_agent = x_agent
        self._polymarket_agent = polymarket_agent

        # Mutable state reset each cycle
        self._signals_by_ticker: dict[str, list[SignalEstimate]] = {}
        self._scanned_markets: list[Market] = []
        self._markets_by_ticker: dict[str, Market] = {}
        self._built_ideas: list[dict] = []

        system_prompt = (_PROMPTS_DIR / "data_orchestrator.md").read_text()
        self._agent = BaseAgent(
            tools=_SCHEMAS,
            handlers={
                "get_market_signals": self._get_market_signals,
                "build_trade_idea": self._build_trade_idea_handler,
                "scan_open_markets": self._scan_open_markets,
                "run_weather_agent": self._run_weather_agent,
                "run_order_flow_agent": self._run_order_flow_agent,
                "run_market_maker_agent": self._run_market_maker_agent,
                "run_kalshi_bias_agent": self._run_kalshi_bias_agent,
                "run_x_agent": self._run_x_agent,
                "run_polymarket_agent": self._run_polymarket_agent,
            },
            system_prompt=system_prompt,
            model=config.COORDINATOR_MODEL,
        )

    # ------------------------------------------------------------------
    # Public entry point
    # ------------------------------------------------------------------

    async def run_autonomous(self) -> list[TradeIdea]:
        """Run one full trading cycle autonomously.

        The agent will scan markets, invoke specialist agents via tools,
        synthesize signals, apply adversarial challenge, and return surviving ideas.
        """
        self._signals_by_ticker = {}
        self._scanned_markets = []
        self._markets_by_ticker = {}
        self._built_ideas = []

        prompt = (
            "Run one full trading cycle. "
            "Start by calling scan_open_markets() to see what's available. "
            "Then use the specialist agent tools to gather signals on promising markets. "
            "Apply the adversarial challenge framework to each candidate. "
            "Call build_trade_idea() for any idea that survives. "
            "When done, output the final JSON trade slate."
        )
        raw = await self._agent.run(prompt)
        return _parse_trade_ideas(raw)

    # ------------------------------------------------------------------
    # Existing signal tools
    # ------------------------------------------------------------------

    async def _get_market_signals(self, ticker: str) -> list[dict]:
        estimates = self._signals_by_ticker.get(ticker, [])
        market = self._markets_by_ticker.get(ticker)
        result = [_estimate_to_input(e) for e in estimates]
        if market:
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
        idea = _build_trade_idea(
            ticker, side, confidence, reasoning,
            signal_sources, suggested_size_dollars,
            market_price, category,
        )
        self._built_ideas.append(idea)
        return idea

    # ------------------------------------------------------------------
    # Specialist-agent tool handlers
    # ------------------------------------------------------------------

    async def _scan_open_markets(self) -> list[dict]:
        from kalshi_trader.scanner import MarketScanner
        from kalshi_trader.ui.config_manager import cfg

        scanner = MarketScanner(self._kalshi_client)
        markets = await scanner.get_open_markets()
        now = datetime.now(tz=timezone.utc)
        min_oi = cfg.get("filter_min_open_interest")
        min_hrs = cfg.get("filter_min_hours_to_close")
        max_hrs = cfg.get("filter_max_hours_to_close")

        filtered = []
        for m in markets:
            close = m.close_time if m.close_time.tzinfo else m.close_time.replace(tzinfo=timezone.utc)
            hours_left = max(0, (close - now).total_seconds() / 3600)
            if m.open_interest >= min_oi and min_hrs <= hours_left <= max_hrs:
                filtered.append(m)

        # Cache for polymarket_agent and signal accumulation
        self._scanned_markets = filtered
        self._markets_by_ticker = {m.ticker: m for m in filtered}

        return [
            {
                "ticker": m.ticker,
                "title": m.title,
                "category": m.category,
                "yes_ask": m.yes_ask,
                "open_interest": m.open_interest,
                "hours_to_close": round(
                    (
                        (m.close_time if m.close_time.tzinfo else m.close_time.replace(tzinfo=timezone.utc))
                        - now
                    ).total_seconds() / 3600,
                    1,
                ),
            }
            for m in filtered
        ]

    async def _run_weather_agent(self, ticker: str, title: str) -> list[dict]:
        from kalshi_trader.ui.config_manager import cfg
        if not cfg.get("agent_weather_enabled"):
            return []
        try:
            signals = await self._weather_agent.run(ticker, title)
            for s in signals:
                self._signals_by_ticker.setdefault(ticker, []).append(s)
            return [_estimate_to_input(s) for s in signals]
        except Exception as e:
            return [{"error": str(e)}]

    async def _run_order_flow_agent(self, ticker: str, title: str) -> list[dict]:
        from kalshi_trader.ui.config_manager import cfg
        if not cfg.get("agent_order_flow_enabled"):
            return []
        try:
            signals = await self._order_flow_agent.run(ticker, title)
            for s in signals:
                self._signals_by_ticker.setdefault(ticker, []).append(s)
            return [_estimate_to_input(s) for s in signals]
        except Exception as e:
            return [{"error": str(e)}]

    async def _run_market_maker_agent(self, ticker: str, title: str) -> list[dict]:
        from kalshi_trader.ui.config_manager import cfg
        if not cfg.get("agent_market_maker_enabled"):
            return []
        try:
            signals = await self._market_maker_agent.run(ticker, title)
            for s in signals:
                self._signals_by_ticker.setdefault(ticker, []).append(s)
            return [_estimate_to_input(s) for s in signals]
        except Exception as e:
            return [{"error": str(e)}]

    async def _run_kalshi_bias_agent(
        self,
        ticker: str,
        title: str,
        category: str = "",
        hours_to_resolution: float = 72.0,
    ) -> list[dict]:
        from kalshi_trader.ui.config_manager import cfg
        if not cfg.get("agent_kalshi_bias_enabled"):
            return []
        try:
            signals = await self._kalshi_bias_agent.run(
                ticker, title, category, hours_to_resolution
            )
            for s in signals:
                self._signals_by_ticker.setdefault(ticker, []).append(s)
            return [_estimate_to_input(s) for s in signals]
        except Exception as e:
            return [{"error": str(e)}]

    async def _run_x_agent(
        self,
        ticker: str,
        category: str,
        title: str,
    ) -> list[dict]:
        from kalshi_trader.ui.config_manager import cfg
        if not cfg.get("agent_x_enabled"):
            return []
        try:
            signals = await self._x_agent.run(ticker, category, title)
            for s in signals:
                self._signals_by_ticker.setdefault(ticker, []).append(s)
            return [_estimate_to_input(s) for s in signals]
        except Exception as e:
            return [{"error": str(e)}]

    async def _run_polymarket_agent(self) -> list[dict]:
        from kalshi_trader.ui.config_manager import cfg
        if not cfg.get("agent_polymarket_enabled"):
            return []
        if not self._scanned_markets:
            return []
        try:
            ideas = await self._polymarket_agent.run(self._scanned_markets)
            return [
                {
                    "ticker": i.ticker,
                    "side": i.side.value,
                    "confidence": i.confidence,
                    "market_price": i.market_price,
                    "suggested_size_dollars": i.suggested_size_dollars,
                    "reasoning": i.reasoning,
                    "signal_sources": i.signal_sources,
                    "category": i.category,
                    "agent_id": i.agent_id,
                }
                for i in ideas
            ]
        except Exception as e:
            return [{"error": str(e)}]
