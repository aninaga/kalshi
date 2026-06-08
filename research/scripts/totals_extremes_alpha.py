"""NBA totals favorite-longshot / LEVEL bias at extreme over/under prices.

Pre-registered mechanism (see research/TOTALS_EXTREMES_FINDINGS.md):
  Each (strike, prob) row in a totals pkl is a tradeable "over X" contract priced
  at prob = P(final_total > strike). Favorite-longshot bias predicts that at
  EXTREME prices, longshots (price < p_lo) are OVERPRICED (settle YES less often
  than implied -> SELL the over) and near-locks (price > p_hi) are UNDERPRICED
  (settle YES more often than implied -> BUY the over). In both cases buy the
  cheap side of the mispricing and HOLD TO SETTLEMENT.

This is a price-LEVEL edge, distinct from the certified pace-anchoring edge
(which trades the at-the-money implied total). Direction is fixed by the
mechanism (longshots overpriced, locks underpriced), so the full population is a
valid out-of-sample test; we still report train/val and a monthly walk-forward
because the moneyline favorite-longshot inverted OOS.

Honest protocol:
  * Per game, per platform-blended strike, find the FIRST bar (after min_elapsed,
    line fresh within max_stale_min) where a contract's price is extreme.
  * Enter at the price one bar LATER (i+1 latency). One trade per game (the first
    extreme contract encountered across strikes, by timestamp).
  * Outcome = 1{final_total > strike}. Hold to settlement. PnL in prob units.
  * Round-trip cost swept {0,1,2,3,4}c. Scored by promotion_gate.evaluate_trial.

Usage::
    python3 -m research.scripts.totals_extremes_alpha --p-lo 0.10 --p-hi 0.90
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

import numpy as np
import pandas as pd

_ROOT = Path(__file__).resolve().parents[2]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from nba_odds_study import batch, schedule  # noqa: E402
from research.scorer.promotion_gate import evaluate_trial  # noqa: E402

KINDS = {"total"}


def _cached(game) -> bool:
    key = f"{game['date']}_{game['away']}_at_{game['home']}_total_all.pkl"
    return os.path.exists(os.path.join(batch.CACHE_DIR, key))


def _blended_strike_prob(odds_long: pd.DataFrame, index):
    """Per-minute P(over) for every strike, blended across platforms.

    Returns dict {strike -> pd.Series(prob, index=index)} (ffilled within game),
    and a parallel dict {strike -> pd.Series(staleness_minutes)} = bars since the
    quote last actually changed (freshness guard input).
    """
    sub = odds_long[(odds_long.kind == "total") & odds_long.strike.notna()].copy()
    if sub.empty:
        return {}, {}
    sub["minute"] = sub.ts // 60 * 60
    # Blend platforms: mean P(over) per (minute, strike).
    piv = (sub.sort_values("ts").groupby(["minute", "strike"]).prob.last()
           .unstack("strike").reindex(index).ffill())
    probs, stale = {}, {}
    n = len(index)
    for strike in piv.columns:
        s = piv[strike].to_numpy(float)
        # staleness in bars since last actual change (NaN counts as no-change-yet)
        chg = np.r_[True, np.abs(np.diff(np.nan_to_num(s, nan=-999.0))) > 1e-9]
        last_chg = np.maximum.accumulate(np.where(chg, np.arange(n), -1))
        stale[strike] = np.arange(n) - last_chg
        probs[strike] = s
    return probs, stale


def _load(game):
    if not _cached(game):
        return None
    try:
        d = batch.load_or_build(game, kinds=KINDS)
    except Exception:  # noqa: BLE001
        return None
    df = d.minute
    if not len(d.odds_long):
        return None
    idx = list(df.index)
    probs, stale = _blended_strike_prob(d.odds_long, idx)
    if not probs:
        return None
    tot = df["total"].dropna()
    if tot.empty:
        return None
    final_total = float(tot.iloc[-1])
    g = d.game
    gid = f"{game['date']}_{g.away_tri}_at_{g.home_tri}"
    return df, probs, stale, final_total, gid, g


def build_trades(games, *, p_lo, p_hi, side_lock, side_long,
                 entry_lat_min, min_elapsed, max_elapsed, max_stale_min):
    """One trade per game: first extreme contract (by ts) across all strikes.

    side_long: action on a longshot (price<p_lo). 'sell' = bet under (mechanism).
    side_lock: action on a near-lock (price>p_hi). 'buy' = bet over (mechanism).
    """
    rows = []
    for game in games:
        ld = _load(game)
        if ld is None:
            continue
        df, probs, stale, final_total, gid, g = ld
        e = df["elapsed_game_sec"].to_numpy(float)
        idxts = df.index.to_numpy(float)
        n = len(idxts)
        ok_time = np.isfinite(e) & (e >= min_elapsed) & (e <= max_elapsed)

        best = None  # (bar_k, strike, kind) for earliest qualifying signal
        for k in range(n):
            if not ok_time[k]:
                continue
            for strike, parr in probs.items():
                p = parr[k]
                if not np.isfinite(p):
                    continue
                if stale[strike][k] > max_stale_min:
                    continue
                is_long = p < p_lo
                is_lock = p > p_hi
                if not (is_long or is_lock):
                    continue
                best = (k, strike, "long" if is_long else "lock")
                break
            if best is not None:
                break
        if best is None:
            continue

        k, strike, kind = best
        parr = probs[strike]
        # honest latency: take the contract price one bar later as the fill.
        t_entry = idxts[k] + entry_lat_min * 60
        finite = np.isfinite(parr)
        entry_p = float(np.interp(t_entry, idxts[finite], parr[finite]))
        if not np.isfinite(entry_p):
            continue
        outcome = 1.0 if final_total > strike else 0.0
        if kind == "long":
            action = side_long
        else:
            action = side_lock
        if action == "buy":   # long the over
            pnl = outcome - entry_p
            bet = "over"
        else:                 # sell the over == long the under
            pnl = entry_p - outcome
            bet = "under"
        # primary_team: arbitrary but consistent attribution (home if over, away if under)
        prim = g.home_tri if bet == "over" else g.away_tri
        rows.append({"game_id": gid, "date": game["date"], "home_team": g.home_tri,
                     "primary_team": prim, "kind": kind, "bet": bet,
                     "strike": strike, "entry": round(entry_p, 3),
                     "final": final_total, "outcome": outcome, "pnl_prob": pnl})
    return pd.DataFrame(rows)


def evaluate(trades, cost_c, n_trials=1):
    if trades.empty or len(trades) < 2:
        return {"n": len(trades), "cents": float("nan"), "gate": False,
                "ci_lo": float("nan"), "ci_hi": float("nan"), "ngames": 0,
                "reasons": ["no trades"]}
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


def calibration_table(trades):
    """Realized settlement rate vs implied price, per extreme bin (diagnostic).

    Reports the UNCONDITIONAL calibration of extreme over contracts at the
    chosen entry snapshot: realized 1{final>strike} vs entry price.
    """
    if trades.empty:
        return
    # Reconstruct the raw "over" perspective regardless of bet direction.
    # entry price and outcome are stored from the over's perspective already
    # (outcome = 1{final>strike}); for 'under' bets entry was the over price.
    print("\nCalibration of extreme OVER contracts (entry price vs realized YES rate):")
    for kind in ("long", "lock"):
        sub = trades[trades["kind"] == kind]
        if sub.empty:
            continue
        mean_price = sub["entry"].mean()
        realized = sub["outcome"].mean()
        print(f"  {kind:<4} n={len(sub):>4}  mean_entry_price={mean_price:.4f}  "
              f"realized_YES={realized:.4f}  bias(realized-price)={realized-mean_price:+.4f}",
              flush=True)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--start", default="2025-10-21")
    ap.add_argument("--end", default="2026-06-08")
    ap.add_argument("--p-lo", type=float, default=0.10, help="longshot price ceiling")
    ap.add_argument("--p-hi", type=float, default=0.90, help="near-lock price floor")
    ap.add_argument("--entry-lat-min", type=float, default=1.0)
    ap.add_argument("--min-elapsed", type=float, default=720.0)
    ap.add_argument("--max-elapsed", type=float, default=2520.0)
    ap.add_argument("--max-stale-min", type=float, default=2.0)
    ap.add_argument("--mode", default="mechanism",
                    choices=["mechanism", "anti", "all-full"],
                    help="mechanism=sell longshots+buy locks; anti=inverse; "
                         "all-full=run full population too")
    ap.add_argument("--limit", type=int, default=None)
    a = ap.parse_args()

    # Pre-registered direction: longshot -> SELL over, lock -> BUY over.
    if a.mode == "mechanism":
        side_long, side_lock = "sell", "buy"
    else:  # anti (adversarial inverse, for diagnostics only)
        side_long, side_lock = "buy", "sell"

    splits = json.loads((_ROOT / "market_data" / "splits.json").read_text())
    train_ids = set(splits["train_game_ids"])
    val_ids = set(splits.get("val_game_ids", []))
    test_ids = set(splits.get("test_game_ids", []))
    games = schedule.completed_games(a.start, a.end)
    if a.limit:
        games = games[: a.limit]

    def gid_of(gm):
        return f"{gm['date']}_{gm['away']}_at_{gm['home']}"

    # NEVER touch test split.
    games = [g for g in games if gid_of(g) not in test_ids]
    train = [g for g in games if gid_of(g) in train_ids]
    val = [g for g in games if gid_of(g) in val_ids]
    full = [g for g in games if gid_of(g) in (train_ids | val_ids)]
    print(f"games={len(games)} train={len(train)} val={len(val)} full(train+val)={len(full)}",
          flush=True)
    print(f"p_lo={a.p_lo} p_hi={a.p_hi} elapsed=[{a.min_elapsed},{a.max_elapsed}] "
          f"max_stale_min={a.max_stale_min} mode={a.mode}\n", flush=True)

    def run(label, gms):
        tr = build_trades(gms, p_lo=a.p_lo, p_hi=a.p_hi, side_lock=side_lock,
                          side_long=side_long, entry_lat_min=a.entry_lat_min,
                          min_elapsed=a.min_elapsed, max_elapsed=a.max_elapsed,
                          max_stale_min=a.max_stale_min)
        if tr.empty:
            print(f"[{label}] no trades", flush=True)
            return tr
        calibration_table(tr)
        cells = {c: evaluate(tr, c) for c in (0, 1, 2, 3, 4)}
        z, two = cells[0], cells[2]
        print(f"[{label}] n={z['n']} games={z['ngames']}  "
              f"@0={cells[0]['cents']:.2f} @1={cells[1]['cents']:.2f} "
              f"@2={cells[2]['cents']:.2f} @3={cells[3]['cents']:.2f} @4={cells[4]['cents']:.2f}  "
              f"(2c CI [{two['ci_lo']:.2f},{two['ci_hi']:.2f}] gate={two['gate']})", flush=True)
        if two["reasons"]:
            print("   gate@2 reasons:", "; ".join(two["reasons"][:5]), flush=True)
        return tr

    run("TRAIN", train)
    run("VAL", val)
    full_tr = run("FULL(train+val, pre-reg dir)", full)

    # Monthly walk-forward on the full population (each month independent OOS).
    if full_tr is not None and not full_tr.empty:
        print("\nMonthly walk-forward (full population, 2c net):", flush=True)
        full_tr = full_tr.copy()
        full_tr["month"] = pd.to_datetime(full_tr["date"]).dt.to_period("M").astype(str)
        for mon, sub in full_tr.groupby("month"):
            ev = evaluate(sub, 2.0)
            print(f"  {mon}: n={ev['n']:>4} @2={ev['cents']:+.2f} "
                  f"CI[{ev['ci_lo']:+.2f},{ev['ci_hi']:+.2f}]", flush=True)


if __name__ == "__main__":
    main()
