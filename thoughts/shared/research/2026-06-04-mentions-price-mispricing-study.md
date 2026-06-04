---
date: 2026-06-04
author: Claude (Opus 4.8)
status: findings
topic: Q6 mispricing study — is the edge in the price of Kalshi "mentions" markets?
companion: 2026-06-04-mentions-corpus-premise-backtest.md (the Q2 validation)
reproduce: |
  KALSHI_ENV=prod PYTHONPATH=. .venv/bin/python scripts/mentions_price_study.py \
    --asof-minutes 60   --out /tmp/mentions_price_study.json
  KALSHI_ENV=prod PYTHONPATH=. .venv/bin/python scripts/mentions_price_study.py \
    --asof-minutes 1440 --lookback-hours 168 --out /tmp/mentions_price_study_1d.json
---

# Q6 — Is the edge in the price? (mentions mispricing study)

## What this tested

The respondent's deep-research answer asked whether mentions markets are *mispriced*
in a way we can exploit independent of having a great point estimate — specifically:
(a) is the market price itself a good predictor, (b) is there favorite-longshot bias,
and (c) does retail price these with the *same* discredited "ubiquity" heuristic that
sank the GDELT model (i.e. does the market price track `p_gdelt`)?

Method: join a pre-close market price onto the existing 455-market GDELT backtest
sample (`/tmp/mentions_backtest.json`), reading Kalshi candlesticks (prod, read-only,
no orders). The market price is the latest real trade (or book mid) at/before a chosen
as-of offset before close. Reported at **two horizons** because most mentions markets
close right after the event, so a price taken minutes before close has already largely
resolved.

## Headline numbers

| Horizon | n (priced) | Market Brier | Naive Brier | GDELT `p_gdelt` Brier |
|---|---:|---:|---:|---:|
| 60 min before close | 455 | **0.036** | 0.247 | 0.396 |
| 24 h before close | 405 | **0.149** | 0.245 | 0.390 |

(49 markets had no candle ≥24 h before close — they had not opened yet — and are
excluded from the 24 h row; 1 candle fetch error.)

**The market is a strong, well-calibrated predictor.** Even a full day before close it
scores Brier 0.149 — far better than the naive base rate (0.245) and worlds better than
the discredited GDELT model (0.39). Minutes before close it is effectively settled
(0.036): for sports the game has happened, so 148 markets sit at ~1¢ (realized 1.4%)
and 232 at ~99¢ (realized 99.6%).

### Reliability at 24 h (overall)

| price bin | count | mean price | realized | bias (realized − price) |
|---|---:|---:|---:|---:|
| [0.00,0.05) | 2 | 0.030 | 0.000 | −0.03 |
| [0.05,0.15) | 19 | 0.098 | 0.158 | +0.06 |
| **[0.15,0.50)** | **137** | **0.320** | **0.204** | **−0.115** |
| [0.50,0.85) | 158 | 0.700 | 0.728 | +0.028 |
| [0.85,0.95) | 59 | 0.889 | 0.915 | +0.027 |
| [0.95,1.01) | 30 | 0.967 | 1.000 | +0.033 |

## Findings

1. **The edge is in the price, and the bar is the market — not the naive line.** Any
   future mentions signal must beat a market that already scores Brier ≈ 0.15 a day out
   and ≈ 0.04 near close. Beating the naive base rate (0.25) is not enough; the prior
   target of "Brier well under 0.247" is the *wrong* bar. This is the single most
   important result for the roadmap.

2. **The "retail uses the same ubiquity heuristic" hypothesis is refuted.** Across both
   horizons the market price is essentially *uncorrelated* with GDELT ubiquity
   (`corr(price, p_gdelt)` = 0.009 at 60 min, 0.023 at 24 h), and adding `p_gdelt` to a
   logistic model of the outcome over price adds nothing (likelihood-ratio p = 0.32 and
   0.82; the `p_gdelt` coefficient's 95% CI straddles zero in both). The market is *not*
   anchored to "is this word generally common on TV." There is no ubiquity-axis
   mispricing to trade against.

3. **One real structural signature: the 15–50¢ band is systematically overpriced.** A
   day out, markets priced ~0.32 realized only ~0.20 (bias −0.115, n = 137), and this
   replicates in both subtypes (sports 0.32→0.21; politics 0.27→0.15 and a 0.63→0.36
   cell). The complementary 50–85¢ band is mildly *underpriced* (0.70→0.73). This is the
   classic favorite-longshot pattern — mid-range longshots too rich. It is the only
   place a price-only edge plausibly lives: **fade YES / buy NO on 15–50¢ mentions
   longshots a day before close.**

## Recommendation

- **Do not target a calibrated point estimate to "beat the baseline."** The market is
  the baseline and it is already good. A model-based mentions signal is only worth
  building if it can beat Brier ≈ 0.15 a day out — a high bar that the corpus premise
  does not clear (see the companion Q2 findings).
- **The one tradeable candidate is structural, not model-based:** the 15–50¢
  favorite-longshot overpricing. Worth a focused follow-up — quantify it on a clean
  holdout, net of Kalshi fees and the thin liquidity in that band, and check it is not
  an artifact of a few illiquid series — before risking capital. It needs no transcripts.
- Leave the GDELT-only suppression (`MENTIONS_REQUIRE_CORPUS_BACKED = True`) in place.

## Caveats

- Prices come from candlesticks; illiquid markets fall back to the book mid, and 49
  markets had no pre-24 h candle at all (excluded). The favorite-longshot bins have
  moderate n and should be confirmed on a dedicated, liquidity-filtered sample.
- "24 h before close" is a single snapshot; a real strategy would study the full price
  path and entry/exit timing.
