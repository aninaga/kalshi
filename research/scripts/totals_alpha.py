"""Totals (over/under) pace-vs-line alpha test — a different, less-watched market.

Moneyline is efficient (see ALPHA_FINDINGS.md). Totals get less attention and
have a clean mechanism: observed scoring pace projects the final total, and the
market line may lag it. We test whether pace predicts the final total *beyond*
what the line already reflects.

Tradeable formulation (hold to settlement):
  * imp_t = market implied total (strike where P(over)=0.5), so an at-the-money
    over/under contract prices ≈ 0.50.
  * proj_t = observed pace projection = total_t * 2880 / elapsed_t.
  * signal = proj_t - imp_t. `continuation`: proj>line => the line is too low =>
    BUY OVER (bet final > imp_t). `reversion` bets the opposite.
  * Enter the FIRST bar (after min_elapsed) where |signal| >= thresh, one trade
    per game, honest +1-bar latency. Strike locked at imp_entry.
  * PnL (prob units) = payoff - 0.50 - cost, payoff = 1{final_total > strike}
    for an over (1{final_total < strike} for an under).
  * Direction (continuation vs reversion) is FIT ON TRAIN, evaluated on VAL.
    Blocked by game_id, scored through the full promotion gate.

Usage::

    python3 -m research.scripts.totals_alpha --thresh 6 --min-elapsed 720
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

import os  # noqa: E402

from nba_odds_study import analysis, batch, schedule  # noqa: E402
from research.scorer.promotion_gate import evaluate_trial  # noqa: E402

KINDS = {"total"}


def _cached(game) -> bool:
    """True iff this game's totals pkl is already on disk (no live build)."""
    key = f"{game['date']}_{game['away']}_at_{game['home']}_total_all.pkl"
    return os.path.exists(os.path.join(batch.CACHE_DIR, key))


def _load(game):
    if not _cached(game):
        return None  # cache-only: never trigger a slow live build here
    try:
        d = batch.load_or_build(game, kinds=KINDS)
    except Exception:  # noqa: BLE001
        return None
    df = d.minute
    if not len(d.odds_long):
        return None
    idx = list(df.index)
    imp = pd.concat([analysis._implied_total(d.odds_long, "kalshi", idx),
                     analysis._implied_total(d.odds_long, "polymarket", idx)], axis=1).mean(axis=1)
    if imp.notna().sum() < 5:
        return None
    tot = df["total"]
    if tot.dropna().empty:
        return None
    final_total = float(tot.dropna().iloc[-1])
    g = d.game
    gid = f"{game['date']}_{g.away_tri}_at_{g.home_tri}"
    return df, imp, final_total, gid, g


def build_trades(games, *, side, thresh, entry_lat_min, min_elapsed, max_elapsed,
                 max_stale_min=2.0):
    rows = []
    for game in games:
        ld = _load(game)
        if ld is None:
            continue
        df, imp, final_total, gid, g = ld
        e = df["elapsed_game_sec"].to_numpy(float)
        tot = df["total"].to_numpy(float)
        impv = imp.to_numpy(float)
        idxts = df.index.to_numpy(float)
        proj = np.where(e > 120, tot * 2880.0 / e, np.nan)
        # Line freshness: minutes since the implied total last actually changed.
        # The historical total ladder is sparse mid-period (often flat for the
        # whole game), so a stale line vs live points is a measurement artifact,
        # NOT a tradeable edge — you would fill at the fresh line. Guard it.
        chg = np.r_[True, np.abs(np.diff(impv)) > 1e-9]
        last_chg = np.maximum.accumulate(np.where(chg, np.arange(len(impv)), -1))
        stale_min = np.arange(len(impv)) - last_chg
        ok = np.isfinite(e) & np.isfinite(impv) & np.isfinite(proj)
        order = np.argsort(idxts)
        for k in order:
            if not ok[k] or e[k] < min_elapsed or e[k] > max_elapsed:
                continue
            if stale_min[k] > max_stale_min:
                continue
            sig = proj[k] - impv[k]
            if abs(sig) < thresh:
                continue
            # honest latency: lock the strike at the implied total one bar later.
            t_entry = idxts[k] + entry_lat_min * 60
            strike = float(np.interp(t_entry, idxts[ok], impv[ok]))
            if not np.isfinite(strike):
                continue
            pace_over = sig > 0                      # pace says final will be high
            bet_over = pace_over if side == "continuation" else (not pace_over)
            payoff = 1.0 if ((final_total > strike) == bet_over) else 0.0
            pnl = payoff - 0.50
            rows.append({"game_id": gid, "date": game["date"], "home_team": g.home_tri,
                         "primary_team": g.home_tri if bet_over else g.away_tri,
                         "bet": "over" if bet_over else "under", "strike": round(strike, 1),
                         "final": final_total, "pnl_prob": pnl})
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
            "ci_lo": float(dec.block_bootstrap_ci_lo * 100), "ci_hi": float(dec.block_bootstrap_ci_hi * 100),
            "ngames": int(dec.n_games_val), "reasons": dec.reasons}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--start", default="2025-10-21")
    ap.add_argument("--end", default="2026-06-08")
    ap.add_argument("--thresh", type=float, default=6.0)
    ap.add_argument("--entry-lat-min", type=float, default=1.0)
    ap.add_argument("--min-elapsed", type=float, default=720.0)
    ap.add_argument("--max-elapsed", type=float, default=2520.0)
    ap.add_argument("--max-stale-min", type=float, default=2.0,
                    help="reject signals where the line quote is older than this (freshness guard)")
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
    print(f"games={len(games)} train={len(train)} val={len(val)} thresh={a.thresh} "
          f"elapsed=[{a.min_elapsed},{a.max_elapsed}]\n", flush=True)

    # Fit direction on train: pick the side with higher train mean.
    best_side, best_train = None, -1e9
    for side in ("continuation", "reversion"):
        tr = build_trades(train, side=side, thresh=a.thresh, entry_lat_min=a.entry_lat_min,
                          min_elapsed=a.min_elapsed, max_elapsed=a.max_elapsed, max_stale_min=a.max_stale_min)
        z = evaluate(tr, 0.0)
        print(f"[TRAIN {side:<13}] n={z['n']} c/ct@0={z['cents']:.2f} c/ct@2={evaluate(tr,2.0)['cents']:.2f}",
              flush=True)
        if np.isfinite(z["cents"]) and z["cents"] > best_train:
            best_side, best_train = side, z["cents"]
    print(f"\n-> train-selected side: {best_side}\n", flush=True)

    trv = build_trades(val, side=best_side, thresh=a.thresh, entry_lat_min=a.entry_lat_min,
                       min_elapsed=a.min_elapsed, max_elapsed=a.max_elapsed, max_stale_min=a.max_stale_min)
    z, two, three = evaluate(trv, 0.0), evaluate(trv, 2.0), evaluate(trv, 3.0)
    print(f"[VAL {best_side}] n={z['n']} games={z['ngames']}  c/ct@0={z['cents']:.2f}  "
          f"c/ct@2={two['cents']:.2f} (CI [{two['ci_lo']:.2f},{two['ci_hi']:.2f}], gate={two['gate']})  "
          f"c/ct@3={three['cents']:.2f}", flush=True)
    if two["reasons"]:
        print("  val gate@2 reasons:", "; ".join(two["reasons"][:4]), flush=True)


if __name__ == "__main__":
    main()
