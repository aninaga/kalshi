"""Pre-game / closing-line-value (CLV) alpha test — a NEW temporal regime.

All prior NBA work was IN-GAME. This tests the PRE-GAME window only: from market
open (~1-2 days before tip, served by Kalshi historical candlesticks) up to
tip-off ("the close"), holding the winner contract to settlement.

Pre-registered mechanism (see research/reports/alpha/clv_mechanism.md):
  Pregame lines open thin/stale and incorporate information (injuries, lineups,
  sharp money) slowly through the day. The EARLY pregame drift should therefore
  CONTINUE — its direction predicts the settled outcome.

Tradeable formulation (hold to settlement):
  * p_open  = Kalshi home-winner mid at market open (earliest candle within
              `max_lookback_h` of tip).
  * p_obs   = mid at the observation time `obs_min` minutes before tip.
  * drift   = p_obs - p_open.  Signal fires if |drift| >= thresh.
  * Direction `continuation`: drift>0 (home rising) => BUY home; drift<0 => buy away.
    `reversion` is the diagnostic complement.
  * Honest latency: observe drift at obs_min; ENTER one bar later (obs_min-1 min
    before tip) at the then-prevailing mid p_entry.
  * PnL (prob units): long-home = home_won - p_entry; short-home = p_entry - home_won.
  * Round-trip cost swept. One trade per game. Scored by the full promotion gate.

Data source: Kalshi /historical/markets candlesticks (persist after settlement,
unlike Polymarket CLOB prices-history which returns empty for resolved markets).
Tip time + settlement are read from the existing cached winner pkls.

Usage::
    python3 -m research.scripts.clv_alpha --obs-min 60 --thresh 0.02 --split val
"""
from __future__ import annotations

import argparse
import json
import os
import pickle
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import numpy as np
import pandas as pd

_ROOT = Path(__file__).resolve().parents[2]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from nba_odds_study import batch, kalshi_hist, schedule  # noqa: E402
from research.scorer.promotion_gate import evaluate_trial  # noqa: E402

# Separate cache for the wide-pregame Kalshi candle pulls (does NOT touch the
# existing dataset.build pkls — those only hold 30 min pregame).
CLV_CACHE = os.path.join("market_data", "nba_studies", "_clv_cache")


def _winner_pkl(game) -> str:
    key = f"{game['date']}_{game['away']}_at_{game['home']}_winner_all.pkl"
    return os.path.join(batch.CACHE_DIR, key)


def _tip_and_outcome(game):
    """Read tip_ts (first_ts) and home_won (1/0) from the existing winner pkl."""
    p = _winner_pkl(game)
    if not os.path.exists(p):
        return None
    try:
        with open(p, "rb") as f:
            d = pickle.load(f)
    except Exception:  # noqa: BLE001
        return None
    g = d.game
    m = d.minute["margin"].dropna()
    if m.empty:
        return None
    home_won = 1.0 if m.iloc[-1] > 0 else 0.0
    return int(g.first_ts), home_won, g.home_tri, g.away_tri


def _fetch_pregame_candles(game, tip_ts, max_lookback_h):
    """Wide-pregame Kalshi HOME-winner mid series, cached to _clv_cache.

    Returns a sorted DataFrame with columns [ts, mid] of HOME win prob, or None.
    """
    os.makedirs(CLV_CACHE, exist_ok=True)
    ckey = f"{game['date']}_{game['away']}_at_{game['home']}_clvwin_{int(max_lookback_h)}h.pkl"
    cpath = os.path.join(CLV_CACHE, ckey)
    if os.path.exists(cpath):
        try:
            with open(cpath, "rb") as f:
                return pickle.load(f)
        except Exception:  # noqa: BLE001
            pass
    try:
        mk = kalshi_hist.enumerate_markets(game["date"], game["away"], game["home"])
    except Exception:  # noqa: BLE001
        return None
    win = [m for m in mk if m["kind"] == "winner" and m.get("yes_team") == game["home"]]
    if not win:
        # fall back to the away-winner market and invert
        win_away = [m for m in mk if m["kind"] == "winner" and m.get("yes_team") == game["away"]]
        if not win_away:
            with open(cpath, "wb") as f:
                pickle.dump(None, f)
            return None
        m = win_away[0]
        invert = True
    else:
        m = win[0]
        invert = False
    start = tip_ts - int(max_lookback_h * 3600)
    try:
        cs = kalshi_hist.fetch_candles(m["series"], m["ticker"], start, tip_ts + 60, period=1)
    except Exception:  # noqa: BLE001
        return None
    rows = [(c["ts"], c["mid"]) for c in cs if c["mid"] is not None and c["ts"] is not None]
    if not rows:
        with open(cpath, "wb") as f:
            pickle.dump(None, f)
        return None
    df = pd.DataFrame(rows, columns=["ts", "mid"]).sort_values("ts").drop_duplicates("ts")
    if invert:
        df["mid"] = 1.0 - df["mid"]
    df = df[df["ts"] < tip_ts]  # strictly pregame
    with open(cpath, "wb") as f:
        pickle.dump(df, f)
    return df


