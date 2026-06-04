"""Pure ranking + rendering for the standard weather-scan deliverable.

Every weather run should end with one clear table — sorted by edge (descending),
always the top 25 markets, with the live bid/ask — instead of an ad-hoc
hand-assembled summary. This module holds the network-free core: scoring the two
sides of a quote against a model probability, ranking the scored markets, and
rendering the markdown table. ``scripts/weather_scan.py`` does the (read-only)
fetching and feeds the results here.

Tickers are rendered per CLAUDE.md: the backticked ticker wrapped in its safe
series link via :func:`kalshi_trader.web_links.kalshi_market_url`.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone

from kalshi_trader.web_links import kalshi_market_url

# How many markets the standard deliverable always shows (even when fewer have a
# positive edge — the table is the canonical view of the whole scored slate's top).
DEFAULT_TOP_N = 25

# Guardrail thresholds. An edge this large against a market traded this deeply is
# almost always a model failure (stale / biased ensemble), not a real opportunity:
# a liquid market is the stronger prior, so a gap this size flags the model rather
# than an edge. Rows clearing BOTH thresholds are held out of the ranked table and
# shown in a separate, clearly-labelled "suppressed" section (and kept in the full
# JSON) so they stay auditable without dominating the deliverable. Calibrated from
# the 2026-06-04 weather-scan audit, where every +60–98¢ top row on a 3k–17k-volume
# market was a model artifact, not a trade.
SUSPECT_EDGE_CENTS = 50.0
SUSPECT_MIN_VOLUME_24H = 1000.0


def is_suspect_edge(edge_cents: float, volume_24h: float) -> bool:
    """True when an edge is large enough against a liquid enough market that it is
    a likely model-failure artifact rather than a tradeable edge.

    Only liquid markets gate the rule — a thin market is not a strong enough prior
    to call the model wrong — so an illiquid large edge is left in the main table.
    """
    return edge_cents >= SUSPECT_EDGE_CENTS and volume_24h >= SUSPECT_MIN_VOLUME_24H


@dataclass
class ScoredWeatherMarket:
    """One scored weather market: the model's fair value vs the live quote.

    ``edge_cents`` is the edge on the chosen ``side`` — YES edge is ``fair - ask``,
    NO edge is ``bid - fair`` — and is what the table sorts on. ``forecast_point``
    records where the forecast was taken (``station:KLAX`` vs ``centroid``) so the
    table flags station-resolved rows and centroid fallbacks are auditable.
    """

    ticker: str
    forecast_point: str
    side: str  # "YES" or "NO"
    model_probability: float  # 0..1
    fair_cents: float
    yes_bid: float | None  # cents
    yes_ask: float | None  # cents
    edge_cents: float
    volume_24h: float = 0.0
    city: str = ""
    metric: str = ""
    # Set when the edge is large enough against a liquid enough market to be a
    # likely model-failure artifact (see :func:`is_suspect_edge`); such rows are
    # held out of the ranked table but kept in the JSON for audit.
    suspect: bool = False
    metadata: dict = field(default_factory=dict)


def score_sides(
    model_probability: float, yes_bid: float | None, yes_ask: float | None
) -> tuple[str, float, float]:
    """Score both sides of a quote against the model and return the better one.

    Returns ``(side, edge_cents, fair_cents)`` where ``fair_cents`` is the model's
    fair price (``model_probability * 100``), the YES edge is ``fair - yes_ask``
    (buy YES below fair) and the NO edge is ``yes_bid - fair`` (sell YES / buy NO
    above fair). The side with the larger edge wins; a missing quote on a side
    makes that side ineligible (its edge is treated as ``-inf``). Ties go to YES.
    """
    fair_cents = model_probability * 100.0
    yes_edge = (fair_cents - yes_ask) if yes_ask is not None else float("-inf")
    no_edge = (yes_bid - fair_cents) if yes_bid is not None else float("-inf")
    if no_edge > yes_edge:
        return "NO", no_edge, fair_cents
    return "YES", yes_edge, fair_cents


def rank_markets(
    scored_rows: list[ScoredWeatherMarket], top_n: int = DEFAULT_TOP_N
) -> list[ScoredWeatherMarket]:
    """Sort scored markets by edge (descending) and take the top ``top_n``.

    Always returns up to ``top_n`` rows **even if fewer have a positive edge** —
    the deliverable shows the top of the whole scored slate, not only the
    tradeable subset. Returns every row when there are fewer than ``top_n``.
    """
    ranked = sorted(scored_rows, key=lambda market: market.edge_cents, reverse=True)
    return ranked[:top_n]


def _format_cents(value: float | None) -> str:
    return "—" if value is None else f"{value:.0f}"


def _format_bid_ask(yes_bid: float | None, yes_ask: float | None) -> str:
    return f"{_format_cents(yes_bid)}/{_format_cents(yes_ask)}"


def _ticker_cell(ticker: str) -> str:
    """`[`TICKER`](series_url)` — backticked ticker wrapped in its series link."""
    return f"[`{ticker}`]({kalshi_market_url(ticker)})"


def render_table(ranked: list[ScoredWeatherMarket]) -> str:
    """Render ranked weather markets as a markdown table (no surrounding prose)."""
    header = (
        "| # | Market | Forecast point | Side | Model % | Fair¢ | Bid/Ask | Edge¢ | Vol24h |\n"
        "|---:|---|---|---|---:|---:|---:|---:|---:|"
    )
    lines = [header]
    for rank, market in enumerate(ranked, start=1):
        lines.append(
            f"| {rank} | {_ticker_cell(market.ticker)} | {market.forecast_point} "
            f"| {market.side} | {market.model_probability * 100:.0f}% "
            f"| {market.fair_cents:.0f} | {_format_bid_ask(market.yes_bid, market.yes_ask)} "
            f"| {market.edge_cents:+.1f} | {market.volume_24h:,.0f} |"
        )
    return "\n".join(lines)


def rank_and_render(
    scored_rows: list[ScoredWeatherMarket],
    top_n: int = DEFAULT_TOP_N,
    *,
    generated_at: datetime | None = None,
) -> str:
    """Rank scored weather markets and render the standard deliverable.

    Produces a titled markdown section: a one-line summary (how many scored, how
    many shown, how many with positive edge, how many suppressed as suspect)
    followed by the ranked table — sorted by edge descending, always up to
    ``top_n`` rows, with the live bid/ask. Rows flagged suspect by
    :func:`is_suspect_edge` (an outsized edge against a liquid market — a likely
    model artifact) are held out of that table and listed in a separate, clearly
    labelled "suppressed" section so they stay visible for audit without dominating
    the deliverable.
    """
    suspect_rows = [market for market in scored_rows if is_suspect_edge(market.edge_cents, market.volume_24h)]
    trustworthy_rows = [market for market in scored_rows if not is_suspect_edge(market.edge_cents, market.volume_24h)]
    ranked = rank_markets(trustworthy_rows, top_n)
    positive_edge_count = sum(1 for market in trustworthy_rows if market.edge_cents > 0)
    stamp = (generated_at or datetime.now(tz=timezone.utc)).strftime("%Y-%m-%d %H:%M UTC")
    suppressed_note = (
        f" {len(suspect_rows)} suppressed as suspect "
        f"(edge ≥ {SUSPECT_EDGE_CENTS:.0f}¢ against a market with vol ≥ "
        f"{SUSPECT_MIN_VOLUME_24H:,.0f} — likely model artifact, see below)."
        if suspect_rows else ""
    )
    summary = (
        f"# Weather edge scan — {stamp}\n\n"
        f"Scored {len(scored_rows)} markets; showing top {len(ranked)} by edge "
        f"({positive_edge_count} with positive edge).{suppressed_note}\n"
    )
    body = render_table(ranked)
    if suspect_rows:
        body += (
            "\n\n## ⚠️ Suppressed as suspect — likely model failure, NOT tradeable\n\n"
            f"Edge ≥ {SUSPECT_EDGE_CENTS:.0f}¢ against a market with 24h volume ≥ "
            f"{SUSPECT_MIN_VOLUME_24H:,.0f}: a deep market is the stronger prior, so a "
            "disagreement this large flags the model (stale / biased ensemble), not "
            "an opportunity. Shown for audit only.\n\n"
            f"{render_table(rank_markets(suspect_rows, len(suspect_rows)))}"
        )
    return f"{summary}\n{body}\n"
