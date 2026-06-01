# Agentic Prediction Market Trading System — Master Plan

**Competition:** Peak6 Trials / CapMan AI Immersion — June 1–5, 2026  
**Budget:** $500  
**Goal:** Maximum profit by end of week  
**Constraint:** No US equities, ETFs, Index, or derivatives of those products  
**Platforms:** Kalshi (primary), Polymarket (signals + secondary)

---

## 1. Context & Objectives

We are building an agentic trading system that operates on prediction market platforms — primarily **Kalshi** (CFTC-regulated, USD-settled, full Python SDK) and optionally **Polymarket** (blockchain-based, public on-chain holdings, useful for signal extraction). The system must:

1. **Phase 1 (Days 1–2):** Generate trade ideas with a human approval gate before any real money moves
2. **Phase 2 (Days 3–5):** Run autonomously overnight, executing trades without human sign-off
3. Use as many independent signal sources as possible (multi-factor)
4. Be designed as a multi-agent system with specialist agents + a coordinator

The core thesis from expert advice (Vishal, Benny G) and the Prediction Arena paper:
- **Platform selection and order execution matter more than forecasting accuracy alone.** Models averaged -22% on Kalshi but only -1% on Polymarket — microstructure is everything.
- **Flow and volume are better price action signals** than probability models alone.
- **Conditional event underpricings** (later legs of multi-round events) are a systematic inefficiency.
- **Cross-platform data** (Polymarket whale positions) can generate alpha on Kalshi.

---

## 2. Platform Decision

### Primary: Kalshi
- CFTC-regulated, USD-settled, zero surprise on compliance
- Official Python SDK: `kalshi_python_async` (pip install)
- Taker fee formula: `0.035 × C × P × (1−P)` — fees are highest at 50-cent contracts, minimal at extremes
- WebSocket feed for real-time order book
- Rate limits: Basic tier — 200 read tokens/s, 100 write tokens/s
- Key categories: Sports (80% of volume), Politics, Economics, Weather, Crypto, Culture/Mentions

### Signal Layer: Polymarket
- All positions are publicly visible on Polygon blockchain
- Gamma API (`https://gamma-api.polymarket.com`) — free, no auth, 15k req/10s
- py-clob-client SDK for full data access
- Whale/insider tracking: PolyWhaler.com, ScanWhale.com, Dune dashboards
- When same event is priced differently on Polymarket vs. Kalshi → potential edge
- **Critical caveat:** Settlement rules sometimes differ for the same event — always compare before arbing

### Compliance Check
- Kalshi: ✅ CFTC-regulated event contracts, not equities/ETFs
- Polymarket for signal reading: ✅ (not trading US equities)
- Robinhood Agentic Trading: ❌ — currently equity-only; avoid

---

## 3. System Architecture

### 3a. Multi-Agent Design (Vishal's "6 + 1" Pattern)

Run **6 specialist strategy agents** in parallel, each with its own approach to finding edges. A **7th coordinator agent** receives all their outputs and synthesizes a final ranked trade slate.

```
┌─────────────────────────────────────────────────────────┐
│                    Data Ingestion Layer                  │
│  Kalshi WS │ Polymarket API │ External Datasets │ News  │
└─────────────────────────────────────────────────────────┘
                           │
┌─────────────────────────────────────────────────────────┐
│                  Specialist Agents (6)                   │
│                                                         │
│  [A1] Conditional    [A2] Flow/Volume  [A3] Cross-      │
│       Event Arb           Analyst          Platform     │
│                                           Signal        │
│  [A4] Dataset        [A5] Sentiment/   [A6] Market      │
│       Edge                News             Making       │
│                           Monitor          (Illiquid)   │
└─────────────────────────────────────────────────────────┘
                           │
┌─────────────────────────────────────────────────────────┐
│            Coordinator Agent (A7)                        │
│  - Aggregates signals from all 6 agents                 │
│  - Resolves conflicts, ranks opportunities by EV        │
│  - Applies portfolio-level risk rules                   │
│  - Outputs: ranked trade slate with position sizes      │
└─────────────────────────────────────────────────────────┘
                           │
              ┌────────────┴────────────┐
              │                         │
     [Phase 1: STOP]            [Phase 2: AUTO]
     Human review gate          Direct to executor
     Approve/reject each        with guardrails
              │
┌─────────────────────────────────────────────────────────┐
│                   Execution Layer                        │
│  Order sizing (Kelly) │ Kalshi API │ Risk guardrails    │
└─────────────────────────────────────────────────────────┘
```

