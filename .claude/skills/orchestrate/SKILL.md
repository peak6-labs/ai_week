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
- Collect every signal with the **signal agents** (`market-maker-signal`,
  `weather-signal`, `mentions-signal`, `order-flow-signal`,
  `polymarket-whale-signal`, `sportsbook-odds-signal`, `polls-signal`).
  `polymarket-price-signal` and `x-signal` are **disabled** — do not dispatch.
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
# run from the repo root (your project checkout — do not hard-code an absolute path)TS=$(date -u +%Y%m%dT%H%M%SZ)
echo "TS=$TS"
CYCLE=$(( $(wc -l < reports/cycle-log.txt 2>/dev/null || echo 0) + 1 ))
.venv/bin/python scripts/ui_log.py "Orchestrator: cycle $CYCLE started (TS=$TS)"
.venv/bin/python scripts/ui_state.py "{\"cycle_number\": $CYCLE, \"last_cycle_at\": \"$(date -u +%Y-%m-%dT%H:%M:%SZ)\"}"

# Top up the mentions archive for any source past its TTL (cheap no-op when warm;
# only stale sources fetch). Keeps the speaker-attributed corpus + hearing schedule
# fresh for this cycle's mentions markets. Fail-soft — never blocks the cycle.
.venv/bin/python -m kalshi_trader.refresh_mentions_archive --if-stale || true
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

**Dispatch external agents to a focused subset only.** External signal agents
are expensive — cap the subset at **≤ 20 markets** to keep wall time bounded.

- **Deep-signal subset (≤ 20)** = the highest-priority rows for external agent
  dispatch. Select by this priority order until you hit 20:
  1. All weather/climate markets (NOAA is independent and cheap)
  2. All mentions markets (GDELT base-rate)
  3. All sports markets with game-outcome resolution (sportsbook odds)
  4. All markets with `volume_24h > 5000` (OFI eligible)
  5. Top rows by `average_score` to fill remaining slots
  Only this subset gets Step 2's agent dispatch, Steps 3–4 scoring, and
  the adversarial challenge.

For each row compute `hours_to_close` from `close_time`; use `best_market_ticker`
as the tradeable `ticker`.

**Each scout row already carries a `signal_estimates` list** — the deterministic
`microstructure` (directional price/volume/orderbook) and `kalshi_bias`
(calibration) signals, computed during the scan with no extra calls. Keep these;
Step 2 only needs to add the signals that require live lookups or judgment.

**Fetch settlement rules and live prices in parallel** — run both scripts
simultaneously in one Bash call so neither blocks the other:

```bash
KALSHI_ENV=prod PYTHONPATH=. .venv/bin/python scripts/market_rules.py \
  --tickers SUBSET_TICKER1 SUBSET_TICKER2 ... > /tmp/rules_${TS}.json &
KALSHI_ENV=prod PYTHONPATH=. .venv/bin/python scripts/live_prices.py \
  --tickers SUBSET_TICKER1 SUBSET_TICKER2 ... > /tmp/live_prices_${TS}.json &
wait
```

Read both output files. From `/tmp/rules_${TS}.json` carry each market's
`rules_primary` and `settlement_sources` forward — Step 2 passes them to the
settlement-sensitive agents. From `/tmp/live_prices_${TS}.json` **immediately
override each subset market's `yes_bid`/`yes_ask`** before dispatching agents.
A ticker mapping to nulls is illiquid — drop it from the subset.

If the file is empty or missing, log and stop:

```bash
.venv/bin/python scripts/ui_log.py "Orchestrator: no scoreable markets — stopping" warning
```

---

## Step 2 — Dispatch signal agents in parallel

**Dispatch all subset markets in one single parallel message** — one `Agent` tool
call per applicable agent, all firing concurrently. Do NOT batch sequentially; the
rate-limit concern that motivated batches of 3 was x-signal and polymarket-price,
both of which are now disabled. Weather/MM/OFI/mentions hitting their respective
APIs concurrently is safe and reduces total wall time from O(batches × latency)
to a single round-trip.

