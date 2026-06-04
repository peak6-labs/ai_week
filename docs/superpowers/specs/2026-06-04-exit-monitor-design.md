# Exit Monitor — Design Spec
**Date:** 2026-06-04

## Context

The portfolio's deterministic exit checks (`check_stop_loss`, `check_profit_target` in `portfolio_checks.py`) currently only fire during the orchestrator's trading loop cycle. That means a stop-loss can only trigger when the loop happens to run — unacceptable for overnight or unattended trading. The WebSocket client (`kalshi_trader/external/kalshi_ws.py`) was built but never wired in. This spec integrates it as a standalone always-on exit daemon.

## Goal

A standalone process (`scripts/exit_monitor.py`) that watches open positions via the Kalshi WebSocket and fires deterministic exits the moment a price threshold is crossed — independent of the UI server or the orchestrator loop.

Scope is limited to deterministic exits only: `portfolio_checks.EXIT_CHECKS`. AI position review (Step 0.75) is explicitly excluded.

## Architecture

```
KalshiClient (REST)          → position list (refresh every 30s)
KalshiWebSocketClient        → OrderBookState (real-time bid/ask per ticker)
Exit check loop (0.5s)       → reads OrderBookState + position metadata
                             → runs EXIT_CHECKS
                             → places limit sell via KalshiClient on signal
```

No dependency on the UI server or `TradingState`. Runs as a standalone process that can be started once and left running overnight.

## Components Used

| Component | File | Role |
|-----------|------|------|
| `KalshiWebSocketClient` | `kalshi_trader/external/kalshi_ws.py` | Maintains real-time order book |
| `OrderBookState` | `kalshi_trader/orderbook.py` | Bid/ask state per ticker |
| `EXIT_CHECKS` | `kalshi_trader/portfolio_checks.py` | Stop-loss + profit-target logic |
| `KalshiClient` | `kalshi_trader/client.py` | REST: position fetch + order placement |

## Data Flow

### Startup
1. Fetch open positions from `GET /portfolio/positions`
2. Extract `ticker`, `side`, `quantity`, `market_exposure_dollars` from each
3. Create `KalshiWebSocketClient(tickers, orderbook_state)` and start it as a background task

### Main loop (every 0.5s)
For each open position not in `_pending_exits`:
1. Read `orderbook_state.best_bid(ticker)` and `orderbook_state.best_ask(ticker)` (YES-price cents)
2. Compute side-relative price:
   - YES position: `current_price_cents = best_bid()`, `midpoint_yes_price_cents = (bid+ask)/2`
   - NO position: `current_price_cents = 100 - best_ask()`, `midpoint_yes_price_cents = (bid+ask)/2`
3. Build position dict:
   ```python
   {
       "market_exposure_dollars": <from REST>,
       "quantity": <from REST>,
       "current_price_cents": <from WebSocket>,
       "midpoint_yes_price_cents": <from WebSocket>,
   }
   ```
4. Run `EXIT_CHECKS` — first non-None `ExitSignal` wins
5. On signal: go to **Exit execution**

### Exit execution
- Add ticker to `_pending_exits`
- Ignore `signal.exit_price_cents` (it's always midpoint from the check function); determine limit price fresh from `OrderBookState` by exit type:
  - `profit_target` → limit sell at current YES **ask** (passive maker, fee-efficient)
  - `stop_loss` → limit sell at current YES **bid** (aggressive, guarantee fill)
- Place limit sell order via `KalshiClient.create_order()`
- Log: ticker, reason, price, description

### Position refresh (every 30s)
- Re-fetch open positions from REST API
- Remove from `_pending_exits` any ticker no longer in the position list (exit filled)
- If active ticker set changed: `stop()` current WebSocket client, start fresh with updated tickers

## Double-Exit Prevention

`_pending_exits: set[str]` — a ticker is added when an exit is triggered and removed when the 30s refresh confirms the position is gone from the API. The exit check loop skips any ticker in this set.

## Invocation

```bash
python scripts/exit_monitor.py             # live execution
python scripts/exit_monitor.py --dry-run   # log signals, skip order placement
```

`--dry-run` is safe to run alongside night mode for observation before enabling live execution.

## Files Changed

- **New:** `scripts/exit_monitor.py`
- **No changes** to `portfolio_checks.py`, `kalshi_ws.py`, `orderbook.py`, or the server

## Out of Scope

- AI position review (Step 0.75)
- UI integration (the monitor logs to stdout only)
- Partial exits or position sizing adjustments
- Trailing stops
