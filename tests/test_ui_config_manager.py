"""Tests for kalshi_trader.ui.config_manager.ConfigManager."""

import json
import pytest
from pathlib import Path

from kalshi_trader.ui.config_manager import ConfigManager, DEFAULTS


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def make_cfg(tmp_path: Path, filename: str = "cfg.json") -> ConfigManager:
    """Return a fresh ConfigManager backed by a temp file."""
    return ConfigManager(path=tmp_path / filename)


# ---------------------------------------------------------------------------
# get() / defaults
# ---------------------------------------------------------------------------

class TestGetDefaults:
    def test_get_returns_default_when_no_file(self, tmp_path):
        cfg = make_cfg(tmp_path)
        assert cfg.get("weight_noaa") == DEFAULTS["weight_noaa"]

    def test_get_returns_default_for_bool(self, tmp_path):
        cfg = make_cfg(tmp_path)
        assert cfg.get("agent_x_enabled") is False
        assert cfg.get("agent_weather_enabled") is True

    def test_get_returns_none_for_unknown_key(self, tmp_path):
        cfg = make_cfg(tmp_path)
        assert cfg.get("nonexistent_key") is None

    def test_all_defaults_present(self, tmp_path):
        cfg = make_cfg(tmp_path)
        for key, expected in DEFAULTS.items():
            assert cfg.get(key) == expected, f"Mismatch for key '{key}'"


# ---------------------------------------------------------------------------
# File creation
# ---------------------------------------------------------------------------

class TestFileCreation:
    def test_file_created_on_init(self, tmp_path):
        p = tmp_path / "cfg.json"
        assert not p.exists()
        ConfigManager(path=p)
        assert p.exists()

    def test_created_file_contains_valid_json(self, tmp_path):
        p = tmp_path / "cfg.json"
        ConfigManager(path=p)
        data = json.loads(p.read_text())
        assert isinstance(data, dict)
        assert data["weight_noaa"] == DEFAULTS["weight_noaa"]

    def test_created_file_has_all_defaults(self, tmp_path):
        p = tmp_path / "cfg.json"
        ConfigManager(path=p)
        data = json.loads(p.read_text())
        for key in DEFAULTS:
            assert key in data


# ---------------------------------------------------------------------------
# reload()
# ---------------------------------------------------------------------------

class TestReload:
    def test_reload_picks_up_external_changes(self, tmp_path):
        cfg = make_cfg(tmp_path)
        # Externally modify the file
        p = tmp_path / "cfg.json"
        data = json.loads(p.read_text())
        data["weight_noaa"] = 0.42
        p.write_text(json.dumps(data))

        # Before reload — old value
        assert cfg.get("weight_noaa") == DEFAULTS["weight_noaa"]

        cfg.reload()
        assert cfg.get("weight_noaa") == pytest.approx(0.42)

    def test_reload_ignores_unknown_keys(self, tmp_path):
        cfg = make_cfg(tmp_path)
        p = tmp_path / "cfg.json"
        data = json.loads(p.read_text())
        data["totally_unknown"] = 999
        p.write_text(json.dumps(data))
        cfg.reload()
        # Unknown key should not appear in the manager
        assert cfg.get("totally_unknown") is None

    def test_reload_falls_back_to_defaults_on_malformed_file(self, tmp_path):
        p = tmp_path / "cfg.json"
        cfg = ConfigManager(path=p)
        p.write_text("this is not json {{{{")
        cfg.reload()
        # Should still return defaults
        assert cfg.get("weight_noaa") == DEFAULTS["weight_noaa"]


# ---------------------------------------------------------------------------
# validate_and_update() — unknown keys
# ---------------------------------------------------------------------------

class TestValidateAndUpdateUnknownKeys:
    def test_unknown_key_returns_error(self, tmp_path):
        cfg = make_cfg(tmp_path)
        errors = cfg.validate_and_update({"not_a_real_key": 1})
        assert "not_a_real_key" in errors
        assert errors["not_a_real_key"] == "unknown key"

    def test_unknown_key_does_not_save(self, tmp_path):
        cfg = make_cfg(tmp_path)
        original_value = cfg.get("weight_noaa")
        cfg.validate_and_update({"not_a_real_key": 1, "weight_noaa": 0.5})
        # Because there was an error the whole update should be rejected
        assert cfg.get("weight_noaa") == original_value

    def test_multiple_unknown_keys_all_reported(self, tmp_path):
        cfg = make_cfg(tmp_path)
        errors = cfg.validate_and_update({"foo": 1, "bar": 2})
        assert "foo" in errors
        assert "bar" in errors


# ---------------------------------------------------------------------------
# validate_and_update() — out-of-range numeric
# ---------------------------------------------------------------------------

class TestValidateAndUpdateOutOfRange:
    def test_float_too_high_rejected(self, tmp_path):
        cfg = make_cfg(tmp_path)
        errors = cfg.validate_and_update({"weight_noaa": 1.5})  # max 1.0
        assert "weight_noaa" in errors
        assert "out of range" in errors["weight_noaa"]

    def test_float_too_low_rejected(self, tmp_path):
        cfg = make_cfg(tmp_path)
        errors = cfg.validate_and_update({"weight_noaa": -0.1})  # min 0.0
        assert "weight_noaa" in errors

    def test_int_too_high_rejected(self, tmp_path):
        cfg = make_cfg(tmp_path)
        errors = cfg.validate_and_update({"min_agents": 99})  # max 10
        assert "min_agents" in errors

    def test_int_too_low_rejected(self, tmp_path):
        cfg = make_cfg(tmp_path)
        errors = cfg.validate_and_update({"min_agents": 0})  # min 1
        assert "min_agents" in errors

    def test_boundary_values_accepted(self, tmp_path):
        cfg = make_cfg(tmp_path)
        errors = cfg.validate_and_update({"weight_noaa": 0.0})
        assert errors == {}
        errors = cfg.validate_and_update({"weight_noaa": 1.0})
        assert errors == {}

    def test_valid_value_is_saved(self, tmp_path):
        cfg = make_cfg(tmp_path)
        errors = cfg.validate_and_update({"weight_noaa": 0.5})
        assert errors == {}
        assert cfg.get("weight_noaa") == pytest.approx(0.5)

        # Reload from disk to confirm persistence
        cfg2 = ConfigManager(path=tmp_path / "cfg.json")
        assert cfg2.get("weight_noaa") == pytest.approx(0.5)


