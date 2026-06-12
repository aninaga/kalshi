"""Build a one-time joined winner+total per-minute cache for the term-structure
study, so the evaluator can iterate quickly without re-loading raw pkls.

For every game where BOTH the winner pkl and the total pkl are cached, we join
on the minute index and persist:
  game_id, date, home, away, minute(ts), elapsed_game_sec, margin, total,
  wp (blended home win prob), imp (blended implied total line), home_won.

Writes a single parquet to market_data/nba_studies/_cache/_joined_ws.parquet.

Usage:
    python3 -m research.scripts.term_structure_cache
"""
from __future__ import annotations

import pickle
import sys
from pathlib import Path

import numpy as np
import pandas as pd

_ROOT = Path(__file__).resolve().parents[2]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from nba_odds_study import analysis, schedule  # noqa: E402

CACHE = _ROOT / "market_data" / "nba_studies" / "_cache"
OUT = CACHE / "_joined_ws.parquet"


def _load_joint(g):
    wp_path = CACHE / f"{g['date']}_{g['away']}_at_{g['home']}_winner_all.pkl"
    tp_path = CACHE / f"{g['date']}_{g['away']}_at_{g['home']}_total_all.pkl"
    if not (wp_path.exists() and tp_path.exists()):
        return None
    try:
        w = pickle.load(open(wp_path, "rb"))
        t = pickle.load(open(tp_path, "rb"))
    except Exception:  # noqa: BLE001
        return None
    wdf = w.minute
    cols = [c for c in ("kalshi_home_winprob", "pm_home_winprob") if c in wdf]
    if not cols:
        return None
    wp = wdf[cols].mean(axis=1)
    if not len(t.odds_long) or "platform" not in t.odds_long.columns:
        return None
    idx = list(t.minute.index)
    imp = pd.concat(
        [analysis._implied_total(t.odds_long, "kalshi", idx),
         analysis._implied_total(t.odds_long, "polymarket", idx)], axis=1).mean(axis=1)
    if wp.notna().sum() < 5 or imp.notna().sum() < 5:
        return None
    gg = w.game
    gid = f"{g['date']}_{gg.away_tri}_at_{gg.home_tri}"
    m = wdf["margin"]
    if m.dropna().empty:
        return None
    home_won = 1.0 if float(m.dropna().iloc[-1]) > 0 else 0.0
    out = pd.DataFrame({
        "game_id": gid, "date": g["date"], "home": gg.home_tri, "away": gg.away_tri,
        "minute": wdf.index.to_numpy(float),
        "elapsed_game_sec": wdf["elapsed_game_sec"].to_numpy(float),
        "margin": m.to_numpy(float),
        "total": wdf["total"].to_numpy(float),
        "wp": wp.to_numpy(float),
        "imp": imp.reindex(wdf.index).to_numpy(float),
        "home_won": home_won,
    })
    return out


def main():
    games = schedule.completed_games("2025-10-21", "2026-06-08")
    frames, n_ok = [], 0
    for i, g in enumerate(games):
        r = _load_joint(g)
        if r is not None:
            frames.append(r)
            n_ok += 1
        if (i + 1) % 200 == 0:
            print(f"  {i+1}/{len(games)} processed, {n_ok} joined", flush=True)
    big = pd.concat(frames, ignore_index=True)
    big.to_parquet(OUT)
    print(f"games joined: {n_ok}/{len(games)}  rows: {len(big)}  -> {OUT}", flush=True)


if __name__ == "__main__":
    main()
