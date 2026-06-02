from __future__ import annotations
from collections import defaultdict, deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import NamedTuple


class PriceLevel(NamedTuple):
    price: int   # cents 1-99
    size: int    # contracts


@dataclass
class OrderBookState:
    """
    In-memory order book for one or more Kalshi market tickers.
    Thread-safe for single-threaded asyncio use.
    """
    _bids: dict[str, dict[int, int]] = field(default_factory=lambda: defaultdict(dict))
    _asks: dict[str, dict[int, int]] = field(default_factory=lambda: defaultdict(dict))
    # rolling (timestamp, trade_size) for volume velocity
    _trades: dict[str, deque] = field(default_factory=lambda: defaultdict(lambda: deque(maxlen=500)))

    def apply_delta(self, ticker: str, side: str, price: int, delta: int) -> None:
        """Apply an orderbook_delta message. delta=0 means remove level."""
        book = self._bids[ticker] if side == "yes" else self._asks[ticker]
        if delta == 0:
            book.pop(price, None)
        else:
            book[price] = delta

    def apply_snapshot(self, ticker: str, yes_book: list[dict], no_book: list[dict]) -> None:
        """Replace full order book from a snapshot message."""
        self._bids[ticker] = {int(lvl["price"]): int(lvl["quantity"]) for lvl in yes_book}
        self._asks[ticker] = {int(lvl["price"]): int(lvl["quantity"]) for lvl in no_book}

    def record_trade(self, ticker: str, size: int) -> None:
        self._trades[ticker].append((datetime.now(tz=timezone.utc), size))

    def bid_ask_imbalance(self, ticker: str) -> float:
        """
        Returns value in [-1, 1].
        +1 = all size on bid (buy pressure), -1 = all size on ask (sell pressure).
        0 = balanced or no data.
        """
        bid_vol = sum(self._bids[ticker].values())
        ask_vol = sum(self._asks[ticker].values())
        total = bid_vol + ask_vol
        if total == 0:
            return 0.0
        return (bid_vol - ask_vol) / total

    def best_bid(self, ticker: str) -> int | None:
        bids = self._bids[ticker]
        return max(bids) if bids else None

    def best_ask(self, ticker: str) -> int | None:
        asks = self._asks[ticker]
        return min(asks) if asks else None

    def spread_cents(self, ticker: str) -> int | None:
        bid = self.best_bid(ticker)
        ask = self.best_ask(ticker)
        if bid is None or ask is None:
            return None
        return ask - bid

    def volume_velocity(self, ticker: str, window_seconds: int = 300) -> float:
        """Contracts traded per minute over the last `window_seconds`."""
        now = datetime.now(tz=timezone.utc)
        trades = self._trades[ticker]
        cutoff = now.timestamp() - window_seconds
        recent = [size for ts, size in trades if ts.timestamp() > cutoff]
        if not recent:
            return 0.0
        return sum(recent) / (window_seconds / 60.0)

    def tickers(self) -> list[str]:
        return list(set(self._bids) | set(self._asks))
