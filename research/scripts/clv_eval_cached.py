"""Read-only CLV evaluator: builds trades ONLY from already-cached _clv_cache
pkls + the winner pkls. Never makes a network call, so it can run alongside the
fetcher without competing for the Kalshi rate limit.

Fits the direction on TRAIN, applies the pre-registered direction to VAL and to
the FULL ex-test population, and scores every cut through the promotion gate.

Usage:: python3 -m research.scripts.clv_eval_cached --obs-min 60 --thresh 0.02
"""
from __future__ import annotations

import argparse
import json
import os
import pickle
import sys
from pathlib import Path

import numpy as np
import pandas as pd

_ROOT = Path(__file__).resolve().parents[2]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from nba_odds_study import batch, schedule  # noqa: E402
from research.scorer.promotion_gate import evaluate_trial  # noqa: E402

CLV_CACHE = os.path.join("market_data", "nba_studies", "_clv_cache")


def _read_cached(game, max_lookback_h=36.0):
    """Return (tip, home_won, home_tri, away_tri, df) from caches, or None.

    Uses ONLY on-disk pkls; never fetches.
    """
    wp = os.path.join(batch.CACHE_DIR,
                      f"{game['date']}_{game['away']}_at_{game['home']}_winner_all.pkl")
    cp = os.path.join(CLV_CACHE,
                      f"{game['date']}_{game['away']}_at_{game['home']}_clvwin_{int(max_lookback_h)}h.pkl")
    if not (os.path.exists(wp) and os.path.exists(cp)):
        return None
    try:
        with open(wp, "rb") as f:
            d = pickle.load(f)
        with open(cp, "rb") as f:
            cand = pickle.load(f)
    except Exception:  # noqa: BLE001
        return None
    if cand is None or len(cand) < 5:
        return None
    g = d.game
    m = d.minute["margin"].dropna()
    if m.empty:
        return None
    home_won = 1.0 if m.iloc[-1] > 0 else 0.0
    return int(g.first_ts), home_won, g.home_tri, g.away_tri, cand


def build_trades(games, *, side, obs_min, entry_lat_min, thresh, min_open_h,
                 require_open_move, max_lookback_h=36.0):
    rows = []
    for g in games:
        rc = _read_cached(g, max_lookback_h)
        if rc is None:
            continue
        tip, hw, home_tri, away_tri, cand = rc
        ts = cand["ts"].to_numpy(float)
        mid = cand["mid"].to_numpy(float)
        if (tip - ts[0]) / 3600.0 < min_open_h:
            continue
        p_open = float(mid[0])
        t_obs = tip - obs_min * 60
        t_entry = tip - (obs_min - entry_lat_min) * 60
        if t_obs <= ts[0] or t_entry <= ts[0] or ts[-1] < t_entry:
            continue
        p_obs = float(np.interp(t_obs, ts, mid))
        p_entry = float(np.interp(t_entry, ts, mid))
        if not (np.isfinite(p_obs) and np.isfinite(p_entry)):
            continue
        drift = p_obs - p_open
        if require_open_move and abs(drift) < 1e-9:
            continue
        if abs(drift) < thresh:
            continue
        home_rising = drift > 0
        long_home = home_rising if side == "continuation" else (not home_rising)
        pnl = (hw - p_entry) if long_home else (p_entry - hw)
        prim = home_tri if long_home else away_tri
        rows.append({"game_id": f"{g['date']}_{away_tri}_at_{home_tri}", "date": g["date"],
                     "home_team": home_tri, "primary_team": prim,
                     "side": "long_home" if long_home else "short_home",
                     "p_open": round(p_open, 3), "p_obs": round(p_obs, 3),
                     "p_entry": round(p_entry, 3), "drift": round(drift, 4),
                     "home_won": hw, "pnl_prob": pnl})
    return pd.DataFrame(rows)


def evaluate(trades, cost_c, n_trials=1):
    if trades is None or trades.empty or len(trades) < 2:
        return {"n": 0 if trades is None else len(trades), "cents": float("nan"),
                "gate": False, "ci_lo": float("nan"), "ci_hi": float("nan"),
                "ngames": 0, "reasons": ["no trades"]}
    pnl = trades["pnl_prob"].to_numpy(float) - cost_c / 100.0
    dec = evaluate_trial(
        val_pnl_per_trade=pnl, val_game_id_per_trade=trades["game_id"].to_numpy(),
        val_date_per_trade=trades["date"].to_numpy(),
        val_home_team_per_trade=trades["home_team"].to_numpy(),
        val_primary_team_per_trade=trades["primary_team"].to_numpy(),
        n_total_trials_in_registry=n_trials, cost_per_trade_assumed=cost_c / 100.0)
    return {"n": int(len(pnl)), "cents": float(pnl.mean() * 100), "gate": bool(dec.passed),
            "ci_lo": float(dec.block_bootstrap_ci_lo * 100),
            "ci_hi": float(dec.block_bootstrap_ci_hi * 100),
            "ngames": int(dec.n_games_val), "reasons": dec.reasons}


