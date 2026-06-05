# June 5, 2026 Overnight Session Learnings

Reference for future night-mode sessions. Covers cycles 64–85, run during the
June 5, 2026 overnight and through-day trading window.

---

## Critical Pipeline Bugs Fixed

### 1. GEFS Ensemble Returning 0 Members (`open_meteo.py`)

**Bug:** `get_ensemble_members()` computed `forecast_days = days_ahead + 1`.
When `days_ahead = 0` (target date is today UTC), the API is asked for
`forecast_days=1`. With `timezone=auto`, Open-Meteo returns dates in local
time, which is behind UTC for US stations — so the target date wasn't in the
returned window. Result: 0 members returned for all same-day markets.

**Fix (applied):** Changed to `forecast_days = days_ahead + 2` to buffer
against the UTC-vs-local-day offset.

**Impact:** Every pre-fix cycle got `noaa_gfs` parametric fallbacks instead of
the 31-member GEFS ensemble. After the fix, ensemble signals returned correctly.

### 2. `build_signals.py` Header Line Parsing

**Bug:** `live_prices.py` emits a header line before the JSON object (e.g.,
`"live-priced 8/8 tickers (0 unquoted)"`). `build_signals.py` tried to parse
the whole file as JSON and failed.

**Fix (applied):** Added `content.index("{")` slicing before `json.loads()`.

---

## Known Remaining Pipeline Bugs (Unfixed)

### 3. LAX Band Statistics Inconsistency (`KXLOWTLAX-26JUN05-B63.5`)

**Bug:** The pipeline repeatedly reports `members_satisfying=21` (or similar
high count) for the LAX low-temp band market while also reporting
`ensemble_median < band_floor` and `p90 < band_floor`. These are mutually
exclusive — if the 90th percentile is below the band floor, no more than 10%
of members can be in the band.

**Pattern:** Seen in cycles 69, 70, 85. Probability swings between 68-90%.

**Ruling:** Always fail KXLOWTLAX band signals until this bug is diagnosed.
The market pricing (9–20¢ YES) is likely more accurate than the pipeline.

### 4. "Above" Threshold Lock Direction Inverted

**Bug:** When a same-day "above threshold" market (e.g., low > 64°F) has a
realized observation exceeding the threshold, the pipeline should lock P(YES)
near 1.0. Instead it reports P(YES) ≈ 1% with `lock_fraction=0.0`.

**Example:** `KXLOWTPHIL-26JUN05-T64` — realized KPHL low of 71.6°F (>64°F)
should lock YES, but pipeline returned 1% YES. Pipeline correctly handles the
"below threshold" lock direction (seen in `KXLOWTMIA-26JUN05-B72.5` where
realized 78.8°F locked a [72,73] band as NO).

**Ruling:** When a realized observation is available and the market title says
"above X" or ">X", check whether the observed value exceeds X. If yes, treat
as YES-locked regardless of pipeline probability.

---

## Adversarial Challenge Rules (Refined This Session)

### NWS-Ensemble Divergence Rule

When the NWS point forecast and the GEFS ensemble disagree on which side of
the threshold the temperature falls, **fail the challenge if the NWS
settlement-agency forecast points in the opposite direction from the trade.**

- Ensemble says YES, NWS says NO → fail the YES trade
- Ensemble says NO, NWS says YES → fail the NO trade
- **Exception:** If the NWS divergence is very small (<0.5°F from threshold)
  AND the ensemble is unanimous (0/31 or 31/31) with a large buffer, the
  external weather path may still pass. Apply judgment.

Applied in: KXLOWTLV (NWS 79°F vs 78°F threshold), KXLOWTPHIL earlier cycles,
KXLOWTPHX (NWS 79°F), KXHIGHTSFO (NWS 73°F below threshold).

### Market-Maker Position in the Challenge

Market-maker signals are corroboration, not a veto. The pipeline flow is:
1. Weather pipeline is primary (external weather path at 5¢)
2. MM result is fetched in Step 4.5 only for survivors
3. MM is a tiebreaker/corroboration — does NOT override gfs_ensemble or block
   a clean external weather path pass

History: We originally ran MM in parallel with weather signals (amplified price
chasing per June 4 postmortem), then removed it entirely, then added it back
as post-scoring corroboration only. Current state = post-scoring corroboration.

### Realized Observation Override

If the weather signal includes `realized_extreme` and `lock_fraction > 0`,
the market is partially or fully locked. Apply these rules:

- **"Below X" market**, realized value already exceeds X: YES is locked → only
  take YES trades or skip
