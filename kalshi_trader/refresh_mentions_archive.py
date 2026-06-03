"""CLI: python -m kalshi_trader.refresh_mentions_archive [--if-stale] [--markets-file F]

Populate the mentions archive (``kalshi_trader/mentions_archive.db``) with
speaker-attributed transcripts + the hearing schedule, so the mentions pipeline can
count how often *a specific person* says a phrase rather than how often it appears
on TV at all.

Each source runs in isolation: a failure is logged and skipped, and ``refresh_log``
is written **only on success**, so a failed source stays stale and retries next run
while the others still commit. ``--if-stale`` tops up only the sources past their
TTL (a cheap no-op when the archive is warm) — that's the form the orchestrate
setup step calls. ``--markets-file`` (a JSON market snapshot) discovers which
speakers/venues are live so the nightly job can flag new targets.

Sources: Fed speeches/testimony (full HTML), FOMC pressers (PDF, Chair turns),
congress.gov committee schedule, GovInfo CREC floor speeches, White House remarks.
congress.gov + CREC require ``DATA_GOV_API_KEY`` (else they no-op to ``[]``).
"""
from __future__ import annotations

import argparse
import asyncio
import json
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

import kalshi_trader.config  # noqa: F401 — loads .env
from kalshi_trader.external.congress_gov import CongressGovClient
from kalshi_trader.external.fed import FedClient
from kalshi_trader.external.govinfo import GovInfoClient
from kalshi_trader.external.mentions_parser import parse_mention_title
from kalshi_trader.external.speaker_registry import resolve_speaker
from kalshi_trader.external.whitehouse import WhiteHouseClient
from kalshi_trader.mentions.store import MentionsArchiveStore

# Per-source staleness TTLs (seconds).
_SOURCE_TTL: dict[str, int] = {
    "fed_speeches": 20 * 3600,        # the RSS index turns over daily
    "fed_pressers": 7 * 86400,        # ~8 meetings/year — weekly top-up is ample
    "congress_schedule": 4 * 3600,    # hearings get canceled/added intraday
    "crec_floor": 20 * 3600,          # the Congressional Record posts daily
    "whitehouse": 12 * 3600,          # briefing room updates through the day
}
_DEFAULT_TTL = 12 * 3600

# CREC floor speeches to pull on each refresh, by recency.
_CREC_LOOKBACK_DAYS = 7


@dataclass
class _Clients:
    fed: FedClient
    congress: CongressGovClient
    govinfo: GovInfoClient
    whitehouse: WhiteHouseClient


def _current_congress(year: int) -> int:
    """Congress number for a calendar year (the 119th covers 2025-2027)."""
    return (year - 1789) // 2 + 1


# ---------------------------------------------------------------------------
# Target discovery (which speakers/venues are live on Kalshi right now)
# ---------------------------------------------------------------------------

def discover_targets(markets: list[dict]) -> list[dict]:
    """From live market dicts (``{ticker, title}``) → target rows for the archive.

    Parses each title as a mention market and resolves its speaker; mention markets
    that don't parse are skipped. One row per (speaker_key, venue) so the nightly
    job knows which speakers to keep warm and can surface speakers not yet in the
    registry (``is_known=False`` → venue ``""``).
    """
    targets: list[dict] = []
    seen: set[tuple[str, str]] = set()
    for market in markets:
        title = str(market.get("title") or "")
        ticker = str(market.get("ticker") or "")
        parsed = parse_mention_title(ticker, title)
        if parsed is None:
            continue
        profile = resolve_speaker(parsed.get("speaker"))
        if not profile.speaker_key:
            continue
        venues = profile.transcript_venues or [""]
        for venue_type in venues:
            key = (profile.speaker_key, venue_type)
            if key in seen:
                continue
            seen.add(key)
            targets.append({
                "speaker_key": profile.speaker_key,
                "venue_type": venue_type,
                "aliases": [parsed["speaker"]] if parsed.get("speaker") else [],
                "last_seen_ticker": ticker,
            })
    return targets


def load_markets(path: str) -> list[dict]:
    """Load a market snapshot JSON into a list of ``{ticker, title}`` dicts (tolerant)."""
    try:
        data = json.loads(Path(path).read_text())
    except (OSError, json.JSONDecodeError):
        return []
    if isinstance(data, dict):
        data = data.get("markets") or data.get("data") or []
    markets: list[dict] = []
    for item in data or []:
        if isinstance(item, dict) and (item.get("title") or item.get("ticker")):
            markets.append({"ticker": item.get("ticker", ""), "title": item.get("title", "")})
    return markets


# ---------------------------------------------------------------------------
# Per-source refreshers
# ---------------------------------------------------------------------------

