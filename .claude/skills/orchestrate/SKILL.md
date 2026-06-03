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

(Marking prior recommendations to market happens **last**, as a trailing
low-priority step — see Step 10 — so it never delays analysis.)

---

## Step 1 — Find markets with the market-scout agent

Dispatch the **`market-scout`** agent with the `Agent` tool. Tell it explicitly
to write its scored JSON to a path you control so you can read it back
deterministically. Pass this prompt (substituting the real `TS`):

> Scan and score the live Kalshi board. You are in **pipeline mode**: write the
> full scored JSON (the `score_markets.py --json` output) to
> `/tmp/market_scout_<TS>.json` and do **NOT** write the markdown report or
> enumerate events — generating it is slow and bloats the round-trip. In your
> final message, return only the JSON path you wrote plus a one-line summary of
> the hottest themes.

When the agent returns, read `/tmp/market_scout_<TS>.json`. It is a list of event
rows sorted by `average_score` descending. Each row has: `event_ticker`,
`best_market_ticker`, `title`, `category`, `average_score`, `best_score`,
`coverage_pct`, `yes_bid`, `yes_ask`, `spread_cents`, `one_sided`, `last_price`,
`open_interest`, `volume_24h`, `signals`, `close_time`, `series_url`.
**Prices are in cents (0–99).**

**Coverage: score the WHOLE board, dispatch external agents to a subset.** The
deterministic scout signals (`microstructure` + `kalshi_bias`) are already
attached to every one of the ~200 rows at no cost, so we **score and record all
of them** (Steps 3–4) — this is the calibration dataset and it's nearly free.
What is expensive is the external signal *agents* (weather/sportsbook/polymarket/
x), so we only spend those on a prioritized **deep-signal subset**.

- **Coverage set = all scout rows** (cap ~200). Every one gets deterministic
  scoring and, if it reaches 2+ sources, gets recorded for the backtest.
- **Deep-signal subset (≤ ~20)** = the highest-priority rows for external agent
  dispatch. Select by this priority order until you hit 20:
  1. All weather/climate markets (NOAA is independent and cheap)
  2. All sports markets (sportsbook odds are independent and sharp)
  3. All mentions markets (GDELT base-rate)
  4. Top rows by `average_score` to fill remaining slots
  Keep the cap at 20 — market_maker now runs on every market in the subset, so
  each batch of 3 already spawns 6+ parallel agents. Wider subsets slow the cycle
  without proportionally more signal. Only this subset gets Step 2's agent dispatch.

For each row compute `hours_to_close` from `close_time`; use `best_market_ticker`
as the tradeable `ticker`.

**Each scout row already carries a `signal_estimates` list** — the deterministic
`microstructure` (directional price/volume/orderbook) and `kalshi_bias`
(calibration) signals, computed during the scan with no extra calls. Keep these;
Step 2 only needs to add the signals that require live lookups or judgment.

**Fetch settlement rules — only for the deep-signal subset (and later any
candidates).** `market_rules.py` makes one API call per ticker, so do NOT fetch
rules for all ~200; fetch them for the deep-signal subset now, and (in Step 5)
for any market that becomes a candidate:

```bash
KALSHI_ENV=prod PYTHONPATH=. .venv/bin/python scripts/market_rules.py \
  --tickers SUBSET_TICKER1 SUBSET_TICKER2 ... > /tmp/rules_${TS}.json
```

Read `/tmp/rules_${TS}.json` ({ticker: {rules_primary, subtitle, ...}}). Carry
each market's `rules_primary` forward — Step 2 passes it to the cross-venue
agents, and Step 5 checks it.

**Live-price the deep-signal subset.** The snapshot is the market *universe* only;
its prices may be stale. Every market we evaluate deeply (and might recommend)
must be priced from the live API, not the snapshot. Fetch live top-of-book for
the **same subset tickers** now:

```bash
KALSHI_ENV=prod PYTHONPATH=. .venv/bin/python scripts/live_prices.py \
  --tickers SUBSET_TICKER1 SUBSET_TICKER2 ... > /tmp/live_prices_${TS}.json
```

Read `/tmp/live_prices_${TS}.json` ({ticker: {yes_bid, yes_ask, last_price}}).
**Immediately override each subset market's `yes_bid`/`yes_ask` with these live
values right now** — before dispatching any signal agents in Step 2. A ticker
mapping to nulls is illiquid/unquoted — drop it from the subset rather than run
agents on a stale price. Carry the updated prices forward into Step 2 (agent
dispatch) and Step 3 (signals file). This guarantees signal agents see the live
market price, not the potentially-stale scout snapshot.

If the file is empty or missing, log and stop:

```bash
.venv/bin/python scripts/ui_log.py "Orchestrator: no scoreable markets — stopping" warning
```

---

## Step 2 — Dispatch signal agents in parallel

