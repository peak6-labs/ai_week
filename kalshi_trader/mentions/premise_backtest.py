"""Walk-forward scorer for the "speaker-attributed rate beats the base rate" premise.

This is the statistical core of the corpus-premise backtest (research question Q2):
does *how often this speaker has said a phrase* predict whether they say it on a new
occasion better than *how often anyone says it* (the global base rate)?

The test is strictly walk-forward and self-supervised over a transcript corpus — each
transcript is one "event," and for each event we predict P(phrase appears in it) from
**only earlier** transcripts, then compare against what the event actually contained.
Two nested models are scored:

* ``global``  — the unconditional rate of the phrase across *all* speakers' earlier
  transcripts in the venue (the null: "ubiquity").
* ``speaker`` — the rate across *this speaker's* earlier transcripts, shrunk toward the
  global rate so sparse speakers borrow strength instead of reading 0/1 (the premise).

The decision rule lives in the caller: if the speaker model's Brier Skill Score over
the global model has a bootstrap credible interval that includes or sits below zero,
the premise is not supported out-of-sample.

Pure functions only (numpy + scipy); no I/O, so it is unit-testable with inline events.
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy

from kalshi_trader.external.mentions_parser import normalize_for_match

# Predictions are clamped away from {0, 1} so a hard 0/1 prior cannot inflict an
# infinite-feeling Brier penalty on a single surprising event — applied equally to
# both models so neither is advantaged.
PROBABILITY_CLAMP_LOW = 0.01
PROBABILITY_CLAMP_HIGH = 0.99

# Shrinkage strength toward the global rate for the speaker model. Mirrors
# CORPUS_SHRINKAGE_K in kalshi_trader/signals/mentions.py so the backtest's speaker
# estimate is built the same way the production signal would build it.
DEFAULT_SHRINKAGE_STRENGTH = 5.0


def _clamp_probability(probability: float) -> float:
    return min(max(probability, PROBABILITY_CLAMP_LOW), PROBABILITY_CLAMP_HIGH)


@dataclass
class ScoredPrediction:
    """One walk-forward prediction for a (phrase, event) pair."""

    phrase: str
    speaker_key: str
    event_date: str
    realized_outcome: int           # 1 if this event's text contained the phrase
    global_prediction: float        # null model: all-speaker prior rate
    speaker_prediction: float       # premise: this-speaker prior rate, shrunk to global
    speaker_had_prior_history: bool # False when the speaker had no earlier transcripts


def walk_forward_predictions_for_phrase(
    events: list[dict],
    phrase: str,
    *,
    shrinkage_strength: float = DEFAULT_SHRINKAGE_STRENGTH,
) -> list[ScoredPrediction]:
    """Score one phrase across one venue's events, strictly walk-forward.

    Args:
        events: transcript events for a single venue, each a dict with
            ``speaker_key``, ``event_date`` (``YYYY-MM-DD``), and ``norm_text``
            (already punctuation-folded). Order does not matter — sorted internally.
            Events without an attributed speaker or a date are dropped (they cannot
            be placed in the timeline or attributed).
        phrase: the phrase whose occurrence is being predicted.
        shrinkage_strength: pseudo-count pulling the speaker rate toward the global rate.

    Returns:
        One ``ScoredPrediction`` per event that had a non-empty prior global history
        (the earliest date group is unscored — there is nothing earlier to predict
        from). Same-date events never see each other: priors use only events whose
        date is **strictly before** the event being scored, so there is no look-ahead.
    """
    normalized_phrase = normalize_for_match(phrase)
    if not normalized_phrase:
        return []

    annotated_events: list[tuple[str, str, int]] = []
    for event in events:
        speaker_key = event.get("speaker_key") or ""
        event_date = event.get("event_date") or ""
        if not speaker_key or not event_date:
            continue
        contains_phrase = 1 if normalized_phrase in (event.get("norm_text") or "") else 0
        annotated_events.append((event_date, speaker_key, contains_phrase))
    annotated_events.sort(key=lambda row: row[0])

    predictions: list[ScoredPrediction] = []
    global_document_count = 0
    global_match_count = 0
    speaker_document_counts: dict[str, int] = {}
    speaker_match_counts: dict[str, int] = {}

    event_index = 0
    total_events = len(annotated_events)
    while event_index < total_events:
        current_date = annotated_events[event_index][0]
        date_group: list[tuple[str, str, int]] = []
        while event_index < total_events and annotated_events[event_index][0] == current_date:
            date_group.append(annotated_events[event_index])
            event_index += 1

        # Score this date's events against priors built from strictly-earlier events.
        if global_document_count > 0:
            global_rate = global_match_count / global_document_count
            for (event_date, speaker_key, contains_phrase) in date_group:
                prior_speaker_documents = speaker_document_counts.get(speaker_key, 0)
                prior_speaker_matches = speaker_match_counts.get(speaker_key, 0)
                speaker_rate = (
                    (prior_speaker_matches + shrinkage_strength * global_rate)
                    / (prior_speaker_documents + shrinkage_strength)
                )
                predictions.append(
                    ScoredPrediction(
                        phrase=phrase,
                        speaker_key=speaker_key,
                        event_date=event_date,
                        realized_outcome=contains_phrase,
                        global_prediction=_clamp_probability(global_rate),
                        speaker_prediction=_clamp_probability(speaker_rate),
                        speaker_had_prior_history=prior_speaker_documents > 0,
                    )
                )

        # Only now fold this date's events into the running priors.
        for (event_date, speaker_key, contains_phrase) in date_group:
            global_document_count += 1
            global_match_count += contains_phrase
            speaker_document_counts[speaker_key] = speaker_document_counts.get(speaker_key, 0) + 1
            speaker_match_counts[speaker_key] = speaker_match_counts.get(speaker_key, 0) + contains_phrase

    return predictions


def score_corpus_premise(
    events: list[dict],
    phrases: list[str],
    *,
    shrinkage_strength: float = DEFAULT_SHRINKAGE_STRENGTH,
) -> list[ScoredPrediction]:
    """Pool walk-forward predictions across every phrase for one venue's events."""
    pooled: list[ScoredPrediction] = []
    for phrase in phrases:
        pooled.extend(
            walk_forward_predictions_for_phrase(
                events, phrase, shrinkage_strength=shrinkage_strength
            )
        )
    return pooled