Log before dispatch:

```bash
.venv/bin/python scripts/ui_log.py "Orchestrator: collecting signals for TICKER1, TICKER2, ... (N markets)"
```

`microstructure` and `kalshi_bias` already come from the scout row — **do not
dispatch agents for those.** Dispatch only the signals that need a live lookup or
judgment, and prefer independent sources (they corroborate the price-derived
scout signals, which are correlated with each other):

**Dispatch for every market in the batch:**

| Agent | Args to pass in the prompt |
|-------|----------------------------|
| `market-maker-signal` | ticker, title, **YES_BID** and **YES_ASK** from `/tmp/live_prices_${TS}.json` (extract per ticker). These anchor the probability to the same price the scorer uses, preventing stale-price artifacts. |

**Conditional (only when it applies — keeps dispatch load bounded):**

For `weather-signal`, `mentions-signal`, and `polls-signal`, extract the market's
settlement context from `/tmp/rules_${TS}.json` and pass it as `SETTLEMENT_JSON`:

```python
import json
rules = json.load(open(f'/tmp/rules_{TS}.json'))
settlement_json = json.dumps(rules.get('TICKER', {}))  # substitute real ticker
```

Pass this as the `SETTLEMENT_JSON` arg in the agent prompt. The agent forwards
it to the pipeline's `--settlement-json` flag so it forecasts/counts against the
contract's actual settlement source — not just the default (e.g. AccuWeather, not
NOAA; prepared remarks only, not Q&A).

- `mentions-signal` — for **"mentions"** markets (category `mentions`, or the title
  asks whether a person will *say/mention/utter* a word/phrase in a hearing,
  briefing, floor speech, or press conference); args: ticker, title, SETTLEMENT_JSON.
  **Always dispatch for mentions markets** — **except** Love Island mentions
  (`KXLOVEISLMENTION*`), which go to `love-island-signal` instead (GDELT has zero
  entertainment coverage and returns empty).
- `love-island-signal` — for **Love Island** markets: ticker starts with
  `KXLOVEISL`, `KXLIUSA`, or `KXLIUK` (covers bombshell/Casa Amor binaries,
  eliminations, winners/couples/rankings, and the `KXLOVEISLMENTION*` mentions
  market), **or** the title contains "Love Island"; args: ticker, title, category.
  Uses official pre-episode YouTube teasers + Grok X sentiment — no settlement
  context needed.
- `weather-signal` — only if `category` contains "weather" or "climate";
  args: ticker, title, SETTLEMENT_JSON. The pipeline now uses GEFS ensemble
  (empirical-CDF) + X meteorologist authority as a second source — these run
  internally; you just pass the same args. The settlement context ensures it
  measures against the contract's actual station/provider.
- `polls-signal` — for **federal elections only** (senate, house, governor,
  generic-ballot — category `elections` and a federal race in the title);
  args: ticker, title, SETTLEMENT_JSON. Returns empty cleanly for races 538 does
  not cover (primaries, local offices, ballot measures) — no mismatch risk.
  Do not dispatch for mayoral, runoff, or international races.
- `sportsbook-odds-signal` — for **sports** markets (category sports, or ticker
  contains a league like WTA/ATP/NBA/NHL/MLB/NFL/UFC); args: ticker, title,
  plus `rules_primary` and `settlement_sources` from the rules file in the prompt
  so the agent only signals when the external contract resolves on the same criterion.
- `order-flow-signal` — only if `volume_24h > 5000`; args: ticker, title.
  Below 5k vol the trade tape is too sparse for reliable OFI direction — VPIN
  elevates but stays neutral, adding noise without edge.
- `polymarket-whale-signal` — **disabled**. Returned 0 real signals across all
  test cycles; the markets we score don't have Polymarket whale coverage.
