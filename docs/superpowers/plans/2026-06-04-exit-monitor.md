# Exit Monitor Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build `scripts/exit_monitor.py` — a standalone daemon that watches open Kalshi positions via WebSocket and fires deterministic stop-loss / take-profit exits the moment a price threshold is crossed.

**Architecture:** A single async script with two loops: the WebSocket client pushes real-time bid/ask into an `OrderBookState` object, while a 0.5s check loop reads that state, runs `portfolio_checks.EXIT_CHECKS`, and places a limit sell order on signal. Position metadata is refreshed from the Kalshi REST API every 30 seconds. `_pending_exits` is pre-populated from resting sell orders on startup to prevent duplicates.

**Tech Stack:** Python asyncio, `kalshi_trader.external.kalshi_ws.KalshiWebSocketClient`, `kalshi_trader.orderbook.OrderBookState`, `kalshi_trader.portfolio_checks.EXIT_CHECKS`, `kalshi_trader.client.KalshiClient`, `aiohttp` (for server notification), `kalshi_trader.db.close_position`.

---

## Key Constants and Semantics

- `OrderBookState.best_bid(ticker)` → YES bid price in integer cents (price to sell YES aggressively)
- `OrderBookState.best_ask(ticker)` → YES ask price in integer cents (price to sell YES passively)
- `KalshiClient.create_order(ticker, action, side, count, order_type, yes_price)` always takes `yes_price` in YES-cent terms
- `portfolio_checks.EXIT_CHECKS` needs position dict with: `market_exposure_dollars`, `quantity`, `current_price_cents`, `midpoint_yes_price_cents`
- `/api/log` on `http://localhost:8000` already exists and accepts `{"message": str, "level": str}`

## File Map

| File | Status | Purpose |
|------|--------|---------|
| `scripts/exit_monitor.py` | **Create** | Standalone exit daemon |
| `tests/test_exit_monitor.py` | **Create** | Unit tests for helper functions |

No changes to `portfolio_checks.py`, `kalshi_ws.py`, `orderbook.py`, `server.py`, or `db.py`.

---

## Task 1: Helper function tests (write failing tests first)

**Files:**
- Create: `tests/test_exit_monitor.py`

- [ ] **Step 1: Create the test file**

```python
# tests/test_exit_monitor.py
import importlib.util
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from kalshi_trader.orderbook import OrderBookState

spec = importlib.util.spec_from_file_location(
    "exit_monitor", os.path.join(os.path.dirname(__file__), "..", "scripts", "exit_monitor.py")
)
exit_monitor = importlib.util.module_from_spec(spec)
spec.loader.exec_module(exit_monitor)

_build_position_dict = exit_monitor._build_position_dict
_select_yes_price = exit_monitor._select_yes_price

TICKER = "KXTEST-1"


def _make_orderbook(bid: int, ask: int, ticker: str = TICKER) -> OrderBookState:
    state = OrderBookState()
    state.apply_delta(ticker, "yes", bid, 10)
    state.apply_delta(ticker, "no", ask, 10)
    return state


def _make_meta(side: str = "yes", quantity: float = 10.0, exposure: float = 5.0) -> dict:
    return {"side": side, "quantity": quantity, "market_exposure_dollars": exposure}


class TestBuildPositionDict:
    def test_yes_position_uses_bid_as_current_price(self):
        state = _make_orderbook(bid=47, ask=53)
        result = _build_position_dict(_make_meta(side="yes"), state, TICKER)
        assert result is not None
        assert result["current_price_cents"] == 47.0

    def test_no_position_uses_100_minus_ask_as_current_price(self):
        state = _make_orderbook(bid=47, ask=53)
        result = _build_position_dict(_make_meta(side="no"), state, TICKER)
        assert result is not None
        assert result["current_price_cents"] == 47.0  # 100 - 53

    def test_midpoint_is_average_of_bid_and_ask(self):
        state = _make_orderbook(bid=47, ask=53)
        result = _build_position_dict(_make_meta(), state, TICKER)
        assert result["midpoint_yes_price_cents"] == 50.0

    def test_returns_none_when_no_bid(self):
        state = OrderBookState()
        state.apply_delta(TICKER, "no", 53, 10)
        assert _build_position_dict(_make_meta(), state, TICKER) is None

    def test_returns_none_when_no_ask(self):
        state = OrderBookState()
        state.apply_delta(TICKER, "yes", 47, 10)
        assert _build_position_dict(_make_meta(), state, TICKER) is None

    def test_passes_through_exposure_and_quantity(self):
        state = _make_orderbook(bid=47, ask=53)
        result = _build_position_dict(_make_meta(exposure=5.0, quantity=10.0), state, TICKER)
        assert result["market_exposure_dollars"] == 5.0
        assert result["quantity"] == 10.0


class TestSelectYesPrice:
    def test_yes_stop_loss_uses_bid(self):
        state = _make_orderbook(bid=47, ask=53)
        assert _select_yes_price("stop_loss", "yes", state, TICKER) == 47

    def test_yes_profit_target_uses_ask(self):
        state = _make_orderbook(bid=47, ask=53)
        assert _select_yes_price("profit_target", "yes", state, TICKER) == 53

    def test_no_stop_loss_uses_ask(self):
        # NO aggressive sell crosses NO bids (= YES ask side)
        state = _make_orderbook(bid=47, ask=53)
        assert _select_yes_price("stop_loss", "no", state, TICKER) == 53

    def test_no_profit_target_uses_bid(self):
        # NO passive sell rests at NO ask (= 100 - YES bid)
        state = _make_orderbook(bid=47, ask=53)
        assert _select_yes_price("profit_target", "no", state, TICKER) == 47

    def test_returns_none_when_no_data(self):
        state = OrderBookState()
        assert _select_yes_price("stop_loss", "yes", state, TICKER) is None
```

