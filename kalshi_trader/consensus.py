"""Consensus filter for multi-agent trade ideas.

Only passes ideas where ≥ min_agents distinct agents agree on ticker+side.
When consensus is reached, merges signal_sources and picks the highest-confidence
reasoning as the output idea.
"""
from __future__ import annotations

from collections import defaultdict

from kalshi_trader.models import OrderAction, TradeIdea
from kalshi_trader.ui.config_manager import cfg


def apply_consensus(ideas: list[TradeIdea], min_agents: int | None = None) -> list[TradeIdea]:
    """Return ideas where ≥ min_agents distinct agents agree on (ticker, side).

    Args:
        ideas: All trade ideas from all specialist agents.
        min_agents: How many distinct agent_ids must agree before passing.
                    Defaults to cfg.get("min_agents") (runtime-configurable).

    Returns:
        One merged TradeIdea per (ticker, side) group that reached consensus.
        The merged idea uses the highest confidence and union of signal_sources.
    """
    if min_agents is None:
        min_agents = cfg.get("min_agents")

    groups: dict[tuple[str, str], list[TradeIdea]] = defaultdict(list)
    for idea in ideas:
        key = (idea.ticker, idea.side.value)
        groups[key].append(idea)

    result = []
    for (ticker, side_val), group in groups.items():
        distinct_agents = {i.agent_id for i in group}
        if len(distinct_agents) < min_agents:
            continue

        best = max(group, key=lambda i: i.confidence)
        merged_sources = sorted({s for i in group for s in i.signal_sources})

        result.append(TradeIdea(
            agent_id="consensus",
            ticker=best.ticker,
            side=best.side,
            action=OrderAction.BUY,
            confidence=best.confidence,
            market_price=best.market_price,
            reasoning=f"{len(distinct_agents)} agents agree: {best.reasoning}",
            signal_sources=merged_sources,
            suggested_size_dollars=best.suggested_size_dollars,
            category=best.category,
        ))

    return result
