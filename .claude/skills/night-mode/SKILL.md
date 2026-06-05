---
name: night-mode
description: >-
  Autonomous overnight trading pipeline. Runs the full Kalshi orchestration
  cycle (scout → signals → score → challenge → execute) without human
  involvement. Executes approved weather trades only, using sized limit
  orders (Kelly base = remaining session budget) up to a hard session cap of $200 or 10 trades. Deterministic
  stop-loss and profit-target exits always run unconditionally. Step 0.75
  (AI position review) is skipped entirely. Non-weather and love island
  markets are excluded.
  Use /loop 20m /night-mode to run overnight.
  Use when the user says "night mode", "run overnight", or "start night trading".
---

# Night Mode Orchestrator

You are running the Kalshi trading pipeline in **autonomous night mode**. All
execution is handled by deterministic Python scripts — no human confirmation,
no AI-driven position review.

**Two hard guarantees:**
1. `scripts/portfolio_loop.sh` is kept running in the background so
   `evaluate_portfolio.py --execute` continues checking exits every 30 seconds
   regardless of the main night-mode loop cadence.
2. All night-mode rules (weather-only filter, edge gate, settlement gate,
   duplicate guard, session cap, sizing) are enforced by
   `scripts/night_execute.py` — not by this prompt.
   The script is the authority.

**Only June 5, 2026 daily weather markets are tradeable overnight.** The skill
should only carry forward weather/climate contracts for the settlement date
**2026-06-05** (typically tickers containing `-26JUN05`). The execution script
still rejects any candidate whose category is not weather/climate, and also
rejects love island explicitly.

---

## Step 0 — Setup

Before running the overnight cycle, review:

- `.claude/skills/night-mode/references/2026-06-04-weather-execution-postmortem.md`

That post-mortem is the current reference for weather execution mistakes,
especially overconfidence in narrow same-day bands, fragmented execution
logging, and repeated same-thesis entries after repricing.

```bash
cd /Users/scorley/code
TS=$(date -u +%Y%m%dT%H%M%SZ)
echo "TS=$TS"
CYCLE=$(( $(wc -l < reports/cycle-log.txt 2>/dev/null || echo 0) + 1 ))
SESSION_DATE=$(date -u +%Y%m%d)
SESSION_FILE="reports/night-mode-session-${SESSION_DATE}.json"
mkdir -p reports
.venv/bin/python scripts/ui_log.py "Night mode: cycle $CYCLE started (TS=$TS)"
.venv/bin/python scripts/ui_state.py "{\"cycle_number\": $CYCLE, \"last_cycle_at\": \"$(date -u +%Y-%m-%dT%H:%M:%SZ)\"}"
```

Balance is still useful context for the overnight run and downstream records:

```bash
BALANCE=$(curl -s -m 3 http://localhost:8000/api/state \
  | python3 -c "import sys,json; print(json.load(sys.stdin).get('balance_dollars') or '')" 2>/dev/null)
BALANCE=${BALANCE:-${KALSHI_BALANCE:-1000}}
```

---

## Step 0.5 — Ensure Portfolio Exit Loop Is Running

**This step always runs, even if the session cap has been reached.** The goal is
to keep the continuous exit monitor running in the background, independent of
the main night-mode cycle.

```bash
cd /Users/scorley/code
if pgrep -f "scripts/portfolio_loop.sh" >/dev/null; then
  echo "portfolio_loop.sh already running"
else
  nohup bash scripts/portfolio_loop.sh --night-mode > reports/portfolio-loop.log 2>&1 &
  echo $! > reports/portfolio-loop.pid
fi
```

Log the status:

```bash
.venv/bin/python scripts/ui_log.py "Night mode: portfolio exit loop verified" 2>/dev/null || true
```

Continue to Step 1 unconditionally.

---

## Step 1 — Find markets with the market-scout agent

The candle cache has a 55-minute TTL, so scout scores don't change between
30-minute cycles. Reuse a recent scout file if one exists:

```bash
SCOUT_FILE=$(ls -t /tmp/market_scout_*.json 2>/dev/null | head -1)
SCOUT_AGE=9999
if [ -n "$SCOUT_FILE" ]; then
  SCOUT_AGE=$(( $(date -u +%s) - $(stat -f %m "$SCOUT_FILE" 2>/dev/null || stat -c %Y "$SCOUT_FILE") ))
fi
if [ "$SCOUT_AGE" -lt 3000 ]; then
  echo "Reusing scout file $SCOUT_FILE (age ${SCOUT_AGE}s)"
  MARKET_SCOUT_FILE="$SCOUT_FILE"
else
  echo "Scout cache stale or missing (age ${SCOUT_AGE}s) — running fresh scan"
  MARKET_SCOUT_FILE="/tmp/market_scout_${TS}.json"
fi
```

