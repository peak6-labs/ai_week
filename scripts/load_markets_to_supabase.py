#!/usr/bin/env python
"""Load the live_markets.json board snapshot into the Supabase ``markets`` table.

Streams the ~281MB snapshot with ijson so memory stays bounded over the
~484k markets, maps each market dict to a public.markets row (dedupe key:
ticker), and upserts in batches of 500 via kalshi_trader.db.upsert_markets.

Supabase isolation: writes go ONLY through kalshi_trader.db, which validates
the project ref (xhyqdrhrwgebidvsnwbx) and refuses any other project. INSERT/
UPSERT only — no DELETE. This script never executes any trade.

Usage:
    # Test run — first 1000 markets, then verify the row count:
    python scripts/load_markets_to_supabase.py --limit 1000

    # Full load:
    python scripts/load_markets_to_supabase.py

    # Add a pause between batches if the API throttles:
    python scripts/load_markets_to_supabase.py --sleep 0.05
"""
from __future__ import annotations

import argparse
import asyncio
import time

import ijson

import kalshi_trader.config  # noqa: F401 — loads .env

DEFAULT_PATH = "/Users/scorley/code/live_markets.json"
BATCH_SIZE = 500


def _read_snapshot_saved_at(path: str) -> str | None:
    """Read the top-level ``saved_at`` value without loading the whole file."""
    try:
        with open(path, "rb") as file_handle:
            for prefix, _event, value in ijson.parse(file_handle):
                if prefix == "saved_at":
                    return value.replace("Z", "+00:00") if isinstance(value, str) else None
                if prefix == "markets":
                    # Reached the big list — saved_at precedes it, so stop.
                    return None
    except Exception:
        return None
    return None


async def _run(path: str, limit: int | None, sleep_between_batches: float) -> None:
    from kalshi_trader import db

    snapshot_at = _read_snapshot_saved_at(path)
    print(f"Snapshot saved_at: {snapshot_at}")

    start_time = time.monotonic()
    pending_rows: list[dict] = []
    seen_count = 0
    written_count = 0
    skipped_count = 0

    async def flush() -> None:
        nonlocal written_count, pending_rows
        if not pending_rows:
            return
        written_count += await db.upsert_markets(pending_rows)
        pending_rows = []
        if sleep_between_batches > 0:
            await asyncio.sleep(sleep_between_batches)

    with open(path, "rb") as file_handle:
        # use_float=True: decode JSON numbers as float, not Decimal — Decimal is
        # not JSON-serializable and would break the jsonb ``raw`` column write.
        for market in ijson.items(file_handle, "markets.item", use_float=True):
            if not market.get("ticker"):
                skipped_count += 1
                continue
            try:
                pending_rows.append(db.prepare_market_row(market, snapshot_at))
            except (KeyError, TypeError, ValueError) as caught_exception:
                skipped_count += 1
                print(f"  skip malformed market: {caught_exception}")
                continue

            seen_count += 1
            if len(pending_rows) >= BATCH_SIZE:
                await flush()
                if seen_count % 50_000 == 0:
                    elapsed = time.monotonic() - start_time
                    print(f"  ...{seen_count} read, {written_count} written ({elapsed:.0f}s)")

            if limit is not None and seen_count >= limit:
                break

    await flush()

    elapsed = time.monotonic() - start_time
    print(
        f"Done. read={seen_count} written={written_count} skipped={skipped_count} "
        f"in {elapsed:.1f}s"
    )

    total_in_table = await db.count_markets()
    print(f"markets table now holds {total_in_table} rows")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--path", default=DEFAULT_PATH, help="Path to live_markets.json")
    parser.add_argument(
        "--limit", type=int, default=None,
        help="Stop after this many markets (test run). Omit for full load.",
    )
    parser.add_argument(
        "--sleep", type=float, default=0.0,
        help="Seconds to sleep between batches if the API throttles.",
    )
    args = parser.parse_args()
    asyncio.run(_run(args.path, args.limit, args.sleep))


if __name__ == "__main__":
    main()
