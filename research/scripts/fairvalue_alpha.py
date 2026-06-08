"""Fair-value-gap mean-reversion alpha test — the second honest candidate.

Mechanism: fit a fair in-game win-probability surface fair(margin, elapsed) on
TRAIN games only (empirical settlement rate by margin x time bucket, the
canonical NBA live-WP model). The tradeable signal is the gap

    gap_t = market_wp_t - fair_wp_t

If the market is noisily mispriced, a large positive gap (market too high on the
home team relative to the score/clock) should revert: SHORT the rich side; a
large negative gap should revert: LONG the cheap side. We bet reversion.

Honesty (the traps that killed the prior project):
  * The fair surface is fit ONLY on train game_ids from market_data/splits.json;
    evaluation is on val game_ids. No peeking.
  * Honest entry latency: signal computed at bar t, ENTER at t + entry_lat_min
    (next observable price). Exit at t + exit_min, before settlement.
  * Realized PnL on the raw contract, round-trip cost swept {0,1,2,3}c.
  * One trade per game per direction at the FIRST bar the gap clears threshold
    (no overlapping re-entries milking one game).
  * Blocked by game_id, scored through the full promotion gate.

Usage::

    python3 -m research.scripts.fairvalue_alpha --gap 0.06 --exit-min 6
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
MARGIN_BINS = np.array([-100, -20, -15, -11, -8, -5, -3, -1, 1, 3, 5, 8, 11, 15, 20, 100])
TIME_BINS = np.array([0, 360, 720, 1080, 1440, 1800, 2160, 2520, 2760, 2880])


def _load(game):
    try:
        d = batch.load_or_build(game, kinds=KINDS)
    except Exception:  # noqa: BLE001
        return None
    df = d.minute
    wp, _ = wp_impact._wp_columns(df)
    if wp.notna().sum() < 5:
        return None
    g = d.game
    gid = f"{game['date']}_{g.away_tri}_at_{g.home_tri}"
    return d, df, wp, gid, None


def _home_won(g, df) -> float | None:
    # GameData has no explicit winner field; infer from the final scored margin.
    m = df["margin"].dropna()
    if m.empty:
        return None
    return 1.0 if m.iloc[-1] > 0 else 0.0


def fit_fair_surface(games: list[dict]) -> dict:
    """Empirical P(home win | margin-bin, time-bin) from TRAIN games."""
    num = np.zeros((len(MARGIN_BINS) - 1, len(TIME_BINS) - 1))
    den = np.zeros_like(num)
    for game in games:
        ld = _load(game)
        if ld is None:
            continue
        d, df, wp, gid, _ = ld
        hw = _home_won(d.game, df)
        if hw is None:
            continue
        m = df["margin"].to_numpy(float)
        e = df["elapsed_game_sec"].to_numpy(float)
        ok = np.isfinite(m) & np.isfinite(e) & (e >= 0) & (e <= 2880)
        mi = np.clip(np.digitize(m[ok], MARGIN_BINS) - 1, 0, num.shape[0] - 1)
        ti = np.clip(np.digitize(e[ok], TIME_BINS) - 1, 0, num.shape[1] - 1)
        for a, b in zip(mi, ti):
            den[a, b] += 1
            num[a, b] += hw
    with np.errstate(invalid="ignore", divide="ignore"):
        surf = np.where(den > 30, num / den, np.nan)
    return {"surf": surf}


def _fair(surf, margin, elapsed) -> float:
    if not (np.isfinite(margin) and np.isfinite(elapsed)):
        return np.nan
    a = int(np.clip(np.digitize([margin], MARGIN_BINS)[0] - 1, 0, surf.shape[0] - 1))
    b = int(np.clip(np.digitize([elapsed], TIME_BINS)[0] - 1, 0, surf.shape[1] - 1))
    return float(surf[a, b])


def _interp(idx, y):
    ok = np.isfinite(y)
    x, yy = idx[ok].astype(float), y[ok].astype(float)
    if len(x) < 2:
        return lambda t: np.nan
    return lambda t: float(np.interp(t, x, yy))


def build_trades(games, surf, *, gap_thresh=0.06, entry_lat_min=1.0, exit_min=6.0,
                 min_elapsed=360.0, max_elapsed=2520.0) -> pd.DataFrame:
    rows = []
    for game in games:
        ld = _load(game)
        if ld is None:
            continue
        d, df, wp, gid, _ = ld
        g = d.game
        idx = df.index.to_numpy(float)
        f_wp = _interp(idx, wp.to_numpy(float))
        e = df["elapsed_game_sec"].to_numpy(float)
        m = df["margin"].to_numpy(float)
        w = wp.to_numpy(float)
        ts = idx
        fired_long = fired_short = False
        for k in range(len(ts)):
            if not (np.isfinite(e[k]) and np.isfinite(m[k]) and np.isfinite(w[k])):
                continue
            if e[k] < min_elapsed or e[k] > max_elapsed:
                continue
            fair = _fair(surf, m[k], e[k])
            if not np.isfinite(fair):
                continue
            gap = w[k] - fair                     # >0: market too high on home
            t_entry, t_exit = ts[k] + entry_lat_min * 60, ts[k] + exit_min * 60
            if t_entry < g.first_ts or t_exit > g.last_ts:
                continue
            ewp, xwp = f_wp(t_entry), f_wp(t_exit)
            if not (np.isfinite(ewp) and np.isfinite(xwp)):
                continue
            if gap >= gap_thresh and not fired_short:
                # market too high on home -> revert down -> SHORT home (long away)
                fired_short = True
                pnl = -(xwp - ewp)
                rows.append({"game_id": gid, "date": game["date"], "home_team": g.home_tri,
                             "primary_team": g.away_tri, "dir": "short_home",
                             "pnl_prob": pnl})
            elif gap <= -gap_thresh and not fired_long:
                fired_long = True
                pnl = (xwp - ewp)                 # LONG home
                rows.append({"game_id": gid, "date": game["date"], "home_team": g.home_tri,
                             "primary_team": g.home_tri, "dir": "long_home",
                             "pnl_prob": pnl})
            if fired_long and fired_short:
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
    ap.add_argument("--gap", type=float, default=0.06)
    ap.add_argument("--exit-min", type=float, default=6.0)
    ap.add_argument("--entry-lat-min", type=float, default=1.0)
    ap.add_argument("--limit", type=int, default=None)
    a = ap.parse_args()

    splits = json.loads((_ROOT / "market_data" / "splits.json").read_text())
    train_ids = set(splits["train_game_ids"])
    val_ids = set(splits.get("val_game_ids", []))

    games = schedule.completed_games(a.start, a.end)
    if a.limit:
        games = games[: a.limit]

    def gid_of(gm):
        return f"{gm['date']}_{gm['away']}_at_{gm['home']}"

    train = [g for g in games if gid_of(g) in train_ids]
    val = [g for g in games if gid_of(g) in val_ids]
    print(f"games={len(games)} train={len(train)} val={len(val)} "
          f"gap={a.gap} exit={a.exit_min}min entry_lat={a.entry_lat_min}min", flush=True)

    surf = fit_fair_surface(train)
    print("fair surface fit on train.", flush=True)

    for label, gms in (("TRAIN", train), ("VAL", val)):
        tr = build_trades(gms, surf["surf"], gap_thresh=a.gap,
                          entry_lat_min=a.entry_lat_min, exit_min=a.exit_min)
        z, two, three = evaluate(tr, 0.0), evaluate(tr, 2.0), evaluate(tr, 3.0)
        print(f"\n[{label}] n={z['n']} games={z['ngames']}  "
              f"c/ct@0={z['cents']:.2f}  c/ct@2={two['cents']:.2f} "
              f"(CI [{two['ci_lo']:.2f},{two['ci_hi']:.2f}], gate={two['gate']})  "
              f"c/ct@3={three['cents']:.2f}", flush=True)
        if label == "VAL" and two["reasons"]:
            print("  val gate@2 reasons:", "; ".join(two["reasons"][:4]), flush=True)


if __name__ == "__main__":
    main()
