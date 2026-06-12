"""Monthly walk-forward certification of the spread pace-anchoring edge.

Direction (`continuation`) is mechanism-pre-registered (NOT fit), so EVERY game
is a valid out-of-sample observation. Runs the full-population gate
(block-bootstrap-by-game over all cached games) plus a monthly walk-forward:
each month is an independent OOS slice. A real edge is stable across months; an
artifact is concentrated in one window.

Usage::

    python3 -m research.scripts.spread_walkforward --thresh 6 --max-stale-min 2
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

import numpy as np
import pandas as pd

_ROOT = Path(__file__).resolve().parents[2]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from nba_odds_study import schedule  # noqa: E402
from research.scripts.spread_alpha import build_trades, evaluate  # noqa: E402


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--start", default="2025-10-21")
    ap.add_argument("--end", default="2026-06-08")
    ap.add_argument("--thresh", type=float, default=6.0)
    ap.add_argument("--entry-lat-min", type=float, default=1.0)
    ap.add_argument("--min-elapsed", type=float, default=600.0)
    ap.add_argument("--max-elapsed", type=float, default=2520.0)
    ap.add_argument("--max-stale-min", type=float, default=2.0)
    ap.add_argument("--cost", type=float, default=2.0)
    ap.add_argument("--limit", type=int, default=None)
    a = ap.parse_args()

    games = schedule.completed_games(a.start, a.end)
    if a.limit:
        games = games[: a.limit]
    print(f"{len(games)} games in {a.start}..{a.end}; continuation (pre-registered), "
          f"thresh={a.thresh}, cost sweep + monthly walk-forward\n", flush=True)

    tr = build_trades(games, side="continuation", thresh=a.thresh,
                      entry_lat_min=a.entry_lat_min, min_elapsed=a.min_elapsed,
                      max_elapsed=a.max_elapsed, max_stale_min=a.max_stale_min)
    if tr.empty:
        print("no trades (data not built yet?)"); return
    tr["month"] = pd.to_datetime(tr["date"]).dt.to_period("M").astype(str)

    print("=== FULL POPULATION (all cached games, direction pre-registered) ===")
    for c in (0.0, 1.0, 2.0, 3.0, 4.0):
        z = evaluate(tr, c)
        flag = "  <-- gate PASS" if z["gate"] else ""
        print(f"  cost={c:.0f}c: n={z['n']:>4} games={z['ngames']:>4} "
              f"c/ct={z['cents']:+.2f}  CI[{z['ci_lo']:+.2f},{z['ci_hi']:+.2f}] "
              f"gate={z['gate']}{flag}", flush=True)
    zc = evaluate(tr, a.cost)
    if zc["reasons"]:
        print(f"  gate@{a.cost:.0f}c reasons:", "; ".join(zc["reasons"][:5]), flush=True)

    print(f"\n=== MONTHLY WALK-FORWARD (each month independent OOS, cost={a.cost:.0f}c) ===")
    print(f"  {'month':<9}{'n':>5}{'win%':>7}{'c/ct@0':>9}{f'c/ct@{a.cost:.0f}':>9}{'CIlo':>8}")
    pos = 0
    months = sorted(tr["month"].unique())
    for m in months:
        sub = tr[tr["month"] == m]
        z0 = evaluate(sub, 0.0)
        zc = evaluate(sub, a.cost)
        win = (sub["pnl_prob"] > 0).mean()
        if np.isfinite(zc["cents"]) and zc["cents"] > 0:
            pos += 1
        print(f"  {m:<9}{len(sub):>5}{win*100:>6.0f}%{z0['cents']:>9.2f}"
              f"{zc['cents']:>9.2f}{zc['ci_lo']:>8.2f}", flush=True)
    print(f"\n  months net-positive after {a.cost:.0f}c cost: {pos}/{len(months)}", flush=True)


if __name__ == "__main__":
    main()