### 3b. Technology Stack
- **Language:** Python 3.11+
- **Kalshi SDK:** `kalshi_python_async` (async order placement + WebSocket)
- **Polymarket:** `py-clob-client` + Gamma API direct HTTP calls
- **LLM backbone:** Claude API (`claude-sonnet-4-6` for agents, `claude-opus-4-8` for coordinator)
- **Data/compute:** `pandas`, `numpy`, `scipy` for probability modeling
- **Async:** `asyncio` + `aiohttp` for parallel data fetching
- **Scheduling:** `APScheduler` or simple `asyncio` loop for overnight runs
- **Persistence:** SQLite (lightweight, no infra needed) for trade log, signal history
- **Notifications:** Telegram bot or Discord webhook for alerts
- **Inspiration:** Crucix pattern (parallel `asyncio.gather()` across all data sources with graceful degradation; delta-based alerting on market changes; modular per-source data fetchers)

---

## 4. The 6 Specialist Strategies

### Strategy A1: Conditional Event Underpricing
**Thesis (Vishal):** In sequential multi-round events (NBA playoffs, elections, tournaments), downstream/later contracts are systematically underpriced because price-insensitive buyers concentrate on near-term outcomes. The convexity of later events is underappreciated.

**Implementation:**
- Scan Kalshi for multi-leg event families (e.g., NBA: Game 6 win → Series win → Conference Finals → Finals)
- Model the implied probability chain: `P(team wins finals) ≥ P(wins series) × P(wins game)`
- Identify gaps where the market violates the conditional probability constraint
- Also look for convexity bets: a sub-20% contract that should move to 40% if an upstream event fires
- **Preferred entry:** Markets priced under 40%, ideally sub-20%
- **Exit:** Either at event resolution or when price reaches ~40%

**Data needed:** Kalshi market metadata + historical odds + external sports data APIs

---

### Strategy A2: Flow & Volume Signal Trader
**Thesis (Vishal):** Volume and order flow are better leading indicators of price action than static probability models. Unusual volume spikes often precede significant price moves.

**Implementation:**
- Monitor Kalshi order book in real time via WebSocket
- Track cumulative volume, volume velocity (rate of change), bid/ask imbalance
- Flag markets where volume is accelerating relative to 24h baseline
- Implement simple order-book imbalance signals: if bids >> asks at current price, expect upward move
- Cross-reference with time-to-resolution: imminent resolution + unusual flow = higher signal strength
- Also watch for large trades (block-sized) that move the market

**Data needed:** Kalshi WebSocket feed, 24h volume history

---

### Strategy A3: Cross-Platform Signal Arbitrage
**Thesis (Benny G):** Polymarket and Kalshi often price the same event differently, and Polymarket whale activity can predict Kalshi price moves before they happen. Also, settlement rule differences can create "false arbitrage" that is actually directional alpha.

**Implementation:**
- Use Gamma API to pull all Polymarket markets; fuzzy-match to Kalshi by event title/category
- Compare prices for matched markets — flag >3 cent discrepancies (after fees)
- **Before executing any arb:** Compare settlement rules for both markets carefully
- Track top-10 "whale" wallets on Polymarket (via PolyWhaler.com / on-chain queries); monitor when they take large positions; use as directional signal on Kalshi
- Pure risk-free arb requires: YES(Kalshi) + NO(Polymarket) < $1.00 or vice versa (after all fees)
- More commonly: use Polymarket price as a "fair value" estimate, trade Kalshi when it's far away

**Data needed:** Gamma API, Polymarket CLOB, Kalshi REST/WS, whale wallet tracker

---

### Strategy A4: External Dataset Edge
**Thesis (Vishal/Benny G):** Markets that have related external datasets (weather, economic releases, sports statistics) are more mispriced when fewer participants have access to or are using those datasets. Find niche data that others aren't plugging in.

