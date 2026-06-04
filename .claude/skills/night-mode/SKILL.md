---
name: night-mode
description: >-
  Autonomous overnight trading pipeline. Runs the full Kalshi orchestration
  cycle (scout → signals → score → challenge → execute) without human
  involvement. Executes approved weather trades only, using sized limit
  orders up to a hard session cap of $100 or 10 trades. Deterministic
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

**Only weather markets are tradeable overnight.** The execution script rejects
any candidate whose category is not weather/climate, and also rejects love
island explicitly.

---

## Step 0 — Setup

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

Dispatch the **`market-scout`** agent:

> Scan and score the live Kalshi board. You are in **pipeline mode**: write the
> full scored JSON (the `score_markets.py --json` output) to
> `/tmp/market_scout_<TS>.json` and do **NOT** write the markdown report. In
> your final message, return only the JSON path you wrote plus a one-line
> summary of the hottest themes.

Read `/tmp/market_scout_<TS>.json`. Rows are sorted by `average_score`
descending. Each has: `event_ticker`, `best_market_ticker`, `title`,
`category`, `average_score`, `best_score`, `coverage_pct`, `yes_bid`,
`yes_ask`, `spread_cents`, `one_sided`, `last_price`, `open_interest`,
`volume_24h`, `signals`, `close_time`, `series_url`. **Prices are in cents.**

**Deep-signal subset (≤ ~20):** Select only weather/climate markets, ordered by
`average_score` descending. Ignore non-weather rows entirely for night mode.

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

## Step 2 — Dispatch signal agents in parallel

Process the deep-signal subset in **batches of 3**. For each batch, dispatch
all applicable agents **in a single message**. Wait for the batch before
starting the next.

**For every market:**
- `polymarket-price-signal` — ticker, title, midpoint from live prices, hours_to_close
- `market-maker-signal` — ticker, title

**Conditional:**
- `sportsbook-odds-signal` — sports markets only
- `weather-signal` — weather/climate markets only
- `mentions-signal` — mentions markets only
- `order-flow-signal` — only if `volume_24h > 500`
- `polymarket-whale-signal` — only if `volume_24h > 5000`
- `polls-signal` — **disabled** (`agent_polls_enabled: false`)
- `x-signal` — **disabled** (`agent_x_enabled: false`)

Include each market's `rules_primary` when dispatching cross-venue agents.

Log before each batch:
```bash
.venv/bin/python scripts/ui_log.py "Night mode: signals for TICKER1, TICKER2 ..."
```

---

## Step 3 — Build the signals file

For every market in the **full coverage set (~200)**, build one entry with
`signal_estimates` starting from the scout row's estimates (microstructure +
kalshi_bias) and appending agent estimates for the deep-signal subset. Write
with the **Write** tool to `/tmp/signals_${TS}.json`:

```json
[
  {
    "ticker": "KXFOO-25DEC01",
    "title": "...",
    "category": "politics",
    "yes_bid": 33.0,
    "yes_ask": 35.0,
    "hours_to_close": 24.0,
    "actionability_score": 0.72,
    "coverage_pct": 80.0,
    "volume_24h": 12708,
    "signal_estimates": [
      {"source": "kalshi_bias", "probability": 0.62, "uncertainty": 0.05,
       "weight": 0.65, "data_issued_at": "...", "metadata": {}},
      {"source": "polymarket_price", "probability": 0.58, "uncertainty": 0.03,
       "weight": 0.75, "data_issued_at": "...", "metadata": {}}
    ]
  }
]
```

Use live prices for subset markets. Carry both `yes_bid` and `yes_ask`.

---

## Step 4 — Score deterministically

```bash
cd /Users/scorley/code
PYTHONPATH=. .venv/bin/python scripts/score_signals.py \
  --signals-file /tmp/signals_${TS}.json \
  --config runtime_config.json > /tmp/scored_${TS}.json
.venv/bin/python scripts/ui_log.py "Night mode: deterministic scoring complete"
```

Keep only markets where `worth_trading == true` AND `n_sources >= 2`.

Persist this cycle:

```bash
PYTHONPATH=. .venv/bin/python scripts/persist_cycle.py \
  --scout-file /tmp/market_scout_${TS}.json \
  --scored-file /tmp/scored_${TS}.json --cycle-ts ${TS} || true
```

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
3. **Source independence** — three paths:
   - External path (5¢): independent external signal agrees; fee_adjusted_edge ≥ 5¢
   - Internal path (8¢): market_maker + OFI both directional, same direction; effective_edge ≥ 8¢
   - Directional-book path (10¢): market_maker direction + |depth_imbalance| > 0.4; effective_edge ≥ 10¢
4. **Base rate** — supports this direction?
5. **Fresh-eyes test** — would you act on this with no prior conviction?

Log each decision:

```bash
.venv/bin/python scripts/ui_log.py "TICKER passed challenge → candidate slate"
.venv/bin/python scripts/ui_log.py "TICKER failed — REASON" warning
```

---

## Step 6 — Build candidate ideas

For each **weather** market that passed the challenge:

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
  "signal_sources": ["polymarket_price", "kalshi_bias"],
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

Surface results to the Monitor tab:

```bash
.venv/bin/python scripts/ui_state.py --file /tmp/recent_${TS}.json
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

## Running overnight

```
/loop 20m /night-mode
```

Each cycle starts by tending to open positions (Step 0.5) and ends by marking
prior recommendations. The session cap accumulates across cycles — once $100 /
10 trades is hit for the calendar day, no new entries are placed. Love island
markets are always excluded regardless of signal quality.
