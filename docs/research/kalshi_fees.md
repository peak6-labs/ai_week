# Kalshi Fee Structure — Research Findings

Research date: 2026-06-03. Goal: understand Kalshi maker vs taker fees and how to
minimize them so we can later improve our fee model (`scripts/score_signals.py`,
`kalshi_trader/risk.py`) and edge thresholds. Research only — no code changed.

## TL;DR

- **Taker (immediately matched) fee** is still `round_up(0.07 × C × P × (1−P))` per
  order, charged in dollars. Our code's `0.07 × price × (1−price) × 100` (cents)
  matches this — except we do **not** apply the per-order round-up-to-the-cent.
- **Makers are NO LONGER universally free.** The current schedule has a dedicated
  "Maker Fees" section: on *selected* markets a resting order that later fills pays
  `round_up(0.0175 × C × P × (1−P))` — exactly **25% of the taker rate** (4x cheaper).
  On all *other* markets, maker/resting orders still pay **zero**. Which markets
  carry maker fees is set per-series and changes over time.
- **Index markets (S&P 500 `INX*`, Nasdaq-100 `NASDAQ100*`)** use a halved taker
  coefficient of **0.035**. Our `risk.py` already encodes this as
  `INDEX_MARKET_FEE_COEFFICIENT = 0.035` and filters index markets out of the
  tradeable universe.
- **No settlement fee. No membership fee. ACH/wire deposits & withdrawals free; a
  $2 flat fee historically applied to bank withdrawals (newer secondary sources say
  ACH withdrawal is now free, debit-card withdrawal $2).** None of this matters for
  a paper-trading strategy — there are no real cash movements.
- **Actionable:** for our edge-seeking strategy, entering with a **maker/limit order
  on a maker-fee market cuts entry fee by 75%**, and on a non-maker-fee market cuts
  it to **zero**. That can justify lowering the `worth_trading` edge bar — but only
  if we model fill risk, because a resting order is not guaranteed to fill.

## 1. The exact trading fee formula (general / taker)

The current general trading fee, per the official Kalshi fee schedule, is:

```
fees = round_up( 0.07 × C × P × (1 − P) )      # in dollars, rounded up to next cent
  P = price of one contract in dollars (50¢ = 0.50)
  C = number of contracts traded
```

This is charged only on orders that are **immediately matched** against resting
orders (takers). The 0.07 coefficient and the `P×(1−P)` parabola (peaks at 50¢,
collapses toward price extremes) are confirmed across the primary sources and an
unbroken chain of secondary write-ups dated through 2026.

Sources (primary):
- Kalshi official fee schedule PDF: https://kalshi.com/docs/kalshi-fee-schedule.pdf
- Live fee schedule page: https://kalshi.com/fee-schedule
- Kalshi Help Center "Fees" (updated 2026-04-19): https://help.kalshi.com/en/articles/13823805-fees
- CFTC-filed rule (DCM rulebook, fee schedule): https://www.cftc.gov/sites/default/files/filings/orgrules/22/09/rule091222kexdcm003.pdf

Secondary confirmation (Feb 5, 2026 schedule still 0.07):
- https://kalshiview.com/blog/kalshi-fees-explained-what-you-actually-pay-per-trade/
- https://agentbets.ai/guides/kalshi-fees-guide/
- https://0xinsider.com/learn/kalshi-fees-explained

**Verdict on our code:** `scripts/score_signals.py` uses
`fee = 0.07 * side_price * (1 - side_price) * 100` cents. The coefficient is
**correct and current**. The one discrepancy: the official formula rounds the
*per-order* dollar fee up to the next cent. Our model computes a fractional-cent
fee per contract and never rounds. For sizing/edge purposes the fractional value is
actually the *better* per-contract estimate (the official table's "fee for 1
contract" is just the round-up of the same formula — e.g. 50¢ → raw 1.75¢ shown as
2¢). The round-up only bites on tiny orders; for the 100-contract-ish sizes a $500
book trades it is negligible. No correction needed for edge math, but see §5.

### Official general-fee table (per-contract and per-100, taker)

