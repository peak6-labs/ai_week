"""Polymarket data client.

Two signal layers from the LunarResearcher strategy:
1. Price comparison (Gamma API) — cross-platform mispricing signal
2. Whale copy-trading (data-api) — entry signal when top wallets enter

Both feed into the Kalshi agent as SignalEstimate / WhaleSignal objects.
"""
from __future__ import annotations

import asyncio
import json
import re
import ssl
import time
from datetime import datetime, timezone
from pathlib import Path

import aiohttp
import truststore

_TARGETS_DEFAULT = Path(__file__).parent.parent / "data" / "targets.json"

# SSL context created once per process — truststore.SSLContext is expensive to construct
_SSL_CTX: ssl.SSLContext | None = None


def load_whale_targets(
    scorer: str = "winrate", path: str | Path = _TARGETS_DEFAULT
) -> list[str]:
    """Load a named whale wallet list from the targets file.

    Args:
        scorer: "winrate" (win-rate only) or "harvard" (5-signal composite).
                Falls back to legacy "wallets" key for old files.
        path:   Path to the targets JSON file.
    """
    p = Path(path)
    if not p.exists():
        return []
    data = json.loads(p.read_text())
    return data.get(scorer, data.get("wallets", []))


def save_whale_targets(
    wallets: list[str],
    scorer: str = "winrate",
    path: str | Path = _TARGETS_DEFAULT,
) -> None:
    """Persist a named whale wallet list without overwriting other scorer lists."""
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    existing = json.loads(p.read_text()) if p.exists() else {}
    existing[scorer] = wallets
    p.write_text(json.dumps(existing, indent=2))

from kalshi_trader import config
from kalshi_trader.models import SignalEstimate
from kalshi_trader.ui.config_manager import cfg

