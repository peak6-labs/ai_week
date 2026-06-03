---
name: orchestrate
description: >-
  Runs one full cycle of the Kalshi trading pipeline from the main conversation
  context. Use when the user says "run the orchestrator", "run the pipeline",
  "find trade ideas", "scan for trades", or "start the trading loop". Uses the
  market-scout agent to find markets, dispatches signal agents in parallel per
  market, scores edge deterministically, applies adversarial challenge, runs the
  risk agent to filter out risky trades, and publishes the surviving ideas to the
  dashboard. Use /loop to repeat on a cadence.
---

# Kalshi Trading Orchestrator

You are running the Kalshi trading pipeline for one cycle **from the main
conversation context**. This matters: only the main context can use the `Agent`
tool to dispatch subagents. Subagents cannot dispatch other subagents, so this
pipeline is driven from here, not from inside an agent.

**The whole point of this skill is to USE the agents and scripts we built.** Do
not reimplement their logic inline. Specifically:

- Find markets with the **`market-scout`** agent (not by calling scorers yourself).
- Collect every signal with the **signal agents** (`polymarket-price-signal`,
  `order-flow-signal`, `market-maker-signal`, `kalshi-bias-signal`,
  `polymarket-whale-signal`, `weather-signal`, `x-signal`).
- Do all math with **`scripts/score_signals.py`** — never compute probabilities,
  edges, or Kelly fractions yourself.
- Filter risk with the **`risk`** agent — never decide sizing or rejections yourself.
- Publish with the **`idea-publisher`** agent.

**Never place orders. Never invent a signal. Never do the math by hand.** If a
step's tool fails, report the failure — do not paper over it with a hand-written
Python snippet that approximates the tool.

---

## Step 0 — Setup

```bash
cd /Users/scorley/code
TS=$(date -u +%Y%m%dT%H%M%SZ)
echo "TS=$TS"
CYCLE=$(( $(wc -l < reports/cycle-log.txt 2>/dev/null || echo 0) + 1 ))
.venv/bin/python scripts/ui_log.py "Orchestrator: cycle $CYCLE started (TS=$TS)"
.venv/bin/python scripts/ui_state.py "{\"cycle_number\": $CYCLE, \"last_cycle_at\": \"$(date -u +%Y-%m-%dT%H:%M:%SZ)\"}"
```

Remember `TS` and `CYCLE` — every output path below uses `TS`.

**UI telemetry.** Two fire-and-forget helpers feed the dashboard and never break
the pipeline if the UI is down: `scripts/ui_log.py "msg" [level]` appends to the
event log, and `scripts/ui_state.py '<json>'` merges structured state
(`cycle_number`, `last_cycle_at`, `agent_statuses`, `recent_ideas`). Use both as
directed below so the Monitor, Agents, and Ideas tabs all populate.

Available account balance for risk sizing is read **live** from the dashboard
(the UI polls the real Kalshi account), falling back to `KALSHI_BALANCE`, then to
`1000`:

```bash
BALANCE=$(curl -s -m 3 http://localhost:8000/api/state \
  | python3 -c "import sys,json; print(json.load(sys.stdin).get('balance_dollars') or '')" 2>/dev/null)
BALANCE=${BALANCE:-${KALSHI_BALANCE:-1000}}
echo "BALANCE=$BALANCE"
```

**Mark prior paper recommendations to market** (updates would-be P&L on past
recommendations; read-only, never executes):

```bash
KALSHI_ENV=prod PYTHONPATH=. .venv/bin/python scripts/paper_track.py mark || true
```

---

## Step 1 — Find markets with the market-scout agent

Dispatch the **`market-scout`** agent with the `Agent` tool. Tell it explicitly
to write its scored JSON to a path you control so you can read it back
deterministically. Pass this prompt (substituting the real `TS`):

> Scan and score the live Kalshi board. Write the full scored JSON (the
> `score_markets.py --json` output) to `/tmp/market_scout_<TS>.json` and also
> save your markdown report. In your final message, return the JSON path you
> wrote and a one-line summary of the hottest themes.

