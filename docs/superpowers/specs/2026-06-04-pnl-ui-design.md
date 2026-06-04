# PnL UI: Realized vs Unrealized Breakdown + Closed Positions Fix

**Date:** 2026-06-04  
**Status:** Approved

## Problem

1. The status bar shows only unrealized P&L. There is no realized P&L figure.
2. The closed positions table is always empty. Root cause: `_poll_closed_positions` fetches from Supabase, but positions are only recorded there when the executor's `close_position()` DB call runs. Positions closed by Kalshi market settlement (the common case) are never written to Supabase, so the table stays empty.

## Goal

- Show realized P&L and unrealized P&L as separate figures in the status bar.
- Populate the closed positions table from the Kalshi fills API so it works regardless of how a position was closed (manual sell or market settlement).
- Add a totals row to the closed positions table.
- Keep API traffic well within rate limits.

## Design

### Data pipeline: fills-based closed position reconstruction

Replace the Supabase `_poll_closed_positions` poller with a new `_poll_fills` task.

**Cadence:** every 5 minutes. Closed positions change slowly; 12 calls/hour is negligible.

**Cursor-incremental fetching:**
- `KalshiClient.get_fills()` gains an optional `cursor: str | None = None` parameter, passed as a query param to `GET /portfolio/fills`.
- The response includes a `cursor` field for the next page. The method returns both the fills list and the next cursor.
- On first startup, the poller pages through all fills until cursor is exhausted (full history, one-time cost).
- On subsequent ticks, it passes the last cursor to fetch only new fills since the previous poll.

**Cache structure (local to `server.py`, not in `TradingState`):**
```python
_fills_cache: dict[str, list[dict]] = {}   # ticker → list of fill dicts
_fills_cursor: str | None = None
```

**Closed position detection:**
After each incremental fetch, recompute from cache:
1. Build the set of currently open tickers from `trading_state.positions`.
2. For each ticker in `_fills_cache` that is NOT in the open tickers set:
   - Compute net quantity: `sum(buy counts) - sum(sell counts)`.
   - If net quantity == 0: the position is fully closed.
3. For each fully closed ticker, compute:
   - `side`: from the `side` field of the first buy fill ("YES" or "NO").
   - `contracts`: total buy count (equals total sell count).
   - `avg_entry_cents`: AVCO of buy fills — `sum(fill.yes_price * fill.count for buy fills) / contracts`. For NO positions, entry price in NO terms = `100 - avg_entry_yes_cents`.
   - `avg_exit_cents`: AVCO of sell fills, same-side price.
   - `opened_at`: earliest buy fill `created_time`.
   - `closed_at`: latest sell fill `created_time`.
   - `realized_pnl_dollars`: computed as `(avg_exit_side_cents - avg_entry_side_cents) * contracts / 100` where "side cents" = `yes_price` for YES, `100 - yes_price` for NO.
4. Write the resulting list to `trading_state.closed_positions`.

**Shape written to `trading_state.closed_positions`** (matches existing table rendering):
```python
{
    "ticker": str,
    "side": "YES" | "NO",
    "contracts": int,
    "entry_price_cents": float,   # avg entry in side terms
    "exit_price_cents": float,    # avg exit in side terms
    "opened_at": str,             # ISO timestamp
    "closed_at": str,             # ISO timestamp
    "realized_pnl_dollars": float,
}
```

**Startup task registration:** `_poll_fills` is started as an `asyncio.create_task` in the `_start_account_poller` startup event, alongside the existing `_poll_kalshi_account` task. `_poll_closed_positions` (the Supabase poller) is removed.

### `KalshiClient` and `ReadOnlyKalshiClient` changes

`KalshiClient.get_fills()`:
```python
async def get_fills(
    self,
    ticker: str | None = None,
    cursor: str | None = None,
    limit: int = 1000,
) -> dict:
    params: dict = {"limit": limit}
    if ticker:
        params["ticker"] = ticker
    if cursor:
        params["cursor"] = cursor
    return await self.get("/portfolio/fills", params=params)
```
Returns the raw response dict; callers extract `response["fills"]` and `response.get("cursor")`.

`ReadOnlyKalshiClient.get_fills()`: updated signature to match; passes all params through.

### Status bar changes (HTML + JS)

Add a `Realized P&L` stat chip between `Unreal. P&L` and `Fees Paid`:
```html
<div class="stat-group">
  <span class="stat-label">Real. P&amp;L</span>
  <span class="stat-value" id="stat-realized-pnl">$0.00</span>
</div>
```

In `updateStatusBar()`:
```js
const realizedPnl = (state.closed_positions || [])
  .reduce((sum, p) => sum + (p.realized_pnl_dollars ?? 0), 0);
const realizedEl = document.getElementById('stat-realized-pnl');
realizedEl.textContent = (realizedPnl >= 0 ? '+' : '') + fmt$(realizedPnl);
realizedEl.className = 'stat-value ' + pnlClass(realizedPnl);
```

### Closed positions table totals row

Add a `<tfoot>` to the closed positions table with a totals row:
- Blank cells for Ticker, Side, Opened, Closed, Entry ¢, Exit ¢.
- `Contracts`: sum of all contracts.
- `P&L`: sum of all `realized_pnl_dollars`, color-coded.

Rendered at the bottom of `renderClosedPositions()` when there is at least one closed position.

## What is not changing

- `TradingState` shape: `closed_positions: list[dict]` already exists.
- Open positions table and its unrealized P&L column: unchanged.
- The existing `daily_pnl_dollars` field (used for `Unreal. P&L` in the status bar): unchanged.
- Supabase positions/trades tables: still written by the executor; this change only affects what the UI reads.

## Rate limit analysis

| Call | Cadence | Calls/hour |
|------|---------|------------|
| `get_fills` (incremental, cursor) | every 5 min | 12 |
| Existing `get_balance` / `get_positions` / `get_orders` | every 10s | 1,080 |
| Existing `get_market` per open position/order | every 10s | ~180 (est.) |

The fills poll adds 12 calls/hour to an existing baseline of ~1,260. Negligible.

## Open questions / future work

- If the same ticker is traded multiple times (opened, closed, reopened, closed again), the current design aggregates all fills into a single row. Future work: track round trips and show one row per entry/exit pair.
- Fees are not included in the fills response. The realized PnL figure computed from fills will be gross (before fees). If Kalshi includes a fees field in the fills response, subtract it; otherwise the `Realized P&L` stat chip and the P&L column in the closed positions table should show a `(gross)` label so the user is not misled. The existing open positions table shows fees separately, so this is a consistent treatment.
