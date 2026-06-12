"""Cross-contract relative value: WINNER vs TOTAL term structure (joint mispricing).

Pre-registered mechanism (see research/TERM_STRUCTURE_FINDINGS.md):
  The winner contract reacts to the margin trajectory; the total contract reacts
  to pace. A random-walk model ties them: final home margin ~ M + N(0, sigma^2)
  with sigma = k * sqrt(R), R = remaining implied scoring = imp_total - total_now.
  model win prob mwp = Phi(M / (k*sqrt(R))). If the winner contract under-uses the
  pace signal carried by the total leg, then gap = mwp - wp_market predicts the
  outcome residual: gap>+tau => leader underpriced => BUY leader; gap<-tau => SELL.

Trade the REAL winner contract (binary, settles 0/1), hold to settlement.
Honest latency: observe gap at bar t, enter at bar t+1. One trade per game.
PnL (prob units) = (outcome - entry_price) long / (entry_price - outcome) short,
minus swept round-trip cost. Scored ONLY by promotion_gate.evaluate_trial.

`k` is calibrated ONCE on train by log-loss; the trade DIRECTION (buy/sell the
leg the gap points to) is fixed by the mechanism (not fished).

Controls:
  --signal {gap, pace_only}
     gap       = full model gap (margin-rw model vs market)
     pace_only = the INCREMENTAL total-leg contribution only
                 (gap_real - gap_const), where gap_const uses a fixed median R.
                 This isolates whether the TOTAL leg adds winner-relevant info
                 beyond a margin-only model (the genuine cross-contract test).

Usage:
    python3 -m research.scripts.term_structure_alpha --signal gap --tau 0.05
    python3 -m research.scripts.term_structure_alpha --signal pace_only --tau 0.02
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd
from scipy.stats import norm

_ROOT = Path(__file__).resolve().parents[2]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from research.scorer.promotion_gate import evaluate_trial  # noqa: E402

CACHE = _ROOT / "market_data" / "nba_studies" / "_cache"
JOINED = CACHE / "_joined_ws.parquet"


def load_panel():
    if not JOINED.exists():
        raise SystemExit("run: python3 -m research.scripts.term_structure_cache first")
    df = pd.read_parquet(JOINED)
    df = df.dropna(subset=["elapsed_game_sec", "margin", "total", "wp", "imp"])
    df["rem_pts"] = (df["imp"] - df["total"]).clip(lower=1.0)
    return df


def calibrate_k(train_df):
    """k that minimizes log-loss of mwp vs realized home_won on train."""
    m = train_df["margin"].to_numpy(float)
    r = train_df["rem_pts"].to_numpy(float)
    y = train_df["home_won"].to_numpy(float)
    best = None
    for k in np.arange(0.6, 2.21, 0.05):
        p = np.clip(norm.cdf(m / (k * np.sqrt(r))), 1e-4, 1 - 1e-4)
        ll = -(y * np.log(p) + (1 - y) * np.log(1 - p)).mean()
        if best is None or ll < best[1]:
            best = (float(k), float(ll))
    return best[0]


def add_signal(df, k, rem_const, signal):
    m = df["margin"].to_numpy(float)
    r = df["rem_pts"].to_numpy(float)
    mwp = np.clip(norm.cdf(m / (k * np.sqrt(r))), 1e-4, 1 - 1e-4)
    gap = mwp - df["wp"].to_numpy(float)
    if signal == "gap":
        df = df.assign(sig=gap)
    elif signal == "pace_only":
        mwp_c = np.clip(norm.cdf(m / (k * np.sqrt(rem_const))), 1e-4, 1 - 1e-4)
        gap_c = mwp_c - df["wp"].to_numpy(float)
        df = df.assign(sig=gap - gap_c)
    else:
        raise ValueError(signal)
    return df


def build_trades(df, *, tau, entry_lat_min, min_elapsed, max_elapsed, max_stale_min):
    """One trade per game: first bar after min_elapsed with |sig|>=tau. Enter at t+1."""
    rows = []
    for gid, g in df.groupby("game_id", sort=False):
        g = g.sort_values("minute")
        ts = g["minute"].to_numpy(float)
        e = g["elapsed_game_sec"].to_numpy(float)
        sig = g["sig"].to_numpy(float)
        wp = g["wp"].to_numpy(float)
        imp = g["imp"].to_numpy(float)
        hw = float(g["home_won"].iloc[0])
        home = g["home"].iloc[0]
        away = g["away"].iloc[0]
        date = g["date"].iloc[0]
        # freshness of the implied total line (minutes since imp last changed):
        # a stale total leg is a measurement artifact, not a tradeable signal.
        chg = np.r_[True, np.abs(np.diff(imp)) > 1e-9]
        last_chg = np.maximum.accumulate(np.where(chg, np.arange(len(imp)), -1))
        stale = np.arange(len(imp)) - last_chg
        ok = np.isfinite(sig) & np.isfinite(wp) & np.isfinite(e)
        for i in range(len(ts)):
            if not ok[i] or e[i] < min_elapsed or e[i] > max_elapsed:
                continue
            if stale[i] > max_stale_min:
                continue
            if abs(sig[i]) < tau:
                continue
            t_entry = ts[i] + entry_lat_min * 60
            entry_wp = float(np.interp(t_entry, ts[ok], wp[ok]))
            if not np.isfinite(entry_wp):
                continue
            long_leader = sig[i] > 0  # model says home underpriced => buy home
            if long_leader:
                pnl = hw - entry_wp
                prim = home
            else:
                pnl = entry_wp - hw
                prim = away
            rows.append({"game_id": gid, "date": date, "home_team": home,
                         "primary_team": prim, "side": "long_home" if long_leader else "short_home",
                         "entry": round(entry_wp, 3), "sig": round(float(sig[i]), 4),
                         "pnl_prob": pnl})
            break
    return pd.DataFrame(rows)


def evaluate(trades, cost_c, n_trials=1):
    if trades.empty or len(trades) < 2:
        return {"n": len(trades), "cents": float("nan"), "gate": False,
                "ci_lo": float("nan"), "ci_hi": float("nan"), "ngames": 0, "reasons": ["no trades"]}
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


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--signal", choices=["gap", "pace_only"], default="gap")
    ap.add_argument("--tau", type=float, default=0.05)
    ap.add_argument("--entry-lat-min", type=float, default=1.0)
    ap.add_argument("--min-elapsed", type=float, default=600.0)
    ap.add_argument("--max-elapsed", type=float, default=2520.0)
    ap.add_argument("--max-stale-min", type=float, default=2.0)
    ap.add_argument("--full", action="store_true",
                    help="evaluate on full train+val population (pre-registered direction)")
    a = ap.parse_args()

    splits = json.load(open(_ROOT / "market_data" / "splits.json"))
    train_ids = set(splits["train_game_ids"])
    val_ids = set(splits["val_game_ids"])

    panel = load_panel()
    panel = panel[(panel["elapsed_game_sec"] >= a.min_elapsed) &
                  (panel["elapsed_game_sec"] <= a.max_elapsed)]
    train_panel = panel[panel["game_id"].isin(train_ids)]
    val_panel = panel[panel["game_id"].isin(val_ids)]

    k = calibrate_k(train_panel)
    rem_const = float(train_panel["rem_pts"].median())
    print(f"signal={a.signal} tau={a.tau} k(train)={k:.2f} rem_const={rem_const:.1f} "
          f"train_obs={len(train_panel)} val_obs={len(val_panel)}", flush=True)

    def run(name, p):
        p = add_signal(p, k, rem_const, a.signal)
        tr = build_trades(p, tau=a.tau, entry_lat_min=a.entry_lat_min,
                          min_elapsed=a.min_elapsed, max_elapsed=a.max_elapsed,
                          max_stale_min=a.max_stale_min)
        z0 = evaluate(tr, 0.0)
        rs = {c: evaluate(tr, c) for c in (1, 2, 3, 4)}
        print(f"\n[{name}] n={z0['n']} games={z0['ngames']} "
              f"c/ct@0={z0['cents']:.2f}", flush=True)
        for c in (1, 2, 3, 4):
            z = rs[c]
            print(f"    @{c}c: {z['cents']:+.2f}  CI[{z['ci_lo']:+.2f},{z['ci_hi']:+.2f}] gate={z['gate']}",
                  flush=True)
        z2 = rs[2]
        if z2["reasons"]:
            print("    gate@2 reasons:", "; ".join(z2["reasons"][:5]), flush=True)
        return tr

    run("TRAIN", train_panel)
    run("VAL", val_panel)
    if a.full:
        run("FULL(train+val)", panel[panel["game_id"].isin(train_ids | val_ids)])


if __name__ == "__main__":
    main()
