"""Main orchestrator agent — loads orchestrator.md as system prompt.

Claude drives the full pipeline by calling subagents as tools.
Python handlers are thin wrappers: they instantiate the agent and call run().
All orchestration logic lives in orchestrator.md.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path

from kalshi_trader import config
from kalshi_trader.agents.base import BaseAgent
from kalshi_trader.agents.data_orchestrator_agent import DataOrchestratorAgent
from kalshi_trader.agents.kalshi_bias_agent import KalshiBiasAgent
from kalshi_trader.agents.market_maker_agent import MarketMakerAgent
from kalshi_trader.agents.order_flow_agent import OrderFlowAgent
from kalshi_trader.agents.polymarket_price_agent import PolymarketPriceAgent
from kalshi_trader.agents.polymarket_whale_agent import PolymarketWhaleAgent
from kalshi_trader.agents.parsing import estimate_to_dict
from kalshi_trader.agents.weather_agent import WeatherAgent
from kalshi_trader.agents.x_agent import XAgent
from kalshi_trader.actionability.scorer import MarketScorer
from kalshi_trader.actionability.store import SnapshotStore
from kalshi_trader.models import Market, SignalEstimate
from kalshi_trader.risk import RiskManager
from kalshi_trader.scanner import MarketScanner

log = logging.getLogger(__name__)
_PROMPTS_DIR = Path(__file__).parent / "prompts"
_WEATHER_KEYWORDS = {"weather", "temperature", "rain", "precip", "wind", "storm", "snow"}


def _hours_to_close(market: Market) -> float:
    ct = market.close_time
    if ct.tzinfo is None:
        ct = ct.replace(tzinfo=timezone.utc)
    return max(0.0, (ct - datetime.now(tz=timezone.utc)).total_seconds() / 3600)


def _is_weather(market: Market) -> bool:
    return any(kw in (market.title + market.category).lower() for kw in _WEATHER_KEYWORDS)


_SCHEMAS: list[dict] = [
    {
        "name": "run_market_selector",
        "description": "Score and rank all open Kalshi markets by actionability. Returns the top markets to analyze this cycle.",
        "input_schema": {
            "type": "object",
            "properties": {
                "top_n": {"type": "integer", "default": 20, "description": "How many markets to return"},
            },
            "required": [],
        },
    },
    {
        "name": "run_polymarket_price_agent",
        "description": "Run the Polymarket price gap agent for a single market. Returns a SignalEstimate if a meaningful gap exists.",
        "input_schema": {
            "type": "object",
            "properties": {
                "ticker": {"type": "string"},
                "title": {"type": "string"},
                "midpoint_cents": {"type": "number"},
                "hours_to_close": {"type": "number"},
            },
            "required": ["ticker", "title", "midpoint_cents", "hours_to_close"],
        },
    },
    {
        "name": "run_polymarket_whale_agent",
        "description": "Run the whale copy-trading agent for a market. Returns a signal if tracked high-PnL wallets are positioned.",
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
        "description": "Run the order flow agent (OFI + VPIN) for a market. Returns a signal based on Kalshi trade pressure.",
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
        "description": "Run the market maker agent for a market. Returns a signal based on spread dynamics and depth imbalance.",
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
        "description": "Run the Kalshi bias agent for a market. Returns a calibration signal correcting for favorite-longshot bias and political underconfidence.",
        "input_schema": {
            "type": "object",
            "properties": {
                "ticker": {"type": "string"},
                "title": {"type": "string"},
                "category": {"type": "string"},
                "hours_to_close": {"type": "number"},
            },
            "required": ["ticker", "title"],
        },
    },
    {
        "name": "run_weather_agent",
        "description": "Run the NOAA weather agent for a weather market. Only call this for markets about temperature, precipitation, or storms.",
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
        "name": "run_x_agent",
        "description": "Run the X/Grok sentiment agent for a market. Returns a signal based on social media sentiment and expert opinion.",
        "input_schema": {
            "type": "object",
            "properties": {
                "ticker": {"type": "string"},
                "category": {"type": "string"},
                "title": {"type": "string"},
            },
            "required": ["ticker", "title"],
        },
    },
    {
        "name": "run_data_orchestrator",
        "description": "Pass collected signals to the data orchestrator for adversarial synthesis into trade ideas.",
        "input_schema": {
            "type": "object",
            "properties": {
                "markets_json": {"type": "string", "description": "JSON list of market dicts from run_market_selector"},
                "signals_json": {"type": "string", "description": "JSON object mapping ticker -> list of signal dicts"},
            },
            "required": ["markets_json", "signals_json"],
        },
    },
    {
        "name": "run_risk_check",
        "description": "Run the risk manager on a list of trade ideas. Filters out ideas that violate position limits, exposure caps, or edge requirements. Returns only approved ideas.",
        "input_schema": {
            "type": "object",
            "properties": {
                "ideas_json": {"type": "string", "description": "JSON list of trade ideas from run_data_orchestrator"},
                "portfolio_json": {"type": "string", "description": "JSON dict with balance_dollars and open positions"},
            },
            "required": ["ideas_json", "portfolio_json"],
        },
    },
    {
        "name": "report_slate",
        "description": "Log the final approved trade slate for review. Executor is not connected — no trades are placed yet.",
        "input_schema": {
            "type": "object",
            "properties": {
                "ideas_json": {"type": "string", "description": "JSON list of risk-approved trade ideas"},
            },
            "required": ["ideas_json"],
        },
    },
]


class MainOrchestratorAgent:
    def __init__(self, scanner: MarketScanner, risk_manager: RiskManager) -> None:
        self._scanner = scanner
        self._scorer = MarketScorer()
        self._store = SnapshotStore()
        self._risk = risk_manager
        self._markets_cache: dict[str, Market] = {}

        self._price_agent = PolymarketPriceAgent()
        self._whale_agent = PolymarketWhaleAgent()
        self._ofi_agent = OrderFlowAgent()
        self._mm_agent = MarketMakerAgent()
        self._bias_agent = KalshiBiasAgent()
        self._weather_agent = WeatherAgent()
        self._x_agent = XAgent()
        self._data_orchestrator = DataOrchestratorAgent()

        system_prompt = (_PROMPTS_DIR / "orchestrator.md").read_text()
        self._agent = BaseAgent(
            tools=_SCHEMAS,
            handlers={
                "run_market_selector":       self._run_market_selector,
                "run_polymarket_price_agent": self._run_polymarket_price_agent,
                "run_polymarket_whale_agent": self._run_polymarket_whale_agent,
                "run_order_flow_agent":       self._run_order_flow_agent,
                "run_market_maker_agent":     self._run_market_maker_agent,
                "run_kalshi_bias_agent":      self._run_kalshi_bias_agent,
                "run_weather_agent":          self._run_weather_agent,
                "run_x_agent":               self._run_x_agent,
                "run_data_orchestrator":      self._run_data_orchestrator,
                "run_risk_check":            self._run_risk_check,
                "report_slate":              self._report_slate,
            },
            system_prompt=system_prompt,
            model=config.COORDINATOR_MODEL,
        )

    async def run(self) -> str:
        return await self._agent.run("Run a trading cycle.")

    # ------------------------------------------------------------------
    # Tool handlers — thin wrappers only, no orchestration logic
    # ------------------------------------------------------------------

    async def _run_market_selector(self, top_n: int = 20) -> list[dict]:
        markets = await self._scanner.get_open_markets()
        scored = self._scorer.score_all(markets, self._store)
        self._markets_cache = {s.market.ticker: s.market for s in scored[:top_n]}
        return [
            {
                "ticker": s.market.ticker,
                "title": s.market.title,
                "category": s.market.category,
                "composite_score": round(s.composite_score, 4),
                "yes_ask": s.market.yes_ask,
                "hours_to_close": round(_hours_to_close(s.market), 1),
                "is_weather": _is_weather(s.market),
            }
            for s in scored[:top_n]
        ]

    async def _run_polymarket_price_agent(self, ticker: str, title: str,
                                           midpoint_cents: float, hours_to_close: float) -> list[dict]:
        estimates = await self._price_agent.run(ticker, title, midpoint_cents, hours_to_close)
        return [estimate_to_dict(e) for e in estimates]

    async def _run_polymarket_whale_agent(self, ticker: str, title: str) -> list[dict]:
        estimates = await self._whale_agent.run(ticker, title)
        return [estimate_to_dict(e) for e in estimates]

    async def _run_order_flow_agent(self, ticker: str, title: str) -> list[dict]:
        estimates = await self._ofi_agent.run(ticker, title)
        return [estimate_to_dict(e) for e in estimates]

    async def _run_market_maker_agent(self, ticker: str, title: str) -> list[dict]:
        estimates = await self._mm_agent.run(ticker, title)
        return [estimate_to_dict(e) for e in estimates]

    async def _run_kalshi_bias_agent(self, ticker: str, title: str,
                                      category: str = "", hours_to_close: float = 72.0) -> list[dict]:
        estimates = await self._bias_agent.run(ticker, title, category, hours_to_close)
        return [estimate_to_dict(e) for e in estimates]

    async def _run_weather_agent(self, ticker: str, title: str) -> list[dict]:
        estimates = await self._weather_agent.run(ticker, title)
        return [estimate_to_dict(e) for e in estimates]

    async def _run_x_agent(self, ticker: str, title: str, category: str = "") -> list[dict]:
        estimates = await self._x_agent.run(ticker, category, title)
        return [estimate_to_dict(e) for e in estimates]

    async def _run_data_orchestrator(self, markets_json: str, signals_json: str) -> list[dict]:
        markets_data = json.loads(markets_json)
        signals_data = json.loads(signals_json)
        markets = [self._markets_cache[m["ticker"]]
                   for m in markets_data if m["ticker"] in self._markets_cache]
        signals_by_ticker: dict[str, list[SignalEstimate]] = {
            ticker: [
                SignalEstimate(
                    source=s["source"],
                    probability=float(s["probability"]),
                    uncertainty=float(s["uncertainty"]),
                    weight=float(s["weight"]),
                    data_issued_at=datetime.now(tz=timezone.utc),
                    metadata={"narrative": s.get("narrative", "")},
                )
                for s in sig_list
            ]
            for ticker, sig_list in signals_data.items()
        }
        ideas = await self._data_orchestrator.run(markets, signals_by_ticker)
        return [
            {
                "agent_id": i.agent_id,
                "ticker": i.ticker,
                "side": i.side.value,
                "confidence": i.confidence,
                "market_price": i.market_price,
                "reasoning": i.reasoning,
                "signal_sources": i.signal_sources,
                "suggested_size_dollars": i.suggested_size_dollars,
                "category": i.category,
            }
            for i in ideas
        ]

    async def _run_risk_check(self, ideas_json: str, portfolio_json: str) -> list[dict]:
        from kalshi_trader.models import OrderAction, Side, TradeIdea
        ideas_data = json.loads(ideas_json)
        portfolio_data = json.loads(portfolio_json)
        balance = float(portfolio_data.get("balance_dollars", 0))

        approved = []
        for item in ideas_data:
            idea = TradeIdea(
                agent_id=item["agent_id"],
                ticker=item["ticker"],
                side=Side(item["side"]),
                action=OrderAction.BUY,
                confidence=float(item["confidence"]),
                market_price=float(item["market_price"]),
                reasoning=item["reasoning"],
                signal_sources=item.get("signal_sources", []),
                suggested_size_dollars=float(item.get("suggested_size_dollars", 0)),
                category=item.get("category", ""),
            )
            decision = self._risk.check_trade(idea, balance)
            if decision.approved:
                item["approved_size_dollars"] = decision.approved_size_dollars
                approved.append(item)
            else:
                log.info("Risk rejected %s: %s", idea.ticker, decision.rejection_reason)
        return approved

    async def _report_slate(self, ideas_json: str) -> dict:
        ideas = json.loads(ideas_json)
        if not ideas:
            log.info("No ideas approved this cycle.")
            return {"count": 0}
        for idea in ideas:
            log.info("SLATE: %s %s | confidence=%.0f%% | size=$%.0f | %s",
                     idea["ticker"], idea["side"].upper(),
                     idea["confidence"] * 100,
                     idea.get("approved_size_dollars", 0),
                     idea.get("reasoning", "")[:120])
        return {"count": len(ideas), "tickers": [i["ticker"] for i in ideas]}

    async def close(self) -> None:
        for agent in [self._price_agent, self._whale_agent, self._ofi_agent,
                      self._mm_agent, self._bias_agent, self._weather_agent, self._x_agent]:
            if hasattr(agent, "close"):
                await agent.close()
