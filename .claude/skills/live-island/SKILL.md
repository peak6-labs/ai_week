---
name: live-island
description: >-
  Live-play price-action arbitrage for Love Island USA S8E2 markets during
  the episode. Polls prices, detects significant moves between polls, and
  recommends cross-market arbitrage trades automatically — no TV watching
  required. Enforces $10 hard cap per trade and $100 night exposure cap.
  Use when the user says "live island", "island arb", "check love island",
  or "love island trade".
---

# Live Island — Price-Action Arb Skill

**No agent pipeline. No TV narration. Pure price-action.**

Execution is **disabled** by default (paper mode). Remind the user once
per session to re-enable execution before placing orders.

---

## Core arb logic

**Elimination markets are mutually exclusive.** Only one contestant can be
eliminated per episode. When informed money starts flowing into one contestant's
YES, the other contestants' YES prices are still stale — that gap is the arb.

- Contestant A's YES spikes +15¢ in one poll → buy NO on B, C, D, E, F
  (they will NOT be eliminated this episode)
- Contestant A's YES drops sharply → no action unless another spiked

**Mention markets:** if a word has been said on TV, informed watchers buy YES.
A +10¢ spike on a mention market signals the word was said.

---

## Step 1 — Run the snapshot

```bash
cd /Users/scorley/code
KALSHI_ENV=prod PYTHONPATH=. .venv/bin/python scripts/live_island_snapshot.py
```

Parse the JSON output at the bottom. Key fields:
- `alerts` — human-readable list of detected movements this poll
- `arb_opportunities` — structured list of recommended trades (already computed)
- `remaining_headroom_dollars` — how much more you can deploy tonight
- `past_hard_exit` — if true, go directly to Step 4 (close all)
- `tradeable_tickers` — markets with spread ≤ 3¢ right now

**If `past_hard_exit` is true:** skip to Step 4 immediately.

**If no `arb_opportunities`:** report the table to the user with a "no signals
this poll" note. Done for this cycle.

---

## Step 2 — Interpret arb opportunities

The script pre-computes two types:

### Type: `elim_cross_arb`

One contestant's YES moved **+15¢ or more** in a single poll. This is a
strong signal that elimination is being priced in.

```json
{
  "type": "elim_cross_arb",
  "trigger_ticker": "KXLIUSAELIMINATION-26JUN03-KEN",
  "trigger_label": "Kenzie",
  "trigger_delta_poll": 22,
  "trigger_yes_bid": 31,
  "action": "BUY_NO_OTHERS",
  "targets": [
    {"ticker": "...-BRY", "label": "Bryce", "yes_bid": 9, "yes_ask": 10, "fee_no_per_5": 0.032},
    ...
  ]
}
```

**Trade plan:**
1. Buy NO on every target in `targets` (these are the non-spiking elimination markets).
   - NO entry cost per contract = `100 - yes_bid` cents (you're buying the NO side)
   - Size: **$5 per target** (multiple trades in one burst → minimum size)
2. Optionally buy YES on the trigger market if `trigger_yes_bid` < 60¢ and
   spread ≤ 3¢ — size **$10** (the single highest-conviction trade in the burst).
   Do not chase if already above 60¢ (most of the move has happened).

### Type: `mention_spike`

A mention market's YES bid jumped **+10¢ or more** in one poll.

```json
{
  "type": "mention_spike",
  "trigger_ticker": "KXLOVEISLMENTION-26JUN03-DRAM",
  "trigger_label": "'Drama'",
  "trigger_delta_poll": 15,
  "action": "BUY_YES",
  "yes_ask": 62,
  "fee_yes_per_5": 0.133
}
```

**Trade plan:** BUY YES at `yes_ask` — size **$5–$10** (use $10 if this is
the only trade in the burst, $5 if combined with elimination trades).
Only enter if `yes_ask < 95` and spread ≤ 3¢.

### Type: `mention_drop`

A mention market's YES bid fell **-10¢ or more** and bid is still ≥ 15¢
(meaning there's still meaningful profit margin in buying NO).

**Trade plan:** BUY NO — size **$5** flat.

---

## Step 3 — Present recommendations

For each trade, show in this format:

```
BUY NO  KXLIUSAELIMINATION-26JUN03-BRY  (Bryce)
  Entry: 91¢ NO (= 100 - 9¢ YES)   Size: $5
  Fee: $0.03   If Bryce not eliminated: +$0.46 net
  If Bryce IS eliminated: -$5.00

BUY YES KXLIUSAELIMINATION-26JUN03-KEN  (Kenzie — triggered)
  Entry: 31¢ YES   Size: $10
  Fee: $0.48   If Kenzie eliminated: +$22.62 net
  If not eliminated: -$10.00

Total burst: $35 across 5 trades
```

State: "Total burst: $X across N trades" — confirm it fits within
`remaining_headroom_dollars`.

Then wait for user confirmation before placing any order.

---

## Step 4 — Exit all positions (hard exit or user request)

When `past_hard_exit` is true OR user says "exit all":

1. Run snapshot to get current open positions.
2. For each open position, determine exit method:
   - **YES on a market that has resolved** (price at 98-100¢): hold to
     settlement — auto-resolves. No action needed.
   - **YES on an unresolved market** (price still moving): SELL at current bid.
   - **NO on any market**: BUY YES at current ask to close.
3. Show the full exit plan and total expected proceeds.
4. Wait for user confirmation, then place orders.

---

## Constraints — enforce every poll

1. **$10 hard cap per trade.** Never exceed $10 on a single LI position.
   Use $5 when placing multiple trades in one burst.
2. **$35 per-burst cap.** A single arb event triggers at most $35 in new positions.
3. **$100 total LI night cap.** Refuse new trades if `remaining_headroom_dollars < $5`.
4. **No new entries when `minutes_to_hard_exit < 15`.** Exits only.
5. **Spread ≤ 3¢.** Only enter markets listed in `tradeable_tickers`. A market
   with `wide_spread: true` is automatically excluded by the script.
6. **Only pre-defined tickers.** No other markets.
7. **Show fee on every recommendation.** Use `fee_no_per_5` / `fee_yes_per_5`
   from the snapshot, scaled proportionally to actual size.

---

## Fee reference

Kalshi fee = `0.07 × size × (1 − price)` for YES buys,
`0.07 × size × price` for NO buys.

| Trade | Entry | Fee on $5 | Notes |
|---|---|---|---|
| NO on Bryce (YES at 9¢) | 91¢ NO | $0.03 | Lowest-fee trade tonight |
| YES on Kenzie (at 9¢) | 9¢ YES | $0.32 | Fine for large potential upside |
| YES on Drama (at 47¢) | 47¢ YES | $0.19 | Acceptable |
| Any wide-spread market | — | — | Skip — spread is the real cost |

---

## Running during the episode

Use `/loop 2m /live-island` to poll every 2 minutes automatically.
The skill will only surface recommendations when `arb_opportunities` is
non-empty — silent polls (no signal) are one-line acknowledgements.