def build_trades(games, *, side, obs_min, entry_lat_min, thresh, max_lookback_h,
                 min_open_h, require_open_move, workers=6):
    """One trade per game. Honest +1-bar entry latency, hold to settlement."""
    # Stage 1: gather tip/outcome (local pkl reads) and which games have pkls.
    metas = {}
    for g in games:
        m = _tip_and_outcome(g)
        if m is not None:
            metas[id(g)] = (g, m)

    # Stage 2: fetch pregame candles concurrently (network-bound, idempotent cache).
    def _job(item):
        g, (tip, hw, home_tri, away_tri) = item
        df = _fetch_pregame_candles(g, tip, max_lookback_h)
        return g, tip, hw, home_tri, away_tri, df

    rows = []
    with ThreadPoolExecutor(max_workers=workers) as ex:
        futs = [ex.submit(_job, v) for v in metas.values()]
        for fut in as_completed(futs):
            g, tip, hw, home_tri, away_tri, df = fut.result()
            if df is None or len(df) < 5:
                continue
            ts = df["ts"].to_numpy(float)
            mid = df["mid"].to_numpy(float)
            # open = earliest candle, but only if it is at least min_open_h before tip
            open_age_h = (tip - ts[0]) / 3600.0
            if open_age_h < min_open_h:
                continue
            p_open = float(mid[0])
            # observation time: obs_min before tip
            t_obs = tip - obs_min * 60
            t_entry = tip - (obs_min - entry_lat_min) * 60
            # need candles bracketing both t_obs and t_entry (no extrapolation past data)
            if t_obs <= ts[0] or t_entry <= ts[0] or ts[-1] < t_entry:
                continue
            p_obs = float(np.interp(t_obs, ts, mid))
            p_entry = float(np.interp(t_entry, ts, mid))
            if not (np.isfinite(p_obs) and np.isfinite(p_entry)):
                continue
            drift = p_obs - p_open
            # staleness guard: require the line to have actually moved between
            # open and obs (otherwise drift is a flat-quote artifact).
            if require_open_move and abs(drift) < 1e-9:
                continue
            if abs(drift) < thresh:
                continue
            home_rising = drift > 0
            long_home = home_rising if side == "continuation" else (not home_rising)
            if long_home:
                pnl = hw - p_entry
                prim = home_tri
            else:
                pnl = p_entry - hw
                prim = away_tri
            rows.append({"game_id": f"{g['date']}_{away_tri}_at_{home_tri}",
                         "date": g["date"], "home_team": home_tri, "primary_team": prim,
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
        val_pnl_per_trade=pnl,
        val_game_id_per_trade=trades["game_id"].to_numpy(),
        val_date_per_trade=trades["date"].to_numpy(),
        val_home_team_per_trade=trades["home_team"].to_numpy(),
        val_primary_team_per_trade=trades["primary_team"].to_numpy(),
        n_total_trials_in_registry=n_trials,
        cost_per_trade_assumed=cost_c / 100.0,
    )
    return {"n": int(len(pnl)), "cents": float(pnl.mean() * 100), "gate": bool(dec.passed),
            "ci_lo": float(dec.block_bootstrap_ci_lo * 100),
            "ci_hi": float(dec.block_bootstrap_ci_hi * 100),
            "ngames": int(dec.n_games_val), "reasons": dec.reasons}


def _split_games(start, end, which):
    splits = json.loads((_ROOT / "market_data" / "splits.json").read_text())
    train_ids = set(splits["train_game_ids"])
    val_ids = set(splits.get("val_game_ids", []))
    games = schedule.completed_games(start, end)

    def gid(g):
        return f"{g['date']}_{g['away']}_at_{g['home']}"
    if which == "train":
        return [g for g in games if gid(g) in train_ids]
    if which == "val":
        return [g for g in games if gid(g) in val_ids]
    if which == "trainval":
        return [g for g in games if gid(g) in train_ids or gid(g) in val_ids]
    if which == "full":
        # full population EXCLUDING test (test is human-gated)
        test_ids = set(splits.get("test_game_ids", []))
        return [g for g in games if gid(g) not in test_ids]
    raise ValueError(which)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--start", default="2025-10-21")
    ap.add_argument("--end", default="2026-06-08")
    ap.add_argument("--split", default="val", choices=["train", "val", "trainval", "full"])
    ap.add_argument("--obs-min", type=float, default=60.0, help="minutes before tip to observe drift")
    ap.add_argument("--entry-lat-min", type=float, default=1.0)
    ap.add_argument("--thresh", type=float, default=0.02, help="min |open->obs drift| to trade")
    ap.add_argument("--max-lookback-h", type=float, default=36.0)
    ap.add_argument("--min-open-h", type=float, default=3.0,
                    help="open candle must be at least this many hours before tip")
    ap.add_argument("--require-open-move", action="store_true", default=True)
    ap.add_argument("--no-require-open-move", dest="require_open_move", action="store_false")
    ap.add_argument("--side", default=None, choices=[None, "continuation", "reversion"],
                    help="force a side; default fits on train, applies pre-registered continuation elsewhere")
    ap.add_argument("--workers", type=int, default=8)
    ap.add_argument("--limit", type=int, default=None)
    ap.add_argument("--dump-trades", default=None, help="parquet path to save the trades frame")
    a = ap.parse_args()

    games = _split_games(a.start, a.end, a.split)
    if a.limit:
        games = games[: a.limit]
    print(f"split={a.split} games={len(games)} obs_min={a.obs_min} thresh={a.thresh} "
          f"lookback={a.max_lookback_h}h min_open={a.min_open_h}h\n", flush=True)

    sides = [a.side] if a.side else ["continuation", "reversion"]
    for side in sides:
        tr = build_trades(games, side=side, obs_min=a.obs_min, entry_lat_min=a.entry_lat_min,
                          thresh=a.thresh, max_lookback_h=a.max_lookback_h,
                          min_open_h=a.min_open_h, require_open_move=a.require_open_move,
                          workers=a.workers)
        z0 = evaluate(tr, 0.0)
        results = {c: evaluate(tr, c) for c in (0.0, 1.0, 2.0, 3.0, 4.0)}
        print(f"[{a.split} {side}] n={z0['n']} games={z0['ngames']}", flush=True)
        for c in (0.0, 1.0, 2.0, 3.0, 4.0):
            r = results[c]
            print(f"   @{int(c)}c: {r['cents']:+.2f}c/ct  CI[{r['ci_lo']:+.2f},{r['ci_hi']:+.2f}]  gate={r['gate']}",
                  flush=True)
        if results[2.0]["reasons"]:
            print("   gate@2 reasons:", "; ".join(results[2.0]["reasons"][:5]), flush=True)
        # diagnostics
        if not tr.empty:
            print(f"   drift mean={tr['drift'].mean():+.4f} |drift| mean={tr['drift'].abs().mean():.4f} "
                  f"long_home_frac={(tr['side']=='long_home').mean():.2f} "
                  f"home_win_rate={tr['home_won'].mean():.3f}", flush=True)
        if a.dump_trades and not tr.empty:
            outp = a.dump_trades if len(sides) == 1 else a.dump_trades.replace(".parquet", f"_{side}.parquet")
            tr.to_parquet(outp)
            print(f"   dumped {len(tr)} trades -> {outp}", flush=True)
        print(flush=True)


if __name__ == "__main__":
    main()