- `polymarket-price-signal` — **disabled**. Do not dispatch.
- `x-signal` — **disabled** (`agent_x_enabled: false`). Do not dispatch.

Settlement context routing summary (for reference):

| Agent | Settlement context |
|-------|-------------------|
| `weather-signal`, `mentions-signal`, `polls-signal` | pass as `SETTLEMENT_JSON` |
| `sportsbook-odds-signal` | include `rules_primary` + `settlement_sources` in prompt |
| `order-flow`, `market-maker`, scout signals | none needed — price/flow-derived |

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
`metadata`. For every market in the **deep-signal subset**, build one
`signal_estimates` list by **starting with the scout row's `signal_estimates`**
(microstructure + kalshi_bias) and **appending every estimate from the agents
you dispatched** (an agent may return several — keep them all; each is a
source). Do **not** unwrap to the `metadata` field — the scorer combines the
estimates' own `probability`/`uncertainty`/`weight` directly.

Build the signals file for the **deep-signal subset only** (~40 markets). There
is no need to carry the remaining ~160 coverage-set markets through scoring —
they cannot become candidates and omitting them cuts scoring time significantly.

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
# run from the repo root (your project checkout — do not hard-code an absolute path)PYTHONPATH=. .venv/bin/python scripts/score_signals.py \
  --signals-file /tmp/signals_${TS}.json \
  --config runtime_config.json > /tmp/scored_${TS}.json
.venv/bin/python scripts/ui_log.py "Orchestrator: deterministic scoring complete"
```

Read `/tmp/scored_${TS}.json`. Each entry has `ticker`, `title`, `category`,
`yes_ask`, `hours_to_close`, `combined_probability`, `uncertainty`, `n_sources`,
`sources`, `edge_cents`, `fee_adjusted_edge`, `worth_trading`, `kelly_fraction`,
and `side`.

Keep only markets where **`worth_trading == true`**.
Log the survivor count.

*(persist_cycle.py runs in the trailing step after publish — do not run it here.)*

---

## Step 5 — Adversarial challenge

All candidates were in the deep-signal subset, so `rules_primary` and
`settlement_sources` are already in `/tmp/rules_${TS}.json`. Use that text —
do **not** download or read contract-terms PDFs; the round-trip cost exceeds
the benefit for most markets.

For each surviving market, answer these five questions before letting it onto the
candidate slate:

1. **Settlement rule** — read the market's `rules_primary` and
   `settlement_sources` from `/tmp/rules_${TS}.json`. Does it actually resolve
   on what the title implies, on the source the signals measured? Check the
   signal metadata's `data_quality` field: a signal with
   `data_quality: unavailable` or `data_quality: stale` (>120 min for weather)
   contributed a floor/fallback value — treat it as absent. If the rule has a
   twist the signals didn't account for (wrong source, wrong threshold,
   determination delay), drop the market.
2. **Bear case** — what specific mechanism makes the signal wrong?
3. **Source independence** — three paths to pass:
   - **External path** (lower bar, 5¢): an independent external signal
     (weather/sportsbook/mentions) agrees with the direction. Microstructure +
     kalshi_bias alone are price-derived and correlated — they do not satisfy
     this path. fee_adjusted_edge ≥ 5¢.
   - **Internal path** (higher bar, 8¢): `market_maker.direction` ∈ {YES, NO}
     (not neutral) **and** `order_flow.ofi_direction` ∈ {YES, NO} (not neutral)
     **and** both point the same direction. These two sources are orthogonal —
     the order book snapshot and the trade tape are different data streams. If
     `ofi_score = 0.0` while `buying_fraction` is extreme (normalization bug in
     thin markets), treat OFI direction as absent. Compute:
     ```
     effective_edge = fee_adjusted_edge + actionability_score × 5
     ```
     Pass if effective_edge ≥ 8¢.
   - **Directional-book path** (medium bar, 10¢): `market_maker.direction` ∈
     {YES, NO} (not neutral) **and** `|depth_imbalance| > 0.4` (a skew this
     strong is unlikely to be noise). No OFI required. The order book snapshot
     adds genuine information beyond price history when the imbalance is this
     lopsided. Compute effective_edge as above. Pass if effective_edge ≥ 10¢.
     Note: verify the MM probability was anchored to live prices (YES_BID/YES_ASK
     passed correctly) — if the agent's reported spread is >3× the live spread
     (ask−bid), the probability may be stale; in that case require effective_edge
     ≥ 15¢ as a conservatism buffer.
   If no path is satisfied, fail on source independence.
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

**Keep only ideas where `approved == true`.** These are the final slate.

**Proportional Kelly sizing (small-account correction).** The risk script's raw
`approved_size_dollars` often hits the $25 floor for all ideas simultaneously
because half-Kelly dollar amounts vastly exceed the per-trade cap on a sub-$1000
account. Raw Kelly is not useful when the cap is binding for every trade. Instead,
scale proportionally across the approved slate so the strongest idea gets the cap
and weaker ideas scale down:

```python
import math

