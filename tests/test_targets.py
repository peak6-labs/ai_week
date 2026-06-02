"""Tests for whale targets file I/O."""
import json
from pathlib import Path

import pytest

from kalshi_trader.external.polymarket import load_whale_targets, save_whale_targets


def test_load_returns_empty_for_missing_file(tmp_path):
    result = load_whale_targets(path=tmp_path / "nonexistent.json")
    assert result == []


def test_load_returns_legacy_wallets_key(tmp_path):
    """Legacy files using 'wallets' key are still readable via fallback."""
    p = tmp_path / "targets.json"
    p.write_text(json.dumps({"wallets": ["0xabc", "0xdef"]}))
    assert load_whale_targets(path=p) == ["0xabc", "0xdef"]


def test_load_named_scorer_key(tmp_path):
    """Named scorer keys take precedence over legacy 'wallets' fallback."""
    p = tmp_path / "targets.json"
    p.write_text(json.dumps({"winrate": ["0xwr"], "harvard": ["0xhv"], "wallets": ["0xlegacy"]}))
    assert load_whale_targets(scorer="winrate", path=p) == ["0xwr"]
    assert load_whale_targets(scorer="harvard", path=p) == ["0xhv"]


def test_load_empty_scorer_key(tmp_path):
    p = tmp_path / "targets.json"
    p.write_text(json.dumps({"winrate": []}))
    assert load_whale_targets(scorer="winrate", path=p) == []


def test_save_and_reload_roundtrip(tmp_path):
    p = tmp_path / "targets.json"
    wallets = ["0xw1", "0xw2", "0xw3"]
    save_whale_targets(wallets, scorer="winrate", path=p)
    assert load_whale_targets(scorer="winrate", path=p) == wallets


def test_save_does_not_overwrite_other_scorer(tmp_path):
    """Saving v2 must not erase the v1 list."""
    p = tmp_path / "targets.json"
    save_whale_targets(["0xv1"], scorer="winrate", path=p)
    save_whale_targets(["0xv2"], scorer="harvard", path=p)
    assert load_whale_targets(scorer="winrate", path=p) == ["0xv1"]
    assert load_whale_targets(scorer="harvard", path=p) == ["0xv2"]


def test_save_creates_parent_dir(tmp_path):
    p = tmp_path / "nested" / "dir" / "targets.json"
    save_whale_targets(["0xabc"], scorer="winrate", path=p)
    assert p.exists()


def test_save_preserves_comment_field(tmp_path):
    p = tmp_path / "targets.json"
    p.write_text(json.dumps({"_comment": "keep me", "winrate": []}))
    save_whale_targets(["0xnew"], scorer="winrate", path=p)
    data = json.loads(p.read_text())
    assert data["_comment"] == "keep me"
    assert data["winrate"] == ["0xnew"]


def test_default_targets_file_is_loadable():
    wallets = load_whale_targets(scorer="winrate")
    assert isinstance(wallets, list)


def test_default_targets_file_wallets_are_strings():
    wallets = load_whale_targets(scorer="winrate")
    assert all(isinstance(w, str) for w in wallets)
