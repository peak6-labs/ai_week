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
