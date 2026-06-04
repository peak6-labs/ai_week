# Place Order Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build `scripts/place_order.py` and a companion Claude skill that lets the user place, cancel, or cancel-and-replace Kalshi orders from a single natural language command in under 10 seconds.

**Architecture:** A standalone script containing pure pricing functions, a Haiku 4.5 intent parser, and async operation functions (place, cancel, cancel_and_replace). CLI wires them together with argparse; NL-parsed values fill any fields not provided as flags. A `.claude/skills/place-order/SKILL.md` tells Claude to fire the script immediately without any pre-lookup work.

**Tech Stack:** Python 3.12+, `anthropic` SDK (AsyncAnthropic, already in project), `kalshi_trader.client.KalshiClient`, `kalshi_trader.dashboard.portfolio_mapping.parse_fixed_point`, `pytest`, `pytest-asyncio`

---

## File Map

| File | Action | Responsibility |
|---|---|---|
| `scripts/place_order.py` | Create | All pricing logic, NL parsing, operations, CLI |
| `tests/test_place_order.py` | Create | Unit + integration-dry-run tests |
| `.claude/skills/place-order/SKILL.md` | Create | Claude skill — fires script on order intent |

---

### Task 1: Pricing logic — `compute_limit_price`

**Files:**
- Create: `scripts/place_order.py` (stub + function only)
- Create: `tests/test_place_order.py`

- [ ] **Step 1: Create `tests/test_place_order.py` with failing pricing tests**

```python
"""Tests for scripts/place_order.py"""
from __future__ import annotations
import importlib
import math
import sys
from pathlib import Path
import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))
import kalshi_trader.config  # noqa: F401

place_order = importlib.import_module("scripts.place_order")


def _ob(yes_bid: int, no_bid: int) -> dict:
    """Build a minimal normalized orderbook dict."""
    return {"orderbook": {"yes": [[yes_bid, 100]], "no": [[no_bid, 100]]}}


# --- midmarket_maker ---

def test_midmarket_maker_sell_rounds_up():
    # best_bid=62, best_ask=65 (100-35), midpoint=63.5, ceil=64
    assert place_order.compute_limit_price(_ob(62, 35), "sell", "midmarket_maker") == 64


def test_midmarket_maker_buy_rounds_down():
    # midpoint=63.5, floor=63
    assert place_order.compute_limit_price(_ob(62, 35), "buy", "midmarket_maker") == 63


def test_midmarket_maker_sell_even_spread_stays_maker():
    # best_bid=62, best_ask=64 (100-36), midpoint=63.0, ceil=63, max(63, 63)=63 > bid 62
    assert place_order.compute_limit_price(_ob(62, 36), "sell", "midmarket_maker") == 63


def test_midmarket_maker_sell_spread_1_falls_back_to_join_ask():
    # best_bid=62, best_ask=63 (100-37), spread=1 → join_ask=63
    assert place_order.compute_limit_price(_ob(62, 37), "sell", "midmarket_maker") == 63


def test_midmarket_maker_buy_spread_1_falls_back_to_join_bid():
    # best_bid=62, best_ask=63, spread=1 → join_bid=62
    assert place_order.compute_limit_price(_ob(62, 37), "buy", "midmarket_maker") == 62


# --- join_ask / join_bid ---

def test_join_ask_returns_best_ask():
    # best_ask = 100 - 35 = 65
    assert place_order.compute_limit_price(_ob(62, 35), "sell", "join_ask") == 65


def test_join_bid_returns_best_bid():
    assert place_order.compute_limit_price(_ob(62, 35), "buy", "join_bid") == 62


# --- cross_spread ---

def test_cross_spread_sell_uses_best_bid():
    # sell at bid = immediate taker fill
    assert place_order.compute_limit_price(_ob(62, 35), "sell", "cross_spread") == 62


def test_cross_spread_buy_uses_best_ask():
    # buy at ask = immediate taker fill
    assert place_order.compute_limit_price(_ob(62, 35), "buy", "cross_spread") == 65


# --- error cases ---

def test_empty_yes_book_raises():
    ob = {"orderbook": {"yes": [], "no": [[35, 100]]}}
    with pytest.raises(ValueError, match="best_bid"):
        place_order.compute_limit_price(ob, "sell", "midmarket_maker")


def test_empty_no_book_raises():
    ob = {"orderbook": {"yes": [[62, 100]], "no": []}}
    with pytest.raises(ValueError, match="best_ask"):
        place_order.compute_limit_price(ob, "sell", "midmarket_maker")
```