If `SCOUT_AGE >= 3000` (50 minutes), dispatch the **`market-scout`** agent:

> Scan and score **only climate/weather markets**. You are in **pipeline mode**:
> write the full scored JSON (using the `score_markets.py --category "climate and weather" --json`
> output, or equivalent weather-only market-scout flow) to
> `/tmp/market_scout_<TS>.json` and do **NOT** write the markdown report. In
> your final message, return only the JSON path you wrote plus a one-line
> summary of the hottest themes.

Read `$MARKET_SCOUT_FILE`. Rows are sorted by `average_score`
descending. Each has: `event_ticker`, `best_market_ticker`, `title`,
`category`, `average_score`, `best_score`, `coverage_pct`, `yes_bid`,
`yes_ask`, `spread_cents`, `one_sided`, `last_price`, `open_interest`,
`volume_24h`, `signals`, `close_time`, `series_url`. **Prices are in cents.**

**Filter immediately, before any live-price or rules fetches.** From
`/tmp/market_scout_<TS>.json`, first keep only weather/climate markets for the
daily settlement date **June 5, 2026**. In practice, keep only rows whose
tradeable `best_market_ticker` clearly maps to **`2026-06-05`** (usually
contains `-26JUN05`). Ignore all non-weather rows and all weather rows for any
other date before calling `market_rules.py` or `live_prices.py`.

**Deep-signal subset (exactly 8, rotating each cycle):** After that filter,
sort the remaining June 5 weather rows by `average_score` descending. Divide
them into groups of 8 and use the cycle number to pick a different group each
time:

```python
import math
group_size = 8
group_count = math.ceil(len(june5_rows) / group_size)
group_index = (CYCLE - 1) % group_count
offset = group_index * group_size
subset = june5_rows[offset : offset + group_size]
```

This ensures every market in the June 5 universe gets covered across cycles
rather than always deep-signaling the same top-ranked tickers.

For each row compute `hours_to_close` from `close_time`. Use
`best_market_ticker` as the tradeable `ticker`.

Fetch settlement rules for the subset:

```bash
KALSHI_ENV=prod PYTHONPATH=. .venv/bin/python scripts/market_rules.py \
  --tickers SUBSET_TICKER1 SUBSET_TICKER2 ... > /tmp/rules_${TS}.json
```

Fetch live prices for the subset:

```bash
KALSHI_ENV=prod PYTHONPATH=. .venv/bin/python scripts/live_prices.py \
  --tickers SUBSET_TICKER1 SUBSET_TICKER2 ... > /tmp/live_prices_${TS}.json
```

Override each subset market's `yes_bid`/`yes_ask` with live values. Drop
tickers mapping to nulls (illiquid/unquoted).

---

## Step 2 — Dispatch signal agents

Create the weather signal output directory, then dispatch agents for the
deep-signal subset. Agents write their JSON arrays directly to files so Step 3
can assemble the signals file without Claude constructing JSON in-context.

```bash
mkdir -p /tmp/weather_signals_${TS}
```

**Weather signals only** — send a **single message** containing one `Agent`
tool call per market in the subset (all 8 at once). Do not dispatch them one at
a time. Each call uses the `weather-signal` agent type and passes:
- the market's ticker and title
- its settlement context from `/tmp/rules_${TS}.json`
- `OUTPUT_FILE=/tmp/weather_signals_${TS}/${TICKER}.json` so the agent tees
  its JSON array to disk

All 8 agents run concurrently; wait for all to finish before proceeding.

**Disabled in night mode** (do not run any of these):
- `market-maker-signal` — dropped per June 4 postmortem: amplifies price
  chasing on weather markets rather than adding independent information
- `order-flow-signal`
- `polymarket-price-signal`
- `polymarket-whale-signal`
- `sportsbook-odds-signal`
- `mentions-signal`
- `polls-signal`
- `x-signal`

Log before dispatching:
```bash
.venv/bin/python scripts/ui_log.py "Night mode: weather signals for TICKER1, TICKER2 ..."
```

---

## Step 3 — Build the signals file

Run `build_signals.py` to assemble `/tmp/signals_${TS}.json` from the agent
output files written in Step 2. Do **not** use the Write tool to construct the
signals JSON manually — the script handles this entirely.

```bash
PYTHONPATH=. .venv/bin/python scripts/build_signals.py \
  --scout-file       ${MARKET_SCOUT_FILE} \
  --weather-dir      /tmp/weather_signals_${TS} \
  --live-prices-file /tmp/live_prices_${TS}.json \
  > /tmp/signals_${TS}.json
.venv/bin/python scripts/ui_log.py "Night mode: signals file built"
```

Only markets that received a weather signal are included (the deep-signal
subset). Markets with only a microstructure estimate cannot clear the
`n_sources >= 2` scoring filter, so excluding them here costs nothing.

---

