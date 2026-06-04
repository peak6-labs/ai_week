# Design: `place_order.py` — Fast Natural-Language Order Execution

**Date:** 2026-06-04
**Status:** Approved

## Motivation

Executing a trade idea currently takes 30–60 seconds of Claude inference: look up client methods, fetch the orderbook, reason about maker/taker pricing, compute the right limit price, then call `create_order()`. This latency caused a missed exit on the ATL position. The goal is to reduce end-to-end time from user intent to live order to under 10 seconds.

---

## New Files

| File | Purpose |
|---|---|
| `scripts/place_order.py` | Unified entry/exit script with NL parsing |
| `.claude/skills/place-order/SKILL.md` | Claude skill — fires script immediately on order intent |

No changes to existing scripts. No new library modules.

---

## Script Interface

```bash
# Natural language (primary path) — ticker always explicit:
python scripts/place_order.py --ticker KXATL-26JUN-A1 "exit full position at midmarket no fees"
python scripts/place_order.py --ticker KXATL-26JUN-A1 "cancel and replace at 65 cents"
python scripts/place_order.py --ticker KXATL-26JUN-A1 "buy 10 dollars yes at midmarket"
python scripts/place_order.py --ticker KXATL-26JUN-A1 "cancel all resting orders"

# Structured flags (override NL-parsed values):
python scripts/place_order.py --ticker KXATL-26JUN-A1 --action sell --quantity all --pricing midmarket_maker

# Dry-run works with either:
python scripts/place_order.py --ticker KXATL-26JUN-A1 "cancel and replace at 65" --dry-run
```

### Arguments

| Argument | Required | Notes |
|---|---|---|
| `--ticker` | always | Market ticker in backticks in Claude responses |
| `intent` | optional positional | Natural language string — parsed by Haiku 4.5 |
| `--action` | structured override | `buy` or `sell` |
| `--side` | structured override | `yes` or `no` |
| `--quantity` | structured override | Integer or the literal `all` |
| `--amount` | structured override | Dollar amount for buys (e.g. `10`) |
| `--pricing` | structured override | `midmarket_maker` (default), `join_bid`, `join_ask`, `cross_spread` |
| `--yes-price` | structured override | Explicit cent price (1–99); skips orderbook fetch |
| `--dry-run` | optional | Print intent, no order placed |

Structured flags take precedence over NL-parsed values. If required fields are missing after both sources, the script exits with a clear error message.

---

## NL Parsing (Haiku 4.5)

The intent string is sent to `claude-haiku-4-5-20251001` with a tight system prompt and a JSON schema response. Expected round-trip: 1–3 seconds.

**System prompt (condensed):**
> You parse Kalshi order instructions into JSON. Return only valid JSON with these fields. Use null for anything not mentioned.

**Output schema:**
```json
{
  "action": "buy" | "sell" | null,
  "side": "yes" | "no" | null,
  "quantity": <integer> | "all" | null,
  "amount_dollars": <float> | null,
  "pricing": "midmarket_maker" | "join_bid" | "join_ask" | "cross_spread" | null,
  "yes_price": <integer 1-99> | null,
  "cancel_first": true | false,
  "cancel_only": true | false
}
```

**Recognized vocabulary:**
- `buy` / `sell` / `exit` / `close` / `get out of` → `action: "sell"` (exit); `action: "buy"` (entry)
- `yes` / `no` → `side`
- `all` / `full position` / `everything` / `the trade` → `quantity: "all"`
- `midmarket` / `mid` / `no fees` / `without fees` / `get filled without fees` → `pricing: "midmarket_maker"`
- `best price` / `best price without fees` → `pricing: "join_ask"` (sells) or `pricing: "join_bid"` (buys)
- `join ask` → `pricing: "join_ask"`
- `join bid` → `pricing: "join_bid"`
- `need to get filled` / `just get me out` / `urgently` / `cross the spread` → `pricing: "cross_spread"`
- `at N cents` / `at N` → `yes_price: N`
- `N dollars` / `$N` → `amount_dollars: N`
- `cancel and replace` / `reprice` / `move my order` → `cancel_first: true`
- `cancel` (alone) → `cancel_only: true`

**Disambiguation rule baked into system prompt:** "no fees" or "without fees" always takes precedence over urgency phrasing. `"get filled without fees"` → `midmarket_maker`, never `cross_spread`.

---

## Operations

