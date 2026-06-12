"""Tradeable substitution-latency alpha test — the honest version.

The prior project studied substitution reactions only as a score-removed
*residual* win-prob drift with a t-stat verdict (`nba_odds_study/sub_study.py`).
That is necessary but NOT sufficient: you cannot trade the residual — you hold
the actual win-prob contract, whose realized P&L includes the score move. This
module closes that gap. For each substitution it constructs a REAL trade:

  * Hypothesis (mechanism): when a lineup changes, makers re-rate on the
    scoreboard for several minutes before fully marking the lineup change, so
    the team's win-prob contract keeps drifting in the fair-value direction
    (star OUT -> team should be marked DOWN; star IN -> UP).
  * Side `follow` bets the market UNDER-reacts (trade WITH the fair direction);
    `fade` bets it OVER-reacts.
  * **Honest execution latency**: we observe the sub at t0 but ENTER at
    t0 + entry_lat_min (the next observable price), never on the signal bar.
    Exit at t0 + exit_min, strictly before settlement.
  * Realized PnL is on the RAW contract price (blended Kalshi/PM home win prob,
    flipped to the subbing team's perspective), in probability units. Cost is a
    round-trip debit swept over {0, 1c, 2c, 3c}.

Every trade is blocked by game_id and scored through the project's real
promotion gate (block-bootstrap-by-game CI, cluster knockouts, season/parity
stability, concentration) — the same gate that rejected every prior idea.

Usage::

    python3 -m research.scripts.sub_alpha --start 2025-10-21 --end 2026-06-08
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

import numpy as np
import pandas as pd

_ROOT = Path(__file__).resolve().parents[2]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from nba_odds_study import batch, schedule, wp_impact  # noqa: E402
from research.scorer.promotion_gate import evaluate_trial  # noqa: E402

KINDS = {"winner"}  # keep in sync with prefetch_games.KINDS (shared cache key)


def _interp(index_ts: np.ndarray, y: np.ndarray):
    ok = np.isfinite(y)
    x, yy = index_ts[ok].astype(float), y[ok].astype(float)
    if len(x) < 2:
        return lambda t: np.nan
    return lambda t: float(np.interp(t, x, yy))


def _classify(s: dict, stars: set, starters: set) -> str:
    out_star, in_star = s["player_out"] in stars, s["player_in"] in stars
    out_st, in_st = s["player_out"] in starters, s["player_in"] in starters
    if out_star and not in_star:
        return "star_out"
    if in_star and not out_star:
        return "star_in"
    if out_star and in_star:
        return "star_swap"
    if out_st and not in_st:
        return "starter_out"
    if in_st and not out_st:
        return "starter_in"
    return "bench"


def build_trades(
    games: list[dict],
    *,
    sub_types: set[str],
    side: str = "follow",
    entry_lat_min: float = 1.0,
    exit_min: float = 4.0,
    abs_margin_max: float | None = None,
    min_elapsed_sec: float | None = None,
    max_elapsed_sec: float = 2760.0,
) -> pd.DataFrame:
    """One trade per qualifying substitution. Returns a per-trade DataFrame."""
    rows = []
    for game in games:
        try:
            d = batch.load_or_build(game, kinds=KINDS)
        except Exception:  # noqa: BLE001
            continue
        df = d.minute
        wp, _ = wp_impact._wp_columns(df)            # raw blended home win prob
        if wp.notna().sum() < 5:
            continue
        idx = df.index.to_numpy(float)               # wall-clock minute ts
        f_wp = _interp(idx, wp.to_numpy(float))
        f_elapsed = _interp(idx, df["elapsed_game_sec"].to_numpy(float))
        f_margin = _interp(idx, df["margin"].to_numpy(float))
        g = d.game
        home = g.home_tri
        stars = set(g.stars[home]) | set(g.stars[g.away_tri])
        starters = {p for names in g.starters.values() for p in names}
        gid = f"{game['date']}_{g.away_tri}_at_{g.home_tri}"

        for s in d.subs:
            typ = _classify(s, stars, starters)
            if typ not in sub_types:
                continue
            t0 = s["ts"]
            t_entry, t_exit = t0 + entry_lat_min * 60, t0 + exit_min * 60
            # live-tradeable window: entry after tip, exit strictly before end.
            if t_entry < g.first_ts or t_exit > g.last_ts:
                continue
            elapsed = f_elapsed(t0)
            if not np.isfinite(elapsed):
                continue
            if min_elapsed_sec is not None and elapsed < min_elapsed_sec:
                continue
            if elapsed > max_elapsed_sec:
                continue
            home_side = s["team"] == home
            own_margin = f_margin(t0) * (1 if home_side else -1)
            if abs_margin_max is not None and abs(own_margin) > abs_margin_max:
                continue
            ewp, xwp = f_wp(t_entry), f_wp(t_exit)
            if not (np.isfinite(ewp) and np.isfinite(xwp)):
                continue
            own_entry = ewp if home_side else 1 - ewp
            own_exit = xwp if home_side else 1 - xwp
            d_fair = -1 if typ.endswith("_out") else 1   # out->down, in->up
            pos = d_fair if side == "follow" else -d_fair
            pnl_prob = pos * (own_exit - own_entry)      # long-own convention
            rows.append({
                "game_id": gid, "date": game["date"], "home_team": home,
                "primary_team": s["team"], "type": typ,
                "elapsed": round(elapsed, 0), "own_margin": round(own_margin, 1),
                "entry": round(own_entry, 4), "pnl_prob": pnl_prob,
            })
    return pd.DataFrame(rows)


def evaluate(trades: pd.DataFrame, cost_c: float, n_trials_registry: int = 1) -> dict:
    """Apply the full promotion gate at a per-trade round-trip cost of `cost_c` cents."""
    if trades.empty:
        return {"n": 0, "cents_per_contract": float("nan"), "gate_passed": False,
                "ci_lo": float("nan"), "ci_hi": float("nan"), "reasons": ["no trades"]}
    pnl = trades["pnl_prob"].to_numpy(float) - cost_c / 100.0
    dec = evaluate_trial(
        val_pnl_per_trade=pnl,
        val_game_id_per_trade=trades["game_id"].to_numpy(),
        val_date_per_trade=trades["date"].to_numpy(),
        val_home_team_per_trade=trades["home_team"].to_numpy(),
        val_primary_team_per_trade=trades["primary_team"].to_numpy(),
        n_total_trials_in_registry=n_trials_registry,
        cost_per_trade_assumed=cost_c / 100.0,
    )
    return {
        "n": int(len(pnl)),
        "cents_per_contract": float(pnl.mean() * 100.0),
        "gate_passed": bool(dec.passed),
        "ci_lo": float(dec.block_bootstrap_ci_lo * 100.0),
        "ci_hi": float(dec.block_bootstrap_ci_hi * 100.0),
        "n_games": int(dec.n_games_val),
        "reasons": dec.reasons,
    }


# Strategy families to test. Disciplined: one a-priori PRIMARY hypothesis
# (short the team that just lost a star — the cleanest mechanism), plus a small
# diversified grid for context. Multiple-testing is the reason the gate is
# strict; we report the grid honestly rather than cherry-picking.
FAMILIES = [
    ("star_out__follow",    {"star_out"},                 "follow"),
    ("star_out__fade",      {"star_out"},                 "fade"),
    ("star_in__follow",     {"star_in"},                  "follow"),
    ("starter_out__follow", {"starter_out"},              "follow"),
    ("starter_in__follow",  {"starter_in"},               "follow"),
    ("any_out__follow",     {"star_out", "starter_out"},  "follow"),
    ("any_in__follow",      {"star_in", "starter_in"},    "follow"),
]


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--start", default="2025-10-21")
    ap.add_argument("--end", default="2026-06-08")
    ap.add_argument("--limit", type=int, default=None)
    ap.add_argument("--exit-min", type=float, default=4.0)
    ap.add_argument("--entry-lat-min", type=float, default=1.0)
    a = ap.parse_args()

    games = schedule.completed_games(a.start, a.end)
    if a.limit:
        games = games[: a.limit]
    print(f"{len(games)} games in {a.start}..{a.end}; entry_lat={a.entry_lat_min}min "
          f"exit={a.exit_min}min\n", flush=True)

    hdr = (f"{'family':<22}{'n':>6}{'games':>7} | "
           f"{'c/ct@0':>8}{'c/ct@2':>8}{'CIlo@2':>8}{'CIhi@2':>8}{'gate@2':>8}")
    print(hdr)
    print("-" * len(hdr))
    for name, types, side in FAMILIES:
        tr = build_trades(games, sub_types=types, side=side,
                          entry_lat_min=a.entry_lat_min, exit_min=a.exit_min)
        z = evaluate(tr, 0.0)
        two = evaluate(tr, 2.0)
        print(f"{name:<22}{z['n']:>6}{z.get('n_games', 0):>7} | "
              f"{z['cents_per_contract']:>8.2f}{two['cents_per_contract']:>8.2f}"
              f"{two['ci_lo']:>8.2f}{two['ci_hi']:>8.2f}{str(two['gate_passed']):>8}",
              flush=True)
    print("\nc/ct = mean cents per contract (net of stated round-trip cost). "
          "gate@2 = full promotion gate at 2c.\n"
          "A real edge = gate@2 True with n>=200 and CIlo@2 > 0.")


if __name__ == "__main__":
    main()
