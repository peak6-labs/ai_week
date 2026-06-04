"""Pure ranking/rendering helpers for the mentions scan deliverable.

No I/O, no network — takes already-scored rows and produces the canonical ranked
markdown table (top 25 by edge, with live bid/ask and a signal-quality flag). The
``scripts/mentions_scan.py`` driver fetches/scores and calls ``rank_and_render``.
"""
from __future__ import annotations

from kalshi_trader.web_links import kalshi_market_url

# Default size of the ranked table — always shown, even if fewer rows are positive.
DEFAULT_TOP_N = 25


def _format_speaker(speaker: str | None) -> str:
    return speaker if speaker else "—"


def _format_window(window: str | None) -> str:
    return window if window else "—"


def rank_and_render(scored_rows: list[dict], top_n: int = DEFAULT_TOP_N) -> str:
    """Render the top-``top_n`` mentions ideas as a markdown table, sorted by edge.

    Each row in ``scored_rows`` is a dict with: ``ticker``, ``speaker``, ``word``,
    ``window``, ``model_probability`` (0-1), ``fair_cents``, ``yes_bid``,
    ``yes_ask``, ``side`` ("YES"/"NO"), ``edge_cents``, ``volume_24h``, and
    ``quality`` ("corpus-backed" | "gdelt-only" | "suppressed").

    The table is sorted by ``edge_cents`` descending and truncated to ``top_n``
    rows **even when fewer rows have positive edge** (so the deliverable is never
    blank). Tickers are backticked and wrapped in their series link per CLAUDE.md.
    """
    ranked = sorted(scored_rows, key=lambda row: row.get("edge_cents", 0.0), reverse=True)[:top_n]

    header = (
        "| # | Market | Speaker | Word | Window | Side | Model % | Fair¢ | Bid/Ask | Edge¢ | Vol24h | Signal |\n"
        "|--:|--------|---------|------|--------|------|--------:|------:|---------|------:|-------:|--------|"
    )
    lines = [header]
    for rank, row in enumerate(ranked, start=1):
        ticker = row.get("ticker", "")
        linked_ticker = f"[`{ticker}`]({kalshi_market_url(ticker)})"
        model_percent = f"{float(row.get('model_probability', 0.0)) * 100:.0f}%"
        fair_cents = f"{float(row.get('fair_cents', 0.0)):.0f}"
        bid_ask = f"{row.get('yes_bid', '—')}/{row.get('yes_ask', '—')}"
        edge_cents = f"{float(row.get('edge_cents', 0.0)):+.1f}"
        volume_24h = f"{int(float(row.get('volume_24h', 0) or 0))}"
        lines.append(
            f"| {rank} | {linked_ticker} | {_format_speaker(row.get('speaker'))} "
            f"| {row.get('word', '')} | {_format_window(row.get('window'))} "
            f"| {row.get('side', '')} | {model_percent} | {fair_cents} | {bid_ask} "
            f"| {edge_cents} | {volume_24h} | {row.get('quality', '')} |"
        )
    return "\n".join(lines)