### place
1. If `pricing` is set (not `yes_price`): `get_orderbook(ticker)` → compute limit price
2. If `quantity == "all"`: `get_positions()` → look up held side + contract count
3. `create_order(ticker, action, side, count, order_type="limit", yes_price=computed_price)`
4. Print: ticker, side, quantity, price, order ID, status

### cancel
1. `get_orders(status="resting")` → filter by ticker
2. `cancel_order(order_id)` for each resting order
3. Print count of cancelled orders

### cancel_and_replace
1. Run **cancel** (above)
2. `asyncio.sleep(0.5)` — let cancels settle
3. Run **place** (above)

---

## Pricing Logic

**Maker strategies** (zero fees — order rests in the book):

**`midmarket_maker` (default)**
- Compute `midpoint = (best_bid + best_ask) / 2`
- Sell: `max(ceil(midpoint), best_bid + 1)` — sits inside spread above the bid
- Buy: `min(floor(midpoint), best_ask - 1)` — sits inside spread below the ask
- Fallback if spread == 1: `join_ask` for sells, `join_bid` for buys
- Use when: you want the most aggressive maker price with the highest fill probability

**`join_ask`**
- Use `best_ask` directly (rests on the ask side, never crosses)
- Use when: selling and want the best (highest) maker price, willing to wait longer for a fill

**`join_bid`**
- Use `best_bid` directly (rests on the bid side, never crosses)
- Use when: buying and want the best (lowest) maker price, willing to wait longer for a fill

**Taker strategy** (fees apply — order crosses the spread immediately):

**`cross_spread`**
- Sell: use `best_bid` as limit price — immediately fills against top of bid stack
- Buy: use `best_ask` as limit price — immediately fills against bottom of ask stack
- Script prints a visible warning: `WARNING: cross_spread incurs taker fees (~7% of profit)`
- Use when: immediate execution matters more than fees

**Explicit `yes_price`**
- Skips orderbook fetch entirely; uses the provided value directly
- Placed as a limit order — no fee guarantee (caller's responsibility)

**Empty book:** if either side of the book is empty and a computed strategy is requested, the script exits: `ERROR: No best_bid/best_ask available for <ticker> — use --yes-price to set price explicitly`.

---

## Quantity Resolution

| Source | Condition | Behavior |
|---|---|---|
| `--quantity all` or NL `"all"` | sell only | Calls `get_positions()`, reads held side + count |
| `--quantity N` or NL integer | any | Uses N contracts directly |
| `--amount D` or NL `"N dollars"` | buy only | `floor(D / (yes_price_cents / 100))` |
| None provided | — | Script exits: `ERROR: quantity or amount required` |

For `--quantity all`, if no position is found for the ticker the script exits: `ERROR: No open position for <ticker>`.

---

## Skill: `place-order`

**File:** `.claude/skills/place-order/SKILL.md`

The skill fires immediately — no clarifying questions, no orderbook lookups, no reasoning about maker pricing. The script handles all of that.

**Trigger phrases** (any of these mean invoke the skill):
- "place an order", "put in an order"
- "buy", "sell", "exit", "close my position"
- "cancel and replace", "reprice", "move my order"
- "enter a position", "get out of"

**Behavior:**
1. Extract ticker from the user's message or recent conversation context
2. Pass the user's exact words as the intent string
3. Run: `python scripts/place_order.py --ticker <ticker> "<user's exact words>"`
4. Report the result (order ID, price, status) in one sentence

**Speed target:** skill load + Claude + script = under 10 seconds end to end.

---

## Error Handling

| Condition | Behavior |
|---|---|
| Missing ticker | Argparse error — ticker is always required |
| Ambiguous/missing intent fields | Script exits with specific field name missing |
| No position for `--quantity all` | Clear error, no order placed |
| Empty orderbook for computed pricing | Clear error, suggest `--yes-price` |
| Kalshi API 429 | `with_retry` handles with backoff (up to 6 attempts) |
| Order rejected by Kalshi | Print full API error response |

---

## Testing

- Unit tests for pricing logic: midmarket rounding (spread > 1, spread == 1, empty side), cross_spread prices
- Unit tests for NL parser: standard phrases, cancel-and-replace, explicit price, "need to get filled" → cross_spread, "get filled without fees" → midmarket_maker, "best price" → join_ask/join_bid
- Integration test (dry-run): verify correct price computation and `create_order` args without live placement