| Price | Fee, 1 contract | Fee, 100 contracts | Raw 0.07·P·(1−P)·100 |
|------:|----------------:|-------------------:|---------------------:|
| $0.10 | $0.01 | $0.63 | 0.63¢ |
| $0.20 | $0.02 | $1.12 | 1.12¢ |
| $0.50 | $0.02 | $1.75 | 1.75¢ |
| $0.80 | $0.02 | $1.12 | 1.12¢ |
| $0.90 | $0.01 | $0.63 | 0.63¢ |

(The per-100 column is the un-rounded formula; the per-1 column is the round-up.)

## 2. Maker vs taker — the important update

Older Kalshi marketing (2022–2023) said flatly **"At Kalshi, we charge no fees for
Maker Orders"** and used limit orders as a way to pay *zero* fees:
- https://news.kalshi.com/p/makers-and-takers
- https://news.kalshi.com/p/utilizing-limit-orders

That is **no longer universally true.** The current fee schedule adds a "Maker Fees"
section, and on **July 1, 2025** Kalshi introduced a formula-based maker fee on
*selected* markets (replacing an earlier flat 0.25¢/contract maker fee on those
markets). On a maker-fee market, a resting order pays, when it executes:

```
maker fee = round_up( 0.0175 × C × P × (1 − P) )      # 25% of the taker coefficient
```

Key facts:
- The maker coefficient `0.0175` is **exactly one-quarter** of the taker `0.07`
  (a 4x / 75%-cheaper relationship), same parabolic shape.
- Maker fees apply **only on markets Kalshi designates** (typically the most popular
  / institutionally-market-made series, including many sports markets). On every
  other market, resting/maker orders are still **free**.
- Maker fees are charged **only if the resting order actually fills.** Placing and
  canceling an unfilled resting order costs nothing.
- There are **no maker rebates** in the liquidity-incentive sense. The only "rebate"
  is a rounding-overpayment refund: if cent-rounding made you overpay maker fees,
  Kalshi reimburses the excess in the first week of the following month, but only if
  the monthly excess exceeds $10.

How to know per-market: the API exposes the fee type and multiplier per series.
The `FeeType` enum is `quadratic` (taker-only, makers free),
`quadratic_with_maker_fees` (taker + the 0.0175 maker fee), or `flat`, plus a
`fee_multiplier`. Endpoints: `GET /series/fee_changes` and the `fee`-related fields
on `/markets`. So the **source of truth for whether a given market charges maker
fees is the API**, not a hard-coded assumption.
- https://docs.kalshi.com/api-reference/exchange/get-series-fee-changes
- https://news.kalshi.com/p/makers-and-takers (legacy "makers free" framing)
- https://www.ingame.com/kalshis-change-may-increase-fee-sports-traders/ (July 1 2025 maker-fee change, sports impact)
- https://kalshiview.com/blog/kalshi-resting-orders-explained/

### Maker-vs-taker comparison (per-contract, raw formula, before round-up)

| Price | Taker fee/contract (0.07) | Maker fee/contract, maker-fee mkt (0.0175) | Maker fee, non-maker-fee mkt |
|------:|--------------------------:|-------------------------------------------:|-----------------------------:|
| $0.20 | 1.12¢ | 0.28¢ | $0.00 |
| $0.50 | 1.75¢ | 0.44¢ | $0.00 |
| $0.80 | 1.12¢ | 0.28¢ | $0.00 |

Per 100 contracts: taker $1.12 / $1.75 / $1.12; maker-fee-market maker $0.28 /
$0.44 / $0.28; non-maker-fee-market maker $0.00 / $0.00 / $0.00.

So the entry-cost saving from going maker instead of taker, in cents per contract:
- On a maker-fee market: save **~0.84¢ @20¢, ~1.31¢ @50¢, ~0.84¢ @80¢**.
- On a normal (non-maker-fee) market: save the **entire** taker fee
  (~1.12¢ / ~1.75¢ / ~1.12¢) — fee goes to zero.

## 3. Per-series differences, minimums, caps, rounding

- **Index markets** (`INX*` = S&P 500; `NASDAQ100*` = Nasdaq-100): taker coefficient
  **0.035** (half the general rate), set since Sept 2022. Maker fees scale
  proportionally. We filter these out of the tradeable universe anyway.
  - https://news.kalshi.com/p/were-halving-the-fees
