"""Calibration / favorite-longshot alpha test — hold-to-settlement, level edge.

The most documented inefficiency in betting markets is the favorite-longshot
bias: longshots are overpriced and favorites underpriced, so realized outcome
rates deviate systematically from implied price. This is a price-LEVEL edge
(potentially a fat per-contract cushion) rather than a timing edge, which is
exactly the kind that can survive realistic cost.

Protocol (honest):
  * On TRAIN game_ids only, bin in-game observations by market win-prob PRICE
    and measure realized settlement rate per bin -> the calibration bias
    bias(price_bin) = realized_rate - mean_price. Persistent + => underpriced
    (buy), persistent - => overpriced (sell).
  * On VAL, at the FIRST bar a contract's price enters a bin flagged on TRAIN as
    mispriced beyond `edge_thresh`, take the indicated side and HOLD TO
    SETTLEMENT. PnL = (outcome - entry_price) for a long, (entry_price - outcome)
    for a short. One trade per game per side. Honest entry latency (enter the
    bar after the signal). Round-trip cost swept.
  * Blocked by game_id, scored through the full promotion gate.

Usage::

    python3 -m research.scripts.calib_alpha --edge 0.03 --min-elapsed 1440
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd

_ROOT = Path(__file__).resolve().parents[2]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from nba_odds_study import batch, schedule, wp_impact  # noqa: E402
from research.scorer.promotion_gate import evaluate_trial  # noqa: E402

KINDS = {"winner"}
PRICE_BINS = np.array([0.0, 0.05, 0.10, 0.20, 0.35, 0.5, 0.65, 0.80, 0.90, 0.95, 1.0])


def _load(game):
    try:
        d = batch.load_or_build(game, kinds=KINDS)
    except Exception:  # noqa: BLE001
        return None
    df = d.minute
    wp, _ = wp_impact._wp_columns(df)
    if wp.notna().sum() < 5:
        return None
    m = df["margin"].dropna()
    if m.empty:
        return None
    home_won = 1.0 if m.iloc[-1] > 0 else 0.0
    g = d.game
    gid = f"{game['date']}_{g.away_tri}_at_{g.home_tri}"
    return d, df, wp, gid, home_won


def fit_bias(games, min_elapsed, max_elapsed):
    """realized home-win rate minus mean price, per home-price bin (train)."""
    nb = len(PRICE_BINS) - 1
    num = np.zeros(nb); den = np.zeros(nb); psum = np.zeros(nb)
    for game in games:
        ld = _load(game)
        if ld is None:
            continue
        d, df, wp, gid, hw = ld
        e = df["elapsed_game_sec"].to_numpy(float)
        w = wp.to_numpy(float)
        ok = np.isfinite(e) & np.isfinite(w) & (e >= min_elapsed) & (e <= max_elapsed)
        bi = np.clip(np.digitize(w[ok], PRICE_BINS) - 1, 0, nb - 1)
        for b, price in zip(bi, w[ok]):
            den[b] += 1; num[b] += hw; psum[b] += price
    with np.errstate(invalid="ignore", divide="ignore"):
        realized = np.where(den > 50, num / den, np.nan)
        meanprice = np.where(den > 50, psum / den, np.nan)
    return realized - meanprice, realized, meanprice, den


def build_trades(games, bias, *, edge_thresh, entry_lat_min, min_elapsed, max_elapsed):
    nb = len(PRICE_BINS) - 1
    rows = []
    for game in games:
        ld = _load(game)
        if ld is None:
            continue
        d, df, wp, gid, hw = ld
        g = d.game
        idx = df.index.to_numpy(float)
        e = df["elapsed_game_sec"].to_numpy(float)
        w = wp.to_numpy(float)
        ok = np.isfinite(e) & np.isfinite(w)
        order = np.argsort(idx)
        fired = False
        for k in order:
            if not ok[k] or e[k] < min_elapsed or e[k] > max_elapsed or fired:
                continue
            b = int(np.clip(np.digitize([w[k]], PRICE_BINS)[0] - 1, 0, nb - 1))
            bz = bias[b]
            if not np.isfinite(bz) or abs(bz) < edge_thresh:
                continue
            # entry latency: take the home win-prob one bar later as the price.
            t_entry = idx[k] + entry_lat_min * 60
            entry_w = float(np.interp(t_entry, idx[ok], w[ok]))
            if not np.isfinite(entry_w):
                continue
            # bias>0 => home underpriced => LONG home; bias<0 => SHORT home.
            if bz > 0:
                pnl = hw - entry_w; prim = g.home_tri; direction = "long_home"
            else:
                pnl = entry_w - hw; prim = g.away_tri; direction = "short_home"
            rows.append({"game_id": gid, "date": game["date"], "home_team": g.home_tri,
                         "primary_team": prim, "dir": direction, "entry": round(entry_w, 3),
                         "pnl_prob": pnl})
            fired = True
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
            "ci_lo": float(dec.block_bootstrap_ci_lo * 100), "ci_hi": float(dec.block_bootstrap_ci_hi * 100),
            "ngames": int(dec.n_games_val), "reasons": dec.reasons}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--start", default="2025-10-21")
    ap.add_argument("--end", default="2026-06-08")
    ap.add_argument("--edge", type=float, default=0.03)
    ap.add_argument("--entry-lat-min", type=float, default=1.0)
    ap.add_argument("--min-elapsed", type=float, default=1440.0)
    ap.add_argument("--max-elapsed", type=float, default=2700.0)
    ap.add_argument("--limit", type=int, default=None)
    a = ap.parse_args()

    splits = json.loads((_ROOT / "market_data" / "splits.json").read_text())
    train_ids, val_ids = set(splits["train_game_ids"]), set(splits.get("val_game_ids", []))
    games = schedule.completed_games(a.start, a.end)
    if a.limit:
        games = games[: a.limit]

    def gid_of(gm):
        return f"{gm['date']}_{gm['away']}_at_{gm['home']}"
    train = [g for g in games if gid_of(g) in train_ids]
    val = [g for g in games if gid_of(g) in val_ids]
    print(f"games={len(games)} train={len(train)} val={len(val)} edge={a.edge} "
          f"elapsed=[{a.min_elapsed},{a.max_elapsed}]", flush=True)

    bias, realized, meanprice, den = fit_bias(train, a.min_elapsed, a.max_elapsed)
    print("\nTRAIN calibration by home-price bin (realized - price):")
    for i in range(len(PRICE_BINS) - 1):
        if np.isfinite(bias[i]):
            print(f"  [{PRICE_BINS[i]:.2f},{PRICE_BINS[i+1]:.2f}) n={int(den[i]):>6} "
                  f"realized={realized[i]:.3f} price={meanprice[i]:.3f} bias={bias[i]:+.3f}", flush=True)

    for label, gms in (("TRAIN", train), ("VAL", val)):
        tr = build_trades(gms, bias, edge_thresh=a.edge, entry_lat_min=a.entry_lat_min,
                          min_elapsed=a.min_elapsed, max_elapsed=a.max_elapsed)
        z, two = evaluate(tr, 0.0), evaluate(tr, 2.0)
        print(f"\n[{label}] n={z['n']} games={z['ngames']}  c/ct@0={z['cents']:.2f}  "
              f"c/ct@2={two['cents']:.2f} (CI [{two['ci_lo']:.2f},{two['ci_hi']:.2f}], gate={two['gate']})",
              flush=True)
        if label == "VAL" and two["reasons"]:
            print("  val gate@2 reasons:", "; ".join(two["reasons"][:4]), flush=True)


if __name__ == "__main__":
    main()