cap = 25.0          # hard per-trade ceiling
balance = BALANCE   # live balance

for idea in approved_ideas:
    p = idea["confidence"]          # probability of winning
    q = 1 - p
    cost = idea["market_price"] / 100.0   # cost per $1 of payout
    b = (1 - cost) / cost           # net odds (payout per dollar staked)
    full_kelly = max(0, (p * b - q) / b)
    idea["_half_kelly"] = full_kelly / 2

max_hk = max(idea["_half_kelly"] for idea in approved_ideas) or 1
for idea in approved_ideas:
    norm = idea["_half_kelly"] / max_hk
    idea["suggested_size_dollars"] = max(1, round(norm * cap))
```

This gives the highest-conviction idea the full cap ($25) and scales others down
proportionally. If only one idea survives, it gets the full cap. Never size below
$1. Log:

```bash
.venv/bin/python scripts/ui_log.py "Orchestrator: risk approved K of N ideas"
```

**Immediately write the slate and push to Ideas History** — do this now, before
any other post-processing, so ideas appear in the UI while they are still
actionable:

```bash
mkdir -p reports
```

Write **`reports/orchestrator-${TS}.json`** with the **Write** tool — the
approved slate, one object per idea: `ticker`, `side`, `confidence`,
`market_price`, `suggested_size_dollars`, `reasoning`, `signal_sources`,
`category`, `agent_id`, `selection_summary`.

**Record to Ideas History immediately:**

```bash
PYTHONPATH=. .venv/bin/python scripts/paper_track.py record \
  --ideas-file reports/orchestrator-${TS}.json --cycle-ts ${TS} \
  --disposition approved || true
```

Then dispatch the **`idea-publisher`** agent right now (do not wait for Step 9):

> Publish the approved trade ideas to the dashboard.
> `IDEAS_FILE=reports/orchestrator-<TS>.json`.

---

## Step 8 — Write remaining outputs

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

## Step 9 — Log completion

```bash
# run from the repo root (your project checkout — do not hard-code an absolute path).venv/bin/python scripts/ui_log.py "Orchestrator: cycle complete — N markets, K approved ideas"
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

**Also persist this cycle's scored markets to Supabase** (kept here so it never
delays publishing):

```bash
PYTHONPATH=. .venv/bin/python scripts/persist_cycle.py \
  --scout-file /tmp/market_scout_${TS}.json \
  --scored-file /tmp/scored_${TS}.json --cycle-ts ${TS} || true
```

Read-only and best-effort; never blocks.

---

## Running on a cadence

This skill runs **one** cycle. To repeat it on a schedule, the user runs:

```
/loop 20m /orchestrate
```

Do not build a sleep loop yourself — `/loop` owns the cadence and keeps the user
in control of starting and stopping it.
