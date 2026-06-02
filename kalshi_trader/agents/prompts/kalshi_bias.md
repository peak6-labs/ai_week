# Kalshi Bias Agent — System Logic

**Signal type:** Calibration bias correction (favorite-longshot + political underconfidence)
**Source:** `kalshi_bias`
**Weight:** 0.55

## What this measures
Applies documented calibration corrections to Kalshi prices based on known behavioral
biases. Contracts at price extremes are systematically mis-priced (favorite-longshot bias).
Political markets are systematically underconfident (compressed toward 50¢).

## Signal thresholds (from research)
- price < 15¢: true_prob ≈ market_prob × 0.65 → YES is overpriced → signal NO
- price > 85¢: true_prob ≈ 1 - (1 - market_prob) × 0.65 → YES is underpriced → signal YES  
- political market, |price - 50| > 5¢: nudge 5-8¢ further from 50 → signal the leading side
- horizon < 12h: reduce adjustment 70%; 12-48h: reduce 40%; >48h: full

## When to fire
- Non-political: price < 20¢ OR price > 80¢ (bias large enough to exceed fees)
- Political: price < 45¢ OR price > 55¢
- After adjustment, minimum edge vs fees required: adjusted_prob must differ from
  market_prob by at least 5¢ (after 1.2% average Kalshi fee)
