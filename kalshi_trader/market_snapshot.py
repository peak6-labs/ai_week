from __future__ import annotations
import json
from datetime import datetime, timezone
from pathlib import Path

from kalshi_trader.models import Market


def save_snapshot(markets: list[Market], path: Path | str) -> None:
    payload = {
        "saved_at": datetime.now(timezone.utc).isoformat(),
        "count": len(markets),
        "markets": [_market_to_dict(m) for m in markets],
    }
    Path(path).write_text(json.dumps(payload, indent=2))


def load_snapshot(path: Path | str, now: datetime | None = None) -> list[Market]:
    data = json.loads(Path(path).read_text())
    now_dt = now or datetime.now(timezone.utc)
    markets = [_dict_to_market(d) for d in data["markets"]]
    return [m for m in markets if m.close_time > now_dt]


def _market_to_dict(m: Market) -> dict:
    return {
        "ticker": m.ticker,
        "event_ticker": m.event_ticker,
        "series_ticker": m.series_ticker,
        "title": m.title,
        "yes_bid": m.yes_bid,
        "yes_ask": m.yes_ask,
        "last_price": m.last_price,
        "volume_24h": m.volume_24h,
        "open_interest": m.open_interest,
        "category": m.category,
        "close_time": m.close_time.isoformat(),
        "status": m.status,
    }


def _dict_to_market(d: dict) -> Market:
    return Market(
        ticker=d["ticker"],
        event_ticker=d["event_ticker"],
        series_ticker=d["series_ticker"],
        title=d["title"],
        yes_bid=d["yes_bid"],
        yes_ask=d["yes_ask"],
        last_price=d["last_price"],
        volume_24h=d["volume_24h"],
        open_interest=d["open_interest"],
        category=d["category"],
        close_time=datetime.fromisoformat(d["close_time"]),
        status=d["status"],
    )
