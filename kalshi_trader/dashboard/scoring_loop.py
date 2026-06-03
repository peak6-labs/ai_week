"""Background scoring loop for the dashboard.

Re-runs the actionability scan every few minutes and caches the grouped result
in memory on the DashboardState. The dashboard's /api/ideas endpoint serves that
cache instantly — it never recomputes per request.

Design notes:
- The sleep is END-anchored: the next cycle starts ``interval_seconds`` after the
  previous one *finishes*, so a slow cycle never stacks onto the next.
- A scan_in_progress flag guards against any overlap (defense in depth).
- All non-cancellation exceptions are swallowed and recorded so one bad cycle
  never kills the loop; the last good slate keeps being served (status "degraded").
- Categories are enriched (should_enrich_categories=True) — without this the prod
  ideas panel is empty, because prod /markets returns empty category fields.
"""
from __future__ import annotations

import asyncio
import logging
import os
import time
from datetime import datetime, timezone

from kalshi_trader.dashboard.state import DashboardState
from kalshi_trader.grouping import group_by_event
from kalshi_trader.market_snapshot import load_snapshot
from kalshi_trader.models import Market

_log = logging.getLogger(__name__)

DEFAULT_INTERVAL_SECONDS: int = 300  # re-score every 5 minutes


def _load_snapshot_universe(markets_file: str) -> list[Market]:
    """Parse the snapshot catalog off-thread; filtering happens in the scanner."""
    return load_snapshot(markets_file, datetime.now(timezone.utc))


async def _resolve_universe(state: DashboardState) -> list[Market] | None:
    """Return the filtered market universe, reloading the snapshot only when it
    changes on disk. Returns None when no snapshot is configured (live fallback)."""
    markets_file = state.markets_file
    if not markets_file or not os.path.exists(markets_file):
        return None
    mtime = os.path.getmtime(markets_file)
    if state.cached_universe_markets is None or state.cached_universe_mtime != mtime:
        _log.info("Loading market universe from snapshot %s (parsing off-thread)...", markets_file)
        state.cached_universe_markets = await asyncio.to_thread(_load_snapshot_universe, markets_file)
        state.cached_universe_mtime = mtime
        _log.info("Universe loaded: %d snapshot markets", len(state.cached_universe_markets))
    return state.cached_universe_markets


async def run_one_scan_cycle(state: DashboardState) -> None:
    """Run a single scoring scan and publish the result onto ``state``."""
    if state.scan_in_progress:
        _log.warning("Scan already in progress — skipping this cycle")
        return

    state.scan_in_progress = True
    start_time = time.monotonic()
    try:
        universe = await _resolve_universe(state)
        if universe is not None:
            scan_result = await state.scanner.run_scan(
                state.scorer,
                state.snapshot_store,
                markets=universe,
                categories=state.scanner_categories,
            )
        else:
            # No snapshot available — fall back to a live universe scan with category
            # enrichment. This is slow on prod (~500k markets); prefer a snapshot.
            _log.warning("No markets snapshot configured — falling back to live scan (slow on prod)")
            scan_result = await state.scanner.run_scan(
                state.scorer,
                state.snapshot_store,
                should_enrich_categories=True,
                categories=state.scanner_categories,
            )
        ranked = scan_result.ranked_markets
        grouped = group_by_event(ranked)

        state.scored_slate_grouped = grouped
        state.scored_slate_markets = {
            scored_market.market.ticker: scored_market.market for scored_market in ranked
        }
        state.scored_slate_generated_at = datetime.now(timezone.utc)
        state.scored_slate_metadata = scan_result.metadata
        state.last_scan_error = scan_result.metadata.degraded_reason
        state.scan_cycle_number += 1
        _log.info(
            "Scan cycle %d complete in %.1fs — %d markets, %d events",
            state.scan_cycle_number,
            time.monotonic() - start_time,
            len(ranked),
            len(grouped),
        )
    finally:
        state.scan_in_progress = False


async def run_scoring_loop(state: DashboardState, interval_seconds: int = DEFAULT_INTERVAL_SECONDS) -> None:
    """Forever: run a scan cycle, then sleep ``interval_seconds`` after it finishes.

    The first cycle runs immediately (warm-up). Cancellation propagates cleanly
    for shutdown; every other exception is recorded and the loop continues.
    """
    _log.info("Starting scoring loop (interval=%ds)", interval_seconds)
    while True:
        try:
            await run_one_scan_cycle(state)
        except asyncio.CancelledError:
            _log.info("Scoring loop cancelled — stopping")
            raise
        except Exception as caught_exception:
            state.last_scan_error = repr(caught_exception)
            _log.exception("Scoring cycle failed; will retry next interval")
        await asyncio.sleep(interval_seconds)
