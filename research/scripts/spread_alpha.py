"""NBA spread / handicap pace-vs-line alpha test — a not-yet-explored market.

Mirror of the certified totals pace-anchoring edge, applied to the SPREAD book.

Tradeable formulation (hold to settlement):
  * From the spread ladder we reconstruct an IMPLIED HOME MARGIN ``imp_t``: the
    signed point at which P(final home margin > x) = 0.5, interpolated across the
    ladder each minute (the spread analog of ``analysis._implied_total``).
      - Kalshi market "Team T wins by over N pts": if T==home, point (N, prob);
        if T==away, P(home_margin > -N) = 1 - prob, point (-N, 1-prob).
      - Polymarket "Spread: <home_nick> (S)" stores P(home covers S): point
        (-S, prob)  [home covers S means home_margin > -S; S is the home
        handicap, usually negative for a home favorite].
  * proj_t = pace-projected final home margin = margin_t * 2880 / elapsed_t.
  * signal = proj_t - imp_t. ``continuation`` (pre-registered): proj > line =>
    line too low on home side => BUY HOME (bet final home margin > strike).
    proj < line => BUY AWAY. ``reversion`` is the opposite (train-fit comparison
    only; full-pop / walk-forward use the pre-registered continuation).
  * Enter the FIRST bar (after min_elapsed) where |signal| >= thresh, one trade
    per game, honest +1-bar latency. Strike locked at imp one bar later.
  * Freshness guard: reject signals where the implied-margin line last changed
    more than ``max_stale_min`` bars ago (the spread ladder is sparse mid-period;
    a stale line vs live margin is a measurement artifact, not a tradeable edge).
  * PnL (prob units) = payoff - 0.50 - cost, payoff = 1{final home margin >
    strike} for a HOME bet (1{final home margin < strike} for AWAY).

Usage::

    python3 -m research.scripts.spread_alpha --thresh 6 --min-elapsed 720
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from pathlib import Path

import numpy as np
import pandas as pd

_ROOT = Path(__file__).resolve().parents[2]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from nba_odds_study import batch, schedule  # noqa: E402
from research.scorer.promotion_gate import evaluate_trial  # noqa: E402

KINDS = {"spread"}
_TICKER_TEAM_RE = re.compile(r"-([A-Z]+)\d+\.?\d*$")


def _cached(game) -> bool:
    key = f"{game['date']}_{game['away']}_at_{game['home']}_spread_all.pkl"
    return os.path.exists(os.path.join(batch.CACHE_DIR, key))


def _kalshi_team(ticker: str):
    """Recover the yes-side team tri from a KXNBASPREAD ticker suffix (...-MIL9)."""
    if not ticker:
        return None
    suf = str(ticker).rsplit("-", 1)[-1]
    m = re.match(r"([A-Z]+)", suf)
    return m.group(1) if m else None


def _implied_home_margin(odds_long: pd.DataFrame, home_tri: str, away_tri: str, index) -> pd.Series:
    """Per-minute signed home margin where P(final home margin > x) = 0.5.

    Builds (signed_home_strike, P(home_margin > strike)) points each minute from
    BOTH platforms' spread ladders, then interpolates for P=0.5. Curve is
    decreasing in strike, so we sort by prob ascending for np.interp.
    """
    sub = odds_long[(odds_long.kind == "spread") & odds_long.strike.notna()].copy()
    if sub.empty:
        return pd.Series(np.nan, index=index, dtype=float)
    sub["minute"] = (sub.ts // 60 * 60).astype(int)

    home_strike = np.full(len(sub), np.nan)
    p_home_over = np.full(len(sub), np.nan)
    plat = sub.platform.to_numpy()
    keys = sub.key.to_numpy()
    strikes = sub.strike.to_numpy(float)
    probs = sub.prob.to_numpy(float)
    for i in range(len(sub)):
        if plat[i] == "kalshi":
            t = _kalshi_team(keys[i])
            if t == home_tri:          # "home wins by over N" -> (N, p)
                home_strike[i] = strikes[i]
                p_home_over[i] = probs[i]
            elif t == away_tri:        # "away wins by over N" -> P(home>-N)=1-p
                home_strike[i] = -strikes[i]
                p_home_over[i] = 1.0 - probs[i]
        else:  # polymarket: "Spread: <home_nick> (S)" stores P(home covers S)
               # home covers S  <=>  home_margin + S > 0  <=>  home_margin > -S
            home_strike[i] = -strikes[i]
            p_home_over[i] = probs[i]
    sub = sub.assign(hs=home_strike, ph=p_home_over).dropna(subset=["hs", "ph"])
    if sub.empty:
        return pd.Series(np.nan, index=index, dtype=float)

    piv = sub.sort_values("ts").groupby(["minute", "hs"]).ph.last().unstack("hs")
    piv = piv.reindex(index).ffill()
    cols = np.array(sorted(piv.columns))
    out = []
    for _, row in piv.iterrows():
        y = row[cols].to_numpy(dtype=float)
        ok = ~np.isnan(y)
        if ok.sum() < 2:
            out.append(np.nan)
            continue
        xs, ys = cols[ok], y[ok]
        order = np.argsort(ys)  # interp needs increasing x (=prob here)
        out.append(float(np.interp(0.5, ys[order], xs[order])))
    return pd.Series(out, index=index)


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
    g = d.game
    imp = _implied_home_margin(d.odds_long, g.home_tri, g.away_tri, list(df.index))
    if imp.notna().sum() < 5:
        return None
    mar = df["margin"]
    if mar.dropna().empty:
        return None
    final_margin = float(mar.dropna().iloc[-1])
    gid = f"{game['date']}_{g.away_tri}_at_{g.home_tri}"
    return df, imp, final_margin, gid, g


def build_trades(games, *, side, thresh, entry_lat_min, min_elapsed, max_elapsed,
                 max_stale_min=2.0):
    rows = []
    for game in games:
        ld = _load(game)
        if ld is None:
            continue
        df, imp, final_margin, gid, g = ld
        e = df["elapsed_game_sec"].to_numpy(float)
        mar = df["margin"].to_numpy(float)
        impv = imp.to_numpy(float)
        idxts = df.index.to_numpy(float)
        proj = np.where(e > 120, mar * 2880.0 / e, np.nan)
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
            t_entry = idxts[k] + entry_lat_min * 60
            strike = float(np.interp(t_entry, idxts[ok], impv[ok]))
            if not np.isfinite(strike):
                continue
            proj_home = sig > 0                       # pace says home margin high
            bet_home = proj_home if side == "continuation" else (not proj_home)
            payoff = 1.0 if ((final_margin > strike) == bet_home) else 0.0
            pnl = payoff - 0.50
            rows.append({"game_id": gid, "date": game["date"], "home_team": g.home_tri,
                         "primary_team": g.home_tri if bet_home else g.away_tri,
                         "bet": "home" if bet_home else "away", "strike": round(strike, 1),
                         "final": final_margin, "pnl_prob": pnl})
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
    ap.add_argument("--max-stale-min", type=float, default=2.0)
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

    best_side, best_train = None, -1e9
    for side in ("continuation", "reversion"):
        tr = build_trades(train, side=side, thresh=a.thresh, entry_lat_min=a.entry_lat_min,
                          min_elapsed=a.min_elapsed, max_elapsed=a.max_elapsed, max_stale_min=a.max_stale_min)
        z = evaluate(tr, 0.0)
        print(f"[TRAIN {side:<13}] n={z['n']} c/ct@0={z['cents']:.2f} c/ct@2={evaluate(tr,2.0)['cents']:.2f}",
              flush=True)
        if np.isfinite(z["cents"]) and z["cents"] > best_train:
            best_side, best_train = side, z["cents"]
    print(f"\n-> train-selected side: {best_side} (pre-registered = continuation)\n", flush=True)

    trv = build_trades(val, side="continuation", thresh=a.thresh, entry_lat_min=a.entry_lat_min,
                       min_elapsed=a.min_elapsed, max_elapsed=a.max_elapsed, max_stale_min=a.max_stale_min)
    z, two, three = evaluate(trv, 0.0), evaluate(trv, 2.0), evaluate(trv, 3.0)
    print(f"[VAL continuation] n={z['n']} games={z['ngames']}  c/ct@0={z['cents']:.2f}  "
          f"c/ct@2={two['cents']:.2f} (CI [{two['ci_lo']:.2f},{two['ci_hi']:.2f}], gate={two['gate']})  "
          f"c/ct@3={three['cents']:.2f}", flush=True)
    if two["reasons"]:
        print("  val gate@2 reasons:", "; ".join(two["reasons"][:4]), flush=True)


if __name__ == "__main__":
    main()
