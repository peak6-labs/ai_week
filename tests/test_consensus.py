"""Tests for consensus filter — only pass ideas where 2+ agents agree."""
from datetime import datetime, timezone

import pytest

from kalshi_trader.consensus import apply_consensus
from kalshi_trader.models import Market, OrderAction, Side, TradeIdea


def _idea(agent_id="agent_a", ticker="BTC-1", side=Side.YES, confidence=0.6):
    return TradeIdea(
        agent_id=agent_id,
        ticker=ticker,
        side=side,
        action=OrderAction.BUY,
        confidence=confidence,
        market_price=50.0,
        reasoning="test",
        signal_sources=[agent_id],
    )


# --- Filtering ---

def test_single_agent_idea_is_excluded():
    ideas = [_idea(agent_id="agent_a")]
    assert apply_consensus(ideas) == []


def test_two_agents_same_ticker_side_passes():
    ideas = [_idea(agent_id="agent_a"), _idea(agent_id="agent_b")]
    result = apply_consensus(ideas)
    assert len(result) == 1


def test_same_agent_twice_does_not_count_as_consensus():
    """Duplicate agent_ids must not satisfy the 2-agent requirement."""
    ideas = [_idea(agent_id="agent_a"), _idea(agent_id="agent_a")]
    assert apply_consensus(ideas) == []


def test_different_sides_are_not_consensus():
    """YES and NO votes for the same ticker are not agreement."""
    ideas = [
        _idea(agent_id="agent_a", side=Side.YES),
        _idea(agent_id="agent_b", side=Side.NO),
    ]
    assert apply_consensus(ideas) == []


def test_different_tickers_are_not_consensus():
    ideas = [
        _idea(agent_id="agent_a", ticker="BTC-1"),
        _idea(agent_id="agent_b", ticker="ETH-1"),
    ]
    assert apply_consensus(ideas) == []


def test_three_agents_still_passes():
    ideas = [
        _idea(agent_id="agent_a"),
        _idea(agent_id="agent_b"),
        _idea(agent_id="agent_c"),
    ]
    result = apply_consensus(ideas)
    assert len(result) == 1


def test_min_agents_three_requires_three():
    ideas = [_idea(agent_id="agent_a"), _idea(agent_id="agent_b")]
    assert apply_consensus(ideas, min_agents=3) == []


def test_min_agents_three_passes_with_three():
    ideas = [
        _idea(agent_id="agent_a"),
        _idea(agent_id="agent_b"),
        _idea(agent_id="agent_c"),
    ]
    result = apply_consensus(ideas, min_agents=3)
    assert len(result) == 1


def test_empty_input_returns_empty():
    assert apply_consensus([]) == []


# --- Merged output fields ---

def test_consensus_idea_has_highest_confidence():
    ideas = [
        _idea(agent_id="agent_a", confidence=0.50),
        _idea(agent_id="agent_b", confidence=0.80),
    ]
    result = apply_consensus(ideas)
    assert result[0].confidence == pytest.approx(0.80)


def test_consensus_idea_merges_signal_sources():
    ideas = [
        _idea(agent_id="agent_a", confidence=0.5),
        _idea(agent_id="agent_b", confidence=0.6),
    ]
    result = apply_consensus(ideas)
    sources = result[0].signal_sources
    assert "agent_a" in sources
    assert "agent_b" in sources


def test_consensus_agent_id_is_consensus():
    ideas = [_idea(agent_id="agent_a"), _idea(agent_id="agent_b")]
    result = apply_consensus(ideas)
    assert result[0].agent_id == "consensus"


def test_consensus_ticker_and_side_preserved():
    ideas = [
        _idea(agent_id="agent_a", ticker="RAIN-1", side=Side.NO),
        _idea(agent_id="agent_b", ticker="RAIN-1", side=Side.NO),
    ]
    result = apply_consensus(ideas)
    assert result[0].ticker == "RAIN-1"
    assert result[0].side == Side.NO


def test_multiple_markets_independent_consensus():
    """Two ticker groups each with 2 agents → two outputs."""
    ideas = [
        _idea(agent_id="agent_a", ticker="BTC-1"),
        _idea(agent_id="agent_b", ticker="BTC-1"),
        _idea(agent_id="agent_a", ticker="ETH-1"),
        _idea(agent_id="agent_b", ticker="ETH-1"),
    ]
    result = apply_consensus(ideas)
    assert len(result) == 2
    tickers = {r.ticker for r in result}
    assert tickers == {"BTC-1", "ETH-1"}


def test_consensus_reasoning_includes_agent_count():
    ideas = [_idea(agent_id="agent_a"), _idea(agent_id="agent_b")]
    result = apply_consensus(ideas)
    assert "2" in result[0].reasoning