- **"Above X" market**, realized value exceeds X: YES is locked → pipeline
  may report wrong lock direction (see Bug #4 above); apply manually
- **Band market**, realized value outside the band: band cannot resolve YES →
  only take NO trades or skip
- If `lock_fraction = 0` but a realized observation is reported, the day has
  just started — treat as a forecast signal, not a lock.

---

## Band Markets (B-Series Tickers)

`KXHIGHTDAL-26JUN05-B86.5` = "Will the high land in [86, 87]°F?"
(1°F narrow-band market, NOT "above 86.5°F")

The pipeline counts ensemble members strictly in `[threshold, threshold+1]`.
When the ensemble median sits well above or below the band, a low P(YES) and
a NO survivor are correct and expected — not a counting artifact.

Contrast with the LAX inconsistency (Bug #3): the Dallas/OKC/MIN band markets
were correct in cycle 66-67 (median 88°F vs band [86,87] → 4/31 in band → NO
trade valid). The LAX bug is a different issue specific to that station.

---

## Intraday Market Behavior

Markets priced early in the day can move dramatically as:
1. Overnight lows lock in (by ~7am local time)
2. Same-day high temps develop (afternoon peak)
3. Early station observations arrive

**Key lesson from this session:** Phoenix and Las Vegas overnight lows exceeded
our NO thesis. We entered NO on both based on ensemble predictions (~77°F) but
actual observations came in at 82-84°F. The ensemble had an upward bias vs
realized temperatures for both desert markets.

- `KXLOWTPHX-26JUN05-T80` NO: entered at 29¢, realized low 84.2°F — LOSS
- `KXLOWTLV-26JUN05-T78` NO: entered at 28¢, realized low 82.4°F — LOSS

This is the desert heat retention problem: ensemble models can underestimate
overnight lows in desert cities where heat absorption is high and radiative
cooling is inhibited by dry air. Apply extra caution to desert overnight low
markets (LAS, PHX) when trading NO.

---

## Session Configuration Changes

- **`SESSION_TRADE_CAP`**: Originally 10 trades/day. Changed to 9999 (disabled)
  during this session at ~12pm UTC, leaving only the $200 dollar cap.
- **`SESSION_DOLLAR_CAP`**: $200/day — still binding.
- **`CYCLE_TRADE_CAP`**: 3 trades/cycle — still active.

---

## Markets That Performed Well (Exits Confirmed by Portfolio Loop)

| Market | Side | Entry | Thesis |
|--------|------|-------|--------|
| `KXHIGHTBOS-26JUN05-T88` YES | YES | 67¢ | Ensemble 90%, Boston high stayed <88°F |
| `KXHIGHAUS-26JUN05-B85.5` NO | NO | 55¢ | Austin high landed ~88°F, not in [85,86] band |
| `KXHIGHTMIN-26JUN05-B81.5` NO | NO | 60¢ | Minneapolis high ~84°F, not in [81,82] band |
| `KXHIGHTHOU-26JUN05-T85` NO | NO | 75¢ | Houston high well above 85°F |
| `KXLOWTBOS-26JUN05-T60` YES | *(rejected: entry_price_out_of_band)* | — | Realized 69.8°F confirmed YES |

## Markets That Underperformed

| Market | Side | Entry | Outcome |
|--------|------|-------|---------|
| `KXLOWTPHX-26JUN05-T80` NO | NO | 29¢ | Realized 84.2°F = YES (LOSS) |
| `KXLOWTLV-26JUN05-T78` NO | NO | 28¢ | Realized 82.4°F = YES (LOSS) |
| `KXLOWTMIA-26JUN05-B72.5` NO | YES (initially) | 24¢ | Band closed as NO; exited urgently |

---

## Stale Order Handling Notes

Exit orders on illiquid band markets (like `KXLOWTMIA-26JUN05-B72.5`) will
repeatedly go stale. Midmarket pricing won't fill when the true value is near
zero and no buyers exist. After 3+ stale cycles, use **urgency pricing** via
`place_order.py "need to get filled urgently"` to cross the spread and recover
any value rather than letting it expire at $0.

---

## Pipeline Architecture Notes

- **Step 4.5** (Market-maker for survivors only): Added this session. Correct
  placement is AFTER scoring, using survivors list. Run as single parallel
  message with all survivor agents.
- **`build_signals.py`**: Uses OUTPUT_FILE pattern where weather agents write
  directly to `/tmp/weather_signals_${TS}/TICKER.json`. This avoids large JSON
  assembly in-context.
- **Scout TTL**: 3000s (50 min). Reuse if under threshold; dispatch
  `market-scout` agent otherwise.