When the agent returns, read `/tmp/market_scout_<TS>.json`. It is a list of event
rows sorted by `average_score` descending. Each row has: `event_ticker`,
`best_market_ticker`, `title`, `category`, `average_score`, `best_score`,
`coverage_pct`, `yes_bid`, `yes_ask`, `spread_cents`, `one_sided`, `last_price`,
`open_interest`, `volume_24h`, `signals`, `close_time`, `series_url`.
**Prices are in cents (0–99).**

Take the **top 20** rows by `average_score`. For each, compute
`hours_to_close` from `close_time` relative to now. Use `best_market_ticker` as
the tradeable `ticker`.

**Each scout row already carries a `signal_estimates` list** — the deterministic
`microstructure` (directional price/volume/orderbook) and `kalshi_bias`
(calibration) signals, computed during the scan with no extra calls. Keep these;
Step 2 only needs to add the signals that require live lookups or judgment.

**Fetch settlement rules for the selected markets** (the snapshot omits them).
The rule can differ from what the title implies — this matters for cross-venue
matching and for trusting a signal:

```bash
KALSHI_ENV=prod PYTHONPATH=. .venv/bin/python scripts/market_rules.py \
  --tickers TICKER1 TICKER2 ... > /tmp/rules_${TS}.json
```

Read `/tmp/rules_${TS}.json` ({ticker: {rules_primary, subtitle, ...}}). Carry
each market's `rules_primary` forward — Step 2 passes it to the cross-venue
agents, and Step 5 checks it.

If the file is empty or missing, log and stop:

```bash
.venv/bin/python scripts/ui_log.py "Orchestrator: no scoreable markets — stopping" warning
```

---

## Step 2 — Dispatch signal agents in parallel

Process the selected markets in **batches of 5**. For each batch, spawn all
applicable signal agents **in a single message** (multiple `Agent` tool calls)
so they run concurrently. Wait for the whole batch before starting the next.
This keeps concurrent API load bounded — never fan out all 20 markets at once.

Log before each batch:

```bash
.venv/bin/python scripts/ui_log.py "Orchestrator: collecting signals for TICKER1, TICKER2, ..."
```

`microstructure` and `kalshi_bias` already come from the scout row — **do not
dispatch agents for those.** Dispatch only the signals that need a live lookup or
judgment, and prefer independent sources (they corroborate the price-derived
scout signals, which are correlated with each other):

**Dispatch for every market in the batch:**

| Agent | Args to pass in the prompt |
|-------|----------------------------|
| `polymarket-price-signal` | ticker, title, midpoint=`yes_ask` (int), hours_to_close |

**Conditional (only when it applies — keeps dispatch load bounded):**

- `sportsbook-odds-signal` — for **sports** markets (category sports, or ticker
  contains a league like WTA/ATP/NBA/NHL/MLB/NFL/UFC); args: ticker, title.
  This is the sharpest *independent* signal for sports — prioritize it there.
- `polymarket-whale-signal` — only if `volume_24h > 5000`; args: ticker, title
- `weather-signal` — only if `category` contains "weather" or "climate"; args: ticker, title
- `x-signal` — only if `category` is politics, elections, sports, crypto, or current events; args: ticker, title, category
- `order-flow-signal` / `market-maker-signal` — only if `volume_24h > 5000`
  (sparse trade history makes them empty on thin markets); args: ticker, title

When dispatching the **cross-venue** agents (`polymarket-price-signal`,
`sportsbook-odds-signal`), include the market's `rules_primary` in the prompt and
tell the agent to only return a signal if the external contract resolves on the
**same** criterion — this guards against "looks identical, settles differently"
mismatches.

Each agent returns a JSON array of `SignalEstimate` objects. An empty array `[]`
means **no signal** — record it as absent. Never fabricate a signal value to
fill a gap.

**Push agent status to the UI.** Before a batch, mark the agents you are about to
run as `running`; after the whole collection finishes, mark each as `idle` with
the total number of signals it produced across all markets. Example (one call per
state change is fine):

```bash
.venv/bin/python scripts/ui_state.py '{"agent_statuses": {"kalshi-bias-signal": {"status": "running"}, "order-flow-signal": {"status": "running"}}}'
# ...after collection...
.venv/bin/python scripts/ui_state.py '{"agent_statuses": {"kalshi-bias-signal": {"status": "idle", "last_signal_count": 7}, "order-flow-signal": {"status": "idle", "last_signal_count": 3}}}'
```

