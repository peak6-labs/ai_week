"""Runtime configuration manager.

Reads/writes runtime_config.json at the project root. All agent files should
import `from kalshi_trader.ui.config_manager import cfg` and call
`cfg.get("key")` instead of using hardcoded constants.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

DEFAULTS = {
    "min_agents": 2,
    "weight_noaa": 0.85,
    "weight_polymarket_price": 0.75,
    "weight_polymarket_whale": 0.60,
    "weight_x_grok": 0.60,
    "weight_x_claude": 0.75,
    "weight_market_maker": 0.65,
    "weight_kalshi_bias": 0.50,
    "weight_order_flow": 0.55,
    "weight_microstructure": 0.35,
    "weight_sportsbook": 0.85,
    "uncertainty_kalshi_bias": 0.12,
    "uncertainty_order_flow": 0.15,
    "uncertainty_microstructure": 0.20,
    "uncertainty_sportsbook": 0.05,
    "uncertainty_noaa_temp": 0.08,
    "uncertainty_noaa_precip": 0.05,
    "uncertainty_poly_price": 0.03,
    "uncertainty_whale_single": 0.15,
    "uncertainty_whale_multi": 0.10,
    "x_grok_uncertainty_threshold": 0.15,
    "ofi_window_minutes": 30,
    "vpin_bucket_size_usd": 5000.0,
    "trade_count_window_minutes": 60,
    "ofi_prob_scale": 0.25,
    "mm_snapshot_count": 3,
    "mm_snapshot_delay_seconds": 2.0,
    "mm_imbalance_prob_scale": 0.25,
    "mm_spread_withdrawal_cents": 15,
    "mm_spread_trend_threshold": 0.30,
    "mm_directional_imbalance_threshold": 0.60,
    "poly_min_gap_cents": 7.0,
    "poly_gap_scale": 0.20,
    "poly_whale_confidence_boost": 0.15,
    "poly_max_confidence": 0.95,
    "poly_whale_min_size_usd": 500.0,
    "poly_whale_lookback_seconds": 3600,
    "poly_match_min_score": 0.60,
    "polymarket_max_concurrent": 8,
    "bias_political_adjustment": 0.065,
    "bias_longshot_factor": 0.65,
    "bias_political_no_trade_zone": 0.05,
    "bias_nonpolitical_no_trade_zone": 0.30,
    "bias_near_expiry_horizon": 0.30,
    "bias_mid_horizon": 0.60,
    "exit_take_profit_threshold": 0.85,
    "exit_stale_thesis_hours": 24,
    "exit_stale_thesis_min_move": 0.02,
    "filter_min_open_interest": 500,
    "filter_min_hours_to_close": 4,
    "filter_max_hours_to_close": 168,
    "specialist_model": "claude-sonnet-4-6",
    "coordinator_model": "claude-opus-4-8",
    "agent_max_iterations": 30,
    "x_max_concurrent_searches": 3,
    "ws_reconnect_delay_seconds": 5,
    "ws_watchdog_timeout_seconds": 60,
    "agent_weather_enabled": True,
    "agent_polymarket_enabled": True,
    "agent_order_flow_enabled": True,
    "agent_market_maker_enabled": True,
    "agent_kalshi_bias_enabled": True,
    "agent_x_enabled": False,
}

_NUMERIC_RANGES: dict[str, tuple[float, float]] = {
    "min_agents": (1, 10),
    "weight_noaa": (0.0, 1.0),
    "weight_polymarket_price": (0.0, 1.0),
    "weight_polymarket_whale": (0.0, 1.0),
    "weight_x_grok": (0.0, 1.0),
    "weight_x_claude": (0.0, 1.0),
    "weight_market_maker": (0.0, 1.0),
    "weight_kalshi_bias": (0.0, 1.0),
    "weight_order_flow": (0.0, 1.0),
    "weight_microstructure": (0.0, 1.0),
    "weight_sportsbook": (0.0, 1.0),
    "uncertainty_kalshi_bias": (0.0, 0.5),
    "uncertainty_order_flow": (0.0, 0.5),
    "uncertainty_microstructure": (0.0, 0.5),
    "uncertainty_sportsbook": (0.0, 0.5),
    "uncertainty_noaa_temp": (0.0, 0.5),
    "uncertainty_noaa_precip": (0.0, 0.5),
    "uncertainty_poly_price": (0.0, 0.5),
    "uncertainty_whale_single": (0.0, 0.5),
    "uncertainty_whale_multi": (0.0, 0.5),
    "x_grok_uncertainty_threshold": (0.0, 0.5),
    "ofi_window_minutes": (1, 1440),
    "vpin_bucket_size_usd": (100.0, 100000.0),
    "trade_count_window_minutes": (1, 1440),
    "ofi_prob_scale": (0.0, 1.0),
    "mm_snapshot_count": (2, 20),
    "mm_snapshot_delay_seconds": (0.5, 30.0),
    "mm_imbalance_prob_scale": (0.0, 1.0),
    "mm_spread_withdrawal_cents": (1, 50),
    "mm_spread_trend_threshold": (0.0, 2.0),
    "mm_directional_imbalance_threshold": (0.0, 1.0),
    "poly_min_gap_cents": (1.0, 30.0),
    "poly_gap_scale": (0.01, 1.0),
    "poly_whale_confidence_boost": (0.0, 0.5),
    "poly_max_confidence": (0.5, 1.0),
    "poly_whale_min_size_usd": (10.0, 100000.0),
    "poly_whale_lookback_seconds": (60, 86400),
    "poly_match_min_score": (0.0, 1.0),
    "polymarket_max_concurrent": (1, 20),
    "bias_political_adjustment": (0.0, 0.3),
    "bias_longshot_factor": (0.1, 1.0),
    "bias_political_no_trade_zone": (0.0, 0.4),
    "bias_nonpolitical_no_trade_zone": (0.0, 0.5),
    "bias_near_expiry_horizon": (0.0, 1.0),
    "bias_mid_horizon": (0.0, 1.0),
    "exit_take_profit_threshold": (0.1, 1.0),
    "exit_stale_thesis_hours": (1, 168),
    "exit_stale_thesis_min_move": (0.001, 0.2),
    "filter_min_open_interest": (0, 10000),
    "filter_min_hours_to_close": (0, 24),
    "filter_max_hours_to_close": (24, 8760),
    "agent_max_iterations": (5, 100),
    "x_max_concurrent_searches": (1, 10),
    "ws_reconnect_delay_seconds": (1, 60),
    "ws_watchdog_timeout_seconds": (10, 300),
}

# Keys whose default values are bool
_BOOL_KEYS: frozenset[str] = frozenset(
    k for k, v in DEFAULTS.items() if isinstance(v, bool)
)

# Keys whose default values are int (but not bool)
_INT_KEYS: frozenset[str] = frozenset(
    k for k, v in DEFAULTS.items() if isinstance(v, int) and not isinstance(v, bool)
)

# Keys whose default values are float
_FLOAT_KEYS: frozenset[str] = frozenset(
    k for k, v in DEFAULTS.items() if isinstance(v, float)
)

# Keys whose default values are str
_STR_KEYS: frozenset[str] = frozenset(
    k for k, v in DEFAULTS.items() if isinstance(v, str)
)


class ConfigManager:
    def __init__(self, path: Path = Path("runtime_config.json")):
        self._path = path
        self._values: dict = dict(DEFAULTS)
        self._load()

    def _load(self) -> None:
        """Load config from file. Creates file with defaults if it doesn't exist.
        Falls back to defaults if the file is malformed."""
        if not self._path.exists():
            self._save()
            return
        try:
            data = json.loads(self._path.read_text())
            if not isinstance(data, dict):
                raise ValueError("Config file root must be a JSON object")
            # Merge: start from defaults, overlay with stored values for known keys
            merged = dict(DEFAULTS)
            for k, v in data.items():
                if k in DEFAULTS:
                    merged[k] = v
            self._values = merged
        except Exception as exc:
            logger.warning("runtime_config.json is malformed, using defaults: %s", exc)
            self._values = dict(DEFAULTS)

    def _save(self) -> None:
        self._path.write_text(json.dumps(self._values, indent=2))

    def reload(self) -> None:
        """Re-read runtime_config.json. Called by trading loop at start of each cycle."""
        self._load()

    def get(self, key: str) -> Any:
        return self._values.get(key, DEFAULTS.get(key))

    def all(self) -> dict:
        return dict(self._values)

    def validate_and_update(self, updates: dict) -> dict[str, str]:
        """Validate an updates dict.

        Returns {key: error_message} for any invalid fields.
        If no errors, applies all updates and saves to runtime_config.json.
        If any errors, does NOT save anything.
        """
        errors: dict[str, str] = {}
        validated: dict[str, Any] = {}

        for key, raw_value in updates.items():
            if key not in DEFAULTS:
                errors[key] = "unknown key"
                continue

            if key in _BOOL_KEYS:
                # Must be a real bool, not 0/1 (int)
                if not isinstance(raw_value, bool):
                    errors[key] = f"expected bool, got {type(raw_value).__name__}"
                    continue
                validated[key] = raw_value

            elif key in _INT_KEYS:
                # Reject bools passed where int expected
                if isinstance(raw_value, bool):
                    errors[key] = f"expected int, got bool"
                    continue
                try:
                    coerced = int(raw_value)
                except (TypeError, ValueError):
                    errors[key] = f"expected int, got {type(raw_value).__name__}"
                    continue
                if key in _NUMERIC_RANGES:
                    lo, hi = _NUMERIC_RANGES[key]
                    if not (lo <= coerced <= hi):
                        errors[key] = f"value {coerced} out of range [{lo}, {hi}]"
                        continue
                validated[key] = coerced

            elif key in _FLOAT_KEYS:
                # Reject bools passed where float expected
                if isinstance(raw_value, bool):
                    errors[key] = f"expected float, got bool"
                    continue
                try:
                    coerced = float(raw_value)
                except (TypeError, ValueError):
                    errors[key] = f"expected float, got {type(raw_value).__name__}"
                    continue
                if key in _NUMERIC_RANGES:
                    lo, hi = _NUMERIC_RANGES[key]
                    if not (lo <= coerced <= hi):
                        errors[key] = f"value {coerced} out of range [{lo}, {hi}]"
                        continue
                validated[key] = coerced

            elif key in _STR_KEYS:
                validated[key] = str(raw_value)

            else:
                # Fallback: accept as-is
                validated[key] = raw_value

        if errors:
            return errors

        self._values.update(validated)
        self._save()
        return {}


cfg = ConfigManager()
