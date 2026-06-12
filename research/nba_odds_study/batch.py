"""Build many games (cached) and assemble one substitution -> odds-reaction table.

For every substitution we record, from the *subbing team's* perspective and at
minute offsets around the sub: raw win-prob change, score-residual win-prob
change (the part not explained by the scoreboard), and implied-total-line change.
Subs whose forward window would run into settlement are dropped so the reaction
reflects live trading, not the contract resolving to 0/1.
"""
from __future__ import annotations

import os
import pickle

import numpy as np
import pandas as pd

from . import analysis, wp_impact
from . import dataset as ds

CACHE_DIR = os.path.join("market_data", "nba_studies", "_cache")
DEFAULT_KINDS = {"winner", "total"}
OFFSETS = [-2, -1, 0, 1, 2, 3]
MIN_POST_SEC = 180  # require 3 live minutes after the sub


# Data-repro freeze (2026-06-12): set this env var to "1" to recompute each
# cached pkl's content hash on a CACHE HIT and WARN if it drifts from the
# recorded sidecar (a re-fetch silently re-stated the odds/minute content under
# identical code — a non-reproducible verdict). Off by default so routine loads
# pay no hashing cost; a fresh build ALWAYS writes the sidecar regardless.
_CACHE_HASH_CHECK_ENV = "NBA_CACHE_HASH_CHECK"


def load_or_build(game: dict, kinds=DEFAULT_KINDS, platforms=None, cache_dir=CACHE_DIR) -> ds.Dataset:
    os.makedirs(cache_dir, exist_ok=True)
    ptag = "-".join(sorted(platforms)) if platforms else "all"
    key = f"{game['date']}_{game['away']}_at_{game['home']}_{'-'.join(sorted(kinds))}_{ptag}"
    path = os.path.join(cache_dir, key + ".pkl")
    if os.path.exists(path):
        with open(path, "rb") as f:
            d = pickle.load(f)
        # Opt-in drift check: record + expose, never raise, never block.
        if os.environ.get(_CACHE_HASH_CHECK_ENV) == "1":
            try:
                from . import cache_hash
                cache_hash.check_sidecar(path, d, warn=True)
            except Exception:  # noqa: BLE001 — the freeze is never load-critical
                pass
        return d
    d = ds.build(game["date"], game["away"], game["home"], kinds=kinds,
                 platforms=platforms, espn_id=game.get("espn_id"))
    with open(path, "wb") as f:
        pickle.dump(d, f)
    # Freeze the just-built content next to the pkl (best-effort sidecar).
    try:
        from . import cache_hash
        cache_hash.write_sidecar(path, d)
    except Exception:  # noqa: BLE001 — never fail a build on the freeze
        pass
    return d


def _interp(df, series):
    ig = df["game_min"] >= 0
    x = df.index[ig].to_numpy(float)
    y = series[ig].to_numpy(float)
    ok = ~np.isnan(y)
    x, y = x[ok], y[ok]
    if len(x) < 2:
        return lambda t: np.nan
    return lambda t: float(np.interp(t, x, y))


def _total_line(d) -> pd.Series:
    idx = list(d.minute.index)
    k = analysis._implied_total(d.odds_long, "kalshi", idx)
    p = analysis._implied_total(d.odds_long, "polymarket", idx)
    return pd.concat([k, p], axis=1).mean(axis=1)


def sub_reactions(games: list[dict], kinds=DEFAULT_KINDS, platforms=None, cache_dir=CACHE_DIR):
    rows, coverage = [], []
    for i, game in enumerate(games):
        tag = f"{game['date']} {game['away']}@{game['home']}"
        try:
            d = load_or_build(game, kinds=kinds, platforms=platforms, cache_dir=cache_dir)
        except Exception as e:  # noqa: BLE001
            coverage.append({**game, "ok": False, "err": str(e)[:90]})
            print(f"  [{i+1}/{len(games)}] {tag}: FAIL ({str(e)[:60]})")
            continue
        g, df = d.game, d.minute
        wp, resid = wp_impact._wp_columns(df)
        if wp.notna().sum() < 5:
            coverage.append({**game, "ok": False, "err": "no winprob"})
            print(f"  [{i+1}/{len(games)}] {tag}: no winprob")
            continue

        f_wp, f_rs = _interp(df, wp), _interp(df, resid)
        f_margin = _interp(df, df["margin"])
        f_total = _interp(df, _total_line(d))
        f_elapsed = _interp(df, df["elapsed_game_sec"])
        f_totpts = _interp(df, df["total"])
        home = g.home_tri
        stars = set(g.stars[home]) | set(g.stars[g.away_tri])
        starters = {p for names in g.starters.values() for p in names}

        n_used = 0
        for s in d.subs:
            t0 = s["ts"]
            if t0 < g.first_ts or t0 + MIN_POST_SEC > g.last_ts:
                continue
            team = s["team"]
            home_side = team == home
            base_wp = f_wp(t0) if home_side else 1 - f_wp(t0)
            base_rs = f_rs(t0) if home_side else -f_rs(t0)
            base_tot = f_total(t0)
            out_star, in_star = s["player_out"] in stars, s["player_in"] in stars
            out_st, in_st = s["player_out"] in starters, s["player_in"] in starters
            if out_star and not in_star:
                typ = "star_out"
            elif in_star and not out_star:
                typ = "star_in"
            elif out_star and in_star:
                typ = "star_swap"
            elif out_st and not in_st:
                typ = "starter_out"
            elif in_st and not out_st:
                typ = "starter_in"
            else:
                typ = "bench"
            own_margin = (f_margin(t0) if home_side else -f_margin(t0))
            elapsed = f_elapsed(t0)
            quarter = min(int(elapsed // 720) + 1, 4) if not np.isnan(elapsed) else None
            row = {
                "game": f"{game['date']} {g.away_tri}@{g.home_tri}", "date": game["date"],
                "team": team, "ts": t0, "quarter": quarter,
                "player_in": s["player_in"], "player_out": s["player_out"],
                "type": typ, "own_margin": round(own_margin, 1),
                "abs_margin": round(abs(own_margin), 1), "own_wp0": round(base_wp, 3),
                "time_left_sec": round(max(2880 - elapsed, 0), 0) if not np.isnan(elapsed) else np.nan,
            }
            for k in OFFSETS:
                t = t0 + 60 * k
                cur_wp = (f_wp(t) if home_side else 1 - f_wp(t))
                cur_rs = (f_rs(t) if home_side else -f_rs(t))
                row[f"mlraw_{k}"] = cur_wp - base_wp
                row[f"mlres_{k}"] = cur_rs - base_rs
                row[f"tot_{k}"] = (f_total(t) - base_tot) if not np.isnan(base_tot) else np.nan
            row["tot_pts_next3"] = f_totpts(t0 + 180) - f_totpts(t0)
            rows.append(row)
            n_used += 1
        coverage.append({**game, "ok": True, "subs_used": n_used})
        print(f"  [{i+1}/{len(games)}] {tag}: {n_used} subs")
    return pd.DataFrame(rows), pd.DataFrame(coverage)
