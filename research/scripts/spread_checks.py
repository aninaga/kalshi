"""Adversarial checks for the spread pace-anchoring candidate.

(b) ESTIMATOR / MEASUREMENT-BIAS baseline: at a clean mid-game snapshot, is the
implied home margin an UNBIASED estimate of the final home margin? If
P(final home margin > imp_t) ~= 0.50 unconditionally on fresh quotes, the
implied-margin reconstruction is calibrated and any conditional edge is real
signal, not a constant measurement bias. This mirrors the totals teardown's
"unconditional P(over)=0.502" check.

Also reports the directional split of the continuation signal (how often
proj>imp vs proj<imp) and the realized cover rate in each, to expose whether the
edge is one-sided (e.g. only the home/favorite side works).

Usage::

    python3 -m research.scripts.spread_checks --thresh 6 --min-elapsed 720
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

import numpy as np

_ROOT = Path(__file__).resolve().parents[2]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from nba_odds_study import schedule  # noqa: E402
from research.scripts.spread_alpha import _load  # noqa: E402


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--start", default="2025-10-21")
    ap.add_argument("--end", default="2026-06-08")
    ap.add_argument("--snapshot-elapsed", type=float, default=720.0,
                    help="clean snapshot elapsed-sec for the unconditional baseline (Q1 end=720)")
    ap.add_argument("--max-stale-min", type=float, default=2.0)
    ap.add_argument("--thresh", type=float, default=6.0)
    ap.add_argument("--min-elapsed", type=float, default=720.0)
    ap.add_argument("--max-elapsed", type=float, default=2520.0)
    ap.add_argument("--limit", type=int, default=None)
    a = ap.parse_args()

    games = schedule.completed_games(a.start, a.end)
    if a.limit:
        games = games[: a.limit]

    snap_over = []          # unconditional: final > imp at the clean snapshot
    cont_home, cont_away = [], []   # continuation conditional cover outcomes
    n_games = 0
    for g in games:
        ld = _load(g)
        if ld is None:
            continue
        df, imp, final_margin, gid, gg = ld
        n_games += 1
        e = df["elapsed_game_sec"].to_numpy(float)
        impv = imp.to_numpy(float)
        mar = df["margin"].to_numpy(float)
        idxts = df.index.to_numpy(float)
        proj = np.where(e > 120, mar * 2880.0 / e, np.nan)
        chg = np.r_[True, np.abs(np.diff(impv)) > 1e-9]
        last_chg = np.maximum.accumulate(np.where(chg, np.arange(len(impv)), -1))
        stale_min = np.arange(len(impv)) - last_chg
        ok = np.isfinite(e) & np.isfinite(impv)

        # (b) unconditional baseline at clean snapshot (first fresh bar >= snapshot)
        cand = np.where(ok & (e >= a.snapshot_elapsed) & (stale_min <= a.max_stale_min))[0]
        if len(cand):
            k = cand[np.argmin(e[cand])]
            snap_over.append(1.0 if final_margin > impv[k] else 0.0)

        # continuation conditional: first fresh signal bar
        okp = ok & np.isfinite(proj)
        order = np.argsort(idxts)
        for k in order:
            if not okp[k] or e[k] < a.min_elapsed or e[k] > a.max_elapsed:
                continue
            if stale_min[k] > a.max_stale_min:
                continue
            sig = proj[k] - impv[k]
            if abs(sig) < a.thresh:
                continue
            covered = 1.0 if ((final_margin > impv[k]) == (sig > 0)) else 0.0
            (cont_home if sig > 0 else cont_away).append(covered)
            break

    so = np.array(snap_over)
    ch, ca = np.array(cont_home), np.array(cont_away)
    print(f"games used: {n_games}")
    print(f"\n(b) UNCONDITIONAL baseline at elapsed>={a.snapshot_elapsed:.0f}s (fresh quotes):")
    print(f"    P(final home margin > implied margin) = {so.mean():.4f}  (n={len(so)})")
    print(f"    -> calibrated estimator iff ~= 0.50; |bias| = {abs(so.mean()-0.5):.4f}")
    print(f"\n    continuation signal direction split (thresh={a.thresh}, fresh):")
    print(f"    proj>imp (bet HOME): n={len(ch)} realized-cover={ch.mean() if len(ch) else float('nan'):.4f}")
    print(f"    proj<imp (bet AWAY): n={len(ca)} realized-cover={ca.mean() if len(ca) else float('nan'):.4f}")
    allc = np.concatenate([ch, ca]) if len(ch)+len(ca) else np.array([])
    print(f"    pooled continuation cover rate = {allc.mean() if len(allc) else float('nan'):.4f} (n={len(allc)})")
    print(f"    -> edge in cents/contract @0c = {(allc.mean()-0.5)*100 if len(allc) else float('nan'):.2f}")


if __name__ == "__main__":
    main()
