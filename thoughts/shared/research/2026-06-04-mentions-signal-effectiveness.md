---
date: 2026-06-04
author: Claude (Opus 4.8)
status: findings
scope: Backtest of the mentions signal's calibration on settled Kalshi markets
repo: /Users/llewis/ai_week
harness: scripts/mentions_backtest.py
data: /tmp/mentions_backtest.json (455 samples)
---

# Mentions signal — data/method effectiveness backtest

## Question

Does the `mentions_base` signal actually predict whether a phrase gets said in a
single event? Its tradeable component, when the speaker-attributed corpus is empty
(the production state), is the **GDELT TV base rate** — the fraction of monthly
CSPAN/national-news periods in which the phrase appeared at all. The trace audit
(B1) argued this measures the wrong thing. This backtest measures it.

## Method

`scripts/mentions_backtest.py` (read-only) pulls **settled** mentions markets via
`client.get_markets(status="settled", series_ticker=…)` across the recurring
series, reads each market's realized `result` ("yes"/"no") and phrase
(`yes_sub_title`), reconstructs the GDELT base rate **as of the market's close**
(timeline points after the close month are dropped — a look-ahead guard),
produces the probability via the *current* `build_mentions_base_signal`, and
scores predicted-vs-realized.

- **Sample: 455 settled markets** (NBA 132, NHL 120, MLB 124, Fed 48, Love Island 31).
- Skipped: 53 with no GDELT coverage (no signal), 24 with no parseable phrase.
- **All 455 were GDELT-only** — the speaker-attributed corpus is empty, so the
  corpus-backed tier is **not** exercised here (see Caveats).

## Results — the GDELT-only signal has negative skill

| Metric | Value | Reference |
|--------|------:|-----------|
| Samples | 455 | — |
| Base "yes" rate | 0.554 | — |
| **Brier score** | **0.395** | naive (always predict base rate) = **0.247** |
| **Hit-rate** | **0.514** | always-"yes" baseline = **0.554** |

The signal is **worse than guessing the base rate** (Brier 0.395 > 0.247) and
**worse than always predicting "yes"** (hit-rate 0.514 < 0.554). It has negative
skill.

### Calibration is inverted, in every band

| Predicted band | Count | Mean predicted | **Actual yes-rate** |
|----------------|------:|---------------:|--------------------:|
| [0.00, 0.15) | 54 | 0.083 | **0.648** |
| [0.15, 0.50) | 62 | 0.278 | 0.516 |
| [0.50, 0.85) | 40 | 0.706 | **0.325** |
| [0.85, 1.01) | 299 | 0.971 | **0.575** |

When GDELT says a word is *rare* on TV it was actually said ~65% of the time; when
GDELT says ~97% it was said only ~58%. The relationship is inverted/flat — there
is no band where the GDELT-only read is trustworthy.

### Per-series — uniformly bad

| Series | n | yes-rate | Brier |
|--------|--:|---------:|------:|
| KXNBAMENTION | 132 | 0.55 | 0.420 |
| KXNHLMENTION | 120 | 0.59 | 0.387 |
| KXMLBMENTION | 124 | 0.66 | 0.317 |
| KXFEDMENTION | 48 | 0.33 | 0.507 |
| KXLOVEISLMENTION | 31 | 0.32 | 0.459 |

Every series' Brier exceeds the ~0.25 naive baseline.

### Why — the proxy measures the wrong thing

National-TV-news frequency ≠ probability of being said in one specific broadcast/
hearing. The inversion examples make it concrete:

- **`trump` predicted 0.99 → not said**; `lebron` 0.97 → not said; `alien` 0.97 →
  not said. Ubiquitous-on-news words are over-predicted.
- **`chalamet` predicted 0.03 → said**; **`airball` 0.01 → said**. Context-specific
  words (a courtside celebrity, a basketball term) are nearly invisible to national
  news yet very likely in the game broadcast.

## Recommended tradeable regime

**A GDELT-only mentions read is not tradeable.** Require **corpus-backed**
(speaker-attributed) evidence before emitting a tradeable edge. The narrower
saturation gate (predicted ≥0.85 or ≤0.15) alone would suppress 353/455 = **78%**
of these reads — the most-miscalibrated extremes — but the mid-band is also
unreliable, so the full require-corpus regime is the defensible default.

This is implemented in `kalshi_trader/signals/mentions.py`:
- `config.MENTIONS_REQUIRE_CORPUS_BACKED = True` (default) → every GDELT-only read
  is flagged non-informative (`uncertainty>=0.99`), so the scorer drops it.
- `config.MENTIONS_SATURATION_HIGH/LOW = 0.85/0.15` → the fallback gate if
  require-corpus is ever disabled.
- Corpus-backed reads (`document_count >= CORPUS_BACKED_DOC_THRESHOLD = 12`) are
  exempt and remain tradeable.

**Practical consequence:** with the corpus archive empty, the mentions scan will
correctly show **every current market as `suppressed` / not tradeable** — the
honest state until the speaker-attributed corpus is populated (which the now-fixed
`refresh_mentions_archive` enables).

## Caveats

- **Corpus-backed tier unvalidated here** (n=0 — archive empty). The premise that
  speaker-attributed counts predict better is intuitive but not yet measured; re-run
  this backtest after populating the corpus to validate the exempt path.
- **Sports-dominated sample** (406 of 455). The political/hearing markets (Fed 48,
  Love Island 31) are a small slice; their Brier is even worse, but the sample is thin.
- **Look-ahead guard is monthly-granular** (drops timeline points after the close
  month); intra-month leakage is possible but cannot help a base rate this poorly
  correlated.
- Reproduce: `KALSHI_ENV=prod PYTHONPATH=. .venv/bin/python scripts/mentions_backtest.py --max-per-series 150`.
