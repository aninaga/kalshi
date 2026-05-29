"""Measure quote forward-fill staleness across the lake (WS4).

The replay engine forward-fills the last-known PM/Kalshi quote onto each pbp
bar via merge_asof(direction="backward") with no time bound — so a quote could
in principle be marked as "current" minutes (or quarters) after it was last
seen. This script measures the actual forward-fill age distribution so the
staleness bound in research.harness.replay is set from data, not a guess.

Usage:
  ~/code/kvenv/bin/python research/scripts/measure_staleness.py [--n-games 80]
"""
from __future__ import annotations

import argparse
from pathlib import Path

import numpy as np
import pandas as pd
import pyarrow.parquet as pq

_ROOT = Path(__file__).resolve().parents[2]
LAKE = _ROOT / "market_data" / "lake" / "v1"


def _ages(table: str, team_filter: bool, n: int, seed: int) -> np.ndarray:
    tdir = LAKE / table
    covered = [f.stem for f in sorted(tdir.glob("*.parquet"))
               if pq.ParquetFile(f).metadata.num_rows > 20]
    if not covered:
        return np.array([])
    rng = np.random.default_rng(seed)
    sample = rng.choice(covered, size=min(n, len(covered)), replace=False)
    ages: list[float] = []
    for gid in sample:
        pbpf = LAKE / "pbp" / f"{gid}.parquet"
        if not pbpf.exists():
            continue
        pbp = pd.read_parquet(pbpf, columns=["minute_ts", "elapsed_game_sec"])
        pbp = pbp[pbp.elapsed_game_sec.notna()].sort_values("minute_ts")
        q = pd.read_parquet(tdir / f"{gid}.parquet")
        if team_filter and "team" in q.columns:
            q = q[q["team"].notna()]
        if q.empty:
            continue
        ql = q[["minute_ts"]].sort_values("minute_ts").rename(columns={"minute_ts": "q"})
        ql["q2"] = ql["q"]
        m = pd.merge_asof(pbp, ql, left_on="minute_ts", right_on="q", direction="backward")
        ages.extend((m.minute_ts - m.q2).dropna().tolist())
    return np.array(ages, dtype=float)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--n-games", type=int, default=80)
    ap.add_argument("--seed", type=int, default=0)
    args = ap.parse_args()

    for table, tf in (("polymarket_ticks", True), ("kalshi_ticks", True)):
        arr = _ages(table, tf, args.n_games, args.seed)
        print(f"=== {table} forward-fill age (n={arr.size}) ===")
        if arr.size == 0:
            print("  no data")
            continue
        for p in (50, 90, 95, 99, 99.9):
            print(f"  p{p} = {np.percentile(arr, p):.0f}s")
        print(f"  max={arr.max():.0f}s mean={arr.mean():.1f}s")
        for thr in (120, 300, 600):
            print(f"  frac > {thr}s = {(arr > thr).mean():.4f}")


if __name__ == "__main__":
    main()