def _ssl_context() -> ssl.SSLContext:
    global _SSL_CTX
    if _SSL_CTX is None:
        _SSL_CTX = truststore.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    return _SSL_CTX

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
    def __init__(self, max_concurrent: int | None = None) -> None:
        self._session: aiohttp.ClientSession | None = None
        limit = max_concurrent if max_concurrent is not None else config.POLYMARKET_MAX_CONCURRENT
        self._semaphore = asyncio.Semaphore(limit)

    def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            connector = aiohttp.TCPConnector(ssl=_ssl_context())
            self._session = aiohttp.ClientSession(connector=connector)
        return self._session

    async def _get(self, url: str, params: dict | None = None) -> object:
        async with self._semaphore:
            async with self._get_session().get(url, params=params) as resp:
                return await resp.json()

    async def close(self) -> None:
        if self._session and not self._session.closed:
            await self._session.close()
            self._session = None

    async def __aenter__(self) -> "PolymarketClient":
        return self

    async def __aexit__(self, *_: object) -> None:
        await self.close()

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
            weight=cfg.get("weight_polymarket_price"),
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

    def match_market_with_score(
        self, kalshi_title: str, poly_markets: list[dict]
    ) -> tuple[dict, float] | None:
        """Like match_market but also returns the Jaccard similarity score."""
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
        return best_market, best_score

    def detect_volume_spike(self, current: float, recent_volumes: list[float]) -> bool:
        """Return True if current volume exceeds 3x the recent average.

        3x matches the LunarResearcher threshold — smart money exiting
        creates a volume spike signalling the thesis has played out.
        """
        if not recent_volumes:
            return False
        avg = sum(recent_volumes) / len(recent_volumes)
        return current > 3 * avg

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

    def score_wallet_v2(self, positions: list[dict]) -> float:
        """Composite 3-signal scorer (WHALE_SCORER_V2=true).

        Combines:
        - win_rate (0.50 weight): fraction of positions with positive cashPnl
        - direction_accuracy (0.30): fraction where curPrice > avgPrice
          (market moved in the wallet's favour regardless of realised PnL)
        - evidence_weight (0.20): confidence factor; full weight at 10+ positions

        Returns 0.0 if no positions.
        """
        if not positions:
            return 0.0
        n = len(positions)
        wins = sum(1 for p in positions if float(p.get("cashPnl", 0)) > 0)
        win_rate = wins / n
        directional = sum(
            1 for p in positions
            if float(p.get("curPrice", 0)) > float(p.get("avgPrice", 0))
        )
        direction_accuracy = directional / n
        evidence_weight = min(n / 10.0, 1.0)
        return 0.50 * win_rate + 0.30 * direction_accuracy + 0.20 * evidence_weight

    async def get_large_trades(
        self, condition_id: str, min_size_usd: float = 500.0, limit: int = 100
    ) -> list[dict]:
        """Fetch recent trades for a market, filtered to large positions only."""
        raw: list[dict] = await self._get(  # type: ignore[assignment]
            f"{_DATA_BASE}/trades", {"market": condition_id, "limit": str(limit)}
        )
        return [t for t in raw if float(t.get("size", 0)) >= min_size_usd]

    async def get_wallet_positions(self, address: str, limit: int = 100) -> list[dict]:
        """Fetch current open positions for a wallet address."""
        return await self._get(  # type: ignore[return-value]
            f"{_DATA_BASE}/positions", {"user": address, "limit": str(limit)}
        )

    # ------------------------------------------------------------------
    # Gamma API
    # ------------------------------------------------------------------

    async def get_markets(self, page_size: int = 500) -> list[dict]:
        """Fetch all active markets using keyset pagination.

        The regular /markets endpoint hard-caps at 100 results regardless of limit.
        The /markets/keyset endpoint paginates across all 38k+ active markets.
        """
        markets: list[dict] = []
        cursor: str | None = None
        while True:
            params: dict = {"active": "true", "closed": "false", "limit": str(page_size)}
            if cursor:
                params["after_cursor"] = cursor
            data: dict = await self._get(f"{_GAMMA_BASE}/markets/keyset", params)  # type: ignore[assignment]
            batch = data.get("markets", [])
            markets.extend(m for m in batch if m.get("active") and not m.get("closed"))
            cursor = data.get("next_cursor")
            if not cursor or not batch:
                break
        return markets

    async def bootstrap_whale_targets(
        self,
        min_score: float = 0.6,
        top_n: int = 50,
        market_limit: int = 100,
        trade_min_size: float = 100.0,
    ) -> list[str]:
        """Build initial target wallet list by scanning markets for profitable large traders.

        Steps:
        1. Fetch market_limit active markets.
        2. Fetch large trades for ALL markets concurrently.
        3. Collect all unique wallet addresses from those trades.
        4. Fetch positions for ALL wallets concurrently and score profitability.
        5. Keep wallets with score >= min_score, return top N by score.
        """
        markets = await self.get_markets()
        print(f"  Scanning {len(markets)} markets for trades >= ${trade_min_size}…", flush=True)

        # Parallel: fetch trades for all markets at once
        trade_results = await asyncio.gather(
            *[self.get_large_trades(m.get("conditionId", ""), min_size_usd=trade_min_size)
              for m in markets],
            return_exceptions=True,
        )

        unique_wallets: set[str] = set()
        for result in trade_results:
            if isinstance(result, BaseException):
                continue
            for trade in result:
                wallet = trade.get("proxyWallet")
                if wallet:
                    unique_wallets.add(wallet)

        print(f"  Found {len(unique_wallets)} unique wallets, scoring…", flush=True)
        if not unique_wallets:
            return []

        # Parallel: score all wallets at once
        wallet_list = list(unique_wallets)
        pos_results = await asyncio.gather(
            *[self.get_wallet_positions(w) for w in wallet_list],
            return_exceptions=True,
        )

        scorer = self.score_wallet_v2 if config.WHALE_SCORER_V2 else self.score_wallet_profitability
        scored: list[tuple[float, str]] = []
        for wallet, result in zip(wallet_list, pos_results):
            if isinstance(result, BaseException):
                continue
            score = scorer(result)
            if score >= min_score:
                scored.append((score, wallet))

        scored.sort(key=lambda x: x[0], reverse=True)
        return [wallet for _, wallet in scored[:top_n]]
