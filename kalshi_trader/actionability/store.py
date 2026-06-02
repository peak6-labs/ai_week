from __future__ import annotations

import asyncio
import logging
import sqlite3
import time
from collections import defaultdict
from typing import TYPE_CHECKING

from kalshi_trader.models import Candle

if TYPE_CHECKING:
    from kalshi_trader.client import KalshiClient

_log = logging.getLogger(__name__)


_CREATE_CANDLES = """
CREATE TABLE IF NOT EXISTS candles (
    ticker           TEXT    NOT NULL,
    period_interval  INTEGER NOT NULL,
    end_period_ts    INTEGER NOT NULL,
    volume           REAL,
    open_interest    REAL,
    price_open       REAL,
    price_high       REAL,
    price_low        REAL,
    price_close      REAL,
    price_mean       REAL,
    price_previous   REAL,
    PRIMARY KEY (ticker, period_interval, end_period_ts)
);
"""

_CREATE_REFRESH_LOG = """
CREATE TABLE IF NOT EXISTS refresh_log (
    ticker           TEXT    NOT NULL,
    period_interval  INTEGER NOT NULL,
    refreshed_at     INTEGER NOT NULL,
    PRIMARY KEY (ticker, period_interval)
);
"""


def _parse_candle_row(row: tuple) -> Candle:
    return Candle(
        end_period_ts=int(row[0]),
        volume=float(row[1] or 0),
        open_interest=float(row[2] or 0),
        price_open=float(row[3]) if row[3] is not None else None,
        price_high=float(row[4]) if row[4] is not None else None,
        price_low=float(row[5]) if row[5] is not None else None,
        price_close=float(row[6]) if row[6] is not None else None,
        price_mean=float(row[7]) if row[7] is not None else None,
        price_previous=float(row[8]) if row[8] is not None else None,
    )


def _candle_from_api(raw: dict) -> Candle:
    """Parse one raw API candlestick dict. Converts dollar prices to cents."""
    def _to_cents(v: str | float | None) -> float | None:
        if v is None:
            return None
        try:
            return round(float(v) * 100, 4)
        except (ValueError, TypeError):
            return None

    price = raw.get("price") or {}
    return Candle(
        end_period_ts=int(raw["end_period_ts"]),
        volume=float(raw.get("volume") or 0),
        open_interest=float(raw.get("open_interest") or 0),
        price_open=_to_cents(price.get("open")),
        price_high=_to_cents(price.get("high")),
        price_low=_to_cents(price.get("low")),
        price_close=_to_cents(price.get("close")),
        price_mean=_to_cents(price.get("mean")),
        price_previous=_to_cents(price.get("previous")),
    )