- [ ] **Step 2: Run tests — verify they all fail with ImportError (module doesn't exist yet)**

```bash
source .venv/bin/activate && pytest tests/test_place_order.py -v 2>&1 | head -20
```

Expected: `ModuleNotFoundError: No module named 'scripts.place_order'`

- [ ] **Step 3: Create `scripts/place_order.py` with the pricing function**

```python
#!/usr/bin/env python3
"""Place, cancel, or cancel-and-replace Kalshi orders via natural language or structured flags.

Usage:
  python scripts/place_order.py --ticker KXATL-26JUN-A1 "exit full position at midmarket no fees"
  python scripts/place_order.py --ticker KXATL-26JUN-A1 "cancel and replace at 65 cents"
  python scripts/place_order.py --ticker KXATL-26JUN-A1 "i need to get filled"
  python scripts/place_order.py --ticker KXATL-26JUN-A1 --action sell --quantity all --pricing midmarket_maker
  python scripts/place_order.py --ticker KXATL-26JUN-A1 "cancel and replace at 65" --dry-run
"""
from __future__ import annotations

import argparse
import asyncio
import json
import math
import sys

sys.path.insert(0, ".")

import kalshi_trader.config  # noqa: F401 — loads .env


def compute_limit_price(orderbook_data: dict, action: str, pricing: str) -> int:
    """Compute a yes_price (1-99 cents) for a limit order given a pricing strategy.

    orderbook_data: normalized dict from KalshiClient.get_orderbook() — must contain
        {"orderbook": {"yes": [[price_cents, qty], ...], "no": [[price_cents, qty], ...]}}
    action: "buy" or "sell"
    pricing: "midmarket_maker" | "join_bid" | "join_ask" | "cross_spread"

    Maker strategies (midmarket_maker, join_bid, join_ask) never cross the spread.
    cross_spread crosses immediately — taker fees apply.
    Raises ValueError if either side of the book is empty.
    """
    ob = orderbook_data.get("orderbook", {})
    yes_prices = [lvl[0] for lvl in ob.get("yes", []) if lvl]
    no_prices = [lvl[0] for lvl in ob.get("no", []) if lvl]

    if not yes_prices:
        raise ValueError("best_bid unavailable — YES book is empty. Use --yes-price to set price explicitly.")
    if not no_prices:
        raise ValueError("best_ask unavailable — NO book is empty. Use --yes-price to set price explicitly.")

    best_bid = max(yes_prices)
    best_ask = 100 - max(no_prices)

    if pricing == "join_bid":
        return best_bid
    if pricing == "join_ask":
        return best_ask
    if pricing == "cross_spread":
        return best_bid if action == "sell" else best_ask
    # midmarket_maker (default)
    spread = best_ask - best_bid
    if spread <= 1:
        return best_ask if action == "sell" else best_bid
    midpoint = (best_bid + best_ask) / 2.0
    if action == "sell":
        return max(math.ceil(midpoint), best_bid + 1)
    return min(math.floor(midpoint), best_ask - 1)
```

- [ ] **Step 4: Run pricing tests — verify they all pass**

```bash
source .venv/bin/activate && pytest tests/test_place_order.py -v -k "midmarket or join or cross or empty"
```

Expected: 11 tests pass.

- [ ] **Step 5: Commit**

```bash
git add scripts/place_order.py tests/test_place_order.py
git commit -m "feat(place-order): add compute_limit_price with full pricing strategy tests"
```

---

### Task 2: NL intent parser — `parse_intent`

**Files:**
- Modify: `scripts/place_order.py` — add `INTENT_SYSTEM_PROMPT` and `parse_intent()`
- Modify: `tests/test_place_order.py` — add NL parsing tests

- [ ] **Step 1: Add NL parser tests to `tests/test_place_order.py`**

Append these tests to the existing file:

```python
import anthropic
from unittest.mock import AsyncMock, MagicMock


def _mock_anthropic(response_json: dict):
    """Return a mock AsyncAnthropic client that returns response_json as a text block."""
    mock_message = MagicMock()
    mock_message.content = [MagicMock(text=json.dumps(response_json))]
    mock_client = MagicMock()
    mock_client.messages.create = AsyncMock(return_value=mock_message)
    return mock_client


@pytest.mark.asyncio
async def test_parse_intent_exit_midmarket():
    haiku_response = {
        "action": "sell", "side": None, "quantity": "all", "amount_dollars": None,
        "pricing": "midmarket_maker", "yes_price": None,
        "cancel_first": False, "cancel_only": False,
    }
    with patch_haiku(haiku_response):
        result = await place_order.parse_intent("exit full position at midmarket no fees")
    assert result["action"] == "sell"
    assert result["quantity"] == "all"
    assert result["pricing"] == "midmarket_maker"
    assert result["cancel_only"] is False


@pytest.mark.asyncio
async def test_parse_intent_cancel_and_replace():
    haiku_response = {
        "action": "sell", "side": None, "quantity": None, "amount_dollars": None,
        "pricing": None, "yes_price": 65,
        "cancel_first": True, "cancel_only": False,
    }
    with patch_haiku(haiku_response):
        result = await place_order.parse_intent("cancel and replace at 65 cents")
    assert result["cancel_first"] is True
    assert result["yes_price"] == 65


@pytest.mark.asyncio
async def test_parse_intent_cross_spread():
    haiku_response = {
        "action": "sell", "side": None, "quantity": "all", "amount_dollars": None,
        "pricing": "cross_spread", "yes_price": None,
        "cancel_first": False, "cancel_only": False,
    }
    with patch_haiku(haiku_response):
        result = await place_order.parse_intent("i need to get filled")
    assert result["pricing"] == "cross_spread"


@pytest.mark.asyncio
async def test_parse_intent_cancel_only():
    haiku_response = {
        "action": None, "side": None, "quantity": None, "amount_dollars": None,
        "pricing": None, "yes_price": None,
        "cancel_first": False, "cancel_only": True,
    }
    with patch_haiku(haiku_response):
        result = await place_order.parse_intent("cancel all resting orders")
    assert result["cancel_only"] is True


@pytest.mark.asyncio
async def test_parse_intent_buy_with_amount():
    haiku_response = {
        "action": "buy", "side": "yes", "quantity": None, "amount_dollars": 10.0,
        "pricing": "midmarket_maker", "yes_price": None,
        "cancel_first": False, "cancel_only": False,
    }
    with patch_haiku(haiku_response):
        result = await place_order.parse_intent("buy 10 dollars yes at midmarket")
    assert result["action"] == "buy"
    assert result["amount_dollars"] == 10.0
    assert result["side"] == "yes"
```

Add this helper at the top of the test file (after the existing imports):

```python
from contextlib import contextmanager
from unittest.mock import patch


@contextmanager
def patch_haiku(response_json: dict):
    mock_client = _mock_anthropic(response_json)
    with patch("scripts.place_order.anthropic.AsyncAnthropic", return_value=mock_client):
        yield mock_client
```

- [ ] **Step 2: Run new tests — verify they fail**

```bash
source .venv/bin/activate && pytest tests/test_place_order.py -v -k "parse_intent"
```

Expected: `AttributeError: module 'scripts.place_order' has no attribute 'parse_intent'`

- [ ] **Step 3: Add `parse_intent` to `scripts/place_order.py`**

Add after the existing imports at the top of the file:

```python
import anthropic
```

Add the system prompt and function after `compute_limit_price`:

```python
_INTENT_SYSTEM_PROMPT = """\
You parse Kalshi trading order instructions into JSON.
Return ONLY valid JSON matching the schema below. Use null for any field not mentioned.

DISAMBIGUATION RULES (highest priority):
- "no fees" / "without fees" ALWAYS → pricing: "midmarket_maker" (never "cross_spread")
- "need to get filled" / "just get me out" / "urgently" / "asap" / "cross the spread" → pricing: "cross_spread"
- "get out of" / "exit" / "close" / "sell" / "liquidate" → action: "sell"
- "buy" / "enter" / "open" / "get into" → action: "buy"
- "all" / "full position" / "everything" / "the trade" / "the position" → quantity: "all"
- "best price" on a sell → pricing: "join_ask"; "best price" on a buy → pricing: "join_bid"
- "midmarket" / "mid" / "no fees" / "without fees" / "get filled without fees" → pricing: "midmarket_maker"
- "join ask" → pricing: "join_ask"; "join bid" → pricing: "join_bid"
- "cancel and replace" / "reprice" / "move my order" → cancel_first: true
- "cancel" alone (without "replace") → cancel_only: true
- "at N cents" / "at N" / "@ N" → yes_price: N (integer 1-99)
- "N dollars" / "$N" → amount_dollars: N (float)

Schema:
{
  "action": "buy" | "sell" | null,
  "side": "yes" | "no" | null,
  "quantity": <integer> | "all" | null,
  "amount_dollars": <float> | null,
  "pricing": "midmarket_maker" | "join_bid" | "join_ask" | "cross_spread" | null,
  "yes_price": <integer 1-99> | null,
  "cancel_first": false,
  "cancel_only": false
}\
"""


async def parse_intent(intent: str) -> dict:
    """Parse a natural language order instruction into a structured dict via Haiku 4.5."""
    client = anthropic.AsyncAnthropic()
    message = await client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=256,
        system=_INTENT_SYSTEM_PROMPT,
        messages=[{"role": "user", "content": intent}],
    )
    text = next((b.text for b in message.content if hasattr(b, "text")), "{}")
    parsed = json.loads(text)
    return {
        "action": parsed.get("action"),
        "side": parsed.get("side"),
        "quantity": parsed.get("quantity"),
        "amount_dollars": parsed.get("amount_dollars"),
        "pricing": parsed.get("pricing"),
        "yes_price": parsed.get("yes_price"),
        "cancel_first": bool(parsed.get("cancel_first", False)),
        "cancel_only": bool(parsed.get("cancel_only", False)),
    }
```

- [ ] **Step 4: Run NL parser tests — verify they all pass**

```bash
source .venv/bin/activate && pytest tests/test_place_order.py -v -k "parse_intent"
```

Expected: 5 tests pass.

- [ ] **Step 5: Commit**

```bash
git add scripts/place_order.py tests/test_place_order.py
git commit -m "feat(place-order): add Haiku intent parser with NL vocabulary tests"
```

---

### Task 3: Quantity and position resolution — `resolve_quantity`

**Files:**
- Modify: `scripts/place_order.py` — add `resolve_quantity()`
- Modify: `tests/test_place_order.py` — add quantity resolution tests

- [ ] **Step 1: Add quantity resolution tests to `tests/test_place_order.py`**

```python
@pytest.mark.asyncio
async def test_resolve_quantity_explicit_int():
    assert await place_order.resolve_quantity("KXFOO", 10, "sell", None) == ("sell", 10)


@pytest.mark.asyncio
async def test_resolve_quantity_all_reads_position():
    from unittest.mock import AsyncMock, MagicMock
    mock_client = MagicMock()
    mock_client.get_positions = AsyncMock(return_value={
        "market_positions": [
            {"ticker": "KXFOO", "position_fp": "20.00"},
        ]
    })
    side, count = await place_order.resolve_quantity("KXFOO", "all", "sell", mock_client)
    assert side == "yes"
    assert count == 20


@pytest.mark.asyncio
async def test_resolve_quantity_all_no_position_raises():
    from unittest.mock import AsyncMock, MagicMock
    mock_client = MagicMock()
    mock_client.get_positions = AsyncMock(return_value={"market_positions": []})
    with pytest.raises(SystemExit):
        await place_order.resolve_quantity("KXFOO", "all", "sell", mock_client)


@pytest.mark.asyncio
async def test_resolve_quantity_amount_dollars():
    # $10 at 50 cents/contract = floor(10 / 0.50) = 20 contracts
    side, count = await place_order.resolve_quantity("KXFOO", None, "buy", None,
                                                      amount_dollars=10.0, yes_price_cents=50)
    assert side == "buy"
    assert count == 20


@pytest.mark.asyncio
async def test_resolve_quantity_missing_raises():
    with pytest.raises(SystemExit):
        await place_order.resolve_quantity("KXFOO", None, "buy", None)
```

- [ ] **Step 2: Run tests — verify they fail**

```bash
source .venv/bin/activate && pytest tests/test_place_order.py -v -k "resolve_quantity"
```

Expected: `AttributeError: module 'scripts.place_order' has no attribute 'resolve_quantity'`

- [ ] **Step 3: Add `resolve_quantity` to `scripts/place_order.py`**

Add `from kalshi_trader.dashboard.portfolio_mapping import parse_fixed_point` to the imports block at the top of the file (alongside the other `from kalshi_trader.*` imports). Then add the function body after `parse_intent`:

```python
async def resolve_quantity(
    ticker: str,
    quantity_spec,   # int | "all" | None
    action: str,
    client,          # KalshiClient | None
    *,
    amount_dollars: float | None = None,
    yes_price_cents: int | None = None,
) -> tuple[str, int]:
    """Return (side, contract_count) ready to pass to create_order.

    quantity_spec:
      int        → use directly; side comes from action
      "all"      → fetch open position for ticker; side auto-detected from position_fp sign
      None       → compute from amount_dollars / yes_price_cents (buys only)
    """
    if quantity_spec == "all":
        positions_response = await client.get_positions()
        market_positions = positions_response.get("market_positions", [])
        held = next((p for p in market_positions if p.get("ticker") == ticker), None)
        if held is None:
            print(f"ERROR: No open position for {ticker}", file=sys.stderr)
            sys.exit(1)
        signed = parse_fixed_point(held.get("position_fp"))
        side = "yes" if signed >= 0 else "no"
        return side, int(abs(signed))

    if isinstance(quantity_spec, int) and quantity_spec > 0:
        return action, quantity_spec

    if amount_dollars is not None and yes_price_cents is not None and yes_price_cents > 0:
        count = math.floor(amount_dollars / (yes_price_cents / 100.0))
        if count < 1:
            print(
                f"ERROR: ${amount_dollars} at {yes_price_cents}¢/contract yields 0 contracts",
                file=sys.stderr,
            )
            sys.exit(1)
        return action, count

    print("ERROR: quantity or amount required — provide --quantity, --quantity all, or --amount",
          file=sys.stderr)
    sys.exit(1)
```

- [ ] **Step 4: Run quantity tests — verify they all pass**

```bash
source .venv/bin/activate && pytest tests/test_place_order.py -v -k "resolve_quantity"
```

Expected: 5 tests pass.

- [ ] **Step 5: Commit**

```bash
git add scripts/place_order.py tests/test_place_order.py
git commit -m "feat(place-order): add resolve_quantity — handles explicit count, 'all', and dollar amount"
```

---

### Task 4: Operations — `cancel_orders`, `place_order_op`, and `cancel_and_replace`

**Files:**
- Modify: `scripts/place_order.py` — add the three operation functions
- Modify: `tests/test_place_order.py` — add operation tests

- [ ] **Step 1: Add operation tests to `tests/test_place_order.py`**

```python
@pytest.mark.asyncio
async def test_cancel_orders_cancels_all_for_ticker():
    from unittest.mock import AsyncMock, MagicMock, call
    mock_client = MagicMock()
    mock_client.get_orders = AsyncMock(return_value={"orders": [
        {"order_id": "ord1", "ticker": "KXFOO"},
        {"order_id": "ord2", "ticker": "KXFOO"},
        {"order_id": "ord3", "ticker": "KXBAR"},  # different ticker, should be skipped
    ]})
    mock_client.cancel_order = AsyncMock(return_value={})
    count = await place_order.cancel_orders("KXFOO", mock_client, dry_run=False)
    assert count == 2
    mock_client.cancel_order.assert_has_calls([call("ord1"), call("ord2")], any_order=True)


@pytest.mark.asyncio
async def test_cancel_orders_dry_run_returns_count_without_cancelling():
    from unittest.mock import AsyncMock, MagicMock
    mock_client = MagicMock()
    mock_client.get_orders = AsyncMock(return_value={"orders": [
        {"order_id": "ord1", "ticker": "KXFOO"},
    ]})
    mock_client.cancel_order = AsyncMock()
    count = await place_order.cancel_orders("KXFOO", mock_client, dry_run=True)
    assert count == 1
    mock_client.cancel_order.assert_not_called()


@pytest.mark.asyncio
async def test_place_order_op_places_limit_order():
    from unittest.mock import AsyncMock, MagicMock
    mock_client = MagicMock()
    mock_client.create_order = AsyncMock(return_value={
        "order": {"order_id": "ord_xyz", "status": "resting", "yes_price_dollars": "0.6400"}
    })
    result = await place_order.place_order_op(
        ticker="KXFOO", action="sell", side="yes", count=20,
        yes_price=64, client=mock_client, dry_run=False,
    )
    assert result["order_id"] == "ord_xyz"
    mock_client.create_order.assert_called_once_with(
        ticker="KXFOO", action="sell", side="yes",
        count=20, order_type="limit", yes_price=64,
    )


@pytest.mark.asyncio
async def test_place_order_op_dry_run_skips_api():
    from unittest.mock import AsyncMock, MagicMock
    mock_client = MagicMock()
    mock_client.create_order = AsyncMock()
    result = await place_order.place_order_op(
        ticker="KXFOO", action="sell", side="yes", count=20,
        yes_price=64, client=mock_client, dry_run=True,
    )
    assert result["dry_run"] is True
    mock_client.create_order.assert_not_called()
```

- [ ] **Step 2: Run tests — verify they fail**

```bash
source .venv/bin/activate && pytest tests/test_place_order.py -v -k "cancel_orders or place_order_op"
```

Expected: `AttributeError: module has no attribute 'cancel_orders'`

- [ ] **Step 3: Add operation functions to `scripts/place_order.py`**

Add `from kalshi_trader.client import KalshiClient` to the imports block at the top of the file. Then add the three functions after `resolve_quantity`:

```python


async def cancel_orders(ticker: str, client, dry_run: bool = False) -> int:
    """Cancel all resting orders for ticker. Returns count of (would-be-)cancelled orders."""
    orders_response = await client.get_orders(status="resting")
    resting = [o for o in orders_response.get("orders", []) if o.get("ticker") == ticker]
    if dry_run:
        print(f"[DRY-RUN] Would cancel {len(resting)} resting order(s) for {ticker}")
        return len(resting)
    for order in resting:
        await client.cancel_order(order["order_id"])
    print(f"Cancelled {len(resting)} order(s) for {ticker}")
    return len(resting)


async def place_order_op(
    ticker: str,
    action: str,
    side: str,
    count: int,
    yes_price: int,
    client,
    dry_run: bool = False,
) -> dict:
    """Place a single limit order. Returns result dict."""
    if dry_run:
        print(
            f"[DRY-RUN] Would {action.upper()} {side.upper()} {count} contracts of "
            f"{ticker} at yes_price={yes_price}¢"
        )
        return {"ticker": ticker, "action": action, "side": side,
                "count": count, "yes_price": yes_price, "dry_run": True}

    if action == "sell" and yes_price in (None,) or (
        "pricing" in locals() and locals().get("pricing") == "cross_spread"
    ):
        pass  # fee warning printed by caller

    order_response = await client.create_order(
        ticker=ticker, action=action, side=side,
        count=count, order_type="limit", yes_price=yes_price,
    )
    order_data = order_response.get("order", {})
    order_id = order_data.get("order_id", "")
    status = order_data.get("status", "unknown")
    print(
        f"{'EXECUTED' if not dry_run else '[DRY-RUN]'} "
        f"{ticker} {action.upper()} {side.upper()} "
        f"qty={count} yes_price={yes_price}¢ "
        f"order_id={order_id} status={status}"
    )
    return {"ticker": ticker, "action": action, "side": side,
            "count": count, "yes_price": yes_price,
            "order_id": order_id, "status": status, "dry_run": False}


async def cancel_and_replace(
    ticker: str,
    action: str,
    side: str,
    count: int,
    yes_price: int,
    client,
    dry_run: bool = False,
) -> dict:
    """Cancel all resting orders for ticker, then place a new limit order."""
    await cancel_orders(ticker, client, dry_run=dry_run)
    if not dry_run:
        await asyncio.sleep(0.5)
    return await place_order_op(ticker, action, side, count, yes_price, client, dry_run=dry_run)
```

- [ ] **Step 4: Clean up the `place_order_op` function — remove the dead branch left by earlier draft**

Replace the body of `place_order_op` with the clean version (the `if action == "sell"` branch in the middle is a leftover — delete it):

```python
async def place_order_op(
    ticker: str,
    action: str,
    side: str,
    count: int,
    yes_price: int,
    client,
    dry_run: bool = False,
) -> dict:
    """Place a single limit order. Returns result dict."""
    if dry_run:
        print(
            f"[DRY-RUN] Would {action.upper()} {side.upper()} {count} contracts of "
            f"{ticker} at yes_price={yes_price}¢"
        )
        return {"ticker": ticker, "action": action, "side": side,
                "count": count, "yes_price": yes_price, "dry_run": True}

    order_response = await client.create_order(
        ticker=ticker, action=action, side=side,
        count=count, order_type="limit", yes_price=yes_price,
    )
    order_data = order_response.get("order", {})
    order_id = order_data.get("order_id", "")
    status = order_data.get("status", "unknown")
    print(
        f"EXECUTED {ticker} {action.upper()} {side.upper()} "
        f"qty={count} yes_price={yes_price}¢ "
        f"order_id={order_id} status={status}"
    )
    return {"ticker": ticker, "action": action, "side": side,
            "count": count, "yes_price": yes_price,
            "order_id": order_id, "status": status, "dry_run": False}
```

- [ ] **Step 5: Run operation tests — verify they all pass**

```bash
source .venv/bin/activate && pytest tests/test_place_order.py -v -k "cancel_orders or place_order_op"
```

Expected: 4 tests pass.

- [ ] **Step 6: Commit**

```bash
git add scripts/place_order.py tests/test_place_order.py
git commit -m "feat(place-order): add cancel_orders, place_order_op, cancel_and_replace operations"
```

---

### Task 5: CLI wiring — argparse, merge, dispatch, and dry-run integration test

**Files:**
- Modify: `scripts/place_order.py` — add `_merge_params`, `_main`
- Modify: `tests/test_place_order.py` — add dry-run integration test

- [ ] **Step 1: Add dry-run integration test**

```python
@pytest.mark.asyncio
async def test_main_dry_run_sell_all_midmarket(capsys):
    """End-to-end dry-run: NL intent + orderbook fetch + position fetch → correct price printed."""
    from unittest.mock import AsyncMock, MagicMock, patch as mock_patch

    mock_client = MagicMock()
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=None)
    mock_client.get_orderbook = AsyncMock(return_value={
        "orderbook": {"yes": [[62, 100]], "no": [[35, 100]]}
    })
    mock_client.get_positions = AsyncMock(return_value={
        "market_positions": [{"ticker": "KXFOO", "position_fp": "20.00"}]
    })

    haiku_response = {
        "action": "sell", "side": None, "quantity": "all", "amount_dollars": None,
        "pricing": "midmarket_maker", "yes_price": None,
        "cancel_first": False, "cancel_only": False,
    }

    with mock_patch("scripts.place_order.KalshiClient", return_value=mock_client), \
         patch_haiku(haiku_response):
        await place_order._run(
            ticker="KXFOO",
            intent="exit full position at midmarket no fees",
            flags={},
            dry_run=True,
        )

    captured = capsys.readouterr()
    assert "[DRY-RUN]" in captured.out
    assert "yes_price=64" in captured.out   # ceil((62+65)/2) = 64
    assert "qty=20" in captured.out
```

- [ ] **Step 2: Run test — verify it fails**

```bash
source .venv/bin/activate && pytest tests/test_place_order.py -v -k "test_main_dry_run"
```

Expected: `AttributeError: module has no attribute '_run'`

- [ ] **Step 3: Add `_merge_params`, `_run`, and `_main` to `scripts/place_order.py`**

```python
def _merge_params(parsed_intent: dict, flags: dict) -> dict:
    """Merge NL-parsed intent with explicit CLI flags. Flags take precedence."""
    merged = dict(parsed_intent)
    for key, value in flags.items():
        if value is not None:
            merged[key] = value
    return merged


async def _run(ticker: str, intent: str | None, flags: dict, dry_run: bool) -> None:
    """Core async logic: parse intent, resolve params, dispatch operation."""
    if intent:
        parsed = await parse_intent(intent)
    else:
        parsed = {
            "action": None, "side": None, "quantity": None, "amount_dollars": None,
            "pricing": None, "yes_price": None, "cancel_first": False, "cancel_only": False,
        }

    params = _merge_params(parsed, flags)

    cancel_only: bool = params.get("cancel_only", False)
    cancel_first: bool = params.get("cancel_first", False)
    action: str | None = params.get("action")
    side: str | None = params.get("side")
    quantity_spec = params.get("quantity")
    amount_dollars: float | None = params.get("amount_dollars")
    pricing: str | None = params.get("pricing") or "midmarket_maker"
    yes_price: int | None = params.get("yes_price")

    async with KalshiClient() as client:
        if cancel_only:
            await cancel_orders(ticker, client, dry_run=dry_run)
            return

        # Resolve limit price
        if yes_price is None:
            orderbook_data = await client.get_orderbook(ticker)
            yes_price = compute_limit_price(orderbook_data, action or "sell", pricing)
            if pricing == "cross_spread":
                print("WARNING: cross_spread incurs taker fees (~7% of profit)")

        # Resolve quantity and side
        resolved_side, count = await resolve_quantity(
            ticker, quantity_spec, action or "sell", client,
            amount_dollars=amount_dollars, yes_price_cents=yes_price,
        )
        final_side = side or resolved_side
        final_action = action or "sell"

        if cancel_first:
            await cancel_and_replace(ticker, final_action, final_side, count, yes_price,
                                     client, dry_run=dry_run)
        else:
            await place_order_op(ticker, final_action, final_side, count, yes_price,
                                 client, dry_run=dry_run)


def _main() -> None:
    parser = argparse.ArgumentParser(
        description="Place, cancel, or cancel-and-replace Kalshi orders"
    )
    parser.add_argument("--ticker", required=True, help="Market ticker, e.g. KXATL-26JUN-A1")
    parser.add_argument("intent", nargs="?", default=None,
                        help="Natural language order instruction (parsed by Haiku)")
    parser.add_argument("--action", choices=["buy", "sell"])
    parser.add_argument("--side", choices=["yes", "no"])
    parser.add_argument("--quantity", help="Integer contract count or 'all'")
    parser.add_argument("--amount", type=float, dest="amount_dollars",
                        help="Dollar amount for buys (e.g. 10)")
    parser.add_argument("--pricing",
                        choices=["midmarket_maker", "join_bid", "join_ask", "cross_spread"])
    parser.add_argument("--yes-price", type=int, dest="yes_price",
                        help="Explicit limit price in cents (1-99)")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    quantity_spec = None
    if args.quantity is not None:
        quantity_spec = "all" if args.quantity.lower() == "all" else int(args.quantity)

    flags = {
        "action": args.action,
        "side": args.side,
        "quantity": quantity_spec,
        "amount_dollars": args.amount_dollars,
        "pricing": args.pricing,
        "yes_price": args.yes_price,
    }

    asyncio.run(_run(
        ticker=args.ticker,
        intent=args.intent,
        flags=flags,
        dry_run=args.dry_run,
    ))


if __name__ == "__main__":
    _main()
```

- [ ] **Step 4: Run all tests — verify everything passes**

```bash
source .venv/bin/activate && pytest tests/test_place_order.py -v
```

Expected: all tests pass (no failures, no errors).

- [ ] **Step 5: Smoke-test the CLI help output to confirm argparse wiring**

```bash
source .venv/bin/activate && python scripts/place_order.py --help
```

Expected: usage message showing `--ticker`, `intent`, `--action`, `--side`, `--quantity`, `--amount`, `--pricing`, `--yes-price`, `--dry-run`.

- [ ] **Step 6: Commit**

```bash
git add scripts/place_order.py tests/test_place_order.py
git commit -m "feat(place-order): wire CLI, merge NL+flags, dispatch operations — script complete"
```

---

### Task 6: Claude skill — `place-order`

**Files:**
- Create: `.claude/skills/place-order/SKILL.md`

- [ ] **Step 1: Create the skill directory and file**

```bash
mkdir -p .claude/skills/place-order
```

Create `.claude/skills/place-order/SKILL.md`:

```markdown
---
description: Place, cancel, or cancel-and-replace Kalshi orders from natural language. Use when the user wants to enter a position, exit, cancel, or reprice an order.
---

# Place Order Skill

When the user mentions placing, buying, selling, exiting, closing, canceling, or repricing a Kalshi order — invoke this skill immediately. No clarifying questions. No orderbook lookups. No reasoning about maker/taker pricing. The script handles all of that.

## Trigger phrases

- "place an order", "put in an order"
- "buy", "sell", "exit", "close my position", "get out of"
- "cancel and replace", "reprice", "move my order", "cancel"
- "enter a position", "I need to get filled", "get me out"

## How to invoke

1. Extract the ticker from the user's message or recent conversation context. If ambiguous, ask ONE question: "Which ticker?" then run immediately.
2. Run:
   ```bash
   source .venv/bin/activate && python scripts/place_order.py --ticker <TICKER> "<user's exact words>"
   ```
3. Report the result in one sentence: ticker, order ID, price placed, status.

## Defaults the script applies automatically

- No pricing specified → `midmarket_maker` (maker order, zero fees)
- "exit" / "get out of" / "close" with no quantity → `--quantity all` (full position)
- Sells without explicit side → auto-detected from held position when `--quantity all`

## Pricing cheat sheet (for dry-run or override)

| User says | Script uses | Fees? |
|---|---|---|
| "midmarket" / "no fees" / "without fees" | `midmarket_maker` | None |
| "best price" (sell) | `join_ask` | None |
| "best price" (buy) | `join_bid` | None |
| "need to get filled" / "urgently" | `cross_spread` | ~7% of profit |
| "at 65 cents" | explicit `yes_price=65` | Depends on price |

## Speed target

Under 10 seconds from user message to live order placed.
```

- [ ] **Step 2: Verify the skill file is readable**

```bash
cat .claude/skills/place-order/SKILL.md
```

Expected: full skill content printed with no errors.

- [ ] **Step 3: Commit**

```bash
git add .claude/skills/place-order/SKILL.md
git commit -m "feat(place-order): add Claude skill for instant NL order execution"
```

---

### Task 7: Full test run and final verification

- [ ] **Step 1: Run the complete test suite to check for regressions**

```bash
source .venv/bin/activate && pytest tests/ -v --tb=short 2>&1 | tail -30
```

Expected: all existing tests pass; new `test_place_order.py` tests pass; zero failures.

- [ ] **Step 2: Dry-run smoke test with a real ticker format**

```bash
source .venv/bin/activate && python scripts/place_order.py \
  --ticker KXLAMAYORMATCHUP-26JUN-NRAMKBAS \
  "exit full position at midmarket no fees" \
  --dry-run
```

Expected output (exact ticker and qty will vary, but format must match):
```
[DRY-RUN] Would SELL YES 20 contracts of KXLAMAYORMATCHUP-26JUN-NRAMKBAS at yes_price=64¢
```

- [ ] **Step 3: Dry-run smoke test for cancel-and-replace**

```bash
source .venv/bin/activate && python scripts/place_order.py \
  --ticker KXLAMAYORMATCHUP-26JUN-NRAMKBAS \
  "cancel and replace at 66 cents" \
  --dry-run
```

Expected: dry-run cancel message followed by dry-run place message at `yes_price=66`.