## Step 4 — Score deterministically

```bash
cd /Users/scorley/code
PYTHONPATH=. .venv/bin/python scripts/score_signals.py \
  --signals-file /tmp/signals_${TS}.json \
  --config runtime_config.json > /tmp/scored_${TS}.json
.venv/bin/python scripts/ui_log.py "Night mode: deterministic scoring complete"
```

Filter survivors and log them — do not read the full file in context:

```bash
PYTHONPATH=. .venv/bin/python3 -c "
import json, sys
scored = json.load(open('/tmp/scored_${TS}.json'))
survivors = [m for m in scored if m.get('worth_trading') and m.get('n_sources', 0) >= 2]
print(f'Survivors: {len(survivors)} of {len(scored)}')
for m in survivors:
    print(f\"  {m['ticker']}  edge={m.get('fee_adjusted_edge_cents',0):.1f}c  combined={m.get('combined_probability',0):.3f}  n_sources={m.get('n_sources')}\")
" 2>&1
```

Pass the survivor ticker list and their combined probabilities / edge values forward to Step 4.5. Do not re-read the scored file.

Persist this cycle:

```bash
PYTHONPATH=. .venv/bin/python scripts/persist_cycle.py \
  --scout-file /tmp/market_scout_${TS}.json \
  --scored-file /tmp/scored_${TS}.json --cycle-ts ${TS} || true
```

---

## Step 4.5 — Market-maker signals for survivors

If there are no survivors, skip to Step 5.

Otherwise, send a **single message** containing one `Agent` tool call per
survivor (all at once). Do not dispatch them one at a time. Each call uses the
`market-maker-signal` agent type and passes:
- the survivor's ticker and title
- live yes_bid and yes_ask from `/tmp/live_prices_${TS}.json`
- `OUTPUT_FILE=/tmp/mm_signals_${TS}/${TICKER}.json`

```bash
mkdir -p /tmp/mm_signals_${TS}
```

All agents run concurrently; wait for all to finish before proceeding to Step 5.
The market-maker results are used only in the adversarial challenge — they do
not affect the scored probabilities or edge values from Step 4.

---

## Step 5 — Adversarial challenge

Fetch settlement rules for surviving candidates not already in the subset:

```bash
KALSHI_ENV=prod PYTHONPATH=. .venv/bin/python scripts/market_rules.py \
  --tickers CANDIDATE_TICKER... >> /tmp/rules_${TS}.json
```

Read each PDF and for each surviving market answer five questions:

1. **Settlement rule** — does `rules_primary`, `settlement_sources`, and the
   rules metadata confirm the market resolves on what the signals measured? Check
   `data_quality` fields. Drop if there's a timing/threshold twist.
2. **Bear case** — what specific mechanism makes the signal wrong?
3. **Source independence** — two paths:
   - **External weather path (5¢):** `gfs_ensemble` is present and supports the
     chosen direction; fee_adjusted_edge ≥ 5¢. This is the primary path.
   - **Directional-book corroboration (tiebreaker):** if the market-maker signal
     file exists at `/tmp/mm_signals_${TS}/${TICKER}.json`, read it. If
     `depth_imbalance` points the same direction as the thesis, treat this as
     corroborating evidence — but not sufficient on its own. Do not use it to
     override a gfs_ensemble result or to rescue a candidate that fails the
     weather path.
4. **Base rate** — supports this direction?
5. **Fresh-eyes test** — would you act on this with no prior conviction?

**Band markets (`B`-series tickers):** Tickers like `KXHIGHTDAL-26JUN05-B86.5`
are 1°F narrow-band markets — they ask "will the temperature land in [86, 87]°F?"
The pipeline counts ensemble members inside that window, which is correct. When the
ensemble median sits well above or below the band, a low P(YES) and a resulting NO
survivor are expected and valid — not a pipeline artifact.

Log each decision:

```bash
.venv/bin/python scripts/ui_log.py "TICKER passed challenge → candidate slate"
.venv/bin/python scripts/ui_log.py "TICKER failed — REASON" warning
```

---

## Step 6 — Build candidate ideas

For each **June 5, 2026 weather** market that passed the challenge:

- **YES**: `confidence = combined_probability`, `market_price = yes_ask`
- **NO**: `confidence = 1 - combined_probability`, `market_price = 100 - yes_bid`

Write `/tmp/candidates_${TS}.json` with the **Write** tool. **Night mode adds
`hours_to_close`** — required by `night_execute.py` for the weather settlement gate:

```json
{
  "ticker": "...",
  "side": "yes|no",
  "confidence": 0.0,
  "market_price": 0.0,
  "reasoning": "...",
  "signal_sources": ["gfs_ensemble", "kalshi_bias"],
  "category": "...",
  "agent_id": "night_mode",
  "selection_summary": "1–2 sentences on why this passed the challenge",
  "hours_to_close": 18.5
}
```