class SnapshotStore:
    """SQLite-backed cache for Kalshi candlestick data.

    Candle data changes slowly (daily candles once/day, hourly once/hour),
    so this cache avoids re-fetching history on every scoring cycle.
    Each machine keeps its own local copy at db_path.
    """

    DAILY_TTL_SECONDS: int = 82800    # 23 hours
    HOURLY_TTL_SECONDS: int = 3300    # 55 minutes
    PERIOD_DAILY: int = 1440
    PERIOD_HOURLY: int = 60
    DAILY_LOOKBACK_DAYS: int = 30
    HOURLY_LOOKBACK_HOURS: int = 48
    BATCH_SIZE: int = 100

    def __init__(self, db_path: str = "kalshi_trader/candle_cache.db"):
        self._conn = sqlite3.connect(db_path, check_same_thread=False)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=NORMAL")
        self._conn.execute(_CREATE_CANDLES)
        self._conn.execute(_CREATE_REFRESH_LOG)
        self._conn.commit()

    # ------------------------------------------------------------------
    # Staleness checks
    # ------------------------------------------------------------------

    def _last_refreshed(self, ticker: str, period_interval: int) -> int:
        row = self._conn.execute(
            "SELECT refreshed_at FROM refresh_log WHERE ticker=? AND period_interval=?",
            (ticker, period_interval),
        ).fetchone()
        return int(row[0]) if row else 0

    def is_daily_stale(self, ticker: str) -> bool:
        return (time.time() - self._last_refreshed(ticker, self.PERIOD_DAILY)) > self.DAILY_TTL_SECONDS

    def is_hourly_stale(self, ticker: str) -> bool:
        return (time.time() - self._last_refreshed(ticker, self.PERIOD_HOURLY)) > self.HOURLY_TTL_SECONDS

    # ------------------------------------------------------------------
    # Read
    # ------------------------------------------------------------------

    def get_daily(self, ticker: str) -> list[Candle]:
        rows = self._conn.execute(
            "SELECT end_period_ts, volume, open_interest, price_open, price_high, "
            "price_low, price_close, price_mean, price_previous "
            "FROM candles WHERE ticker=? AND period_interval=? "
            "ORDER BY end_period_ts ASC",
            (ticker, self.PERIOD_DAILY),
        ).fetchall()
        return [_parse_candle_row(r) for r in rows]

    def get_hourly(self, ticker: str) -> list[Candle]:
        rows = self._conn.execute(
            "SELECT end_period_ts, volume, open_interest, price_open, price_high, "
            "price_low, price_close, price_mean, price_previous "
            "FROM candles WHERE ticker=? AND period_interval=? "
            "ORDER BY end_period_ts ASC",
            (ticker, self.PERIOD_HOURLY),
        ).fetchall()
        return [_parse_candle_row(r) for r in rows]

    # ------------------------------------------------------------------
    # Write
    # ------------------------------------------------------------------

    def update_daily(self, ticker: str, candles: list[Candle], commit: bool = True) -> None:
        self._upsert_candles(ticker, self.PERIOD_DAILY, candles, commit=commit)

    def update_hourly(self, ticker: str, candles: list[Candle], commit: bool = True) -> None:
        self._upsert_candles(ticker, self.PERIOD_HOURLY, candles, commit=commit)

    def _upsert_candles(self, ticker: str, period: int, candles: list[Candle], commit: bool = True) -> None:
        now = int(time.time())
        rows = [
            (ticker, period, c.end_period_ts, c.volume, c.open_interest,
             c.price_open, c.price_high, c.price_low, c.price_close,
             c.price_mean, c.price_previous)
            for c in candles
        ]
        self._conn.executemany(
            "INSERT OR REPLACE INTO candles VALUES (?,?,?,?,?,?,?,?,?,?,?)", rows
        )
        self._conn.execute(
            "INSERT OR REPLACE INTO refresh_log VALUES (?,?,?)",
            (ticker, period, now),
        )
        if commit:
            self._conn.commit()

    # ------------------------------------------------------------------
    # Refresh stale tickers from the Kalshi API
    # ------------------------------------------------------------------

    async def refresh_stale(
        self,
        tickers: list[str],
        client: "KalshiClient",
        now: int,
    ) -> None:
        """Batch-fetch candles for any tickers whose cache is stale."""
        stale_daily = [t for t in tickers if self.is_daily_stale(t)]
        stale_hourly = [t for t in tickers if self.is_hourly_stale(t)]

        if not stale_daily and not stale_hourly:
            _log.info("Candle cache is warm — no refresh needed")
            return

        if stale_daily:
            _log.info(
                "Refreshing daily candles for %d/%d tickers (%d-day lookback)",
                len(stale_daily), len(tickers), self.DAILY_LOOKBACK_DAYS,
            )
        if stale_hourly:
            _log.info(
                "Refreshing hourly candles for %d/%d tickers (%dh lookback)",
                len(stale_hourly), len(tickers), self.HOURLY_LOOKBACK_HOURS,
            )

        tasks = []
        if stale_daily:
            tasks.append(self._fetch_and_store(
                stale_daily, client, now,
                period=self.PERIOD_DAILY,
                lookback_seconds=self.DAILY_LOOKBACK_DAYS * 86400,
            ))
        if stale_hourly:
            tasks.append(self._fetch_and_store(
                stale_hourly, client, now,
                period=self.PERIOD_HOURLY,
                lookback_seconds=self.HOURLY_LOOKBACK_HOURS * 3600,
            ))

        if tasks:
            await asyncio.gather(*tasks)
        _log.info("Candle cache refresh complete")

    async def _fetch_and_store(
        self,
        tickers: list[str],
        client: "KalshiClient",
        now: int,
        period: int,
        lookback_seconds: int,
    ) -> None:
        label = "daily" if period == self.PERIOD_DAILY else "hourly"
        start_ts = now - lookback_seconds
        batches = [tickers[i:i + self.BATCH_SIZE] for i in range(0, len(tickers), self.BATCH_SIZE)]
        _log.info("Fetching %s candles in %d parallel batches...", label, len(batches))

        sem = asyncio.Semaphore(8)

        async def _fetch_one(batch: list[str], batch_num: int) -> dict[str, list[Candle]]:
            _log.debug("Fetching %s batch %d/%d (%d tickers)", label, batch_num, len(batches), len(batch))
            async with sem:
                for attempt in range(4):
                    try:
                        resp = await client.get_market_candlesticks_batch(batch, start_ts, now, period)
                        break
                    except Exception as exc:
                        status = getattr(getattr(exc, "response", None), "status_code", None)
                        if status == 429:
                            wait = 2 ** attempt
                            _log.debug("%s batch %d/%d: 429, retrying in %ds", label, batch_num, len(batches), wait)
                            await asyncio.sleep(wait)
                        else:
                            _log.warning("%s batch %d/%d failed: %s", label, batch_num, len(batches), exc)
                            return {}
                else:
                    _log.warning("%s batch %d/%d: giving up after retries", label, batch_num, len(batches))
                    return {}
            by_ticker: dict[str, list[Candle]] = defaultdict(list)
            for entry in (resp.get("candles") or []):
                ticker = entry.get("market_ticker") or entry.get("ticker", "")
                for raw in (entry.get("candlesticks") or []):
                    try:
                        by_ticker[ticker].append(_candle_from_api(raw))
                    except (KeyError, TypeError):
                        continue
            _log.debug("%s batch %d/%d: %d candles across %d tickers",
                       label, batch_num, len(batches),
                       sum(len(v) for v in by_ticker.values()), len(by_ticker))
            return by_ticker

        results = await asyncio.gather(*[_fetch_one(b, i + 1) for i, b in enumerate(batches)])

        for by_ticker in results:
            for ticker, candles in by_ticker.items():
                if period == self.PERIOD_DAILY:
                    self.update_daily(ticker, candles, commit=False)
                else:
                    self.update_hourly(ticker, candles, commit=False)
        self._conn.commit()

    def close(self) -> None:
        self._conn.close()
