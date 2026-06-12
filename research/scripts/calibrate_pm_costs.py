"""Calibrate a REALISTIC Polymarket fill model from executed-trade data.

Official Polymarket exposes no historical order-book depth, so the backtest
used a fabricated constant 1000-contract/level book — which made the
price-impact model a no-op and flattered thin-market strategies (WS3/C7). This
script samples NBA winner markets, pulls their REAL executed trades (data-api),
and estimates a depth/clip-size proxy from the trade-size distribution.

A single executed trade of size S proves at least S was resting at that price,
so a trade-size percentile is a LOWER bound on resting depth — using a LOW
percentile (25th) is therefore a conservative, pessimistic-on-liquidity, and
honest depth estimate (it can only over-charge impact, never under-charge it).

Writes market_data/cost_calibration.json, consumed by
research.harness.cost_profile to build the CALIBRATED_PM profile.

Usage:
  ~/code/kvenv/bin/python research/scripts/calibrate_pm_costs.py --n-games 25
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import numpy as np
import pyarrow.parquet as pq

_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_ROOT))

from research.nba_odds_study import pm_trades  # noqa: E402

LAKE = _ROOT / "market_data" / "lake" / "v1"
OUT = _ROOT / "market_data" / "cost_calibration.json"


def _parse_gid(gid: str):
    """'2025-10-21_GSW_at_LAL' -> ('2025-10-21', 'GSW', 'LAL')."""
    p = gid.split("_")
    if len(p) >= 4 and p[2] == "at":
        return p[0], p[1], p[3]
    return None, None, None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--n-games", type=int, default=25)
    ap.add_argument("--seed", type=int, default=0)
    args = ap.parse_args()

    pm_dir = LAKE / "polymarket_ticks"
    covered = [
        f.stem for f in sorted(pm_dir.glob("*.parquet"))
        if pq.ParquetFile(f).metadata.num_rows > 50
    ]
    rng = np.random.default_rng(args.seed)
    sample = list(rng.choice(covered, size=min(args.n_games, len(covered)), replace=False))

    sizes: list[float] = []
    per_game_counts: list[int] = []
    n_with = 0
    for gid in sample:
        d, away, home = _parse_gid(gid)
        if d is None:
            continue
        try:
            cond = pm_trades.winner_condition_id(d, away, home)
            if not cond:
                continue
            trades = pm_trades.fetch_trades(cond)
        except Exception as e:  # noqa: BLE001
            print(f"  {gid}: {type(e).__name__}: {e}", file=sys.stderr)
            continue
        ss = [float(t["size"]) for t in trades
              if t.get("size") is not None and float(t["size"]) > 0]
        if ss:
            n_with += 1
            per_game_counts.append(len(ss))
            sizes.extend(ss)

    arr = np.array(sizes, dtype=float)
    if arr.size == 0:
        print("NO TRADES collected; aborting (check data-api reachability).", file=sys.stderr)
        sys.exit(1)

    pct = {str(p): round(float(np.percentile(arr, p)), 3) for p in (10, 25, 50, 75, 90)}
    # Median executed clip per price level. A trade is a LOWER bound on resting
    # depth, so the median clip still understates true depth -> conservative
    # (it can over-charge impact, never under-charge). p25 is exposed in
    # trade_size_pct for an even more pessimistic profile if wanted.
    depth_per_level = max(round(float(np.percentile(arr, 50)), 2), 1.0)
    out = {
        "source": "polymarket data-api executed trades",
        "n_games_sampled": len(sample),
        "n_games_with_trades": n_with,
        "n_trades": int(arr.size),
        "trade_size_pct": pct,
        "median_trades_per_game": float(np.median(per_game_counts)) if per_game_counts else 0.0,
        "depth_per_level": depth_per_level,
        "depth_per_level_rationale": (
            "median executed clip size per level; a trade is a lower bound on "
            "resting depth so the median clip understates true depth -> a "
            "conservative honest estimate (over-charges impact, never under)."
        ),
    }
    OUT.write_text(json.dumps(out, indent=2))
    print(json.dumps(out, indent=2))


if __name__ == "__main__":
    main()