Process the **deep-signal subset** (≤ ~20) in **batches of 3 markets**. For each
batch, spawn all applicable signal agents **in a single message** (multiple
`Agent` tool calls) so they run concurrently. Wait for the whole batch before
starting the next. Batching is what keeps concurrent API load bounded — now that
`market-maker-signal` runs on every market, each batch already spawns 6+ parallel
agents. Keep batches small so each one completes quickly. The rest of the coverage
set rides on its deterministic scout signals.

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
| `polymarket-price-signal` | ticker, title, midpoint = live `yes_ask` from `/tmp/live_prices_${TS}.json` (fallback: scout row `yes_ask`), hours_to_close |

**Conditional (only when it applies — keeps dispatch load bounded):**

- `sportsbook-odds-signal` — for **sports** markets (category sports, or ticker
  contains a league like WTA/ATP/NBA/NHL/MLB/NFL/UFC); args: ticker, title.
  This is the sharpest *independent* signal for sports — prioritize it there.
- `polymarket-whale-signal` — only if `volume_24h > 5000`; args: ticker, title
- `weather-signal` — only if `category` contains "weather" or "climate"; args: ticker, title
- `mentions-signal` — for **"mentions"** markets (category `mentions`, or the title
  asks whether a person will *say/mention/utter* a word/phrase in a hearing,
  briefing, floor speech, or press conference); args: ticker, title. Independent
  base-rate signal (GDELT TV captions) for a family that otherwise has no
  independent source.
- `polls-signal` — **disabled** (`agent_polls_enabled: false` in `runtime_config.json`).
  Returns no data outside a U.S. general-election cycle. Re-enable in fall 2026
  when 538 has live polling for federal races.
- `x-signal` — **disabled** (`agent_x_enabled: false` in `runtime_config.json`). Do not dispatch.
- `market-maker-signal` — **every market in the deep-signal subset**; args:
  ticker, title. This is an orderbook-snapshot signal — it does not require
  trade history and is the highest-value signal we have. Dispatch it for every
  market in every batch, no volume gate.
- `order-flow-signal` — only if `volume_24h > 5000` (reads live trade tape;
  returns empty on thin markets); args: ticker, title

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

Build the signals file for the **entire coverage set (~200 markets)**, not just
the subset: start each market from its scout `signal_estimates`, and for the
deep-signal subset additionally append the agent estimates you collected. Markets
outside the subset simply carry their two deterministic scout signals — that's
fine; they still get scored and (if 2+ sources) recorded for the backtest.

**Confirm live prices.** Subset markets already have their `yes_bid`/`yes_ask`
updated from `/tmp/live_prices_${TS}.json` (done at the end of Step 1, before
agent dispatch). Write these same live values into the signals file — do not
fall back to the scout row prices for subset markets. Coverage-set markets
outside the subset keep snapshot prices (recorded for calibration only, never
recommended).

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
    "volume_24h": 12708,
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

First, **fetch settlement rules for any surviving candidate not already in the
deep-signal subset** (so every candidate has `rules_primary` for question 1):

```bash
KALSHI_ENV=prod PYTHONPATH=. .venv/bin/python scripts/market_rules.py \
  --tickers CANDIDATE_TICKER... >> /tmp/rules_${TS}.json   # merge/extend
```

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
to market on later cycles; no execution). Record the risk-approved slate first
(disposition `approved`), then record **all other** scored 2+ source markets
(disposition `worth_trading` if they cleared the edge bar but risk/challenge
dropped them, else `insufficient_edge`). Recording the rejected ones too lets us
mark them to market and judge whether the 5¢ edge bar is set correctly —
`paper_track.py report --by-edge-bucket --by-disposition` reads it back:

```bash
PYTHONPATH=. .venv/bin/python scripts/paper_track.py record \
  --ideas-file reports/orchestrator-${TS}.json --cycle-ts ${TS} \
  --disposition approved || true
PYTHONPATH=. .venv/bin/python scripts/paper_track.py record-scored \
  --scored-file /tmp/scored_${TS}.json --cycle-ts ${TS} \
  --exclude-file reports/orchestrator-${TS}.json --min-sources 2 || true
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

## Step 10 — Mark prior recommendations (trailing, lowest priority)

**Only after** publishing, mark prior recommendations to current market prices.
This is the **lowest-priority** step and must never delay analysis: it runs
last, and **if the cycle is already running long, skip it** — the next cycle
catches up (marks being a little late is fine).

Source open recommendations from **Supabase** with `--from-supabase` so ideas
recorded on *any* machine get marked (the dashboard reads Supabase; local-only
marking misses ideas recorded elsewhere). Bound the work with
`--max-age-minutes` so it re-prices only recently-recorded ideas (toward the
5/15/30/60/120-minute checkpoints) rather than the whole open book:

```bash
KALSHI_ENV=prod PYTHONPATH=. .venv/bin/python \
  scripts/paper_track.py mark --from-supabase --max-age-minutes 130 || true
```

Read-only; never executes trades. This is what keeps the Ideas History P&L
timeline populated.

---

## Running on a cadence

This skill runs **one** cycle. To repeat it on a schedule, the user runs:

```
/loop 20m /orchestrate
```

Do not build a sleep loop yourself — `/loop` owns the cadence and keeps the user
in control of starting and stopping it.