def _report(label, tr, dump=None):
    z = evaluate(tr, 0.0)
    print(f"[{label}] n={z['n']} games={z['ngames']}", flush=True)
    for c in (0.0, 1.0, 2.0, 3.0, 4.0):
        r = evaluate(tr, c)
        print(f"   @{int(c)}c: {r['cents']:+.2f}c/ct CI[{r['ci_lo']:+.2f},{r['ci_hi']:+.2f}] gate={r['gate']}",
              flush=True)
    r2 = evaluate(tr, 2.0)
    if r2["reasons"]:
        print("   gate@2 reasons:", "; ".join(r2["reasons"][:5]), flush=True)
    if tr is not None and not tr.empty:
        print(f"   drift mean={tr['drift'].mean():+.4f} |drift|={tr['drift'].abs().mean():.4f} "
              f"long_home_frac={(tr['side']=='long_home').mean():.2f} "
              f"home_win_rate={tr['home_won'].mean():.3f}", flush=True)
        if dump:
            tr.to_parquet(dump)
            print(f"   dumped -> {dump}", flush=True)
    print(flush=True)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--start", default="2025-10-21")
    ap.add_argument("--end", default="2026-06-08")
    ap.add_argument("--obs-min", type=float, default=60.0)
    ap.add_argument("--entry-lat-min", type=float, default=1.0)
    ap.add_argument("--thresh", type=float, default=0.02)
    ap.add_argument("--min-open-h", type=float, default=3.0)
    ap.add_argument("--max-lookback-h", type=float, default=36.0)
    ap.add_argument("--require-open-move", action="store_true", default=True)
    ap.add_argument("--no-require-open-move", dest="require_open_move", action="store_false")
    ap.add_argument("--dump-prefix", default=None)
    a = ap.parse_args()

    splits = json.loads((_ROOT / "market_data" / "splits.json").read_text())
    train_ids = set(splits["train_game_ids"])
    val_ids = set(splits.get("val_game_ids", []))
    test_ids = set(splits.get("test_game_ids", []))
    games = schedule.completed_games(a.start, a.end)

    def gid(g):
        return f"{g['date']}_{g['away']}_at_{g['home']}"
    train = [g for g in games if gid(g) in train_ids]
    val = [g for g in games if gid(g) in val_ids]
    full = [g for g in games if gid(g) not in test_ids]  # full ex-test

    kw = dict(obs_min=a.obs_min, entry_lat_min=a.entry_lat_min, thresh=a.thresh,
              min_open_h=a.min_open_h, require_open_move=a.require_open_move,
              max_lookback_h=a.max_lookback_h)

    # Fit direction on TRAIN (1-bit: higher 0c mean).
    print(f"=== DIRECTION FIT (train, cached only) obs_min={a.obs_min} thresh={a.thresh} ===", flush=True)
    fits = {}
    for side in ("continuation", "reversion"):
        tr = build_trades(train, side=side, **kw)
        z = evaluate(tr, 0.0)
        fits[side] = z["cents"] if np.isfinite(z["cents"]) else -1e9
        print(f"  train {side}: n={z['n']} 0c={z['cents']:+.2f}c", flush=True)
    best = max(fits, key=fits.get)
    print(f"  -> train-selected side: {best}\n", flush=True)

    print("=== PRE-REGISTERED direction = continuation ===\n", flush=True)
    chosen = "continuation"
    dp = (lambda s: f"{a.dump_prefix}_{s}.parquet") if a.dump_prefix else (lambda s: None)
    _report(f"TRAIN {chosen}", build_trades(train, side=chosen, **kw), dp("train"))
    _report(f"VAL {chosen}", build_trades(val, side=chosen, **kw), dp("val"))
    _report(f"FULL-ex-test {chosen}", build_trades(full, side=chosen, **kw), dp("full"))


if __name__ == "__main__":
    main()
