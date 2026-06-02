# Market Maker Agent — System Logic

**Signal type:** Spread dynamics / maker withdrawal detection
**Source:** `kalshi_mm_spread`
**Weight:** 0.65

## What this measures
Detects when market makers are withdrawing liquidity or widening spreads defensively,
which precedes large price moves (75%+ probability within 60 seconds for full withdrawal).

## Signal conditions (from research)
- spread > 15¢ → maker withdrawal → HIGH signal
- spread trending up >30% over 3 snapshots → spread widening → MEDIUM signal
- spread widening AND |imbalance| > 0.60 → directional move signal
- Normal Kalshi spread: 3–8 cents. Anything > 12¢ is elevated.

## Interpretation
- Widening spread + positive imbalance (bids > asks) → YES directional pressure
- Widening spread + negative imbalance → NO directional pressure
- Pure withdrawal (no directional bias) → high uncertainty, skip
