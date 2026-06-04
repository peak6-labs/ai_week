#!/usr/bin/env bash
# Runs evaluate_portfolio.py in --execute mode every 30 seconds.
# Usage: bash scripts/portfolio_loop.sh &
# Stop: kill %1  (or kill the PID printed at startup)

set -uo pipefail
cd "$(dirname "$0")/.."

readonly SLEEP_SECONDS=30
readonly CMD=(.venv/bin/python scripts/evaluate_portfolio.py --execute)

echo "Portfolio loop started (PID $$) — press Ctrl-C or kill $$ to stop"

while true; do
    TS=$(date -u +"%H:%M:%SZ")

    if OUTPUT=$(KALSHI_ENV=prod PYTHONPATH=. "${CMD[@]}" 2>&1); then
        # Print only interesting lines (skip resting-order skips unless something triggered)
        TRIGGERED=$(printf '%s\n' "$OUTPUT" | grep -v "^SKIP" || true)
        if [ -n "$TRIGGERED" ]; then
            echo "[$TS] $TRIGGERED"
        else
            SKIP_COUNT=$(printf '%s\n' "$OUTPUT" | grep -c "^SKIP" || true)
            echo "[$TS] No exits triggered (${SKIP_COUNT} skipped)"
        fi
    else
        STATUS=$?
        echo "[$TS] ERROR evaluate_portfolio.py exited ${STATUS}"
        if [ -n "${OUTPUT:-}" ]; then
            echo "$OUTPUT"
        fi
    fi

    sleep "${SLEEP_SECONDS}"
done
