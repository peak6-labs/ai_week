from __future__ import annotations
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from kalshi_trader.models import Market, SignalEstimate
from kalshi_trader.external.polymarket import PolymarketClient
from kalshi_trader.external.market_scorer import score_market
from kalshi_trader.agents.base import BaseAgent
from kalshi_trader.agents.parsing import parse_signal_estimates, estimate_to_dict
from kalshi_trader.signals.polymarket import build_price_signal

_PROMPTS_DIR = Path(__file__).parent / "prompts"

_SCHEMAS: list[dict] = [
    {
        "name": "find_polymarket_match",
        "description": "Fetch active Polymarket markets and find the best match for the Kalshi title. Returns {condition_id, poly_prob, match_score} or null if no match found.",
        "input_schema": {
            "type": "object",
            "properties": {"kalshi_title": {"type": "string"}},
            "required": ["kalshi_title"],
        },
    },
    {
        "name": "check_price_gap",
        "description": "Check whether the Polymarket/Kalshi price gap and market quality pass filters. Returns {gap_cents} if the market is worth trading, null if filtered out (gap < 7c, OI < 500, or hours out of range).",
        "input_schema": {
            "type": "object",
            "properties": {
                "ticker": {"type": "string"},
                "kalshi_midpoint_cents": {"type": "number"},
                "poly_prob": {"type": "number"},
                "open_interest": {"type": "integer"},
                "hours_to_close": {"type": "number"},
            },
            "required": ["ticker", "kalshi_midpoint_cents", "poly_prob", "open_interest", "hours_to_close"],
        },
    },
    {
        "name": "build_price_signal",
        "description": "Convert a Polymarket price gap into a SignalEstimate dict.",
        "input_schema": {
            "type": "object",
            "properties": {
                "ticker": {"type": "string"},
                "poly_prob": {"type": "number"},
                "gap_cents": {"type": "number"},
                "match_score": {"type": "number"},
            },
            "required": ["ticker", "poly_prob", "gap_cents", "match_score"],
        },
    },
]


class PolymarketPriceAgent:
    def __init__(self, client: PolymarketClient | None = None) -> None:
        self._client = client or PolymarketClient()
        system_prompt = (_PROMPTS_DIR / "polymarket_price.md").read_text()
        self._agent = BaseAgent(
            tools=_SCHEMAS,
            handlers={
                "find_polymarket_match": self._find_polymarket_match,
                "check_price_gap": self._check_price_gap,
                "build_price_signal": self._build_price_signal,
            },
            system_prompt=system_prompt,
        )

    async def run(
        self,
        ticker: str,
        title: str,
        kalshi_midpoint_cents: float,
        open_interest: int,
        hours_to_close: float,
    ) -> list[SignalEstimate]:
        prompt = (
            f"Analyze this Kalshi market:\n"
            f"ticker: {ticker}\n"
            f"title: {title}\n"
            f"kalshi_midpoint_cents: {kalshi_midpoint_cents}\n"
            f"open_interest: {open_interest}\n"
            f"hours_to_close: {hours_to_close}"
        )
        raw = await self._agent.run(prompt)
        return self._parse_estimates(raw)

    def _parse_estimates(self, raw: str) -> list[SignalEstimate]:
        return parse_signal_estimates(raw)

    async def _find_polymarket_match(self, kalshi_title: str) -> dict | None:
        poly_markets = await self._client.get_markets()
        result = self._client.match_market_with_score(kalshi_title, poly_markets)
        if result is None:
            return None
        market, score = result
        try:
            poly_prob = float(json.loads(market["outcomePrices"])[0])
        except (KeyError, json.JSONDecodeError, IndexError, ValueError):
            return None
        return {
            "condition_id": market["conditionId"],
            "poly_prob": poly_prob,
            "match_score": round(score, 4),
        }

    async def _check_price_gap(
        self,
        ticker: str,
        kalshi_midpoint_cents: float,
        poly_prob: float,
        open_interest: int,
        hours_to_close: float,
    ) -> dict | None:
        close_time = datetime.now(tz=timezone.utc) + timedelta(hours=hours_to_close)
        market = Market(
            ticker=ticker,
            event_ticker="", series_ticker="", title="",
            yes_bid=kalshi_midpoint_cents - 0.5,
            yes_ask=kalshi_midpoint_cents + 0.5,
            last_price=kalshi_midpoint_cents,
            volume_24h=0,
            open_interest=open_interest,
            category="",
            close_time=close_time,
            status="open",
        )
        result = score_market(market, poly_prob)
        if result is None:
            return None
        gap_cents = (poly_prob - kalshi_midpoint_cents / 100.0) * 100.0
        return {"gap_cents": round(gap_cents, 2)}

    async def _build_price_signal(
        self,
        ticker: str,
        poly_prob: float,
        gap_cents: float,
        match_score: float,
    ) -> dict:
        estimate = build_price_signal(ticker, poly_prob, gap_cents, match_score)
        return estimate_to_dict(estimate)

    async def close(self) -> None:
        pass