---

## Step 3 — Build the signals file

Each signal agent returns a JSON **array of `SignalEstimate` objects** — each
with `source`, `probability`, `uncertainty`, `weight`, `data_issued_at`, and
`metadata`. For every market, build one `signal_estimates` list by **starting
with the scout row's `signal_estimates`** (microstructure + kalshi_bias) and
**appending every estimate from the agents you dispatched** (an agent like
`x-signal` may return several — keep them all; each is a source). Do **not**
unwrap to the `metadata` field — the scorer combines the estimates' own
`probability`/`uncertainty`/`weight` directly.

Use the **Write** tool to create `/tmp/signals_<TS>.json` as an array of market
objects:

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
    "signal_estimates": [
      {"source": "kalshi_bias", "probability": 0.62, "uncertainty": 0.05, "weight": 0.65, "data_issued_at": "...", "metadata": {}},
      {"source": "polymarket_price", "probability": 0.58, "uncertainty": 0.03, "weight": 0.75, "data_issued_at": "...", "metadata": {}}
    ]
  }
]
```

Carry `yes_bid` through as well as `yes_ask` — Step 6 needs both to price the NO
side correctly. The scorer automatically drops non-informative estimates
(`uncertainty ≥ 0.99`, e.g. an X search that found no posts), so include every
non-empty estimate the agents returned and let the scorer filter.

---

## Step 4 — Score deterministically

```bash
cd /Users/scorley/code
PYTHONPATH=. .venv/bin/python scripts/score_signals.py \
  --signals-file /tmp/signals_${TS}.json \
  --config runtime_config.json > /tmp/scored_${TS}.json
.venv/bin/python scripts/ui_log.py "Orchestrator: deterministic scoring complete"
```

Read `/tmp/scored_${TS}.json`. Each entry has `ticker`, `title`, `category`,
`yes_ask`, `hours_to_close`, `combined_probability`, `uncertainty`, `n_sources`,
`sources`, `edge_cents`, `fee_adjusted_edge`, `worth_trading`, `kelly_fraction`,
and `side`.

Keep only markets where **`worth_trading == true`** AND **`n_sources >= 2`**.
Log the survivor count.

**Persist this cycle** (scored-market snapshots + run stats → Supabase, with a
local fallback; best-effort, never blocks):

```bash
PYTHONPATH=. .venv/bin/python scripts/persist_cycle.py \
  --scout-file /tmp/market_scout_${TS}.json \
  --scored-file /tmp/scored_${TS}.json --cycle-ts ${TS} || true
