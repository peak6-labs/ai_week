from __future__ import annotations
import json
from pathlib import Path
from kalshi_trader.models import SignalEstimate
from kalshi_trader.external.polymarket import PolymarketClient, load_whale_targets
from kalshi_trader.agents.base import BaseAgent
from kalshi_trader.agents.parsing import parse_signal_estimates, estimate_to_dict
from kalshi_trader.signals.polymarket import build_whale_signal

_PROMPTS_DIR = Path(__file__).parent / "prompts"

_SCHEMAS: list[dict] = [
    {
        "name": "load_whale_targets",
        "description": "Load the list of tracked high-performing wallet addresses from targets.json.",
        "input_schema": {"type": "object", "properties": {}, "required": []},
    },
    {
        "name": "find_polymarket_match",
        "description": "Fetch active Polymarket markets and find the best match for the Kalshi title. Returns {condition_id, poly_prob, match_score} or null.",
        "input_schema": {
            "type": "object",
            "properties": {"kalshi_title": {"type": "string"}},
            "required": ["kalshi_title"],
        },
    },
    {
        "name": "get_whale_entries",
        "description": "Fetch recent large trades for a Polymarket condition and return entries from target wallets. Returns list of {wallet_address, side, entry_price, size_usd, timestamp}.",
        "input_schema": {
            "type": "object",
            "properties": {
                "condition_id": {"type": "string"},
                "target_wallets": {"type": "array", "items": {"type": "string"}},
            },
            "required": ["condition_id", "target_wallets"],
        },
    },
    {
        "name": "build_whale_signal",
        "description": "Build a whale SignalEstimate from target wallet entries. Returns a SignalEstimate dict, or null if no entries.",
        "input_schema": {
            "type": "object",
            "properties": {
                "ticker": {"type": "string"},
                "whale_entries": {"type": "array", "items": {"type": "object"}},
            },
            "required": ["ticker", "whale_entries"],
        },
    },
]


WHALE_SCORERS = ("leaderboard_alltime", "leaderboard_week", "winrate", "harvard")


class PolymarketWhaleAgent:
    """One instance per scorer key. Run all four in parallel for independent signals."""

    def __init__(
        self,
        scorer: str = "leaderboard_alltime",
        client: PolymarketClient | None = None,
    ) -> None:
        assert scorer in WHALE_SCORERS, f"scorer must be one of {WHALE_SCORERS}"
        self._scorer = scorer
        self._client = client or PolymarketClient()
        system_prompt = (_PROMPTS_DIR / "polymarket_whale.md").read_text()
        self._agent = BaseAgent(
            tools=_SCHEMAS,
            handlers={
                "load_whale_targets": self._load_whale_targets,
                "find_polymarket_match": self._find_polymarket_match,
                "get_whale_entries": self._get_whale_entries,
                "build_whale_signal": self._build_whale_signal,
            },
            system_prompt=system_prompt,
        )

    async def run(self, ticker: str, title: str) -> list[SignalEstimate]:
        prompt = f"Analyze this Kalshi market:\nticker: {ticker}\ntitle: {title}"
        raw = await self._agent.run(prompt)
        estimates = self._parse_estimates(raw)
        # Tag each estimate with the scorer so the orchestrator can distinguish them
        for e in estimates:
            e.source = f"polymarket_whale_{self._scorer}"
            e.metadata["scorer"] = self._scorer
        return estimates

    def _parse_estimates(self, raw: str) -> list[SignalEstimate]:
        return parse_signal_estimates(raw)

    async def _load_whale_targets(self) -> list[str]:
        return load_whale_targets(scorer=self._scorer)

    async def _find_polymarket_match(self, kalshi_title: str) -> dict | None:
        # Full paginated market list via Gamma keyset API (38k+ markets)
        markets = await self._client.get_markets_cached()
        result = self._client.match_market_with_score(kalshi_title, markets)
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

    async def _get_whale_entries(
        self, condition_id: str, target_wallets: list[str]
    ) -> list[dict]:
        trades = await self._client.get_large_trades(condition_id)
        signals = self._client.detect_whale_entries(trades)
        target_set = set(target_wallets)
        return [
            {
                "wallet_address": s.wallet_address,
                "side": s.side,
                "entry_price": s.entry_price,
                "size_usd": s.size_usd,
                "timestamp": s.timestamp.isoformat(),
            }
            for s in signals
            if s.wallet_address in target_set
        ]

    async def _build_whale_signal(self, ticker: str, whale_entries: list[dict]) -> dict | None:
        estimate = build_whale_signal(ticker, whale_entries)
        if estimate is None:
            return None
        return estimate_to_dict(estimate)

    async def close(self) -> None:
        pass
