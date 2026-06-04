---
date: 2026-06-04T07:26:37-05:00
researcher: Alexandra Lewis
git_commit: 3ce57f11b0959fe6fd5a77ee7a7833ecb938238d
branch: lle-bot-improvements
repository: peak6-labs/ai_week
topic: "Current state of the codebase + a full start-to-finish trace through the orchestrator"
tags: [research, codebase, orchestrate, pipeline, agents, signals, risk, portfolio, night-mode, dashboard]
status: complete
last_updated: 2026-06-04
last_updated_by: Alexandra Lewis
---

# Research: Current State & Full Orchestrator Trace

**Date**: 2026-06-04T07:26:37-05:00
**Researcher**: Alexandra Lewis
**Git Commit**: 3ce57f11b0959fe6fd5a77ee7a7833ecb938238d
**Branch**: lle-bot-improvements
**Repository**: peak6-labs/ai_week

## Research Question

What is the current state of the codebase? Describe succinctly how a full trace would run from start to finish via the orchestrator.

## Summary

The system is an **agentic Kalshi prediction-market pipeline**: the [orchestrate skill](../../../.claude/skills/orchestrate/SKILL.md) runs one cycle from the main conversation context, scanning the live board, collecting signals from a roster of specialist sub-agents, doing all probability/edge/Kelly math in deterministic Python, gating each survivor through a five-question adversarial challenge, applying risk sizing, and publishing surviving ideas to a FastAPI dashboard for human review.

Since the [2026-06-03 agent-structure research](2026-06-03-agent-structure-trade-idea-evaluation.md) (commit `4cefa83`), the pipeline has grown from a 9-step *read-only* idea generator into a **12-stage flow with a position-management front end and an autonomous execution back end**. The headline changes on the current `lle-bot-improvements` branch:

- **It is no longer purely read-only.** Two new front-end steps place *real exit orders* on open Kalshi positions (`Step 0.5` automatically on stop-loss/profit-target; `Step 0.75` after user y/n approval). A separate standalone `night_execute.py` places *real entry orders* under a deterministic rule engine with a $100/10-trade nightly session cap. The new-idea path (Steps 1–9) is still paper-only — those ideas go to the dashboard, never to `create_order`.
- **Three signal agents were disabled**: `x-signal`, `polymarket-price-signal`, `polymarket-whale-signal`. The active roster is `market-maker`, `weather`, `mentions`, `polls`, `sportsbook-odds`, `order-flow` (plus the two free deterministic scout signals `microstructure` + `kalshi_bias`).
- **Live-pricing + contract-PDF steps were added.** The deep-signal subset is re-priced from the live API (`live_prices.py`) before agents run, and survivors' full contract-terms PDFs are downloaded (`contract_terms_doc.py`) and read during the adversarial challenge.
- **The mentions signal was rebuilt** into a multi-source speaker-routed pipeline (speaker registry + SQLite archive + nightly TTL refresh) emitting up to four sources.
- **The weather signal** moved to a GEFS-ensemble empirical-CDF model with a same-day live-observation override and an X-meteorologist second source.
- **Adversarial reasoning is now stored and displayed** in the ideas-history view.

