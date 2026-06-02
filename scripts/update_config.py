#!/usr/bin/env python
"""Update a single key in runtime_config.json. Called by Claude when adjusting weights.

Usage:
    python scripts/update_config.py --key weight_noaa --value 0.90
    python scripts/update_config.py --key weight_polymarket_price --value 0.80 --reason "price gap signal been accurate last 5 trades"

Always prints the updated value to confirm.
"""
import argparse, json
from pathlib import Path

def main() -> None:
    parser = argparse.ArgumentParser(description="Update a runtime_config.json value")
    parser.add_argument("--key", required=True, help="Config key to update")
    parser.add_argument("--value", required=True, help="New value (auto-typed: float if numeric, bool if true/false)")
    parser.add_argument("--reason", default="", help="Why this weight is being changed (logged only)")
    parser.add_argument("--config", default="runtime_config.json")
    args = parser.parse_args()

    p = Path(args.config)
    cfg = json.loads(p.read_text()) if p.exists() else {}

    # Type coercion
    val: float | bool | str
    if args.value.lower() == "true":
        val = True
    elif args.value.lower() == "false":
        val = False
    else:
        try:
            val = float(args.value)
        except ValueError:
            val = args.value

    old = cfg.get(args.key, "NOT SET")
    cfg[args.key] = val
    p.write_text(json.dumps(cfg, indent=2))

    print(f"Updated {args.key}: {old} → {val}")
    if args.reason:
        print(f"Reason: {args.reason}")

if __name__ == "__main__":
    main()
