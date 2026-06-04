#!/usr/bin/env python3
"""Q2 corpus-premise backtest: does a speaker-attributed rate beat the base rate?

Read-only research harness. It does **not** touch Kalshi or place orders, and it
writes only to a dedicated archive db (default ``/tmp/mentions_premise_archive.db``),
never the production ``kalshi_trader/mentions_archive.db``.

This is the gate the respondent (2026-06-04 deep-research answer) put before any
transcript-based build: validate, on free attributed transcripts, that "how often
*this speaker* has said a phrase" predicts a new occasion better than the global base
rate. If it does not, the transcript pipeline is not worth building.

Two phases:

* ``--populate`` — fill the dedicated archive with free speaker-attributed transcripts
  (Fed speeches/testimony + FOMC pressers always; CREC floor speeches with
  ``--with-crec`` and a ``DATA_GOV_API_KEY``). Idempotent; safe to re-run.
* ``--score``   — run the strict walk-forward nested-model scorer
  (:mod:`kalshi_trader.mentions.premise_backtest`) per venue and report the
  speaker-vs-global Brier Skill Score with a bootstrap credible interval. The
  decision rule: a credible interval whose lower bound is at or below zero means the
  premise is not supported out-of-sample.

Single-speaker venues (FOMC pressers — Powell only) cannot test speaker-vs-global
(the speaker rate *is* the global rate there), so they are skipped in 2A; they feed
the separate 2B Fed-market tie-in in scripts/mentions_backtest.py.

Usage:
  PYTHONPATH=. .venv/bin/python scripts/mentions_corpus_premise.py \
    --db /tmp/mentions_premise_archive.db --populate [--with-crec --crec-since 2025-06-01]
  PYTHONPATH=. .venv/bin/python scripts/mentions_corpus_premise.py \
    --db /tmp/mentions_premise_archive.db --score --out /tmp/mentions_corpus_premise.json
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
from datetime import datetime, timezone

sys.path.insert(0, ".")

import kalshi_trader.config  # noqa: F401 — loads .env so DATA_GOV_API_KEY is set
from kalshi_trader.external.fed import FedClient
from kalshi_trader.external.govinfo import GovInfoClient
from kalshi_trader.external.speaker_registry import VENUE_FED_PRESSER
from kalshi_trader.mentions.premise_backtest import score_corpus_premise, summarize_premise
from kalshi_trader.mentions.store import MentionsArchiveStore

DEFAULT_DB_PATH = "/tmp/mentions_premise_archive.db"

# Real, tradeable Fed/economic mention phrases — the phrases we would actually price.
DEFAULT_PHRASES = [
    "recession", "inflation", "stagflation", "soft landing", "shutdown",
    "tariff", "tariffs", "deficit", "uncertainty", "transitory",
    "labor market", "unemployment", "rate cut", "interest rates",
]

# Venues where only one speaker ever appears → speaker rate == global rate, so the
# premise is untestable there (it is tested for these via the 2B Fed-market tie-in).
SINGLE_SPEAKER_VENUES = {VENUE_FED_PRESSER}
MINIMUM_SPEAKERS_FOR_PREMISE = 2
# Below this many pooled predictions a venue result is reported as directional only.
MINIMUM_PREDICTIONS_FOR_SIGNIFICANCE = 50


async def populate_archive(
    store: MentionsArchiveStore,
    *,
    speeches_since: str | None,
    with_crec: bool,
    crec_since: str,
    crec_max_packages: int,
    crec_granules: int,
) -> None:
    """Fill the dedicated archive with free speaker-attributed transcripts."""
    fed_client = FedClient()
    govinfo_client = GovInfoClient()
    try:
        presser_records = await fed_client.get_presser_transcripts()
        store.upsert_transcripts(presser_records)
        print(f"  fed pressers:            {len(presser_records)} records", file=sys.stderr)

        speech_records = await fed_client.get_speeches(since=speeches_since)
        store.upsert_transcripts(speech_records)
        print(f"  fed speeches/testimony:  {len(speech_records)} records "
              f"(since {speeches_since})", file=sys.stderr)

        if with_crec:
            crec_records = await govinfo_client.get_crec_records(
                since=crec_since,
                max_packages=crec_max_packages,
                max_granules_per_package=crec_granules,
            )
            store.upsert_transcripts(crec_records)
            attributed = sum(1 for record in crec_records if record.get("speaker_key"))
            print(f"  crec floor:              {len(crec_records)} records "
                  f"({attributed} attributed) since {crec_since}", file=sys.stderr)
            if not crec_records:
                print("    (CREC empty — missing DATA_GOV_API_KEY, or rate-limited)", file=sys.stderr)
    finally:
        await fed_client.close()
        await govinfo_client.close()


def score_archive(store: MentionsArchiveStore, phrases: list[str]) -> dict:
    """Run the per-venue walk-forward premise scorer over everything in the archive."""
    venue_reports: dict[str, dict] = {}
    pooled_predictions: list = []

    for venue_type in store.distinct_venue_types():
        speaker_count = store.distinct_speaker_count(venue_type)
        events = store.list_transcript_events(venue_type)
        report: dict = {
            "venue_type": venue_type,
            "event_count": len(events),
            "speaker_count": speaker_count,
        }
        if venue_type in SINGLE_SPEAKER_VENUES or speaker_count < MINIMUM_SPEAKERS_FOR_PREMISE:
            report["skipped"] = (
                "single-speaker venue — speaker rate equals the global rate here, so "
                "the premise is untestable (see the 2B Fed-market tie-in instead)"
            )
            venue_reports[venue_type] = report
            continue

        predictions = score_corpus_premise(events, phrases)
        report.update(summarize_premise(predictions))
        if report["prediction_count"] < MINIMUM_PREDICTIONS_FOR_SIGNIFICANCE:
            report["caveat"] = (
                f"only {report['prediction_count']} pooled predictions — directional "
                f"only, not significant"
            )
        venue_reports[venue_type] = report
        pooled_predictions.extend(predictions)

    pooled_report: dict = {"prediction_count": len(pooled_predictions)}
    if pooled_predictions:
        pooled_report.update(summarize_premise(pooled_predictions))

    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "phrases": phrases,
        "per_venue": venue_reports,
        "pooled_multi_speaker": pooled_report,
    }


def _print_score_summary(results: dict) -> None:
    print("\n=== Corpus-premise backtest (speaker rate vs global base rate) ===")
    for venue_type, report in results["per_venue"].items():
        if "skipped" in report:
            print(f"\n{venue_type}: SKIPPED — {report['skipped']}")
            print(f"  ({report['event_count']} events, {report['speaker_count']} speaker(s))")
            continue
        interval = report["skill_credible_interval"]
        verdict = "SUPPORTED" if report["premise_supported"] else "NOT supported"
        print(f"\n{venue_type}: {report['event_count']} events, "
              f"{report['speaker_count']} speakers, {report['prediction_count']} predictions")
        print(f"  Brier  global={report['brier_global']:.4f}  speaker={report['brier_speaker']:.4f}")
        print(f"  Skill (speaker vs global) = {report['brier_skill_score']:.4f} "
              f"[95% CI {interval['percentile_2_5']:.4f} .. {interval['percentile_97_5']:.4f}]")
        print(f"  Sign test: speaker wins {report['paired_sign_test']['speaker_wins']} / "
              f"global wins {report['paired_sign_test']['global_wins']} "
              f"(p={report['paired_sign_test']['p_value']:.4f})")
        print(f"  Premise {verdict}." + (f"  [{report['caveat']}]" if "caveat" in report else ""))

    pooled = results["pooled_multi_speaker"]
    if pooled.get("prediction_count"):
        interval = pooled["skill_credible_interval"]
        verdict = "SUPPORTED" if pooled["premise_supported"] else "NOT supported"
        print(f"\nPOOLED (multi-speaker venues): {pooled['prediction_count']} predictions")
        print(f"  Skill = {pooled['brier_skill_score']:.4f} "
              f"[95% CI {interval['percentile_2_5']:.4f} .. {interval['percentile_97_5']:.4f}] → premise {verdict}")
    print("\nDecision rule: premise holds only where the skill credible interval's "
          "LOWER bound is strictly > 0.\n")


def _main() -> None:
    parser = argparse.ArgumentParser(description="Q2 corpus-premise backtest (read-only)")
    parser.add_argument("--db", default=DEFAULT_DB_PATH,
                        help="Dedicated archive db path (NOT the production archive)")
    parser.add_argument("--populate", action="store_true", help="Fetch + store transcripts")
    parser.add_argument("--score", action="store_true", help="Run the walk-forward scorer")
    parser.add_argument("--out", default="/tmp/mentions_corpus_premise.json")
    parser.add_argument("--phrases", nargs="*", default=DEFAULT_PHRASES)
    parser.add_argument("--speeches-since", default="2020-01-01",
                        help="Lower bound for the Fed speeches RSS (it only indexes recent items)")
    parser.add_argument("--with-crec", action="store_true",
                        help="Also pull CREC floor speeches (needs DATA_GOV_API_KEY; slow/rate-limited)")
    parser.add_argument("--crec-since", default="2025-06-01")
    parser.add_argument("--crec-max-packages", type=int, default=20,
                        help="CREC daily issues to walk (each ~ one Congressional Record day)")
    parser.add_argument("--crec-granules", type=int, default=40,
                        help="Max granules (floor statements) per daily issue")
    args = parser.parse_args()

    if not args.populate and not args.score:
        parser.error("pass --populate and/or --score")
    if args.db == MentionsArchiveStore.DEFAULT_DB_PATH:
        parser.error("refusing to use the production archive db; pass a dedicated --db")

    store = MentionsArchiveStore(db_path=args.db)
    try:
        if args.populate:
            print(f"Populating {args.db} ...", file=sys.stderr)
            asyncio.run(populate_archive(
                store,
                speeches_since=args.speeches_since,
                with_crec=args.with_crec,
                crec_since=args.crec_since,
                crec_max_packages=args.crec_max_packages,
                crec_granules=args.crec_granules,
            ))
        if args.score:
            results = score_archive(store, args.phrases)
            with open(args.out, "w") as handle:
                json.dump(results, handle, indent=2, default=str)
            _print_score_summary(results)
            print(f"Wrote {args.out}", file=sys.stderr)
    finally:
        store.close()


if __name__ == "__main__":
    _main()
