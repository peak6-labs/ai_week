"""Tests for the generic settlement-context prompt helper.

Covers tolerant arg parsing, the rendered prompt block (sources + rules + the
shared measure-the-same-thing instruction), the empty-input contract, and the
compact metadata basis used by deterministic pipelines.
"""
from __future__ import annotations

from kalshi_trader.agents import settlement_context
from kalshi_trader.agents.settlement_context import (
    SETTLEMENT_INSTRUCTION,
    format_settlement_block,
    parse_settlement_arg,
    settlement_metadata,
)

_NYC_TEMP = {
    "rules_primary": "Settles to the high temperature reported by AccuWeather.",
    "rules_secondary": "NWS data is NOT authoritative for this market.",
    "subtitle": "High temp in NYC",
    "settlement_sources": [{"name": "AccuWeather", "url": "https://www.accuweather.com"}],
    "contract_terms_url": "https://example.s3/NHIGHD.pdf",
}


def test_parse_settlement_arg_valid_object():
    parsed = parse_settlement_arg('{"rules_primary": "x"}')
    assert parsed == {"rules_primary": "x"}


def test_parse_settlement_arg_tolerates_bad_input():
    assert parse_settlement_arg(None) is None
    assert parse_settlement_arg("") is None
    assert parse_settlement_arg("   ") is None
    assert parse_settlement_arg("not json") is None
    assert parse_settlement_arg("[1, 2, 3]") is None  # array, not object


def test_format_settlement_block_includes_source_rules_and_instruction():
    block = format_settlement_block(_NYC_TEMP)
    assert "AccuWeather" in block
    assert "https://www.accuweather.com" in block
    assert "NOT authoritative" in block  # rules_secondary carried through
    assert "High temp in NYC" in block
    assert "https://example.s3/NHIGHD.pdf" in block
    assert SETTLEMENT_INSTRUCTION in block


def test_format_settlement_block_empty_when_nothing_useful():
    assert format_settlement_block(None) == ""
    assert format_settlement_block({}) == ""
    # Keys present but all empty/falsy -> nothing to render.
    assert format_settlement_block({"rules_primary": "", "settlement_sources": []}) == ""


def test_format_settlement_block_handles_sources_without_url():
    block = format_settlement_block({"settlement_sources": [{"name": "ESPN"}]})
    assert "ESPN" in block
    assert "()" not in block  # no empty parens when url is absent


def test_format_settlement_block_partial_fields():
    # Only settlement_sources present: still renders a block with the instruction.
    block = format_settlement_block({"settlement_sources": [{"name": "Polymarket"}]})
    assert "Polymarket" in block
    assert SETTLEMENT_INSTRUCTION in block


def test_settlement_metadata_is_compact_basis():
    basis = settlement_metadata(_NYC_TEMP)
    assert basis == {
        "settlement_sources": [{"name": "AccuWeather", "url": "https://www.accuweather.com"}],
        "contract_terms_url": "https://example.s3/NHIGHD.pdf",
    }


def test_settlement_metadata_empty_inputs():
    assert settlement_metadata(None) == {}
    assert settlement_metadata({}) == {}
    assert settlement_metadata({"rules_primary": "x"}) == {}  # no sources/url -> nothing


def test_module_exposes_shared_instruction():
    # The instruction is a single shared constant so every agent gets the same one.
    assert "same source" in settlement_context.SETTLEMENT_INSTRUCTION
