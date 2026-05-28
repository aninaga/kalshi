"""Phenomenon 1 — Halftime trailing-team buy.

At halftime (elapsed_game_sec ≈ 1440, end of Q2), identify games where one team
trails. Buy that team's win-probability contract at the Kalshi mid (Polymarket
mid as fallback). Hold to settlement (realized 0/1 outcome).

Phase −1 gate (from eda_kit.evaluate_phenomenon): mean net PnL > 0, 95%
block-bootstrap (by game_id) CI lower bound > 0, ≥200 trades, all three cluster
knockouts (top 5% games, top team, earliest 25% of season) positive.
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


def _halftime_row(game_df: pd.DataFrame) -> pd.Series | None:
    """Earliest minute row with elapsed_game_sec ≥ 1440 (end of Q2).
    Some games' elapsed clock skips around — we take the first crossing."""
    g = game_df.dropna(subset=["elapsed_game_sec"])
    g = g[g["elapsed_game_sec"] >= 1440.0]
    if g.empty:
        return None
    return g.iloc[0]


def build_trades(minutes: pd.DataFrame, games: pd.DataFrame) -> pd.DataFrame:
    home_won_map = games.set_index("game_id")["home_won"].to_dict()
    rows = []
    for game_id, gdf in minutes.groupby("game_id", sort=False):
        h = _halftime_row(gdf)
        if h is None:
            continue
        margin = h["margin"]
        if not np.isfinite(margin) or margin == 0:
            continue
        # Try Kalshi mid first, then Polymarket
        wp_home = h.get("kalshi_home_winprob")
        source = "kalshi"
        if not np.isfinite(wp_home):
            wp_home = h.get("pm_home_winprob")
            source = "polymarket"
        if not np.isfinite(wp_home):
            continue

        if margin > 0:
            trailing = "away"
            entry_price = float(1.0 - wp_home)
            primary_team = h["away"]
        else:
            trailing = "home"
            entry_price = float(wp_home)
            primary_team = h["home"]

        if not (0.0 < entry_price < 1.0):
            continue

        home_won = home_won_map.get(game_id)
        if home_won is None:
            continue
        trailing_won = (trailing == "home") == bool(home_won)

        rows.append({
            "game_id": game_id, "date": h["date"],
            "primary_team": primary_team, "trailing_side": trailing,
            "halftime_margin": float(margin),
            "halftime_deficit_abs": float(abs(margin)),
            "entry_price": entry_price, "trailing_won": trailing_won,
            "source": source,
            "pnl_gross": float((1.0 if trailing_won else 0.0) - entry_price),
        })
    return pd.DataFrame(rows)


def run(verbose: bool = True):
    minutes = load_minutes()
    games = load_games()
    trades = build_trades(minutes, games)
    report = evaluate_phenomenon(
        name="P1: Halftime trailing-team buy",
        description=("At halftime (end of Q2), buy the trailing team's contract at the Kalshi "
                     "mid (Polymarket fallback). Hold to settlement."),
        trades=trades,
    )
    if verbose:
        print(report.to_markdown())
        print()
        if not trades.empty:
            t = trades.copy()
            t["bucket"] = pd.cut(t["halftime_deficit_abs"],
                                 bins=[0, 3, 6, 10, 15, 100],
                                 labels=["1-3", "4-6", "7-10", "11-15", "16+"])
            summary = (
                t.groupby("bucket", observed=False)
                 .agg(n=("pnl_gross", "size"),
                      mean_entry=("entry_price", "mean"),
                      mean_gross=("pnl_gross", "mean"),
                      win_rate=("trailing_won", "mean"))
            )
            summary["mean_net"] = summary["mean_gross"] - COST_PER_TRADE
            print("By halftime deficit bucket:")
            print(summary.to_string())
            print()
            print(f"Sources: kalshi={ (trades['source']=='kalshi').sum() }, "
                  f"polymarket={ (trades['source']=='polymarket').sum() }")
    return {"trades": trades, "report": report}


if __name__ == "__main__":
    run()
