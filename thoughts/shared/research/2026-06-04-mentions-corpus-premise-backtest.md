---
date: 2026-06-04
author: Claude (Opus 4.8)
status: findings
topic: Q2 corpus-premise backtest — does a speaker-attributed rate beat the base rate?
companion: 2026-06-04-mentions-price-mispricing-study.md (the Q6 validation)
reproduce: |
  # 2A — populate a DEDICATED archive (never the production db), then score
  PYTHONPATH=. .venv/bin/python scripts/mentions_corpus_premise.py \
    --db /tmp/mentions_premise_archive.db --populate \
    --with-crec --crec-since 2025-09-01 --crec-max-packages 12 --crec-granules 30
  PYTHONPATH=. .venv/bin/python scripts/mentions_corpus_premise.py \
    --db /tmp/mentions_premise_archive.db --score --out /tmp/mentions_corpus_premise.json
  # 2B — tie to real settled Fed markets (prod read-only)
  KALSHI_ENV=prod PYTHONPATH=. .venv/bin/python scripts/mentions_backtest.py \
    --series KXFEDMENTION --fed-corpus-asof --db /tmp/mentions_premise_archive.db \
    --out /tmp/mentions_backtest_fed_corpus.json
---

# Q2 — Does a speaker-attributed rate beat the base rate? (corpus-premise backtest)

## What this tested

The respondent put one gate before any transcript-scraping build: validate, on free
attributed transcripts, that *how often this speaker has said a phrase* predicts a new
occasion **better than the global base rate** — strictly walk-forward, scored by
Brier Skill Score, with the decision rule "if the speaker rate does not beat the global
rate out-of-sample, the premise is refuted and we stop."

Two complementary tests:

- **2A (pure premise, self-supervised):** each transcript is one "event"; for each
  `(speaker, phrase, event)` predict P(phrase in this event) from only *earlier*
  transcripts, comparing the speaker's shrunk rate vs the all-speaker rate. Run per
  venue so document-length differences do not bias the comparison. Corpus: Fed
  speeches/testimony + FOMC pressers (no key) and 136 CREC floor statements (Sep 2025+).
- **2B (tie to real markets):** for the 48 settled `KXFEDMENTION` markets, count
  Powell's pressers as-of each market's close (strict `until` cutoff — no look-ahead)
  and score the corpus-backed fused signal vs naive.

## Results

### 2A — per venue (walk-forward Brier Skill Score, speaker vs global)

| venue | events | speakers | predictions | Brier global | Brier speaker | Skill [95% CI] | Sign test (speaker:global wins) |
|---|---:|---:|---:|---:|---:|---|---|
| congress_floor | 136 | 83 | 1694 | 0.0149 | 0.0148 | **+0.004 [−0.005, +0.019]** | **172 : 8**  (p<0.0001) |
| fed_speech | 27 | 9 | 364 | 0.1499 | 0.1494 | **+0.003 [−0.036, +0.044]** | 129 : 68 (p<0.0001) |
| fed_presser | 27 | 1 | — | — | — | skipped (single speaker) | — |
| **pooled** | — | — | 2058 | — | — | **+0.004 [−0.025, +0.033]** | — |

### 2B — Fed markets (KXFEDMENTION, n = 48; all corpus-backed as-of close)

| model | Brier | 95% CI | note |
|---|---:|---|---|
| naive (predict 0.333 base rate) | 0.222 | — | |
| corpus-backed fused signal | **0.214** | [0.144, 0.290] | hit-rate 0.729 |
| the *market* (politics, 24 h out, from Q6) | **0.153** | — | for comparison |

## The finding: premise NOT supported on the metric that pays, but a real directional signal exists

This is a two-sided result and the distinction matters:

- **On Brier — the metric that pays for calibration-based trading — the speaker rate
  does not beat the global rate.** The skill credible interval includes zero in every
  venue and pooled (lower bound ≤ 0). By the respondent's strict decision rule, the
  premise is **not validated out-of-sample.**
- **But the speaker rate carries genuine "who says it" information.** When the speaker
  and global models *disagree*, the speaker model is right overwhelmingly — 172:8 on the
  floor, 129:68 on speeches (both p<0.0001). The reason this does not show up in Brier:
  floor/speech base rates are extreme (most short granules contain none of the tracked
  economic phrases, so both models predict ~near-zero and rack up near-identical tiny
  errors). The speaker model's correct nudges are directionally right but small in
  absolute probability, so they barely move the pooled Brier.

- **2B confirms it on real Fed markets.** Even with every Fed market corpus-backed, the
  speaker-attributed fused signal (Brier 0.214) is only at *parity* with naive (0.222,
  and the CI [0.144, 0.290] comfortably overlaps it) — and, crucially, it is **worse
  than the market price** (0.153 a day out, from Q6). Speaker attribution does not buy
  enough calibration to beat either bar.

## Recommendation

- **Do not build the transcript-scraping pipeline as a calibrated probability source.**
  Across both the self-supervised corpus test and the real Fed-market tie-in, the
  speaker-attributed rate does not improve Brier over the base rate, and it is beaten by
  the market price the signal would have to trade against (Q6). The intuitive premise is
  real in *direction* but does not translate into a *tradeable calibrated edge* here.
- Keep `MENTIONS_REQUIRE_CORPUS_BACKED = True` (GDELT-only stays suppressed); this work
  does not change it.
- If anything in the mentions category is pursued next, it is the **price-structural**
  candidate from Q6 (15–50¢ favorite-longshot overpricing), not transcripts.

## Caveats (why this is "not supported" rather than "flatly refuted")

- **Thin, recent corpus.** Fed speeches come only from what the RSS currently indexes
  (27 items, 9 speakers); CREC was a bounded Sep-2025+ pull (136 statements) limited by
  the api.data.gov rate cap. Deeper history would tighten the wide CIs — the
  point estimate is mildly positive and the sign test is strongly positive, so a larger
  corpus *could* lift the lower bound above zero. This refutes "build it now," not
  "the idea can never work."
- **Granularity mismatch.** A CREC floor granule (one short statement) and a single
  speech are poor analogs for a whole bounded Kalshi event (a ~45-min presser, a ~2.5-h
  game). Per-granule base rates are far lower than per-event, which compresses Brier and
  hides the directional signal. A proper test would aggregate to event-sized windows.
- **The actual Fed venue is single-speaker.** `fed_presser` (Powell only) cannot be
  tested in 2A; 2B is its only proxy and is limited to ~27 pressers of history.
