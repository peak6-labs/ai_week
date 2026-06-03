"""On-disk cache of per-series Kalshi settlement terms.

How a market settles lives on its **series**, not on the individual market: a
seven-strike temperature ladder is one series with one settlement source and one
contract-terms PDF. ``GET /series/{series_ticker}`` returns three fields the
signal pipeline needs to stay on the resolving question:

    settlement_sources  — [{name, url}], e.g. [{"name": "AccuWeather", ...}]
    contract_terms_url  — public S3 PDF with the full mechanics (not derivable
                          from the ticker; must be read off the series object)
    contract_url        — the product-certification PDF

These terms are near-static, so we cache them by ``series_ticker`` in
``series_contract_terms.json`` and dedup lookups across a ladder — mirroring the
``series_slugs.json`` cache in :mod:`kalshi_trader.web_links`. A handful of
``/series`` calls on the first cycle, zero on the next.
"""
from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

CONTRACT_TERMS_PATH = Path(__file__).with_name("series_contract_terms.json")

# Fields lifted from the series object into each cache entry.
_SERIES_FIELDS = ("settlement_sources", "contract_terms_url", "contract_url")


def load_contract_terms(path: Path | str = CONTRACT_TERMS_PATH) -> dict[str, dict]:
    """Load cached settlement terms keyed by uppercase series ticker."""
    terms_path = Path(path)
    if not terms_path.exists():
        return {}
    raw = json.loads(terms_path.read_text())
    if not isinstance(raw, dict):
        return {}
    return {str(series_ticker).upper(): entry for series_ticker, entry in raw.items()}


def save_contract_terms(terms: dict[str, dict], path: Path | str = CONTRACT_TERMS_PATH) -> None:
    """Persist settlement terms in stable sorted order (one entry per series)."""
    clean = {str(series_ticker).upper(): entry for series_ticker, entry in terms.items()}
    Path(path).write_text(json.dumps(dict(sorted(clean.items())), indent=2) + "\n")


def _extract_entry(series: dict[str, Any]) -> dict[str, Any]:
    """Reduce a series object to the settlement fields we cache."""
    entry: dict[str, Any] = {field: series.get(field) for field in _SERIES_FIELDS}
    entry["settlement_sources"] = entry.get("settlement_sources") or []
    entry["fetched_at"] = datetime.now(tz=timezone.utc).isoformat()
    return entry


async def _fetch_entry(series_ticker: str, client) -> dict[str, Any]:
    """Fetch and reduce one series' settlement terms from the API."""
    detail = await client.get_series_detail(series_ticker)
    series = detail.get("series", detail) if isinstance(detail, dict) else {}
    return _extract_entry(series if isinstance(series, dict) else {})


async def get_or_fetch(
    series_ticker: str,
    client,
    *,
    path: Path | str = CONTRACT_TERMS_PATH,
) -> dict[str, Any]:
    """Return cached settlement terms for one series, fetching+caching on a miss.

    Cache-first: a hit makes zero API calls. On a miss it calls
    ``client.get_series_detail``, extracts the settlement fields, writes the
    cache file, and returns the new entry.
    """
    normalized_ticker = series_ticker.upper()
    cache = load_contract_terms(path)
    if normalized_ticker in cache:
        return cache[normalized_ticker]
    entry = await _fetch_entry(normalized_ticker, client)
    cache[normalized_ticker] = entry
    save_contract_terms(cache, path)
    return entry


async def get_or_fetch_many(
    series_tickers: Iterable[str],
    client,
    *,
    path: Path | str = CONTRACT_TERMS_PATH,
) -> dict[str, dict]:
    """Return settlement terms for a set of series, batching the misses.

    Dedups the input, fetches only the series not already cached (concurrently),
    writes the cache once, and returns ``{series_ticker: entry}`` for every
    requested series that resolved. A series whose fetch raised is simply
    omitted (and left uncached) so one bad lookup never sinks the batch.
    """
    distinct_tickers = {series_ticker.upper() for series_ticker in series_tickers}
    cache = load_contract_terms(path)
    missing_tickers = [ticker for ticker in distinct_tickers if ticker not in cache]

    if missing_tickers:
        fetched = await asyncio.gather(
            *(_fetch_entry(ticker, client) for ticker in missing_tickers),
            return_exceptions=True,
        )
        for ticker, result in zip(missing_tickers, fetched):
            if isinstance(result, Exception):
                continue
            cache[ticker] = result
        save_contract_terms(cache, path)

    return {ticker: cache[ticker] for ticker in distinct_tickers if ticker in cache}
