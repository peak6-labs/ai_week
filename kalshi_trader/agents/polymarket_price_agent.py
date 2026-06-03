from __future__ import annotations
import os
import json
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path
from kalshi_trader.models import Market, SignalEstimate
from kalshi_trader.external.polymarket import PolymarketClient
from kalshi_trader.external.market_scorer import score_market
from kalshi_trader.agents.base import BaseAgent
from kalshi_trader.agents.parsing import parse_signal_estimates, estimate_to_dict
from kalshi_trader.signals.polymarket import build_price_signal

_PROMPTS_DIR = Path(__file__).parent / "prompts"
_CLI = os.environ.get("POLYMARKET_CLI_PATH", str(Path.home() / ".local" / "bin" / "polymarket"))
_MIN_DEPTH_USD = 500.0


def _cli(args: list[str]) -> object:
    r = subprocess.run([_CLI, "-o", "json"] + args, capture_output=True, text=True, timeout=30)
    if r.returncode != 0:
        raise RuntimeError(r.stderr.strip())
    return json.loads(r.stdout)


_SCHEMAS: list[dict] = [
    {
        "name": "find_polymarket_match",
        "description": (
            "Fetch active Polymarket markets via CLI and find the best title match. "
            "Also fetches the live CLOB midpoint for the matched market. "
            "Returns {matched: true, condition_id, token_id, clob_mid, match_score} on success, "
            "or {matched: false, reason} where reason is one of: "
            "'no_title_match' (Jaccard < 0.20 or < 2 shared words), "
            "'different_subjects' (both titles name different people/entities), "
            "'no_token_ids' (market found but CLOB token missing), "
            "'clob_price_unavailable' (could not fetch live or snapshot price)."
        ),
        "input_schema": {
            "type": "object",
            "properties": {"kalshi_title": {"type": "string"}},
            "required": ["kalshi_title"],
        },
    },
    {
        "name": "check_order_book_depth",
        "description": (
            "Fetch the live CLOB order book for a token and check whether there is "
            "sufficient liquidity on both sides (>= $500 each). "
            "Returns {bid_depth_usd, ask_depth_usd, sufficient}."
        ),
        "input_schema": {
            "type": "object",
            "properties": {"token_id": {"type": "string"}},
            "required": ["token_id"],
        },
    },
    {
        "name": "check_price_gap",
        "description": (
            "Check whether the Polymarket/Kalshi price gap passes filters. "
            "Always returns {gap_cents, passes, reason}. "
            "'passes' is true only when gap ≥ 10¢ and hours are in range. "
            "'reason' is 'actionable' when passing, or a short explanation when not "
            "(e.g. 'gap 4.2¢ below 10¢ threshold', 'hours_to_close out of range')."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "ticker": {"type": "string"},
                "kalshi_midpoint_cents": {"type": "number"},
                "poly_prob": {"type": "number"},
                "hours_to_close": {"type": "number"},
            },
            "required": ["ticker", "kalshi_midpoint_cents", "poly_prob", "hours_to_close"],
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
        # client kept for match_market_with_score text matching (no HTTP needed)
        self._client = client or PolymarketClient()
        system_prompt = (_PROMPTS_DIR / "polymarket_price.md").read_text()
        self._agent = BaseAgent(
            tools=_SCHEMAS,
            handlers={
                "find_polymarket_match": self._find_polymarket_match,
                "check_order_book_depth": self._check_order_book_depth,
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
        hours_to_close: float,
    ) -> list[SignalEstimate]:
        prompt = (
            f"Analyze this Kalshi market:\n"
            f"ticker: {ticker}\n"
            f"title: {title}\n"
            f"kalshi_midpoint_cents: {kalshi_midpoint_cents}\n"
            f"hours_to_close: {hours_to_close}"
        )
        raw = await self._agent.run(prompt)
        return self._parse_estimates(raw)

    def _parse_estimates(self, raw: str) -> list[SignalEstimate]:
        return parse_signal_estimates(raw)

    async def _find_polymarket_match(self, kalshi_title: str) -> dict:
        # Full paginated market list via Gamma keyset API (38k+ markets)
        markets = await self._client.get_markets_cached()
        result = self._client.match_market_with_score(kalshi_title, markets)
        if result is None:
            return {"matched": False, "reason": "no_title_match"}
        market, score = result
        try:
            token_ids = json.loads(market["clobTokenIds"])
            token_id = token_ids[0]  # YES token
        except (KeyError, json.JSONDecodeError, IndexError):
            return {"matched": False, "reason": "no_token_ids"}
        # CLI: get live CLOB midpoint (more accurate than Gamma's outcomePrices snapshot)
        try:
            clob_mid = float(_cli(["clob", "midpoint", token_id])["midpoint"])  # type: ignore[index]
        except Exception:
            # fall back to Gamma snapshot if CLOB call fails
            try:
                clob_mid = float(json.loads(market["outcomePrices"])[0])
            except Exception:
                return {"matched": False, "reason": "clob_price_unavailable"}
        return {
            "matched": True,
            "condition_id": market["conditionId"],
            "token_id": token_id,
            "clob_mid": clob_mid,
            "match_score": round(score, 4),
        }

    async def _check_order_book_depth(self, token_id: str) -> dict:
        book: dict = _cli(["clob", "book", token_id])  # type: ignore[assignment]
        bid_depth = sum(float(b["size"]) for b in book.get("bids", [])[:5])
        ask_depth = sum(float(a["size"]) for a in book.get("asks", [])[:5])
        return {
            "bid_depth_usd": round(bid_depth, 2),
            "ask_depth_usd": round(ask_depth, 2),
            "sufficient": bid_depth >= _MIN_DEPTH_USD and ask_depth >= _MIN_DEPTH_USD,
        }

    async def _check_price_gap(
        self,
        ticker: str,
        kalshi_midpoint_cents: float,
        poly_prob: float,
        hours_to_close: float,
    ) -> dict:
        gap_cents = round((poly_prob - kalshi_midpoint_cents / 100.0) * 100.0, 2)
        close_time = datetime.now(tz=timezone.utc) + timedelta(hours=hours_to_close)
        market = Market(
            ticker=ticker,
            event_ticker="", series_ticker="", title="",
            yes_bid=kalshi_midpoint_cents - 0.5,
            yes_ask=kalshi_midpoint_cents + 0.5,
            last_price=kalshi_midpoint_cents,
            volume_24h=0,
            open_interest=9999,  # depth now checked separately via CLOB book
            category="",
            close_time=close_time,
            status="open",
        )
        result = score_market(market, poly_prob)
        if result is None:
            abs_gap = abs(gap_cents)
            if abs_gap < 10.0:
                reason = f"gap {abs_gap:.1f}¢ below 10¢ threshold"
            else:
                reason = "hours_to_close out of range"
            return {"gap_cents": gap_cents, "passes": False, "reason": reason}
        return {"gap_cents": gap_cents, "passes": True, "reason": "actionable"}

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
        await self._client.close()