- [ ] **Step 2: Run tests to confirm they fail (script doesn't exist yet)**

```
python -m pytest tests/test_exit_monitor.py -v
```

Expected: `ModuleNotFoundError` or `AttributeError` — the script doesn't exist yet.

---

## Task 2: Create exit_monitor.py with helper functions

**Files:**
- Create: `scripts/exit_monitor.py`

- [ ] **Step 1: Create the script with sys.path, imports, and the two testable helpers**

```python
"""Standalone real-time exit daemon.

Watches open Kalshi positions via WebSocket and fires deterministic
stop-loss / take-profit sells the moment a price threshold is crossed.
Run independently of the UI server and orchestrator loop.
"""
from __future__ import annotations

import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import argparse
import asyncio
import logging

import aiohttp

from kalshi_trader.client import KalshiClient
from kalshi_trader.external.kalshi_ws import KalshiWebSocketClient
from kalshi_trader.orderbook import OrderBookState
from kalshi_trader.portfolio_checks import EXIT_CHECKS
from kalshi_trader import db as _db

log = logging.getLogger("exit_monitor")


def _build_position_dict(
    position_meta: dict,
    orderbook_state: OrderBookState,
    ticker: str,
) -> dict | None:
    """Build the EXIT_CHECKS position dict from metadata + live WebSocket prices.

    Returns None if bid or ask is not yet available for the ticker.
    """
    bid = orderbook_state.best_bid(ticker)
    ask = orderbook_state.best_ask(ticker)
    if bid is None or ask is None:
        return None
    side = position_meta["side"]
    # Side-relative current price: what we'd receive selling this position now
    current_price_cents = float(bid) if side == "yes" else (100.0 - float(ask))
    midpoint_yes_price_cents = (float(bid) + float(ask)) / 2.0
    return {
        "market_exposure_dollars": position_meta["market_exposure_dollars"],
        "quantity": position_meta["quantity"],
        "current_price_cents": current_price_cents,
        "midpoint_yes_price_cents": midpoint_yes_price_cents,
    }


def _select_yes_price(
    signal_reason: str,
    side: str,
    orderbook_state: OrderBookState,
    ticker: str,
) -> int | None:
    """Return the YES-price limit (cents) for the exit order.

    YES stop_loss:       bid  (aggressive — immediate fill, guarantee exit)
    YES profit_target:   ask  (passive maker — fee-efficient, rest on book)
    NO  stop_loss:       ask  (aggressive NO sell crosses YES ask side)
    NO  profit_target:   bid  (passive NO sell rests at 100 - YES bid)
    """
    bid = orderbook_state.best_bid(ticker)
    ask = orderbook_state.best_ask(ticker)
    if bid is None or ask is None:
        return None
    if side == "yes":
        return bid if signal_reason == "stop_loss" else ask
    else:
        return ask if signal_reason == "stop_loss" else bid
```

- [ ] **Step 2: Run tests — they should pass now**

```
python -m pytest tests/test_exit_monitor.py -v
```

Expected: all 11 tests pass.

- [ ] **Step 3: Commit**

```bash
git add scripts/exit_monitor.py tests/test_exit_monitor.py
git commit -m "feat(exit-monitor): add helper functions with tests"
```

---

## Task 3: Position and order fetching helpers

**Files:**
- Modify: `scripts/exit_monitor.py` (add two async helpers)
- Modify: `tests/test_exit_monitor.py` (add tests for both helpers)

- [ ] **Step 1: Add tests for the fetching helpers**

Append to `tests/test_exit_monitor.py`:

```python
import asyncio


class TestFetchOpenPositions:
    def test_extracts_yes_position(self):
        raw = {
            "market_positions": [
                {
                    "ticker": "KXTEST-1",
                    "position_fp": "10",
                    "market_exposure_dollars": "5.00",
                }
            ]
        }

        class FakeClient:
            async def get_positions(self):
                return raw

        result = asyncio.run(exit_monitor._fetch_open_positions(FakeClient()))
        assert "KXTEST-1" in result
        pos = result["KXTEST-1"]
        assert pos["side"] == "yes"
        assert pos["quantity"] == 10.0
        assert pos["market_exposure_dollars"] == 5.0

    def test_extracts_no_position(self):
        raw = {
            "market_positions": [
                {
                    "ticker": "KXTEST-2",
                    "position_fp": "-5",
                    "market_exposure_dollars": "2.50",
                }
            ]
        }

        class FakeClient:
            async def get_positions(self):
                return raw

        result = asyncio.run(exit_monitor._fetch_open_positions(FakeClient()))
        assert result["KXTEST-2"]["side"] == "no"
        assert result["KXTEST-2"]["quantity"] == 5.0

    def test_skips_zero_position(self):
        raw = {
            "market_positions": [
                {"ticker": "KXTEST-3", "position_fp": "0", "market_exposure_dollars": "0"}
            ]
        }

        class FakeClient:
            async def get_positions(self):
                return raw

        result = asyncio.run(exit_monitor._fetch_open_positions(FakeClient()))
        assert "KXTEST-3" not in result


class TestFetchRestingSellTickers:
    def test_returns_sell_tickers(self):
        raw = {
            "orders": [
                {"ticker": "KXTEST-1", "action": "sell"},
                {"ticker": "KXTEST-2", "action": "buy"},
            ]
        }

        class FakeClient:
            async def get_orders(self, status="resting"):
                return raw

        result = asyncio.run(exit_monitor._fetch_resting_sell_tickers(FakeClient()))
        assert "KXTEST-1" in result
        assert "KXTEST-2" not in result

    def test_returns_empty_set_when_no_orders(self):
        class FakeClient:
            async def get_orders(self, status="resting"):
                return {"orders": []}

        result = asyncio.run(exit_monitor._fetch_resting_sell_tickers(FakeClient()))
        assert result == set()
```

- [ ] **Step 2: Run tests to confirm they fail**

```
python -m pytest tests/test_exit_monitor.py::TestFetchOpenPositions tests/test_exit_monitor.py::TestFetchRestingSellTickers -v
```

Expected: `AttributeError` — functions not defined yet.

- [ ] **Step 3: Add the two async helpers to exit_monitor.py** (after `_select_yes_price`)

```python
async def _fetch_open_positions(client: KalshiClient) -> dict[str, dict]:
    """Fetch open positions from Kalshi REST API.

    Returns {ticker: {ticker, side, quantity, market_exposure_dollars}}.
    Only includes positions with nonzero quantity.
    """
    response = await client.get_positions()
    raw_positions = response.get("market_positions") or []
    positions: dict[str, dict] = {}
    for raw in raw_positions:
        qty = float(raw.get("position_fp", "0") or 0)
        if qty == 0:
            continue
        ticker = raw.get("ticker", "")
        if not ticker:
            continue
        positions[ticker] = {
            "ticker": ticker,
            "side": "yes" if qty > 0 else "no",
            "quantity": abs(qty),
            "market_exposure_dollars": float(raw.get("market_exposure_dollars", "0") or 0),
        }
    return positions


async def _fetch_resting_sell_tickers(client: KalshiClient) -> set[str]:
    """Return tickers that already have a resting sell order.

    Used on startup and refresh to pre-populate _pending_exits and prevent
    duplicate orders.
    """
    response = await client.get_orders(status="resting")
    orders = response.get("orders") or []
    return {
        o.get("ticker", "")
        for o in orders
        if o.get("action") == "sell" and o.get("ticker")
    }
```

- [ ] **Step 4: Run all tests**

```
python -m pytest tests/test_exit_monitor.py -v
```

Expected: all 18 tests pass.

- [ ] **Step 5: Commit**

```bash
git add scripts/exit_monitor.py tests/test_exit_monitor.py
git commit -m "feat(exit-monitor): add position/order fetching helpers with tests"
```

---

## Task 4: Server notification helper and main run loop

**Files:**
- Modify: `scripts/exit_monitor.py` (add `_notify_server` and `run()`)

- [ ] **Step 1: Add `_notify_server` after the fetching helpers**

```python
async def _notify_server(message: str, level: str = "info") -> None:
    """Fire-and-forget POST to the dashboard's /api/log endpoint.

    Silently swallowed if the server is not running.
    """
    try:
        async with aiohttp.ClientSession() as session:
            await session.post(
                "http://localhost:8000/api/log",
                json={"message": message, "level": level},
                timeout=aiohttp.ClientTimeout(total=2.0),
            )
    except Exception:
        pass
```

- [ ] **Step 2: Add the `run()` coroutine after `_notify_server`**

```python
async def run(dry_run: bool) -> None:
    """Main exit monitor loop. Runs until interrupted."""
    log.info("Exit monitor starting (dry_run=%s)", dry_run)

    async with KalshiClient() as kalshi_client:
        orderbook_state = OrderBookState()
        position_metadata: dict[str, dict] = {}
        pending_exits: set[str] = set()
        ws_client: KalshiWebSocketClient | None = None
        ws_task: asyncio.Task | None = None
        subscribed_tickers: set[str] = set()

        async def _refresh() -> None:
            nonlocal ws_client, ws_task, subscribed_tickers

            new_positions = await _fetch_open_positions(kalshi_client)
            position_metadata.clear()
            position_metadata.update(new_positions)

            # Remove pending exits for positions that are gone (exit filled)
            for ticker in list(pending_exits):
                if ticker not in position_metadata:
                    pending_exits.discard(ticker)

            # Pre-populate pending_exits from any resting sell orders
            resting_sell_tickers = await _fetch_resting_sell_tickers(kalshi_client)
            pending_exits.update(resting_sell_tickers & set(position_metadata))

            # Update WebSocket subscriptions if positions changed
            new_tickers = set(position_metadata)
            if new_tickers != subscribed_tickers:
                if ws_client is not None:
                    await ws_client.stop()
                if ws_task is not None and not ws_task.done():
                    ws_task.cancel()
                    try:
                        await ws_task
                    except asyncio.CancelledError:
                        pass

                subscribed_tickers = new_tickers
                if subscribed_tickers:
                    ws_client = KalshiWebSocketClient(list(subscribed_tickers), orderbook_state)
                    ws_task = asyncio.create_task(ws_client.run())
                    log.info("WebSocket subscribed to %d tickers: %s",
                             len(subscribed_tickers), sorted(subscribed_tickers))
                else:
                    ws_client = None
                    ws_task = None
                    log.info("No open positions — WebSocket not active")

        await _refresh()
        last_refresh_time = asyncio.get_event_loop().time()

        while True:
            now = asyncio.get_event_loop().time()
            if now - last_refresh_time >= 30.0:
                try:
                    await _refresh()
                except Exception as refresh_exc:
                    log.warning("Position refresh failed: %s", refresh_exc)
                last_refresh_time = now

            for ticker, meta in list(position_metadata.items()):
                if ticker in pending_exits:
                    continue

                position_dict = _build_position_dict(meta, orderbook_state, ticker)
                if position_dict is None:
                    continue  # WebSocket not yet priced this ticker

                for check in EXIT_CHECKS:
                    signal = check(position_dict)
                    if signal is None:
                        continue

                    yes_price = _select_yes_price(
                        signal.reason, meta["side"], orderbook_state, ticker
                    )
                    if yes_price is None:
                        break

                    pending_exits.add(ticker)
                    msg = (
                        f"{signal.reason} fired: {ticker} ({meta['side'].upper()}) "
                        f"at {yes_price}¢ — {signal.description}"
                    )
                    log.warning(msg)

                    if dry_run:
                        log.info("DRY-RUN: would sell %s %s %dx at yes_price=%d¢",
                                 meta["side"].upper(), ticker, int(meta["quantity"]), yes_price)
                    else:
                        try:
                            order_response = await kalshi_client.create_order(
                                ticker=ticker,
                                action="sell",
                                side=meta["side"],
                                count=int(meta["quantity"]),
                                order_type="limit",
                                yes_price=yes_price,
                            )
                            order_id = order_response.get("order", {}).get("order_id")
                            log.info("Exit order placed: order_id=%s", order_id)

                            if order_id:
                                realized_pnl = (
                                    yes_price / 100.0 * meta["quantity"]
                                    - meta["market_exposure_dollars"]
                                )
                                try:
                                    await _db.close_position(
                                        ticker=ticker,
                                        closing_trade_id=order_id,
                                        realized_pnl_dollars=realized_pnl,
                                        exit_reason=signal.reason,
                                    )
                                except Exception as db_exc:
                                    log.debug("Supabase close_position skipped: %s", db_exc)
                        except Exception as order_exc:
                            log.error("Exit order failed for %s: %s", ticker, order_exc)
                            pending_exits.discard(ticker)

                    await _notify_server(msg, "warning")
                    break  # only first signal per ticker per loop iteration

            await asyncio.sleep(0.5)
```

- [ ] **Step 3: Confirm existing tests still pass**

```
python -m pytest tests/test_exit_monitor.py -v
```

Expected: all 18 tests pass (run() is not covered by unit tests, that's fine).

- [ ] **Step 4: Commit**

```bash
git add scripts/exit_monitor.py
git commit -m "feat(exit-monitor): add server notification and main run loop"
```

---

## Task 5: CLI entry point and dry-run smoke test

**Files:**
- Modify: `scripts/exit_monitor.py` (add `__main__` block)

- [ ] **Step 1: Append the CLI entry point**

```python
if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Real-time deterministic exit monitor for open Kalshi positions."
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Log exit signals but do not place orders or write to Supabase",
    )
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s  %(levelname)-7s  %(name)s — %(message)s",
        datefmt="%H:%M:%S",
    )

    asyncio.run(run(dry_run=args.dry_run))
```

- [ ] **Step 2: Verify `--help` works**

```
python scripts/exit_monitor.py --help
```

Expected output includes `--dry-run`.

- [ ] **Step 3: Run in dry-run mode and verify startup**

```
python scripts/exit_monitor.py --dry-run
```

Expected log lines within the first 10 seconds:
```
HH:MM:SS  INFO     exit_monitor — Exit monitor starting (dry_run=True)
HH:MM:SS  INFO     exit_monitor — No open positions — WebSocket not active
```
(or `WebSocket subscribed to N tickers` if positions exist)

Press Ctrl-C to stop.

- [ ] **Step 4: Final commit**

```bash
git add scripts/exit_monitor.py
git commit -m "feat(exit-monitor): add CLI entry point — standalone exit daemon complete"
```

---

## Spec Coverage Checklist

| Spec requirement | Task |
|-----------------|------|
| Standalone script, no server dependency | Task 5 |
| Fetch positions from REST API every 30s | Task 4 (_refresh) |
| WebSocket subscription per open ticker | Task 4 (_refresh) |
| 0.5s exit check loop | Task 4 (run loop) |
| `_build_position_dict` with side-relative price | Task 2 |
| `_select_yes_price` — bid/ask by exit type and side | Task 2 |
| `_pending_exits` double-exit guard | Task 4 |
| Pre-populate from resting sell orders | Task 3 + Task 4 |
| Restart WebSocket on ticker change | Task 4 (_refresh) |
| Limit sell via `create_order` | Task 4 |
| `db.close_position()` after fill | Task 4 |
| `/api/log` server notification | Task 4 |
| `--dry-run` flag | Task 5 |