**Implementation:**
- **Weather markets:** Pull NWS/NOAA forecasts, ensemble model data (GFS/ECMWF) — compare to Kalshi weather market prices
- **Economic data markets:** Pull FRED economic calendar (CPI, jobs, GDP). Use nowcasting models to estimate actual vs. consensus before release. Trade the spread.
- **Sports stats:** ESPN/sports-reference APIs for injury reports, home/away splits, rest days — use as inputs for sports market probability models
- **Billboard/music mention markets:** If Kalshi has "will X song chart" markets, pull Spotify trending/YouTube data
- **Crucix-style aggregation:** Pull NASA FIRMS, GDELT conflict data, shipping AIS for any geopolitical event contracts
- Build a "dataset coverage score" for each market: higher score = more data inputs available = potentially larger edge

**Data needed:** NOAA, FRED, sports APIs, GDELT (all free), possibly EIA for energy markets

---

### Strategy A5: Sentiment & News Monitor
**Thesis:** Breaking news and sentiment shifts move prediction markets in predictable ways. An agent that reads faster than the market can front-run re-pricing.

**Implementation:**
- Monitor news RSS feeds (Reuters, AP, BBC) and social media sentiment (Twitter/X via search API or Nitter)
- Classify news by relevance to open Kalshi market categories
- Use LLM (Claude) to assess impact magnitude and direction
- Calculate "news surprise" score — how much does this deviate from prior expectations?
- Flag high-surprise news events as potential trade triggers
- Prioritize time-sensitive questions (the Prediction Arena paper found reasoning models excel here)
- Filter: only act on news within 1 hour of detection to avoid stale signals

**Data needed:** RSS feeds, optionally Twitter API, LLM inference

---

### Strategy A6: Illiquid Market Market-Making
**Thesis (Benny G):** Market-making illiquid markets can work well. If you're the only liquidity provider on a thin market, you capture the spread.

**Implementation:**
- Scan Kalshi for markets with very low liquidity (wide bid/ask spreads, low open interest)
- Target categories: weather mention markets, niche cultural markets
- Place resting limit orders on both sides of the spread
- Manage inventory: close out positions before they become directional bets you didn't intend
- Monitor for any news that could cause directional moves (exit market-making immediately if news breaks)
- **Risk limit:** No more than $50 total exposure in market-making positions at once
- Reference: `kalshi-market-making` repo (Avellaneda-Stoikov approach, 20%+ return demonstrated)

**Data needed:** Kalshi order book, market metadata

---

## 5. Coordinator Agent (A7) Logic

The coordinator agent receives structured JSON output from each specialist and must:

1. **Deduplicate** — multiple agents may flag the same market
2. **Score** — assign a composite signal strength score to each opportunity
3. **Check constraints:**
   - Max $100 in any single market
   - Max $250 in any single category (sports, politics, etc.)
   - Total open exposure < $400 (keep $100 reserve)
   - No more than 5 open positions at once in Phase 1
4. **Size positions** using Kelly fraction:
   - `f* = (p × b − q) / b` where p=win probability, q=1-p, b=net odds
   - Cap at 50% of Kelly (half-Kelly) to reduce variance
   - Minimum position: $10 (below this, fees eat too much)
5. **Format output** as ranked trade slate for human review (Phase 1) or direct execution (Phase 2)

---

## 6. Data Sources & Integration

### Free / No-Auth
| Source | Data | Relevance |
|--------|------|-----------|
| Kalshi REST + WS | Market prices, order books, trades | Core trading data |
| Polymarket Gamma API | Market metadata, prices | Cross-platform signals |
| NOAA/NWS | Weather forecasts, model data | Weather markets |
| FRED | Economic releases, calendar | Economic markets |
| GDELT | Global event data, news | Geopolitical markets |
| ESPN/sports APIs | Scores, injuries, stats | Sports markets |
| RSS feeds | Breaking news | Sentiment/event trigger |

### Requires Free Keys
| Source | Data | Key Type |
|--------|------|---------|
| OpenWeatherMap | Hyperlocal weather | Free tier |
| EIA | Energy data | Free FRED-style |
| Twitter/X API | Social sentiment | Free basic tier |

### Paid / Premium (Optional)
| Source | Data | Cost |
|--------|------|------|
| PolymarketData.co | 1-min historical order books | Subscription |
| Telonex | Tick-level data | Freemium |
| Dune Analytics | Whale wallet queries | Free tier often enough |