# ---------------------------------------------------------------------------
# validate_and_update() — wrong types
# ---------------------------------------------------------------------------

class TestValidateAndUpdateWrongTypes:
    def test_string_rejected_for_float_field(self, tmp_path):
        cfg = make_cfg(tmp_path)
        errors = cfg.validate_and_update({"weight_noaa": "high"})
        assert "weight_noaa" in errors

    def test_string_rejected_for_int_field(self, tmp_path):
        cfg = make_cfg(tmp_path)
        errors = cfg.validate_and_update({"min_agents": "two"})
        assert "min_agents" in errors

    def test_none_rejected_for_float_field(self, tmp_path):
        cfg = make_cfg(tmp_path)
        errors = cfg.validate_and_update({"weight_noaa": None})
        assert "weight_noaa" in errors

    def test_string_accepted_for_model_name_field(self, tmp_path):
        cfg = make_cfg(tmp_path)
        errors = cfg.validate_and_update({"specialist_model": "claude-haiku-3-5"})
        assert errors == {}
        assert cfg.get("specialist_model") == "claude-haiku-3-5"


# ---------------------------------------------------------------------------
# validate_and_update() — atomicity
# ---------------------------------------------------------------------------

class TestValidateAndUpdateAtomicity:
    def test_no_partial_save_when_one_field_invalid(self, tmp_path):
        cfg = make_cfg(tmp_path)
        original_noaa = cfg.get("weight_noaa")
        original_agents = cfg.get("min_agents")

        # weight_noaa is valid, min_agents is out of range — nothing should save
        errors = cfg.validate_and_update({"weight_noaa": 0.5, "min_agents": 99})
        assert errors  # has errors
        assert cfg.get("weight_noaa") == original_noaa
        assert cfg.get("min_agents") == original_agents

    def test_all_valid_updates_applied_atomically(self, tmp_path):
        cfg = make_cfg(tmp_path)
        errors = cfg.validate_and_update({
            "weight_noaa": 0.5,
            "min_agents": 3,
            "specialist_model": "claude-haiku-3-5",
        })
        assert errors == {}
        assert cfg.get("weight_noaa") == pytest.approx(0.5)
        assert cfg.get("min_agents") == 3
        assert cfg.get("specialist_model") == "claude-haiku-3-5"


# ---------------------------------------------------------------------------
# validate_and_update() — bool fields
# ---------------------------------------------------------------------------

class TestValidateAndUpdateBoolFields:
    def test_true_accepted(self, tmp_path):
        cfg = make_cfg(tmp_path)
        errors = cfg.validate_and_update({"agent_x_enabled": True})
        assert errors == {}
        assert cfg.get("agent_x_enabled") is True

    def test_false_accepted(self, tmp_path):
        cfg = make_cfg(tmp_path)
        errors = cfg.validate_and_update({"agent_weather_enabled": False})
        assert errors == {}
        assert cfg.get("agent_weather_enabled") is False

    def test_int_1_rejected_for_bool_field(self, tmp_path):
        cfg = make_cfg(tmp_path)
        errors = cfg.validate_and_update({"agent_x_enabled": 1})
        assert "agent_x_enabled" in errors

    def test_int_0_rejected_for_bool_field(self, tmp_path):
        cfg = make_cfg(tmp_path)
        errors = cfg.validate_and_update({"agent_x_enabled": 0})
        assert "agent_x_enabled" in errors

    def test_string_true_rejected_for_bool_field(self, tmp_path):
        cfg = make_cfg(tmp_path)
        errors = cfg.validate_and_update({"agent_x_enabled": "true"})
        assert "agent_x_enabled" in errors

    def test_bool_rejected_for_float_field(self, tmp_path):
        """True is a subclass of int; make sure it's not coerced into a float field."""
        cfg = make_cfg(tmp_path)
        errors = cfg.validate_and_update({"weight_noaa": True})
        assert "weight_noaa" in errors

    def test_bool_rejected_for_int_field(self, tmp_path):
        """True should not be accepted as int 1 for an int field."""
        cfg = make_cfg(tmp_path)
        errors = cfg.validate_and_update({"min_agents": True})
        assert "min_agents" in errors


# ---------------------------------------------------------------------------
# all() — returns a copy
# ---------------------------------------------------------------------------

class TestAll:
    def test_all_returns_all_defaults(self, tmp_path):
        cfg = make_cfg(tmp_path)
        d = cfg.all()
        assert set(d.keys()) == set(DEFAULTS.keys())

    def test_mutating_returned_dict_does_not_affect_manager(self, tmp_path):
        cfg = make_cfg(tmp_path)
        d = cfg.all()
        d["weight_noaa"] = 0.0
        # Manager should be unaffected
        assert cfg.get("weight_noaa") == DEFAULTS["weight_noaa"]

    def test_all_reflects_validated_updates(self, tmp_path):
        cfg = make_cfg(tmp_path)
        cfg.validate_and_update({"weight_noaa": 0.77})
        d = cfg.all()
        assert d["weight_noaa"] == pytest.approx(0.77)
