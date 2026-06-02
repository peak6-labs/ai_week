"""Polymarket data client.

Two signal layers from the LunarResearcher strategy:
1. Price comparison (Gamma API) — cross-platform mispricing signal
2. Whale copy-trading (data-api) — entry signal when top wallets enter

Both feed into the Kalshi agent as SignalEstimate / WhaleSignal objects.
"""
from __future__ import annotations

import json
import re
import ssl
import time
from datetime import datetime, timezone
from pathlib import Path

import aiohttp
import truststore

_TARGETS_DEFAULT = Path(__file__).parent.parent / "data" / "targets.json"


def load_whale_targets(path: str | Path = _TARGETS_DEFAULT) -> list[str]:
    """Load whale wallet addresses from a JSON targets file.

    Returns an empty list if the file doesn't exist or contains no wallets.
    """
    p = Path(path)
    if not p.exists():
        return []
    data = json.loads(p.read_text())
    return data.get("wallets", [])


def save_whale_targets(wallets: list[str], path: str | Path = _TARGETS_DEFAULT) -> None:
    """Persist whale wallet addresses to the JSON targets file."""
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    existing = json.loads(p.read_text()) if p.exists() else {}
    existing["wallets"] = wallets
    p.write_text(json.dumps(existing, indent=2))

from kalshi_trader.models import SignalEstimate

def _ssl_context() -> ssl.SSLContext:
    ctx = truststore.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    return ctx

from dataclasses import dataclass, field as _field

_GAMMA_BASE = "https://gamma-api.polymarket.com"
_DATA_BASE = "https://data-api.polymarket.com"
_POLYMARKET_WEIGHT = 0.75


@dataclass
class WhaleSignal:
    wallet_address: str
    condition_id: str
    market_question: str
    side: str              # "YES" or "NO"
    size_usd: float
    entry_price: float
    timestamp: datetime
    metadata: dict = _field(default_factory=dict)

_STOPWORDS = frozenset({
    "will", "the", "a", "an", "in", "of", "by", "is", "be", "on", "at",
    "to", "and", "or", "for", "from", "as", "was", "are", "it", "its",
    "that", "this", "with", "have", "has", "do", "does", "before", "after",
    "when", "where", "not", "no", "if",
})

_SUFFIX_MULT = {"k": 1_000, "m": 1_000_000, "b": 1_000_000_000}


def _parse_number(raw: str) -> float | None:
    """Extract a numeric value from a token, handling $, commas, and k/m/b suffixes."""
    s = re.sub(r"[$,]", "", raw.lower()).rstrip("%")
    for suffix, mult in _SUFFIX_MULT.items():
        if s.endswith(suffix):
            try:
                return float(s[:-1]) * mult
            except ValueError:
                return None
    try:
        return float(s)
    except ValueError:
        return None


def _tokenize(title: str) -> tuple[set[str], list[float]]:
    """Return (content_words, numbers) after stripping punctuation and stopwords."""
    words: set[str] = set()
    numbers: list[float] = []
    for raw in title.lower().split():
        clean = raw.strip(".,?!:;\"'()")
        if not clean:
            continue
        n = _parse_number(clean)
        if n is not None:
            numbers.append(n)
        elif clean not in _STOPWORDS and len(clean) > 1:
            words.add(clean)
    return words, numbers


def _numbers_compatible(a: list[float], b: list[float]) -> bool:
    """True when both titles have no numbers, or when at least one pair matches within 20%."""
    if not a or not b:
        return True
    return any(
        abs(x - y) / max(x, y) <= 0.20
        for x in a
        for y in b
        if max(x, y) > 0
    )


