---
name: portfolio
description: >-
  Manages open Kalshi positions in two phases: (1) deterministic auto-exits
  for stop-loss (down 25%) or profit-target (up 75%); (2) AI-driven review
  of surviving positions using web search, presenting exit recommendations
  for user approval. Use when the user says "check positions", "manage
  portfolio", "run portfolio", "stop loss check", "exit losers", or
  "review positions".
---

# Portfolio Management

Two phases:

**Phase 1 (deterministic):** `evaluate_portfolio.py` auto-exits any position
breaching stop-loss (down 25%) or profit-target (up 75%). No judgment — the
script decides and executes immediately.

**Phase 2 (AI-driven):** For surviving positions with `market_exposure_dollars
≥ $2`, dispatch `position-reviewer` with web search to check current events,
present each exit recommendation to the user for y/n approval, then place
approved exits.

---

## Step 1 — Generate a timestamp

```bash
TS=$(date -u +%Y%m%dT%H%M%SZ)
echo "Portfolio eval: TS=$TS"
```

## Step 2 — Auto-exit phase

```bash
cd /Users/scorley/code
KALSHI_ENV=prod PYTHONPATH=. .venv/bin/python scripts/evaluate_portfolio.py \
  --out /tmp/portfolio_eval_${TS}.json
```

Read `/tmp/portfolio_eval_${TS}.json`. Report auto-exits to the user
(ticker, side, qty, reason, order_id). Example:

> Auto-exited 2 positions: `KXFOO-25DEC01` (stop_loss at 35¢), `KXBAR-25DEC01` (profit_target at 91¢)

If `errors` is non-empty, surface each error clearly.

## Step 3 — Filter surviving positions for AI review

Read `clean_positions` from the results. Filter to positions where
`market_exposure_dollars >= 2.0`. If none remain, skip to Step 5.

Compute `hours_to_close` for each position (parse `close_time` ISO string,
subtract from now).

## Step 4 — Dispatch position-reviewer agent

Build a JSON array of position objects. For each qualifying position include
all fields from `clean_positions` plus `hours_to_close`.

Dispatch the **`position-reviewer`** agent with this prompt:

> Review these open Kalshi positions and return a JSON array of recommendations.
> Use web search for every position to check whether current events affect
> resolution — especially look for: events that have already occurred, official
> results that have been announced, forecasts from authoritative sources (NOAA,
> NWS, official results), or news that makes YES/NO resolution highly likely.
>
> [paste the full JSON array]

The agent returns a JSON array with one object per position:
`ticker`, `recommendation` ("exit"|"hold"|"add"), `confidence`,
`reasoning`, `signal_summary`, `re_entry_verdict`, `profit_taking_note`.

## Step 5 — Present recommendations and prompt

For each `"exit"` recommendation, present inline and ask the user:

```
`TICKER` — YES/NO × N contracts | avg Pc → now Qc | unrealized: ±$X
Signals: [signal_summary]
Re-entry: [re_entry_verdict]
Confidence: [confidence × 100]%
[profit_taking_note if non-null]

Recommend: exit at [midpoint_yes_price_cents rounded]¢.
Exit this position? (y/n)
```

**If y:** place the exit:
```bash
cd /Users/scorley/code
KALSHI_ENV=prod PYTHONPATH=. .venv/bin/python scripts/exit_position.py \
  --ticker TICKER \
  --side yes_or_no \
  --quantity N \
  --yes-price PRICE
```
where `PRICE = max(1, min(99, round(midpoint_yes_price_cents)))`.
Report the returned order_id to the user.

**If n:** continue to the next recommendation. Log the decision:
```bash
.venv/bin/python scripts/ui_log.py "Portfolio review: TICKER held by user decision" 2>/dev/null || true
```

After all exit prompts, surface `"hold"` and `"add"` recommendations as an
informational summary only (no action required):

> Holds: `TICKER1` (edge intact), `TICKER2` (within range)
> Add opportunities: `TICKER3` (confidence: 70%)

## Step 6 — Log completion

```bash
cd /Users/scorley/code
.venv/bin/python scripts/ui_log.py \
  "Portfolio: N auto-exits, K AI-exits approved, M held" 2>/dev/null || true
```

(Substitute real counts. `|| true` so a missing dashboard server never fails
a standalone run.)
