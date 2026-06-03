"""CLI: python -m kalshi_trader.pipelines.mentions --ticker X --title "..."

Deterministic, speaker-routed "mentions" signal. Parses the market title into a
phrase + speaker, routes via the speaker registry to the right TV stations and
transcript corpora, fuses a speaker-attributed archive count with the GDELT TV
base rate, and prints a list[SignalEstimate] JSON. Empty list [] on no signal,
on a written-post market (the X signal owns those), or on error. No LLM.
"""
from __future__ import annotations
import argparse
import asyncio
import json
import sys
from datetime import datetime, timedelta, timezone
import kalshi_trader.config  # noqa: F401 — loads .env so API keys are set
from kalshi_trader.agents.parsing import estimate_to_dict
from kalshi_trader.agents.settlement_context import parse_settlement_arg, settlement_metadata
from kalshi_trader.external.gdelt import GDELTClient
from kalshi_trader.external.mentions_parser import (
    extract_chamber_hint,
    extract_committee_hint,
    is_written_post_market,
    parse_mention_title,
    parse_window_days,
    recency_weighted_base_rate,
    window_aligned_fraction,
)
from kalshi_trader.external.speaker_registry import resolve_speaker
from kalshi_trader.external.x_client import XClient
from kalshi_trader.signals.mentions import (
    build_hearing_schedule_signal,
    build_mentions_base_signal,
    build_mentions_live_signal,
    build_x_profile_signal,
)

# A market's resolution window counts as "open" (run near-real-time detection) when
# it resolves over a short window or closes within this many days.
LIVE_WINDOW_MAX_DAYS = 14

# Human-readable window label for the X-profile scan prompt, by resolution-window
# length in days. Falls back to a recent-week window when the title names none.
_WINDOW_LABELS = {1: "today", 7: "this week", 30: "this month", 365: "this year"}


def _is_live_window_open(close_date: str | None, window_days: int | None, now=None) -> bool:
    """True when the market's resolution window is active now (run live detection).

    Open if the title names a short window (≤ ~a month) or the market closes within
    ``LIVE_WINDOW_MAX_DAYS``. A market closing months out is too early for a
    same-day caption match to mean anything, so it stays closed.
    """
    if window_days is not None and window_days <= 31:
        return True
    if close_date:
        try:
            close_datetime = datetime.strptime(close_date, "%Y-%m-%d").replace(tzinfo=timezone.utc)
        except (ValueError, TypeError):
            return False
        days_until_close = (close_datetime - (now or datetime.now(tz=timezone.utc))).days
        return 0 <= days_until_close <= LIVE_WINDOW_MAX_DAYS
    return False


def _resolve_close_date(close_time: str | None, window_days: int | None) -> str | None:
    """Resolve the market's close to a ``YYYY-MM-DD`` window end for the schedule veto.

    Prefers an explicit ``--close-time`` (ISO); otherwise derives ``today + window``
    from the title's resolution window; None when neither is available.
    """
    if close_time:
        try:
            return datetime.fromisoformat(close_time.replace("Z", "+00:00")).strftime("%Y-%m-%d")
        except (ValueError, TypeError):
            pass
    if window_days:
        return (datetime.now(tz=timezone.utc) + timedelta(days=window_days)).strftime("%Y-%m-%d")
    return None


def _open_store():
    """Open the mentions archive, or return None if it can't be opened.

    The archive is an optional accelerant: when present it supplies the
    speaker-attributed corpus count and a cached GDELT base rate. If it can't be
    opened (e.g. before the first refresh) the pipeline still works off live GDELT.
    """
    try:
        from kalshi_trader.mentions.store import MentionsArchiveStore
        return MentionsArchiveStore()
    except Exception:
        return None