If no candidates survive, log and skip to Step 9 with an empty slate.

---

## Step 7 — Night mode execution

**All rules enforced by the script. Do not apply any rules yourself.**

```bash
cd /Users/scorley/code
KALSHI_ENV=prod PYTHONPATH=. .venv/bin/python scripts/night_execute.py \
  --candidates-file /tmp/candidates_${TS}.json \
  --session-file    ${SESSION_FILE} \
  --out             /tmp/night_executed_${TS}.json \
  --cycle-ts        ${TS}
```

Read `/tmp/night_executed_${TS}.json`. Log executed trades and session state:

```bash
.venv/bin/python scripts/ui_log.py "Night mode: PLACED TICKER SIDE qty=N at PRICEc order=ORDER_ID"
.venv/bin/python scripts/ui_log.py "Night mode: K trades placed, $D spent this cycle"
```

If any record has `rejection_reason == "session_cap_reached"`:

```bash
.venv/bin/python scripts/ui_log.py "Night mode: session cap reached — no further trades tonight" warning
```

If any record has `rejection_reason == "cycle_cap_reached"`:

```bash
.venv/bin/python scripts/ui_log.py "Night mode: cycle cap reached (3 trades) — resuming next cycle" warning
```

---

## Step 8 — Write outputs

```bash
cd /Users/scorley/code && mkdir -p reports
```

Write with the **Write** tool:

**`reports/night-mode-${TS}.json`** — executed ideas (entries where
`rejection_reason` is null): `ticker`, `side`, `confidence`, `market_price`,
`suggested_size_dollars`, `reasoning`, `signal_sources`, `category`,
`agent_id`, `selection_summary`.

**`reports/night-mode-${TS}.md`** — human-readable table: ticker, side,
edge_cents, yes_price, order_id, rejection_reason for rejected ideas.

Record for calibration:

```bash
PYTHONPATH=. .venv/bin/python scripts/paper_track.py record \
  --ideas-file reports/night-mode-${TS}.json --cycle-ts ${TS} \
  --disposition night_mode || true
PYTHONPATH=. .venv/bin/python scripts/paper_track.py record-scored \
  --scored-file /tmp/scored_${TS}.json --cycle-ts ${TS} \
  --exclude-file reports/night-mode-${TS}.json --min-sources 2 || true
```

Surface cycle summary to the Monitor tab:

```bash
.venv/bin/python scripts/ui_state.py "{\"last_cycle_at\": \"$(date -u +%Y-%m-%dT%H:%M:%SZ)\", \"cycle_number\": ${CYCLE}}"
```

---

## Step 9 — Publish and log

If executed trades are non-empty, dispatch the **`idea-publisher`** agent:

> Publish the night-mode trades to the dashboard.
> `IDEAS_FILE=reports/night-mode-<TS>.json`.

Log cycle completion:

```bash
cd /Users/scorley/code
.venv/bin/python scripts/ui_log.py "Night mode: cycle complete — N markets, K executed"
echo "$(date -u +%Y-%m-%dT%H:%M:%SZ) | N markets | K executed | session: T trades \$DS" >> reports/cycle-log.txt
```

---

## Step 10 — Mark prior recommendations (trailing, lowest priority)

**Only after publishing. Skip if cycle is running long.**

```bash
KALSHI_ENV=prod PYTHONPATH=. .venv/bin/python \
  scripts/paper_track.py mark --from-supabase --max-age-minutes 130 || true
```

---

## Step 11 — Cancel stale resting orders

Cancel any resting order that has not been filled within 10 minutes.
Exit (sell) orders are replaced at midmarket so the position closes promptly.
Entry (buy) orders are cancelled without replacement — do not re-enter stale buys overnight.

```bash
cd /Users/scorley/code
KALSHI_ENV=prod PYTHONPATH=. .venv/bin/python scripts/cancel_stale_orders.py \
  --minutes 10 > /tmp/stale_orders_${TS}.json
```

Read `/tmp/stale_orders_${TS}.json`. For each record where `cancelled == true`:

- Log the cancellation:
  ```bash
  .venv/bin/python scripts/ui_log.py "Night mode: cancelled stale TICKER ORDER_ID (ACTION)"
  ```
- If `action == "sell"`: dispatch the **`place-order`** skill to replace:
  > Exit all of TICKER at midmarket.
- If `action == "buy"`: log that the buy was cancelled without replacement.

If `/tmp/stale_orders_${TS}.json` is empty (`[]`), skip silently.

---

## Running overnight

```
/loop 20m /night-mode
```

Each cycle starts by tending to open positions (Step 0.5) and ends by marking
prior recommendations. The session cap accumulates across cycles — once $200 /
10 trades is hit for the calendar day, no new entries are placed. Love island
markets are always excluded regardless of signal quality.
