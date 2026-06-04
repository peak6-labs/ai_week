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

import anthropic
import argparse
import asyncio
import json
import math
import sys

sys.path.insert(0, ".")

import kalshi_trader.config  # noqa: F401 — loads .env
from kalshi_trader.dashboard.portfolio_mapping import parse_fixed_point


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
    orderbook = orderbook_data.get("orderbook", {})
    yes_prices = [lvl[0] for lvl in orderbook.get("yes", []) if lvl]
    no_prices = [lvl[0] for lvl in orderbook.get("no", []) if lvl]

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
    anthropic_client = anthropic.AsyncAnthropic()
    message = await anthropic_client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=256,
        system=_INTENT_SYSTEM_PROMPT,
        messages=[{"role": "user", "content": intent}],
    )
    text = next((block.text for block in message.content if hasattr(block, "text")), "{}")
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


async def resolve_quantity(
    ticker: str,
    quantity_spec,
    action: str,
    client,
    *,
    amount_dollars: float | None = None,
    yes_price_cents: int | None = None,
) -> tuple[str, int]:
    """Return (side, contract_count) ready to pass to create_order.

    quantity_spec: int → use directly; "all" → fetch position; None → compute from amount_dollars
    """
    if quantity_spec == "all":
        positions_response = await client.get_positions()
        market_positions = positions_response.get("market_positions", [])
        held_position = next(
            (position for position in market_positions if position.get("ticker") == ticker),
            None,
        )
        if held_position is None:
            print(f"ERROR: No open position for {ticker}", file=sys.stderr)
            sys.exit(1)
        signed_quantity = parse_fixed_point(held_position.get("position_fp"))
        side = "yes" if signed_quantity >= 0 else "no"
        return side, int(abs(signed_quantity))

    if isinstance(quantity_spec, int) and quantity_spec > 0:
        return action, quantity_spec

    if amount_dollars is not None and yes_price_cents is not None and yes_price_cents > 0:
        contract_count = math.floor(amount_dollars / (yes_price_cents / 100.0))
        if contract_count < 1:
            print(
                f"ERROR: ${amount_dollars} at {yes_price_cents}¢/contract yields 0 contracts",
                file=sys.stderr,
            )
            sys.exit(1)
        return action, contract_count

    print(
        "ERROR: quantity or amount required — provide --quantity, --quantity all, or --amount",
        file=sys.stderr,
    )
    sys.exit(1)
