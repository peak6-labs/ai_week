#!/usr/bin/env python
"""Download cached contract-terms PDFs for survivor markets (read-only, Tier 1).

Tier 0 (``market_rules.py``) attaches each candidate's structured settlement
context. For the handful of *survivors* that reach the Step 5 adversarial check,
this fetches the full contract-terms PDF — the document that spells out the
mechanics the API text omits (strict ``>`` vs ``>=``, determination-delay
clauses, "first official report governs", source hierarchy). The orchestrator
then Reads the PDF directly (the Read tool parses PDFs natively).

Resolves each survivor ticker to its distinct **series**, looks up the series'
``contract_terms_url`` in the on-disk cache (populated by ``market_rules.py``),
and downloads each PDF once to ``/tmp/contract_terms_<series>.pdf``. A series not
yet cached is fetched on demand so this works on a survivor that skipped the
deep-signal subset.

Usage:
    KALSHI_ENV=prod PYTHONPATH=. python scripts/contract_terms_doc.py \
        --tickers KXHIGHLAX-26JUN03-T80 KXTEMPNYCH-26JUN0311-T85.99

Prints a JSON object: { series_ticker: {contract_terms_url, path|error} }.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

import requests

import kalshi_trader.config  # noqa: F401 — loads .env (and, via the client, truststore)
from kalshi_trader.client import KalshiClient
from kalshi_trader.contract_terms import get_or_fetch_many

DOWNLOAD_DIR = Path("/tmp")


def _series_ticker(ticker: str) -> str:
    """Reduce a market/event ticker to its series prefix (part before first '-')."""
    return ticker.split("-", 1)[0].upper()


def _download_pdf(url: str, destination: Path, timeout_seconds: int = 30) -> None:
    """Download a public S3 PDF to disk (cache-by-series: skip if present)."""
    if destination.exists() and destination.stat().st_size > 0:
        return
    response = requests.get(url, timeout=timeout_seconds)
    response.raise_for_status()
    destination.write_bytes(response.content)


async def _run(tickers: list[str]) -> None:
    distinct_series = {_series_ticker(ticker) for ticker in tickers}

    client = KalshiClient()
    try:
        terms_by_series = await get_or_fetch_many(distinct_series, client)
    finally:
        await client.aclose()

    results: dict[str, dict] = {}
    for series_ticker in sorted(distinct_series):
        terms = terms_by_series.get(series_ticker)
        contract_terms_url = (terms or {}).get("contract_terms_url")
        if not contract_terms_url:
            results[series_ticker] = {"error": "no contract_terms_url for series"}
            continue
        destination = DOWNLOAD_DIR / f"contract_terms_{series_ticker}.pdf"
        try:
            _download_pdf(contract_terms_url, destination)
            results[series_ticker] = {
                "contract_terms_url": contract_terms_url,
                "path": str(destination),
            }
        except Exception as caught_exception:  # one bad PDF shouldn't sink the batch
            results[series_ticker] = {
                "contract_terms_url": contract_terms_url,
                "error": str(caught_exception)[:120],
            }

    print(json.dumps(results, default=str))


def main() -> None:
    parser = argparse.ArgumentParser(description="Download contract-terms PDFs for survivors")
    parser.add_argument("--tickers", nargs="+", required=True)
    args = parser.parse_args()
    if not args.tickers:
        print("{}")
        return
    asyncio.run(_run(args.tickers))


if __name__ == "__main__":
    main()
