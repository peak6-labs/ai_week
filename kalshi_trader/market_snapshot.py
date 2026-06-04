from __future__ import annotations
import json
from datetime import datetime, timezone
from pathlib import Path

from kalshi_trader.models import Market


def save_snapshot(markets: list[Market], path: Path | str) -> None:
    payload = {
        "saved_at": datetime.now(timezone.utc).isoformat(),
        "count": len(markets),
        "markets": [_market_to_dict(market) for market in markets],
    }
    Path(path).write_text(json.dumps(payload, indent=2))


def load_snapshot(path: Path | str, now: datetime | None = None, filter_expired: bool = True) -> list[Market]:
    data = json.loads(Path(path).read_text())
    markets = [_dict_to_market(market_dict) for market_dict in data["markets"]]
    if not filter_expired:
        return markets
    now_dt = now or datetime.now(timezone.utc)
    return [market for market in markets if market.close_time > now_dt]


def save_checkpoint(markets: list[Market], cursor: str, pages_completed: int, path: Path | str) -> None:
    """Save a resumable checkpoint that includes the cursor for the next page."""
    payload = {
        "saved_at": datetime.now(timezone.utc).isoformat(),
        "count": len(markets),
        "pages_completed": pages_completed,
        "resume_cursor": cursor,
        "markets": [_market_to_dict(market) for market in markets],
    }
    Path(path).write_text(json.dumps(payload, indent=2))


def load_checkpoint(path: Path | str) -> tuple[list[Market], str, int]:
    """Load a resumable checkpoint. Returns (markets, resume_cursor, pages_completed)."""
    data = json.loads(Path(path).read_text())
    markets = [_dict_to_market(market_dict) for market_dict in data["markets"]]
    cursor = data.get("resume_cursor", "")
    pages_completed = data.get("pages_completed", 0)
    return markets, cursor, pages_completed


def _market_to_dict(market: Market) -> dict:
    return {
        "ticker": market.ticker,
        "event_ticker": market.event_ticker,
        "series_ticker": market.series_ticker,
        "title": market.title,
        "yes_bid": market.yes_bid,
        "yes_ask": market.yes_ask,
        "last_price": market.last_price,
        "volume_24h": market.volume_24h,
        "open_interest": market.open_interest,
        "category": market.category,
        "close_time": market.close_time.isoformat(),
        "status": market.status,
    }


def _dict_to_market(market_dict: dict) -> Market:
    return Market(
        ticker=market_dict["ticker"],
        event_ticker=market_dict["event_ticker"],
        series_ticker=market_dict["series_ticker"],
        title=market_dict["title"],
        yes_bid=market_dict["yes_bid"],
        yes_ask=market_dict["yes_ask"],
        last_price=market_dict["last_price"],
        volume_24h=market_dict["volume_24h"],
        open_interest=market_dict["open_interest"],
        category=market_dict["category"],
        close_time=datetime.fromisoformat(market_dict["close_time"]),
        status=market_dict["status"],
    )