def main() -> None:
    parser = argparse.ArgumentParser(description="GDELT mentions signal pipeline")
    parser.add_argument("--ticker", required=True)
    parser.add_argument("--title", required=True)
    parser.add_argument(
        "--settlement-json",
        dest="settlement_json",
        default=None,
        help="JSON object of contract settlement context from market_rules.py. "
        "Recorded on the signal's metadata as the settlement basis, and used to "
        "detect a written-post market (which this spoken-mentions pipeline skips).",
    )
    parser.add_argument(
        "--close-time",
        dest="close_time",
        default=None,
        help="Market close time (ISO 8601). Bounds the hearing-schedule veto's "
        "resolution window; falls back to the window parsed from the title.",
    )
    args = parser.parse_args()

    async def run() -> None:
        client = GDELTClient()
        x_client = XClient()
        store = _open_store()
        try:
            parsed = parse_mention_title(args.ticker, args.title)
            if parsed is None:
                print(json.dumps([]))
                return

            settlement = parse_settlement_arg(args.settlement_json)
            # Wrong-signal guard: a written Truth Social / X post is not spoken, so
            # a transcript/TV base rate measures the wrong thing — let X own it.
            if is_written_post_market(args.title, settlement):
                print(json.dumps([]))
                return

            phrase = parsed["phrase"]
            window_days = parse_window_days(args.title)
            close_date = _resolve_close_date(args.close_time, window_days)
            profile = resolve_speaker(parsed["speaker"])
            # Known speakers route to their registry stations; unknown speakers
            # fall back to the venue-derived station from the title (today's CSPAN
            # default for hearings/briefings).
            stations = profile.gdelt_stations if profile.is_known else [parsed["station"]]
            station_key = "+".join(stations)

            # GDELT base rate — served from the 7-day cache when warm.
            gdelt_base_rate = store.get_gdelt_base_rate(phrase, station_key) if store else None
            if gdelt_base_rate is None:
                timeline = await client.get_mention_timeline(phrase, stations=stations)
                gdelt_base_rate = recency_weighted_base_rate(timeline["points"])
                if window_days:
                    window_summary = window_aligned_fraction(timeline["points"], window_days)
                    gdelt_base_rate["window_fraction"] = window_summary["fraction"]
                    gdelt_base_rate["window_days"] = window_days
                if store and gdelt_base_rate["period_count"] > 0:
                    store.put_gdelt_base_rate(phrase, station_key, gdelt_base_rate)

            # Speaker-attributed corpus count (empty until the archive is populated).
            corpus = None
            if store and profile.speaker_key:
                corpus = store.count_phrase(profile.speaker_key, None, phrase)

            basis = settlement_metadata(settlement)
            estimates = []

            base_estimate = build_mentions_base_signal(
                ticker=args.ticker,
                phrase=phrase,
                stations=stations,
                gdelt_base_rate=gdelt_base_rate,
                corpus=corpus,
                speaker=parsed["speaker"],
                speaker_key=profile.speaker_key,
            )
            if base_estimate is not None:
                estimates.append(base_estimate)

            # X-profile leading indicator: scan the speaker's own handle set for how
            # much they're posting about the topic. Only for registered speakers with
            # a handle set; emits only when those accounts are actually on the topic
            # (post_count==0 → no estimate). Folds into the x_grok family downstream.
            if profile.x_handles:
                window_label = _WINDOW_LABELS.get(window_days, "this week")
                scan = await x_client.profile_topic_scan(profile.x_handles, phrase, window_label)
                profile_estimate = build_x_profile_signal(
                    ticker=args.ticker,
                    phrase=phrase,
                    scan=scan,
                    handles=profile.x_handles,
                    speaker=parsed["speaker"],
                )
                if profile_estimate is not None:
                    estimates.append(profile_estimate)

            # Near-real-time live detector — only while the market window is open.
            # A 2nd (uncached) GDELT call against the last day's captions on the
            # speaker's stations; emits only on a recent match, stamped with the
            # clip's own time. Shares GDELT lineage with mentions_base (not independent).
            if _is_live_window_open(close_date, window_days):
                live_timeline = await client.get_mention_timeline(phrase, stations=stations, live=True)
                live_estimate = build_mentions_live_signal(
                    ticker=args.ticker,
                    phrase=phrase,
                    stations=stations,
                    live_points=live_timeline["points"],
                    speaker=parsed["speaker"],
                )
                if live_estimate is not None:
                    estimates.append(live_estimate)

            # Hearing-schedule veto — only when the title names a committee and we
            # have a resolution window. Reads the schedule the refresh pre-warms;
            # a no-op (no rows) until a DATA_GOV_API_KEY-backed refresh populates it.
            committee_hint = extract_committee_hint(args.title)
            if store and committee_hint and close_date:
                schedule_records = store.get_schedule(chamber=extract_chamber_hint(args.title))
                schedule_estimate = build_hearing_schedule_signal(
                    ticker=args.ticker,
                    phrase=phrase,
                    schedule_records=schedule_records,
                    committee_hint=committee_hint,
                    close_date=close_date,
                    speaker=parsed["speaker"],
                )
                if schedule_estimate is not None:
                    estimates.append(schedule_estimate)

            for estimate in estimates:
                if basis:
                    estimate.metadata["settlement_basis"] = basis
            print(json.dumps([estimate_to_dict(estimate) for estimate in estimates], default=str))
        except Exception as caught_exception:
            print(f"Error: {caught_exception}", file=sys.stderr)
            print(json.dumps([]))
        finally:
            await client.close()
            await x_client.close()
            if store:
                store.close()

    asyncio.run(run())


if __name__ == "__main__":
    main()