```

---

## Step 5 — Adversarial challenge

For each surviving market, answer these five questions before letting it onto the
candidate slate:

1. **Settlement rule** — read the market's `rules_primary` (from `/tmp/rules_${TS}.json`).
   Does it actually resolve on what the title implies, and does any cross-venue
   signal (polymarket/sportsbook) reference the *same* criterion? If the rule has
   a twist the signals didn't account for (specific date, threshold, source of
   truth), drop the market. **Microstructure + kalshi_bias are price-derived and
   correlated** — a slate resting only on those two is weak; prefer markets where
   an independent signal (sportsbook/polymarket) agrees.
2. **Bear case** — what specific mechanism makes the signal wrong?
3. **Source independence** — are the agreeing signals from orthogonal data
   sources, or do they share a common input?
4. **Base rate** — does the historical base rate support this direction?
5. **Fresh-eyes test** — would you act on this with no prior conviction?

Log each decision:

```bash
.venv/bin/python scripts/ui_log.py "TICKER passed challenge → candidate slate"
.venv/bin/python scripts/ui_log.py "TICKER failed — REASON" warning
```

---

## Step 6 — Build candidate ideas (correct yes/no axis)

For each market that passed the challenge, build one idea object. The risk script
measures edge on the axis of the chosen side: `edge = confidence - market_price/100`,
with `market_price` in **cents**. So flip both fields to the chosen side:

- **YES side** (`side == "yes"`): `confidence = combined_probability`,
  `market_price = yes_ask`
- **NO side** (`side == "no"`): `confidence = 1 - combined_probability`,
  `market_price = 100 - yes_bid` (the taker cost to buy NO)

Write `/tmp/candidates_${TS}.json` with the **Write** tool — an array of:

```json
{
  "ticker": "...",
  "side": "yes|no",
  "confidence": 0.0,
  "market_price": 0.0,
  "reasoning": "...",
  "signal_sources": ["polymarket_price", "kalshi_bias"],
  "category": "...",
  "agent_id": "orchestrator",
  "selection_summary": "1–2 sentences on why this passed the challenge"
}
```

If no candidates survive, log it and skip to Step 9 with an empty slate.

---

## Step 7 — Risk filter with the risk agent

Dispatch the **`risk`** agent with the `Agent` tool. Pass it:

> Run the deterministic risk checks on these trade ideas.
> `IDEAS_FILE=/tmp/candidates_<TS>.json`, `BALANCE=<BALANCE>`. Run
> `scripts/run_risk.py`, then report approved ideas (ticker, side, size,
> confidence) and rejected ideas (ticker, reason). Return the full results JSON.

The risk agent runs `scripts/run_risk.py`, which adds `approved`,
`approved_size_dollars`, and `rejection_reason` to each idea. Save its returned
JSON to `/tmp/risk_${TS}.json` (Write tool).

**Keep only ideas where `approved == true`.** These are the final slate. Set each
surviving idea's `suggested_size_dollars = approved_size_dollars`. Log:

```bash
.venv/bin/python scripts/ui_log.py "Orchestrator: risk approved K of N ideas"
```

---

## Step 8 — Write outputs

```bash
cd /Users/scorley/code && mkdir -p reports
```

Write two files with the **Write** tool:

**`reports/orchestrator-${TS}.json`** — the approved slate, one object per idea:
`ticker`, `side`, `confidence`, `market_price`, `suggested_size_dollars`,
`reasoning`, `signal_sources`, `category`, `agent_id`, `selection_summary`.

**Record the slate as paper recommendations** (for the calibration loop — marked
to market on later cycles; no execution):

```bash
PYTHONPATH=. .venv/bin/python scripts/paper_track.py record \
  --ideas-file reports/orchestrator-${TS}.json --cycle-ts ${TS} || true
```

**`reports/orchestrator-${TS}.md`** — a human-readable ranked table: ticker
(backtick-formatted, link via the row's `series_url`), side, edge, size,
signal sources, adversarial notes, and any risk rejections worth surfacing.

Surface this cycle's findings to the Monitor tab's recent-ideas view — **all
risk-checked candidates, approved and rejected**, so a cycle is never blank even
when nothing passes. With the **Write** tool create `/tmp/recent_${TS}.json` as
`{"recent_ideas": [...]}` where each entry has: `ticker`, `side` (upper),
`confidence`, `signal_sources`, `outcome` (`"approved"` else rejected),
`amount_dollars` (approved size), `rejection_reason` (from the risk agent). Then:

```bash
.venv/bin/python scripts/ui_state.py --file /tmp/recent_${TS}.json
```

---

## Step 9 — Publish and log

If the approved slate is non-empty, dispatch the **`idea-publisher`** agent:

> Publish the approved trade ideas to the dashboard.
> `IDEAS_FILE=reports/orchestrator-<TS>.json`.

Then log completion (substitute real counts):

```bash
cd /Users/scorley/code
.venv/bin/python scripts/ui_log.py "Orchestrator: cycle complete — N markets, K approved ideas"
echo "$(date -u +%Y-%m-%dT%H:%M:%SZ) | N markets | K approved | top: TICKER EDGEc" >> reports/cycle-log.txt
```

Return a short summary to the user: markets evaluated, candidates after scoring,
ideas after the adversarial challenge, ideas approved by risk, and the top idea.

---

## Running on a cadence

This skill runs **one** cycle. To repeat it on a schedule, the user runs:

```
/loop 20m /orchestrate
```

Do not build a sleep loop yourself — `/loop` owns the cadence and keeps the user
in control of starting and stopping it.
