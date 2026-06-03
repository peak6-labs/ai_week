"""Filter a market snapshot down to tradeable markets.

A full prod fetch (scripts/fetch_markets.py) returns hundreds of thousands of
zero-liquidity multi-leg markets. This offline filter keeps only markets with
real liquidity so score_markets.py isn't dominated by untradeable noise.

Purely offline — reads and writes JSON snapshots, makes no API calls.

Usage:
    python scripts/filter_markets.py
    python scripts/filter_markets.py --input live_markets.json --output live_markets.tradeable.json
    python scripts/filter_markets.py --min-volume 100 --min-open-interest 50
"""
import argparse
import json
from datetime import datetime, timezone
from pathlib import Path


def filter_snapshot(input_path: str, output_path: str,
                    min_volume: int, min_open_interest: int) -> None:
    snapshot = json.loads(Path(input_path).read_text())
    all_markets = snapshot["markets"]

    tradeable_markets = [
        market for market in all_markets
        if (market.get("volume_24h") or 0) >= min_volume
        and (market.get("open_interest") or 0) >= min_open_interest
    ]

    payload = {
        "saved_at": datetime.now(timezone.utc).isoformat(),
        "count": len(tradeable_markets),
        "markets": tradeable_markets,
    }
    Path(output_path).write_text(json.dumps(payload, indent=2))

    kept_fraction = len(tradeable_markets) / len(all_markets) if all_markets else 0.0
    print(f"Kept {len(tradeable_markets):,} of {len(all_markets):,} markets "
          f"({100 * kept_fraction:.1f}%) → {output_path}")
    print(f"Filter: volume_24h >= {min_volume} AND open_interest >= {min_open_interest}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Filter a market snapshot to tradeable markets.")
    parser.add_argument("--input", default="live_markets.json", help="Input snapshot JSON path")
    parser.add_argument("--output", default="live_markets.tradeable.json", help="Output snapshot JSON path")
    parser.add_argument("--min-volume", type=int, default=1, help="Minimum 24h volume to keep a market")
    parser.add_argument("--min-open-interest", type=int, default=1, help="Minimum open interest to keep a market")
    args = parser.parse_args()

    filter_snapshot(args.input, args.output, args.min_volume, args.min_open_interest)
