"""Shared dashboard state, held on app.state for the lifetime of the process.

Holds the long-lived clients/scanner/store plus the in-memory cache of the most
recent scored slate. Request handlers read this; the background scoring loop
writes the slate fields. There is exactly one DashboardState per process (the
app runs single-worker — see app.py).
"""
from __future__ import annotations

import concurrent.futures
import os
from dataclasses import dataclass, field
from datetime import datetime

from kalshi_trader.actionability import MarketScorer, SnapshotStore
from kalshi_trader.client import KalshiClient
from kalshi_trader.dashboard.read_only_client import ReadOnlyKalshiClient
from kalshi_trader.models import Market, ScanMetadata
from kalshi_trader.scanner import MarketScanner

# A small dedicated pool for the latency-sensitive live polls (balance/positions/
# orders) so they never queue behind the scan's bulk candle/orderbook fetches.
LIVE_POLL_POOL_SIZE: int = 8

# The prod live universe is ~half a million markets — far too large to paginate
# and category-enrich every cycle. Instead the scan scores a daily, category-
# enriched snapshot (produced by scripts/fetch_markets.py); only the signals
# (candles/trades/orderbooks) refresh live each cycle.
DEFAULT_MARKETS_FILE: str = "live_markets.json"
DEFAULT_SCANNER_CATEGORIES: tuple[str, ...] = ()


@dataclass
class DashboardState:
    # Long-lived collaborators (built once at startup).
    scan_client: ReadOnlyKalshiClient
    live_client: ReadOnlyKalshiClient
    live_executor: concurrent.futures.ThreadPoolExecutor
    snapshot_store: SnapshotStore
    scanner: MarketScanner
    scorer: MarketScorer
    kalshi_env: str

    # Market-universe snapshot source + parsed/filtered cache (reloaded only when
    # the snapshot file changes, so we don't re-parse the ~270MB file every cycle).
    markets_file: str | None = None
    cached_universe_markets: list[Market] | None = None
    cached_universe_mtime: float | None = None
    scanner_categories: tuple[str, ...] = DEFAULT_SCANNER_CATEGORIES

    # In-memory scored-slate cache (written by the scoring loop).
    scored_slate_grouped: list | None = None      # output of group_by_event
    scored_slate_markets: dict = field(default_factory=dict)  # ticker -> Market (for joins)
    scored_slate_generated_at: datetime | None = None
    scored_slate_metadata: ScanMetadata | None = None
    last_scan_error: str | None = None
    scan_in_progress: bool = False
    scan_cycle_number: int = 0

    def close(self) -> None:
        """Release resources. Safe to call multiple times."""
        try:
            self.snapshot_store.close()
        finally:
            self.live_executor.shutdown(wait=False, cancel_futures=True)


def create_dashboard_state() -> DashboardState:
    """Construct the real, credential-backed state. Called from the app lifespan.

    Reads KALSHI_ENV from the environment (expected to be ``prod`` for this
    dashboard). Builds two read-only clients: the scan client uses the shared
    thread pool; the live client uses a dedicated small pool for isolation.
    """
    kalshi_env = os.environ.get("KALSHI_ENV", "demo").lower()

    markets_file = os.environ.get("DASHBOARD_MARKETS_FILE", DEFAULT_MARKETS_FILE)
    if not markets_file or not os.path.exists(markets_file):
        markets_file = None
    scanner_categories = tuple(
        category.strip().lower()
        for category in os.environ.get("DASHBOARD_SCANNER_CATEGORIES", "").split(",")
        if category.strip()
    )

    scan_client = ReadOnlyKalshiClient(KalshiClient())

    live_executor = concurrent.futures.ThreadPoolExecutor(
        max_workers=LIVE_POLL_POOL_SIZE, thread_name_prefix="dashboard-live"
    )
    live_client = ReadOnlyKalshiClient(KalshiClient(executor=live_executor))

    snapshot_store = SnapshotStore()
    scanner = MarketScanner(scan_client)
    scorer = MarketScorer()

    return DashboardState(
        scan_client=scan_client,
        live_client=live_client,
        live_executor=live_executor,
        snapshot_store=snapshot_store,
        scanner=scanner,
        scorer=scorer,
        kalshi_env=kalshi_env,
        markets_file=markets_file,
        scanner_categories=scanner_categories,
    )