async def _refresh_fed_speeches(store: MentionsArchiveStore, clients: _Clients) -> tuple[int, int]:
    records = await clients.fed.get_speeches()
    return len(records), store.upsert_transcripts(records)


async def _refresh_fed_pressers(store: MentionsArchiveStore, clients: _Clients) -> tuple[int, int]:
    records = await clients.fed.get_presser_transcripts()
    return len(records), store.upsert_transcripts(records)


async def _refresh_congress_schedule(store: MentionsArchiveStore, clients: _Clients) -> tuple[int, int]:
    congress = _current_congress(datetime.now(tz=timezone.utc).year)
    records: list[dict] = []
    for chamber in ("house", "senate"):
        records.extend(await clients.congress.get_committee_meetings(congress, chamber))
    return len(records), store.upsert_schedule(records)


async def _refresh_crec_floor(store: MentionsArchiveStore, clients: _Clients) -> tuple[int, int]:
    since = (datetime.now(tz=timezone.utc) - timedelta(days=_CREC_LOOKBACK_DAYS)).strftime("%Y-%m-%d")
    records = await clients.govinfo.get_crec_records(since)
    return len(records), store.upsert_transcripts(records)


async def _refresh_whitehouse(store: MentionsArchiveStore, clients: _Clients) -> tuple[int, int]:
    records = await clients.whitehouse.get_briefings()
    return len(records), store.upsert_transcripts(records)


# source name → async refresher(store, clients) → (records_fetched, rows_offered)
_SOURCES = {
    "fed_speeches": _refresh_fed_speeches,
    "fed_pressers": _refresh_fed_pressers,
    "congress_schedule": _refresh_congress_schedule,
    "crec_floor": _refresh_crec_floor,
    "whitehouse": _refresh_whitehouse,
}


async def run_refresh(
    store: MentionsArchiveStore,
    clients,
    if_stale: bool = False,
    sources: dict | None = None,
    ttls: dict | None = None,
) -> dict:
    """Run the per-source refresh loop against an (already-open) store.

    Per-source isolation: each refresher runs in its own try/except; a failure is
    logged and the source is left stale (``refresh_log`` is written **only** after a
    successful refresher), so it retries next run while the others commit. Returns a
    summary ``{source: ("ok"|"skipped"|"failed", fetched, offered)}``. Pure of any
    client construction so it's unit-testable with fakes + a ``:memory:`` store.
    """
    sources = _SOURCES if sources is None else sources
    ttls = _SOURCE_TTL if ttls is None else ttls
    summary: dict[str, tuple] = {}
    for source, refresher in sources.items():
        ttl = ttls.get(source, _DEFAULT_TTL)
        if if_stale and not store.is_stale(source, ttl):
            print(f"{source}: fresh (within {ttl}s TTL) — skipping")
            summary[source] = ("skipped", 0, 0)
            continue
        try:
            fetched, offered = await refresher(store, clients)
            store.mark_refreshed(source)
            print(f"{source}: fetched {fetched} records, {offered} offered to archive")
            summary[source] = ("ok", fetched, offered)
        except Exception as caught_exception:
            # Per-source isolation: log and leave stale so it retries; do NOT write
            # refresh_log for a failed source.
            print(f"{source}: FAILED ({caught_exception!r}) — left stale for retry")
            summary[source] = ("failed", 0, 0)
    pruned = store.prune()
    if pruned:
        print(f"pruned {pruned} transcript(s) past retention")
    return summary


async def _run(if_stale: bool, markets_file: str | None) -> None:
    store = MentionsArchiveStore()
    clients = _Clients(
        fed=FedClient(),
        congress=CongressGovClient(),
        govinfo=GovInfoClient(),
        whitehouse=WhiteHouseClient(),
    )
    try:
        if markets_file:
            targets = discover_targets(load_markets(markets_file))
            if targets:
                store.upsert_targets(targets)
                print(f"targets: discovered {len(targets)} speaker/venue target(s) from {markets_file}")
        await run_refresh(store, clients, if_stale=if_stale)
    finally:
        await clients.fed.close()
        await clients.congress.close()
        await clients.govinfo.close()
        await clients.whitehouse.close()
        store.close()


def main() -> None:
    parser = argparse.ArgumentParser(description="Refresh the mentions archive")
    parser.add_argument(
        "--if-stale",
        action="store_true",
        dest="if_stale",
        help="Only refresh sources past their TTL (cheap no-op when warm).",
    )
    parser.add_argument(
        "--markets-file",
        dest="markets_file",
        default=None,
        help="Optional market snapshot JSON; discovers live speaker/venue targets.",
    )
    args = parser.parse_args()
    asyncio.run(_run(args.if_stale, args.markets_file))


if __name__ == "__main__":
    main()
