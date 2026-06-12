"""Adversarial diagnostics for the CLV trade frames dumped by clv_alpha.

Reads a parquet of trades (columns: p_open, p_obs, p_entry, drift, home_won,
side, pnl_prob, game_id, primary_team, date) and runs the four checks the
mechanism pre-registered:
  1. estimator-bias / calibration: is an UNCONDITIONAL "buy home at p_entry, hold"
     already net ~0 (calibrated)? The edge must be the conditional drift effect.
  2. concentration: top game / top team share of |pnl|.
  3. drift-magnitude monotonicity: does PnL scale with |drift| (mechanism) or is it
     a flat artifact?
  4. cost sweep at the trade level (sanity vs gate).

Usage::  python3 -m research.scripts.clv_diagnostics research/reports/alpha/clv_val.parquet
"""
from __future__ import annotations

import sys

import numpy as np
import pandas as pd


def main():
    path = sys.argv[1]
    df = pd.read_parquet(path)
    n = len(df)
    print(f"loaded {n} trades from {path}\n")

    pnl = df["pnl_prob"].to_numpy(float)
    print(f"gross mean PnL: {pnl.mean()*100:+.2f} c/ct   std={pnl.std()*100:.1f}c")
    print(f"home_win_rate among traded games: {df['home_won'].mean():.3f}")
    print(f"long_home fraction: {(df['side']=='long_home').mean():.3f}\n")

    # --- 1. unconditional baseline (calibration) ---
    # "buy home at p_entry, hold" PnL = home_won - p_entry, for EVERY traded game,
    # regardless of the signal direction. If ~0, the market is calibrated and any
    # edge is conditional (good). If large, the level itself is mispriced.
    base = df["home_won"].to_numpy(float) - df["p_entry"].to_numpy(float)
    print("[calibration] unconditional buy-home-at-entry, hold:")
    print(f"   mean = {base.mean()*100:+.2f} c/ct  (|p_entry| mean={df['p_entry'].mean():.3f})")
    # also: realized vs price calibration across entry-price buckets
    edges = np.array([0, .2, .35, .5, .65, .8, 1.0])
    pe = df["p_entry"].to_numpy(float)
    print("   calibration by entry-price bucket (realized home win - price):")
    for i in range(len(edges) - 1):
        m = (pe >= edges[i]) & (pe < edges[i + 1])
        if m.sum() >= 10:
            print(f"     [{edges[i]:.2f},{edges[i+1]:.2f}) n={m.sum():>4} "
                  f"realized={df['home_won'].to_numpy()[m].mean():.3f} "
                  f"price={pe[m].mean():.3f} diff={(df['home_won'].to_numpy()[m].mean()-pe[m].mean())*100:+.1f}pp")

    # --- 2. concentration ---
    def share(group):
        s = pd.Series(np.abs(pnl)).groupby(df[group].to_numpy()).sum()
        return s.max() / s.sum(), s.idxmax()
    gsh, gtop = share("game_id")
    tsh, ttop = share("primary_team")
    print(f"\n[concentration] top game share={gsh:.3f} ({gtop})  top team share={tsh:.3f} ({ttop})")

    # --- 3. drift-magnitude monotonicity ---
    ad = df["drift"].abs().to_numpy(float)
    print("\n[drift monotonicity] mean PnL by |drift| tercile:")
    qs = np.quantile(ad, [0, 1/3, 2/3, 1.0])
    for i in range(3):
        m = (ad >= qs[i]) & (ad <= qs[i + 1] if i == 2 else ad < qs[i + 1])
        if m.sum() >= 5:
            print(f"   |drift| [{qs[i]:.3f},{qs[i+1]:.3f}] n={m.sum():>4} pnl={pnl[m].mean()*100:+.2f}c")

    # --- 4. cost sweep ---
    print("\n[cost sweep] net mean c/ct:")
    for c in (0, 1, 2, 3, 4):
        print(f"   @{c}c: {(pnl - c/100).mean()*100:+.2f}")


if __name__ == "__main__":
    main()