A still-true documentation fact: the SKILL.md hardcodes the repo root as `/Users/scorley/code` (a different developer's checkout). The Python module paths are correct; only the literal `cd` prefix differs from this machine's `/Users/llewis/ai_week`.

---

## The full trace (start → finish)

Invoked by `/orchestrate` (one cycle); repeated via `/loop 20m /orchestrate`. Every output path is keyed by `TS=$(date -u +%Y%m%dT%H%M%SZ)`.

```
Step 0    Setup ───────────── TS, CYCLE, telemetry, refresh mentions archive, read live BALANCE
Step 0.5  Portfolio exits ─── evaluate_portfolio.py (prod) → REAL sell orders on stop-loss/profit triggers
Step 0.75 AI position review ─ position-reviewer agent → prompt user y/n → REAL exits (pauses for input)
Step 1    market-scout ────── score_markets.py --json → ~200 event rows; pick ≤40 deep subset; rules + live prices
Step 2    signal agents ───── ONE parallel dispatch of applicable agents over the subset
Step 3    signals file ────── build /tmp/signals_TS.json for the whole coverage set
Step 4    score_signals.py ── combine → edge → half-Kelly → worth_trading filter; persist to Supabase
Step 5    adversarial ─────── Claude 5-question gate (reads contract PDFs); 3 source-independence paths
Step 6    candidates ──────── /tmp/candidates_TS.json with yes/no axis flipped to chosen side
Step 7    risk agent ──────── run_risk.py → RiskManager hard limits + half-Kelly sizing; keep approved
Step 8    write outputs ───── reports/orchestrator-TS.{json,md}; record paper recs (approved + rejected)
Step 9    idea-publisher ──── POST surviving slate to dashboard /api/ideas (paper, human-reviewed)
Step 10   mark prior recs ─── trailing/lowest-priority paper_track mark --from-supabase (read-only P&L)
```

Data handed stage-to-stage (all in `/tmp`, keyed by `TS`, except the persisted reports):

```
live_markets.json ─scout→ market_scout_TS.json ─┬─live_prices_TS.json─┐
                                                 ├─rules_TS.json───────┤
                                  signal agents ─┘                     ▼
                                          signals_TS.json ─score→ scored_TS.json
                                          ─adversarial→ candidates_TS.json ─risk→ risk_TS.json
                                          ─→ reports/orchestrator-TS.json ─publish→ dashboard pending_ideas
                                                                                    └→ human approve/reject → Supabase reviewed_ideas (paper_only)
```

### Step 0 — Setup ([SKILL.md:39-75](../../../.claude/skills/orchestrate/SKILL.md#L39))
Stamp `TS`; `CYCLE` = line count of `reports/cycle-log.txt` + 1; fire telemetry via `scripts/ui_log.py` / `scripts/ui_state.py` (both swallow all errors so the UI being down never blocks the cycle). Top up the mentions corpus with `python -m kalshi_trader.refresh_mentions_archive --if-stale` (cheap no-op when warm). Read the sizing bankroll **live** from `curl http://localhost:8000/api/state` → `balance_dollars`, falling back to `KALSHI_BALANCE`, then `1000`.

### Step 0.5 — Portfolio exits ([SKILL.md:79-106](../../../.claude/skills/orchestrate/SKILL.md#L79))
`KALSHI_ENV=prod ... scripts/evaluate_portfolio.py --out /tmp/portfolio_eval_TS.json` (no `--dry-run`). For each open position it runs `kalshi_trader/portfolio_checks.py`: `check_stop_loss` (value < 75% of cost basis) then `check_profit_target` (value > entry + 75%·distance-to-certainty). **Triggered positions get a real limit sell order** (`evaluate_portfolio.py:158` `if not dry_run:` → `client.create_order(action="sell", ...)`). Output carries `exits[]`, `clean_positions[]`, `errors[]`. Never blocks Step 1.

### Step 0.75 — AI position review ([SKILL.md:110-134](../../../.claude/skills/orchestrate/SKILL.md#L110))
Runs only for `clean_positions` with `market_exposure_dollars >= 2.0`. Follows the `/portfolio` skill's pattern: fetch rules → collect signals (batched at 3) → dispatch the **`position-reviewer`** agent ([.claude/agents/position-reviewer.md](../../../.claude/agents/position-reviewer.md), sonnet + WebSearch, *does not place orders*) → present exit/hold/add recommendations → **prompt the user y/n** → place approved exits via `scripts/exit_position.py`. **This step pauses for user input.**

### Step 1 — market-scout ([SKILL.md:138-229](../../../.claude/skills/orchestrate/SKILL.md#L138))
Dispatch the **`market-scout`** agent ([.claude/agents/market-scout.md](../../../.claude/agents/market-scout.md), sonnet) in pipeline mode: it runs `KALSHI_ENV=prod ... scripts/score_markets.py --json --markets-file live_markets.json` and writes `/tmp/market_scout_TS.json` (~200 event rows sorted by `average_score`). It is forbidden from refreshing the snapshot. The actionability score is the weighted-9-signal screening score from [kalshi_trader/actionability/scorer.py](../../../kalshi_trader/actionability/scorer.py), and **each row already carries two free deterministic `signal_estimates` (`microstructure` + `kalshi_bias`)** computed at scan time ([kalshi_trader/grouping.py](../../../kalshi_trader/grouping.py)).

The orchestrator then:
- **Coverage set = all ~200 rows** (scored + recorded for calibration).
- **Deep-signal subset ≤ ~40**, chosen by priority: all weather/climate → all game-outcome sports → all mentions → all `volume_24h > 5000` → top remaining by `average_score`.
- Fetch settlement rules for the subset: `scripts/market_rules.py --tickers ... > /tmp/rules_TS.json` (deduped per series, cached in `series_contract_terms.json`).
- **Live-price the subset**: `scripts/live_prices.py --tickers ... > /tmp/live_prices_TS.json`, then **immediately override each subset market's `yes_bid`/`yes_ask`** so signal agents see live prices, not the stale snapshot. Tickers mapping to nulls are dropped as illiquid.

### Step 2 — signal agents in parallel ([SKILL.md:233-322](../../../.claude/skills/orchestrate/SKILL.md#L233))
**One single parallel message**, one `Agent` call per applicable agent across all subset markets (sequential batching is no longer needed now that the rate-limited agents are disabled). `microstructure`/`kalshi_bias` are NOT dispatched (they ride every scout row). Dispatch matrix:

| Agent | When | Args |
|---|---|---|
| `market-maker-signal` | every subset market | ticker, title, live YES_BID/YES_ASK |
| `weather-signal` | category ~ weather/climate | + SETTLEMENT_JSON (GEFS ensemble + X-meteorologist authority) |
| `mentions-signal` | "mentions" markets | + SETTLEMENT_JSON (multi-source speaker-routed) |
| `polls-signal` | federal elections only | + SETTLEMENT_JSON (538) |
| `sportsbook-odds-signal` | sports markets | + rules_primary, settlement_sources |
| `order-flow-signal` | `volume_24h > 5000` | ticker, title (OFI/VPIN) |
| `polymarket-price`, `polymarket-whale`, `x-signal` | **DISABLED** | — |

Each agent shells out to a `kalshi_trader.pipelines.*` CLI and returns a JSON array of `SignalEstimate` objects (`[]` = no signal, never fabricated). Agent statuses are pushed to the UI (`running` → `idle` + `last_signal_count`).

### Step 3 — build the signals file ([SKILL.md:326-377](../../../.claude/skills/orchestrate/SKILL.md#L326))
Write `/tmp/signals_TS.json` for the **whole coverage set**: each market starts from its scout `signal_estimates` and the subset markets additionally append every estimate the agents returned. Subset markets carry their live `yes_bid`/`yes_ask`; non-subset markets keep snapshot prices (recorded for calibration only).

### Step 4 — deterministic scoring ([SKILL.md:381-405](../../../.claude/skills/orchestrate/SKILL.md#L381))
`scripts/score_signals.py --signals-file /tmp/signals_TS.json --config runtime_config.json > /tmp/scored_TS.json`. All the math lives here:
- `combine_signals()` ([score_signals.py:227-282](../../../scripts/score_signals.py#L227)) — staleness-decayed weighted average, `eff_w = weight·exp(-age_minutes/360)`; disagreement penalty (`uncertainty += spread·0.5` when prob spread > 0.10); optional agreement boost (×0.85 uncertainty when ≥2 independent sources agree within 3%).
- `compute_edge_and_kelly()` ([score_signals.py:285-356](../../../scripts/score_signals.py#L285)) — side selection, `edge_cents = side_prob·100 − side_price·100`, Kalshi fee `0.07·price·(1−price)·100`, `fee_adjusted_edge`, half-Kelly `max(0, ((p·b−q)/b)·0.5)`, and `worth_trading = fee_adjusted_edge > min_edge_cents (5.0) AND entry_price ≤ max_entry_price_cents (90.0)`.

Keep `worth_trading == true`. Then `scripts/persist_cycle.py` upserts `scored_markets` + `cycles` to Supabase (best-effort).

### Step 5 — adversarial challenge ([SKILL.md:409-480](../../../.claude/skills/orchestrate/SKILL.md#L409))
**Claude reasoning, not a script.** Fetch rules for any new candidate, then `scripts/contract_terms_doc.py` downloads each survivor series' contract-terms PDF to `/tmp/contract_terms_<series>.pdf`, which the Read tool parses natively. Five questions gate each survivor:
1. **Settlement rule** — does it resolve on what the title/signals imply, given `rules_primary` + the PDF mechanics (strict `>` vs `>=`, determination-delay clauses, first-report-governs, exact expiry, source hierarchy)? Signals tagged `data_quality: unavailable/stale` are treated as absent.
2. **Bear case** — what mechanism makes the signal wrong?
3. **Source independence** — three pass paths: **External (5¢)** an independent external signal agrees; **Internal (8¢)** MM direction and OFI direction both non-neutral and agree, `effective_edge = fee_adjusted_edge + actionability·5 ≥ 8`; **Directional-book (10¢)** MM direction non-neutral and `|depth_imbalance| > 0.4`. (Microstructure + kalshi_bias alone never satisfy this — they're price-derived/correlated.)
4. **Base rate** — does history support the direction?
5. **Fresh-eyes test** — would you act with no prior conviction?

### Step 6 — build candidates / axis flip ([SKILL.md:484-511](../../../.claude/skills/orchestrate/SKILL.md#L484))
Write `/tmp/candidates_TS.json`, flipping fields to the chosen side so the risk script measures `edge = confidence − market_price/100`: **YES** → `confidence = combined_probability`, `market_price = yes_ask`; **NO** → `confidence = 1 − combined_probability`, `market_price = 100 − yes_bid`.

### Step 7 — risk filter ([SKILL.md:515-533](../../../.claude/skills/orchestrate/SKILL.md#L515))
Dispatch the **`risk`** agent ([.claude/agents/risk.md](../../../.claude/agents/risk.md), sonnet) → `scripts/run_risk.py --ideas-file /tmp/candidates_TS.json --balance BALANCE`. `RiskManager.check_trade` ([kalshi_trader/risk.py:32-79](../../../kalshi_trader/risk.py#L32)) applies hard limits (daily loss, total exposure $400, per-category $250, settlement < 2h, min edge 5¢), sizes via half-Kelly (quarter-Kelly after 3 consecutive category losses), clamps to caps and a $100 max / $10 min, and adds `approved`, `approved_size_dollars`, `rejection_reason`. Keep `approved == true`; set `suggested_size_dollars = approved_size_dollars`.

### Step 8 — write outputs ([SKILL.md:537-579](../../../.claude/skills/orchestrate/SKILL.md#L537))
Write `reports/orchestrator-TS.json` (approved slate) and `reports/orchestrator-TS.md` (human table). Record paper recommendations for the calibration loop: `paper_track.py record --disposition approved` for the slate, then `paper_track.py record-scored --min-sources 2` for all other scored markets (so the 5¢ edge bar can be back-tested). Write `/tmp/recent_TS.json` and push it via `ui_state.py` so the Monitor tab shows all risk-checked candidates (approved + rejected).

### Step 9 — publish ([SKILL.md:583-599](../../../.claude/skills/orchestrate/SKILL.md#L583))
If the slate is non-empty, dispatch the **`idea-publisher`** agent ([.claude/agents/idea-publisher.md](../../../.claude/agents/idea-publisher.md), haiku) → `POST http://localhost:8000/api/ideas`. Log "cycle complete" and append a line to `reports/cycle-log.txt`. Return a short summary (markets evaluated, candidates, ideas after challenge, ideas approved, top idea).

### Step 10 — mark prior recommendations ([SKILL.md:603-622](../../../.claude/skills/orchestrate/SKILL.md#L603))
Trailing, lowest-priority, **skipped if the cycle is running long**: `paper_track.py mark --from-supabase --max-age-minutes 130` re-prices recently-recorded ideas (read-only) to keep the Ideas-History P&L timeline populated.

### Where ideas land
Ideas POSTed in Step 9 enter `state.pending_ideas` ([kalshi_trader/ui/state.py](../../../kalshi_trader/ui/state.py)), render in the Trade Ideas tab, and a human clicks approve/reject → moves to `reviewed_ideas` and persists to Supabase with **`paper_only=True` always** ([kalshi_trader/db.py:218](../../../kalshi_trader/db.py#L218)). No order is placed for new ideas.

---

## Order-placement reality (important nuance)

The 2026-06-03 doc described the pipeline as "read-only by construction." That is now true only of the **new-idea path**. Current state:

| Path | Script | Places real order? | Side | Guard |
|---|---|---|---|---|
| Step 0.5 portfolio exits | `evaluate_portfolio.py` | **Yes** (prod) | sell | `if not dry_run:` ([:158](../../../scripts/evaluate_portfolio.py#L158)); orchestrator passes no `--dry-run` |
| Step 0.75 AI review exits | `exit_position.py` | **Yes** (prod) | sell | requires user y/n approval |
| Steps 1–9 new ideas | orchestrate pipeline | **No** — paper only | — | published to dashboard; no `create_order` |
| Night mode (standalone) | `night_execute.py` | **Yes** (prod) | buy | `if not dry_run:` ([:190](../../../scripts/night_execute.py#L190)) |

**Night mode** ([scripts/night_execute.py](../../../scripts/night_execute.py)) is a *separate standalone entry point*, not part of `/orchestrate`. It reads a candidates file and places flat **$10 limit-buy** orders for candidates that pass a deterministic rule engine: session cap (≤10 trades, ≤$100/day, tracked in `reports/night-mode-session-YYYYMMDD.json`, shared with live-island sessions), Love Island exclusion, 5¢ edge gate, unquoted guard, a **2h settlement gate (all categories)**, and a **12h same-day gate (weather/climate)**. The executor itself ([kalshi_trader/executor.py](../../../kalshi_trader/executor.py)) is only ever instantiated by these three standalone scripts — never by the orchestrate pipeline.

---

## Component inventory (current)

### Agents (`.claude/agents/`, 13 files)
- **Entry:** `market-scout` (sonnet) — runs `score_markets.py`.
- **Active signal agents:** `market-maker-signal`, `weather-signal`, `mentions-signal`, `polls-signal`, `order-flow-signal`, `kalshi-bias-signal` (sonnet; each shells a `kalshi_trader.pipelines.*` CLI).
- **Disabled (files present, not dispatched):** `polymarket-price-signal`, `polymarket-whale-signal`, `x-signal`.
- **Portfolio/exit:** `position-reviewer` (sonnet + WebSearch).
- **Exit of pipeline:** `risk` (sonnet → `run_risk.py`), `idea-publisher` (haiku → POST).

The 13 files in `.claude/agents/` are: `idea-publisher`, `kalshi-bias-signal`, `market-maker-signal`, `market-scout`, `mentions-signal`, `order-flow-signal`, `polls-signal`, `polymarket-price-signal`, `polymarket-whale-signal`, `position-reviewer`, `risk`, `weather-signal`, `x-signal`.

**Discrepancy (confirmed):** SKILL.md Step 2 instructs the orchestrator to dispatch a `sportsbook-odds-signal` agent for sports markets, and the backing `kalshi_trader/pipelines/sportsbook.py` + `kalshi_trader/signals/sportsbook.py` exist — but there is **no `sportsbook-odds-signal.md` file** in `.claude/agents/`. As the code stands today, that dispatch has no agent definition to resolve to.

### Deterministic scripts (`scripts/`)
`score_markets.py` (actionability scan/score) · `score_signals.py` (combine + edge + Kelly) · `run_risk.py` (RiskManager) · `market_rules.py` (settlement rules, cached per series) · `live_prices.py` (live top-of-book) · `contract_terms_doc.py` (download contract PDFs) · `evaluate_portfolio.py` (exit checks + real sell orders) · `exit_position.py` (approved exit orders) · `night_execute.py` (autonomous $10 buys) · `persist_cycle.py` (Supabase upsert) · `paper_track.py` (record / record-scored / mark / report) · `ui_log.py` / `ui_state.py` (fire-and-forget telemetry).

### Signal logic (`kalshi_trader/signals/` + `kalshi_trader/pipelines/`)
`microstructure` (price-derived) · `mentions` (speaker-attributed corpus + GDELT, multi-source) · `weather` (GEFS ensemble empirical-CDF + observation override + X authority) · `polls` (538 normal model) · `sportsbook` (ESPN moneyline de-vig) · `polymarket` (price/whale) · `x` (Grok). Pipelines are the CLI shells the agents call; signals build the `SignalEstimate` objects ([kalshi_trader/models.py:125-136](../../../kalshi_trader/models.py#L125)).

### Config & runtime
`runtime_config.json` (repo root, ~64 keys): agent enable toggles (`agent_x_enabled: false`, etc.), per-source signal weights/uncertainties, agreement-boost params, OFI/MM/bias tuning, portfolio exit thresholds, market filters, models. `min_edge_cents` (5.0) and `max_entry_price_cents` (90.0) are hardcoded defaults in `score_signals.py`, not in the config file.

### Dashboards (two apps)
- **Writable** ([kalshi_trader/ui/server.py](../../../kalshi_trader/ui/server.py), launched by [run_ui.py](../../../run_ui.py) on `0.0.0.0:8000`) — the orchestrator's publish target. Routes include `GET/POST /api/state`, `POST /api/log`, `POST /api/ideas`, `POST /api/ideas/{id}/approve|reject`, `GET /api/ideas/history`, `GET /api/config`, `POST /api/config`, `GET /api/markets/prices`. A background poller refreshes balance/positions/orders from the live Kalshi account every 10s.
- **Read-only** ([kalshi_trader/dashboard/app.py](../../../kalshi_trader/dashboard/app.py)) — a prod portfolio monitor that refuses to start if any non-GET route is registered (`_assert_read_only` at [app.py:54](../../../kalshi_trader/dashboard/app.py#L54)). Not a pipeline target.

---

## Architecture notes (patterns in the current code)

- **Glue-only orchestrator:** SKILL.md only dispatches agents and runs deterministic scripts; it never does math or invents signals.
- **Agents are thin shells; math is deterministic Python** (`score_signals.py`, `risk.py`) — reproducible and testable independent of the LLM.
- **Uniform `SignalEstimate` contract** lets heterogeneous sources feed one staleness-weighted combiner.
- **Two-tier coverage:** free deterministic signals ride all ~200 rows for the calibration backtest; expensive network agents run only on a ≤40 deep subset, dispatched concurrently.
- **Defense-in-depth gating:** `worth_trading` + 2+ sources → 5-question adversarial challenge (with contract-PDF reading + 3 independence paths) → independent risk edge gate + sizing.
- **Live-price re-anchoring** before agents run, so signals and the scorer share one price.
- **Non-blocking telemetry & persistence** (`ui_log`, `ui_state`, `persist_cycle`, paper-track mirrors all swallow exceptions).
- **One cycle per skill run;** cadence delegated to `/loop`.
- **Execution is bounded and gated:** exits are automatic (Step 0.5) or human-approved (Step 0.75); autonomous entries exist only in the separate `night_execute.py` behind hard session caps; new orchestrate ideas are always paper + human-reviewed.

## Historical Context (from thoughts/)

- [2026-06-03-agent-structure-trade-idea-evaluation.md](2026-06-03-agent-structure-trade-idea-evaluation.md) — the prior, very thorough map of the 9-step read-only pipeline (commit `4cefa83`). Still the best reference for the core idea-generation flow; this document records what changed on top of it (portfolio steps, execution paths, disabled agents, live-pricing, contract PDFs, mentions/weather rebuilds, adversarial-reasoning history).
- [2026-06-02-project-summary.md](2026-06-02-project-summary.md) — the underlying scorer/scanner/risk/Kalshi-client layer; the 9 actionability signals + weights, candle cache TTLs, half-Kelly thresholds.
- [2026-06-02-kalshi-market-scoring-latency.md](2026-06-02-kalshi-market-scoring-latency.md) — latency deep-dive of the scan/score phases (point-in-time; treat live code as source of truth).

## Code References

- `.claude/skills/orchestrate/SKILL.md` — the full 0→10 orchestration glue
- `.claude/agents/{market-scout,risk,idea-publisher,position-reviewer}.md` + the signal agents
- `scripts/score_signals.py:227-356` — `combine_signals` + `compute_edge_and_kelly`
- `scripts/run_risk.py` + `kalshi_trader/risk.py:32-128` — risk checks + half-Kelly sizing
- `scripts/evaluate_portfolio.py:158` / `scripts/night_execute.py:190` — the two real-order `create_order` paths (behind `if not dry_run:`)
- `kalshi_trader/grouping.py` — scout output rows incl. inline `microstructure` + `kalshi_bias`
- `kalshi_trader/signals/{mentions,weather}.py` + `kalshi_trader/refresh_mentions_archive.py` + `kalshi_trader/mentions/store.py` + `kalshi_trader/external/speaker_registry.py` — rebuilt mentions/weather
- `kalshi_trader/ui/server.py`, `kalshi_trader/ui/state.py`, `kalshi_trader/db.py` — dashboard + Supabase (paper_only)
- `runtime_config.json` — agent toggles + tuning

## Open Questions

- The SKILL.md repo-root prefix (`/Users/scorley/code`) and a few "pipeline not yet implemented" notes in some signal-agent docs remain stale relative to the live code on this machine (`/Users/llewis/ai_week`); modules exist and run. Documented, not reconciled.
- **Resolved:** the orchestrator dispatches `sportsbook-odds-signal`, and `sportsbook.py` pipeline/signal modules exist, but **no `.claude/agents/sportsbook-odds-signal.md` agent definition exists** (confirmed by `ls .claude/agents/` — 13 files, none for sportsbook). The dispatch instruction currently has no agent to resolve to.
- **Resolved:** Step 0.75 references a `/portfolio` skill "verbatim", but **`.claude/skills/` contains only `orchestrate`** — there is no standalone `/portfolio` skill file. The backing `position-reviewer` agent and `scripts/exit_position.py` both exist, so the pattern is realizable, but the skill it points to is not present as a file.