def _squared_errors(predictions: list[ScoredPrediction], attribute: str) -> numpy.ndarray:
    return numpy.array(
        [(getattr(prediction, attribute) - prediction.realized_outcome) ** 2 for prediction in predictions],
        dtype=float,
    )


def brier_score(predictions: list[ScoredPrediction], attribute: str) -> float:
    """Mean squared error of one model's predictions vs realized outcomes."""
    if not predictions:
        return float("nan")
    return float(_squared_errors(predictions, attribute).mean())


def brier_skill_score(brier_candidate: float, brier_reference: float) -> float:
    """1 - candidate/reference. Positive ⇒ candidate beats the reference model."""
    if not brier_reference:
        return float("nan")
    return 1.0 - brier_candidate / brier_reference


def bootstrap_skill_interval(
    predictions: list[ScoredPrediction],
    *,
    draws: int = 2000,
    seed: int = 12345,
) -> dict:
    """Bootstrap 95% credible interval for the speaker-vs-global Brier Skill Score.

    Resamples (phrase, event) predictions with replacement and recomputes the skill
    each draw. Returns the 2.5 / 50 / 97.5 percentiles. The decision rule reads
    ``percentile_2_5``: if it is at or below zero, the premise is not supported.
    """
    if len(predictions) < 2:
        return {"percentile_2_5": float("nan"), "percentile_50": float("nan"),
                "percentile_97_5": float("nan"), "draws": 0}
    global_errors = _squared_errors(predictions, "global_prediction")
    speaker_errors = _squared_errors(predictions, "speaker_prediction")
    random_generator = numpy.random.default_rng(seed)
    sample_count = len(predictions)
    skills = numpy.empty(draws, dtype=float)
    for draw_index in range(draws):
        resample_indices = random_generator.integers(0, sample_count, sample_count)
        brier_global = global_errors[resample_indices].mean()
        brier_speaker = speaker_errors[resample_indices].mean()
        skills[draw_index] = (1.0 - brier_speaker / brier_global) if brier_global > 0 else numpy.nan
    finite_skills = skills[numpy.isfinite(skills)]
    if finite_skills.size == 0:
        return {"percentile_2_5": float("nan"), "percentile_50": float("nan"),
                "percentile_97_5": float("nan"), "draws": 0}
    return {
        "percentile_2_5": float(numpy.percentile(finite_skills, 2.5)),
        "percentile_50": float(numpy.percentile(finite_skills, 50)),
        "percentile_97_5": float(numpy.percentile(finite_skills, 97.5)),
        "draws": int(finite_skills.size),
    }


def paired_sign_test(predictions: list[ScoredPrediction]) -> dict:
    """Paired sign test: how often does the speaker model beat the global model?

    Counts per-event squared-error 'wins' for the speaker model vs the global model
    (ties dropped) and returns a two-sided binomial p-value against 0.5. A robustness
    check on the bootstrap that does not depend on the magnitude of the error gap.
    """
    speaker_wins = 0
    global_wins = 0
    for prediction in predictions:
        speaker_error = (prediction.speaker_prediction - prediction.realized_outcome) ** 2
        global_error = (prediction.global_prediction - prediction.realized_outcome) ** 2
        if speaker_error < global_error:
            speaker_wins += 1
        elif global_error < speaker_error:
            global_wins += 1
    decisive_count = speaker_wins + global_wins
    if decisive_count == 0:
        return {"speaker_wins": 0, "global_wins": 0, "p_value": float("nan")}
    from scipy.stats import binomtest

    p_value = binomtest(speaker_wins, decisive_count, 0.5).pvalue
    return {"speaker_wins": speaker_wins, "global_wins": global_wins, "p_value": float(p_value)}


def summarize_premise(predictions: list[ScoredPrediction]) -> dict:
    """Full premise summary for one venue: Briers, skill, credible interval, sign test."""
    brier_global = brier_score(predictions, "global_prediction")
    brier_speaker = brier_score(predictions, "speaker_prediction")
    skill = brier_skill_score(brier_speaker, brier_global)
    interval = bootstrap_skill_interval(predictions)
    sign_test = paired_sign_test(predictions)
    premise_supported = (
        interval["percentile_2_5"] == interval["percentile_2_5"]  # not NaN
        and interval["percentile_2_5"] > 0.0
    )
    return {
        "prediction_count": len(predictions),
        "speaker_with_history_count": sum(1 for p in predictions if p.speaker_had_prior_history),
        "brier_global": brier_global,
        "brier_speaker": brier_speaker,
        "brier_skill_score": skill,
        "skill_credible_interval": interval,
        "paired_sign_test": sign_test,
        "premise_supported": bool(premise_supported),
    }
