"""Phenomenon 2 — Late-game cheap-underdog buy.

The plan hypothesis: cheap out-of-the-money contracts (price < 0.10) on the
underdog late in the game may have realized recovery rates exceeding implied.
Markets may systematically underprice tail comebacks.

Trade: in Q4+ (elapsed_game_sec ≥ 2160), at the FIRST minute the underdog's
price drops below a threshold, buy the underdog contract at mid. Hold to
settlement. One trade per game per threshold (no overlapping bets).

We sweep thresholds {0.05, 0.10, 0.15, 0.20} and report each. The plan gate is
applied per threshold.
"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from eda_kit import (
    COST_PER_TRADE,
    evaluate_phenomenon,
    load_games,
    load_minutes,
)


def _wp_home(row: pd.Series) -> float:
    v = row.get("kalshi_home_winprob")
    if isinstance(v, (int, float)) and np.isfinite(v):
        return float(v)
    v = row.get("pm_home_winprob")
    return float(v) if isinstance(v, (int, float)) and np.isfinite(v) else np.nan


def build_trades(minutes: pd.DataFrame, games: pd.DataFrame,
                 threshold: float, min_elapsed: float = 2160.0) -> pd.DataFrame:
    """One trade per game: first Q4 minute where underdog price < threshold."""
    home_won_map = games.set_index("game_id")["home_won"].to_dict()
    rows = []
    for game_id, gdf in minutes.groupby("game_id", sort=False):
        g = gdf.dropna(subset=["elapsed_game_sec"])
        g = g[g["elapsed_game_sec"] >= min_elapsed].sort_values("elapsed_game_sec")
        if g.empty:
            continue
        # Walk forward through Q4 minutes; first time an underdog is priced under threshold, take it
        trade = None
        for _, r in g.iterrows():
            wp_home = _wp_home(r)
            if not np.isfinite(wp_home):
                continue
            # Determine underdog & its price
            if wp_home < 0.5:
                und_side = "home"
                und_price = wp_home
                und_team = r["home"]
            elif wp_home > 0.5:
                und_side = "away"
                und_price = 1.0 - wp_home
                und_team = r["away"]
            else:
                continue
            if und_price < threshold and und_price > 0:
                trade = {"side": und_side, "price": und_price, "team": und_team,
                         "elapsed": float(r["elapsed_game_sec"]), "date": r["date"]}
                break
        if trade is None:
            continue
        home_won = home_won_map.get(game_id)
        if home_won is None:
            continue
        underdog_won = (trade["side"] == "home") == bool(home_won)
        rows.append({
            "game_id": game_id, "date": trade["date"],
            "primary_team": trade["team"], "underdog_side": trade["side"],
            "entry_price": trade["price"], "entry_elapsed_sec": trade["elapsed"],
            "threshold": threshold, "underdog_won": underdog_won,
            "pnl_gross": float((1.0 if underdog_won else 0.0) - trade["price"]),
        })
    return pd.DataFrame(rows)


def run(verbose: bool = True):
    minutes = load_minutes()
    games = load_games()
    out = {}
    for thr in (0.05, 0.10, 0.15, 0.20):
        trades = build_trades(minutes, games, threshold=thr)
        report = evaluate_phenomenon(
            name=f"P2: Cheap-underdog buy (price < {thr:.2f}) at Q4",
            description=(f"In Q4+ (elapsed ≥ 36:00), first minute underdog priced below {thr:.2f}; "
                         f"buy at mid; hold to settlement."),
            trades=trades,
        )
        out[thr] = {"trades": trades, "report": report}
        if verbose:
            print(report.to_markdown())
            print()
    return out


if __name__ == "__main__":
    out = run()
    print("=" * 60)
    print("Summary across thresholds")
    print("=" * 60)
    rows = []
    for thr, r in out.items():
        rep = r["report"]
        rows.append({
            "threshold": thr,
            "n_trades": rep.n_trades,
            "mean_entry": r["trades"]["entry_price"].mean() if not r["trades"].empty else np.nan,
            "win_rate": rep.win_rate,
            "mean_net": rep.mean_pnl_net,
            "ci_lo": rep.ci_lo,
            "ci_hi": rep.ci_hi,
            "passes_gate": rep.passes_gate,
        })
    print(pd.DataFrame(rows).to_string(index=False))