- **Per-series fee overrides** are a first-class concept — `GET /series/fee_changes`
  returns scheduled `fee_multiplier` + `fee_type` changes per series. Fees genuinely
  vary by series and over time, so **read the live `fee` fields from the API rather
  than hard-coding a single coefficient** for anything beyond a default.
- **Effective cap:** because of `P×(1−P)`, the per-contract taker fee peaks at 50¢
  (raw 1.75¢, shown as 2¢ after round-up) and **never exceeds 2¢/contract**; at
  extremes (1¢, 99¢) it is ~0.07¢ (1¢ after round-up). No explicit minimum beyond
  the 1¢ round-up granularity.
- **Round-up / rounding:** the headline formula rounds the per-order fee up to the
  next whole cent. Internally (direct members) trade fees are computed to the
  centicent ($0.0001) and a **fee accumulator** issues whole-cent rebates so that
  fees across many partial fills converge to the single-fill equivalent — i.e.
  per-fill rounding does not systematically overcharge.
  - https://docs.kalshi.com/getting_started/fee_rounding

## 4. Settlement / withdrawal / other costs

- **Settlement fee: none** for simple yes/no markets (sub-cent scalar settlements
  may carry a tiny rounding fee, irrelevant to us).
  - https://docs.kalshi.com/getting_started/market_settlement
- **Membership fee: none.**
- **Deposits:** ACH free, wire free, PayPal/Venmo free, debit/credit card up to 2%,
  crypto = network fees only.
- **Withdrawals:** the CFTC-filed schedule states a **$2 flat fee per withdrawal**
  to a linked bank account (wire withdrawals not supported). Newer 2026 secondary
  sources report ACH withdrawals are now free with a $2 fee only on debit-card
  withdrawals — treat the exact current number as API/UI-confirmable, not load-bearing.
- **For a $500 paper-trading strategy: none of the deposit/withdrawal/settlement
  fees apply.** The only cost that touches our edge math is the **per-trade trading
  fee at entry (and at exit, if we close before settlement).** Holding to settlement
  incurs no extra fee — there is no vig baked into the $1 payout.

## 5. Practical takeaways

**(a) Our fee model.** The 0.07 coefficient in both `score_signals.py` and
`risk.py` is correct for taker entry on general markets, which is the right default
since trade ideas currently enter by crossing the spread. Two refinements worth
making later:
  1. Source the coefficient/maker-status from the API's per-market `fee` /
     `fee_type` / `fee_multiplier` fields instead of the hard-coded 0.07, so index
     markets (0.035) and any future per-series changes are handled automatically.
     `risk.py`'s own comment already flags the API as the source of truth.
  2. Model **round-trip** cost when we expect to exit before settlement: entry fee +
     exit fee. The current single-fee model understates cost for trades we don't
     hold to expiry. (Holding to settlement = entry fee only.)

**(b) Could maker/limit entry lower the 5¢ edge bar?** Yes, materially, but it must
be paired with fill-risk modeling:
  - On a **non-maker-fee market**, a resting limit order pays **zero** fee. Our
    `fee_adjusted_edge = edge_cents − fee` would simply lose the ~0.6–1.75¢ taker
    drag, so the same trade clears a *lower* gross-edge bar. A maker entry could
    justify dropping `worth_trading` from `> 5.0` toward `> ~4.0` for the fee
    component alone.
  - On a **maker-fee market**, the fee shrinks to 25% (e.g. 0.44¢ vs 1.75¢ at 50¢),
    so still a meaningful reduction.
  - **Caveat — fill risk:** a maker/limit order only captures these savings if it
    fills. Posting at the bid/ask we want means we may not get filled, or get filled
    only when the market moves against us (adverse selection). The honest model is to
    keep a conservative edge bar but add a *maker discount* to the fee term when the
    strategy is configured to enter passively, and separately discount expected edge
    by a fill-probability factor. Net: maker entry lowers the *fee* hurdle by
    0.6–1.75¢/contract; whether that translates into a lower *worth_trading* threshold
    depends on the assumed fill rate, which should be modeled explicitly rather than
    assumed = 1.0.

Bottom line: the fee is small relative to a 5¢ bar (≤1.75¢/contract taker, ≤0.44¢
maker), so fees are not the dominant term — but switching entry to maker/limit is
the single highest-leverage way to cut the cost that does exist, and on most
(non-maker-fee) markets it removes the entry fee entirely.