---

## 7. Risk Management

### Hard Rules (cannot override)
- **Max single-trade loss:** $50
- **Max daily loss:** $100 (system pauses, sends alert, waits for human)
- **Max open exposure:** $400 (hold $100 in reserve always)
- **No market-making if news breaking:** Immediately cancel all resting orders
- **Never trade < 2 hours before settlement** unless directional conviction is extremely high (resolution risk)

### Soft Rules (coordinator enforces)
- Prefer markets with >$5,000 open interest (don't fight Jump Trading in deep markets, but also need enough liquidity to exit)
- Prefer time-to-resolution < 5 days for Phase 1 (faster feedback loop while learning)
- Prefer markets in the 15–40% price range for directional bets (convexity, as per Vishal)
- Diversify across at least 2 categories

### Position Sizing
- Kelly fraction, capped at half-Kelly
- Minimum $10, maximum $100 per position
- Reduce size by 50% if last 3 trades in a category were losses (adaptive)

---

## 8. Implementation Roadmap (Day-by-Day)

### Day 1 (Monday, June 2) — Foundation
**Goal:** Core infrastructure running, idea generation working with human gate

- [ ] Set up Kalshi account, generate API keys (RSA private key pair)
- [ ] Install `kalshi_python_async`, test authentication, pull market list
- [ ] Install `py-clob-client`, test Gamma API for Polymarket market list
- [ ] Build the data ingestion layer: parallel async fetchers for Kalshi + Polymarket + NOAA + FRED
- [ ] Build basic market scanner: list all open Kalshi markets with price, volume, spread
- [ ] Implement A1 (Conditional Event) and A3 (Cross-Platform Signal) as first two agents
- [ ] Build human-review gate: print trade slate → user types Y/N for each
- [ ] Make first 1–3 manual trades to understand platform mechanics
- [ ] **DO NOT** automate execution yet

### Day 2 (Tuesday, June 3) — Signals + More Agents
**Goal:** Add more signal sources, evaluate first trade results

- [ ] Implement A2 (Flow/Volume) using Kalshi WebSocket
- [ ] Implement A4 (External Dataset) starting with weather and sports stats
- [ ] Implement A5 (Sentiment/News) with RSS monitoring
- [ ] Build SQLite trade log: record all ideas generated, which were approved, outcome
- [ ] Set up Telegram/Discord notifications for new idea generation
- [ ] Calibrate: compare agent-generated probability estimates vs. actual outcomes
- [ ] Tune position sizing based on first-day performance
- [ ] Evaluate: which agent is finding the best opportunities?

### Day 3 (Wednesday, June 4) — Coordinator + A6
**Goal:** Coordinator agent working, begin overnight automation planning

- [ ] Implement A6 (Market Making) on 1–2 illiquid markets with tiny test positions
- [ ] Build Coordinator Agent (A7): aggregation, conflict resolution, portfolio constraints
- [ ] Test full pipeline: data → 6 agents → coordinator → trade slate
- [ ] Review overnight automation readiness checklist:
  - Hard stop-loss implemented?
  - Alerting on errors/anomalies?
  - Kill switch tested?
- [ ] Begin transition from full human gate to "auto-approve if all agents agree AND size < $25"

### Day 4 (Thursday, June 5 — Morning)
**Goal:** Full autonomous operation

- [ ] Full autonomous run with all 7 agents
- [ ] Monitor closely but don't intervene unless hard stop-loss triggers
- [ ] Analyze agent performance: weight future signals toward best-performing agents
- [ ] Expand position sizes on strategies with positive track record

### Day 5 (Friday, June 6) — Presentation Day
**Goal:** Wind down positions, prepare results

- [ ] Close or reduce open positions ahead of end of week
- [ ] Compile performance metrics: total return, win rate, Brier score per agent
- [ ] Identify which strategies worked and which didn't
- [ ] Prepare presentation materials

---

## 9. Idea Generation Module (Phase 1 Detail)

The idea generation module runs on a configurable interval (suggest: every 15 minutes during trading hours, every 30 minutes overnight).

### Per-Cycle Flow
```
1. Fetch all open Kalshi markets (REST API, paginated)
2. In parallel, run each specialist agent against the market list
   - Each agent returns: [{market_ticker, direction, confidence, reasoning, suggested_size}]
3. Coordinator aggregates, deduplicates, scores, and applies risk rules
4. Output: ranked list of trade ideas
5. [Phase 1] → Format as human-readable report, send to Telegram/Discord, wait for approval
   [Phase 2] → Send directly to execution layer
```

### Human Review Output Format (Phase 1)
```
═══════════════════════════════════════════════
TRADE SLATE — 2026-06-02 14:32 UTC
═══════════════════════════════════════════════

#1 [STRONG] NBA-CELTICS-WIN-FINALS ← YES @ $0.22
   Signal: A1 (conditional underpricing) + A3 (Polymarket 0.31)
   Confidence: 78% | Market: 22% | Edge: +56%
   Suggested: $45 (half-Kelly)
   Reasoning: Polymarket prices this 9 cents higher; A1 detects
   that series win probability implies >35% for finals.
   → APPROVE? [Y/N]

#2 [MODERATE] WEATHER-NYC-RAIN-JUNE3 ← YES @ $0.18
   Signal: A4 (NOAA GFS model: 73% chance of rain)
   Confidence: 65% | Market: 18% | Edge: +47%
   Suggested: $20 (half-Kelly, reduced for weather model uncertainty)
   → APPROVE? [Y/N]
...
═══════════════════════════════════════════════
```

---

## 10. Open Questions & Things to Narrow Down

The following are intentional open items — things to decide once we have more information or have seen the platform in action:

1. **Polymarket trading vs. signal-only:** Should we actually trade on Polymarket too, or just use it for signals feeding into Kalshi trades? Polymarket requires a Polygon wallet and USDC. Compliance is less clear than Kalshi. Recommend: signals-only to start, consider adding execution on Day 3 if there's a clear arb opportunity.

2. **Which market categories to focus on first?** Sports (80% of Kalshi volume, most liquidity) vs. economics (more data-driven, but lower volume). Vishal prefers basketball/politics. Recommend: start with sports for liquidity, add economics day 2.

3. **Overnight automation safety:** What are the right hard stop-loss thresholds? $100/day loss pause seems right but needs calibration. Also need to decide: does the system cancel all open orders when sleeping, or leave resting limit orders?

4. **Market-making depth:** How aggressively to pursue A6 (illiquid market making)? This requires active monitoring — less suitable for pure overnight operation. May be better as a daytime-only strategy.

5. **LLM model routing:** Which Claude model for which agent? Opus is slower/more expensive but better for synthesis (coordinator). Sonnet for the specialists. May want to tune this after seeing latency in practice.

6. **Cross-platform arb execution:** If we do spot a clean arb between Kalshi and Polymarket, does the latency/execution complexity make it worth pursuing? The 2am EST price divergence mentioned in research is interesting. Needs real-world testing.

7. **How many agents should actually be automated?** May want to start with 3–4 most reliable agents automated and keep the riskier strategies (market-making, illiquid markets) human-gated longer.

---

## 11. Key References & Resources

### APIs & SDKs
- Kalshi Python SDK: `pip install kalshi_python_async` — https://docs.kalshi.com/sdks/python/quickstart
- Kalshi API docs: https://docs.kalshi.com/api-reference
- Polymarket py-clob-client: https://github.com/Polymarket/py-clob-client
- Polymarket Gamma API: https://gamma-api.polymarket.com

### Research & Strategy
- Prediction Arena paper (arXiv 2604.07355): frontier models averaged -22% on Kalshi, -1% on Polymarket; platform matters more than model quality
- Crucix (github.com/calesthio/Crucix): reference architecture for multi-source data aggregation, delta alerting, graceful degradation
- Polymarket/agents (github.com/polymarket/agents): official AI agent framework using Claude
- ImMike/polymarket-arbitrage (GitHub): cross-platform arb bot with dry-run mode

### Whale Tracking
- PolyWhaler.com — $10K+ trade tracking
- ScanWhale.com — 30-day PnL rankings
- Dune Analytics — SQL queries on Polymarket on-chain data

### Market Making Reference
- github.com/nikhilnd/kalshi-market-making — Avellaneda-Stoikov model, 20%+ return demonstrated