class PolymarketClient:
    def to_signal_estimate(self, market: dict) -> SignalEstimate:
        prices = json.loads(market["outcomePrices"])
        yes_price = float(prices[0])

        # Uncertainty: markets near 50¢ are harder to call than near-resolved ones.
        # Use distance from 0.5: max at 50¢ (0.10), near zero at extremes.
        uncertainty = 0.10 * (1.0 - abs(yes_price - 0.5) / 0.5)

        issued = datetime.fromisoformat(
            market["updatedAt"].replace("Z", "+00:00")
        )

        return SignalEstimate(
            source="polymarket",
            probability=yes_price,
            uncertainty=round(uncertainty, 4),
            weight=_POLYMARKET_WEIGHT,
            data_issued_at=issued,
            metadata={"volume_24h": int(float(market.get("volume24hr", 0)))},
        )

    def match_market(self, kalshi_title: str, poly_markets: list[dict]) -> dict | None:
        """Find the best-matching Polymarket market.

        Uses Jaccard similarity on content words (stopwords and punctuation removed).
        Rejects candidates where numeric thresholds in the two titles disagree by >20%,
        catching the "$120k vs $1m" class of false positives.
        Requires ≥2 shared content words and ≥20% Jaccard.
        """
        kalshi_words, kalshi_nums = _tokenize(kalshi_title)
        best_score = 0.0
        best_market = None
        for market in poly_markets:
            poly_words, poly_nums = _tokenize(market["question"])
            if not _numbers_compatible(kalshi_nums, poly_nums):
                continue
            union = len(kalshi_words | poly_words)
            if not union:
                continue
            score = len(kalshi_words & poly_words) / union
            if score > best_score:
                best_score = score
                best_market = market
        if best_score < 0.20 or not best_market:
            return None
        best_words, _ = _tokenize(best_market["question"])
        if len(kalshi_words & best_words) < 2:
            return None
        return best_market

    def detect_volume_spike(self, current: float, recent_volumes: list[float]) -> bool:
        """Return True if current volume exceeds 2× the recent average."""
        if not recent_volumes:
            return False
        avg = sum(recent_volumes) / len(recent_volumes)
        return current > 2 * avg

    # ------------------------------------------------------------------
    # Whale copy-trading (data-api.polymarket.com)
    # ------------------------------------------------------------------

    def detect_whale_entries(
        self,
        trades: list[dict],
        min_size_usd: float = 500.0,
        lookback_seconds: int = 3600,
    ) -> list[WhaleSignal]:
        """Return WhaleSignal for each recent large BUY trade.

        Sells are ignored — we copy entries, not exits.
        Old trades (beyond lookback_seconds) are ignored — stale signal.
        """
        cutoff = time.time() - lookback_seconds
        signals = []
        for t in trades:
            if t.get("side") != "BUY":
                continue
            if float(t.get("size", 0)) < min_size_usd:
                continue
            if t.get("timestamp", 0) < cutoff:
                continue
            side = "NO" if t.get("outcome", "Yes").lower() == "no" else "YES"
            signals.append(WhaleSignal(
                wallet_address=t["proxyWallet"],
                condition_id=t["conditionId"],
                market_question=t.get("title", ""),
                side=side,
                size_usd=float(t["size"]),
                entry_price=float(t["price"]),
                timestamp=datetime.fromtimestamp(t["timestamp"], tz=timezone.utc),
            ))
        return signals

    def score_wallet_profitability(self, positions: list[dict]) -> float:
        """Win rate: fraction of positions with positive cash PnL.

        Used to filter out wallets that got lucky on one big trade.
        Returns 0.0 if no positions.
        """
        if not positions:
            return 0.0
        wins = sum(1 for p in positions if float(p.get("cashPnl", 0)) > 0)
        return wins / len(positions)

    async def get_large_trades(
        self, condition_id: str, min_size_usd: float = 500.0, limit: int = 100
    ) -> list[dict]:
        """Fetch recent trades for a market, filtered to large positions only."""
        params = {"market": condition_id, "limit": str(limit)}
        connector = aiohttp.TCPConnector(ssl=_ssl_context())
        async with aiohttp.ClientSession(connector=connector) as session:
            async with session.get(f"{_DATA_BASE}/trades", params=params) as resp:
                raw: list[dict] = await resp.json()
        return [t for t in raw if float(t.get("size", 0)) >= min_size_usd]

    async def get_wallet_positions(self, address: str, limit: int = 100) -> list[dict]:
        """Fetch current open positions for a wallet address."""
        params = {"user": address, "limit": str(limit)}
        connector = aiohttp.TCPConnector(ssl=_ssl_context())
        async with aiohttp.ClientSession(connector=connector) as session:
            async with session.get(f"{_DATA_BASE}/positions", params=params) as resp:
                return await resp.json()

    # ------------------------------------------------------------------
    # Gamma API
    # ------------------------------------------------------------------

    async def get_markets(self, limit: int = 500) -> list[dict]:
        """Fetch active markets from Polymarket Gamma API."""
        params = {"active": "true", "closed": "false", "limit": str(limit)}
        connector = aiohttp.TCPConnector(ssl=_ssl_context())
        async with aiohttp.ClientSession(connector=connector) as session:
            async with session.get(f"{_GAMMA_BASE}/markets", params=params) as resp:
                raw: list[dict] = await resp.json()
        return [m for m in raw if m.get("active") and not m.get("closed")]

    async def bootstrap_whale_targets(
        self,
        min_score: float = 0.6,
        top_n: int = 50,
        market_limit: int = 100,
        trade_min_size: float = 500.0,
    ) -> list[str]:
        """Build initial target wallet list by scanning markets for profitable large traders.

        Steps:
        1. Fetch market_limit active markets.
        2. For each market, fetch large trades (above trade_min_size).
        3. Collect all unique wallet addresses from those trades.
        4. For each unique wallet, fetch positions and score profitability.
        5. Keep wallets with score >= min_score.
        6. Sort by score descending, return top N wallet addresses.
        """
        markets = await self.get_markets(limit=market_limit)

        # Collect unique wallets across all markets
        unique_wallets: set[str] = set()
        for market in markets:
            condition_id = market.get("conditionId", "")
            trades = await self.get_large_trades(condition_id, min_size_usd=trade_min_size)
            for trade in trades:
                wallet = trade.get("proxyWallet")
                if wallet:
                    unique_wallets.add(wallet)

        # Score each wallet
        scored: list[tuple[float, str]] = []
        for wallet in unique_wallets:
            positions = await self.get_wallet_positions(wallet)
            score = self.score_wallet_profitability(positions)
            if score >= min_score:
                scored.append((score, wallet))

        # Sort by score descending, return top N addresses
        scored.sort(key=lambda x: x[0], reverse=True)
        return [wallet for _, wallet in scored[:top_n]]
