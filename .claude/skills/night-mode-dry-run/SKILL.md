---
name: night-mode-dry-run
description: >-
  Autonomous overnight trading pipeline dry run. Runs the full Kalshi
  orchestration cycle (scout → signals → score → challenge → execute) without
  human involvement, but keeps new entry execution in dry-run mode. The
  background exit loop is still kept running continuously. Only June 5, 2026
  weather markets are considered. Use when you want to test night mode safely.
---

# Night Mode Dry Run

You are running the Kalshi trading pipeline in **autonomous night mode dry
run**. All orchestration is the same as live night mode, except new entry
orders must **not** be placed.

**Two hard guarantees:**
1. `scripts/portfolio_loop.sh` is kept running in the background so
   `evaluate_portfolio.py --execute` continues checking exits every 30 seconds
   regardless of the main night-mode loop cadence.
2. All entry-side night-mode rules are enforced by `scripts/night_execute.py`,
   and this dry-run skill must call it with `--dry-run` so it never places new
   entry orders.

**Only June 5, 2026 daily weather markets are tradeable overnight.** The skill
should only carry forward weather/climate contracts for the settlement date
**2026-06-05** (typically tickers containing `-26JUN05`).

Follow the live night-mode skill exactly, with these two differences:

1. Keep Step `0.5` unchanged so the background exit loop remains running.
2. In Step `7`, run `scripts/night_execute.py --dry-run`.

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
.venv/bin/python scripts/ui_log.py "Night mode dry run: cycle $CYCLE started (TS=$TS)"
.venv/bin/python scripts/ui_state.py "{\"cycle_number\": $CYCLE, \"last_cycle_at\": \"$(date -u +%Y-%m-%dT%H:%M:%SZ)\"}"
```

```bash
BALANCE=$(curl -s -m 3 http://localhost:8000/api/state \
  | python3 -c "import sys,json; print(json.load(sys.stdin).get('balance_dollars') or '')" 2>/dev/null)
BALANCE=${BALANCE:-${KALSHI_BALANCE:-1000}}
```

---

## Step 0.5 — Ensure Portfolio Exit Loop Is Running

```bash
cd /Users/scorley/code
if pgrep -f "scripts/portfolio_loop.sh" >/dev/null; then
  echo "portfolio_loop.sh already running"
else
  nohup bash scripts/portfolio_loop.sh --night-mode > reports/portfolio-loop.log 2>&1 &
  echo $! > reports/portfolio-loop.pid
fi
```

```bash
.venv/bin/python scripts/ui_log.py "Night mode dry run: portfolio exit loop verified" 2>/dev/null || true
```

Continue to Step 1 unconditionally.

---

## Steps 1–6

Run Steps `1` through `6` exactly as defined in the live night-mode skill at:

`/Users/scorley/code/.claude/skills/night-mode/SKILL.md`

That includes:
- restricting the universe to June 5, 2026 weather markets
- collecting signals
- scoring
- adversarial challenge
- building `/tmp/candidates_${TS}.json`

---

## Step 7 — Night Mode Execution Dry Run

**All rules are still enforced by the script. Do not apply any rules yourself.**

```bash
cd /Users/scorley/code
KALSHI_ENV=prod PYTHONPATH=. .venv/bin/python scripts/night_execute.py \
  --candidates-file /tmp/candidates_${TS}.json \
  --session-file    ${SESSION_FILE} \
  --out             /tmp/night_executed_${TS}.json \
  --cycle-ts        ${TS} \
  --dry-run
```

Read `/tmp/night_executed_${TS}.json`. Log the simulated executions and session
state:

```bash
.venv/bin/python scripts/ui_log.py "Night mode dry run: simulated TICKER SIDE qty=N at PRICEc"
.venv/bin/python scripts/ui_log.py "Night mode dry run: K trades simulated, $D simulated spend this cycle"
```

If any record has `rejection_reason == "session_cap_reached"`:

```bash
.venv/bin/python scripts/ui_log.py "Night mode dry run: session cap reached — no further trades tonight" warning
```

If any record has `rejection_reason == "cycle_cap_reached"`:

```bash
.venv/bin/python scripts/ui_log.py "Night mode dry run: cycle cap reached (3 trades) — resuming next cycle" warning
```

---

## Steps 8–10

Run Steps `8` through `10` exactly as in the live night-mode skill, but label
logs/messages as dry run where appropriate and do not claim any entry orders
were actually placed.

---

## Step 11 — Cancel stale resting orders (dry run)

```bash
cd /Users/scorley/code
KALSHI_ENV=prod PYTHONPATH=. .venv/bin/python scripts/cancel_stale_orders.py \
  --minutes 10 --dry-run > /tmp/stale_orders_${TS}.json
```

Read `/tmp/stale_orders_${TS}.json`. For each record:

- Log what would happen:
  ```bash
  .venv/bin/python scripts/ui_log.py "Night mode dry run: would cancel stale TICKER ORDER_ID (sell — would replace at midmarket)"
  .venv/bin/python scripts/ui_log.py "Night mode dry run: would cancel stale TICKER ORDER_ID (buy — no replacement)"
  ```
- Do **not** dispatch `place-order` or place any real orders. Dry-run means simulate only.

---

## Running Overnight

```
/loop 20m /night-mode-dry-run
```

This rehearses the entry pipeline without placing new entry orders, while still
leaving the background exit loop running continuously.
