"""Tests for whale targets file I/O."""
import json
from pathlib import Path

import pytest

from kalshi_trader.external.polymarket import load_whale_targets, save_whale_targets


def test_load_returns_empty_for_missing_file(tmp_path):
    result = load_whale_targets(tmp_path / "nonexistent.json")
    assert result == []


def test_load_returns_wallets_list(tmp_path):
    p = tmp_path / "targets.json"
    p.write_text(json.dumps({"wallets": ["0xabc", "0xdef"]}))
    assert load_whale_targets(p) == ["0xabc", "0xdef"]


def test_load_empty_wallets_key(tmp_path):
    p = tmp_path / "targets.json"
    p.write_text(json.dumps({"wallets": []}))
    assert load_whale_targets(p) == []


def test_save_and_reload_roundtrip(tmp_path):
    p = tmp_path / "targets.json"
    wallets = ["0xw1", "0xw2", "0xw3"]
    save_whale_targets(wallets, p)
    assert load_whale_targets(p) == wallets


def test_save_creates_parent_dir(tmp_path):
    p = tmp_path / "nested" / "dir" / "targets.json"
    save_whale_targets(["0xabc"], p)
    assert p.exists()


def test_save_preserves_comment_field(tmp_path):
    p = tmp_path / "targets.json"
    p.write_text(json.dumps({"_comment": "keep me", "wallets": []}))
    save_whale_targets(["0xnew"], p)
    data = json.loads(p.read_text())
    assert data["_comment"] == "keep me"
    assert data["wallets"] == ["0xnew"]


def test_default_targets_file_is_loadable():
    """The bundled targets.json must be valid JSON with a 'wallets' key."""
    wallets = load_whale_targets()
    assert isinstance(wallets, list)


def test_default_targets_file_wallets_are_strings():
    wallets = load_whale_targets()
    assert all(isinstance(w, str) for w in wallets)
