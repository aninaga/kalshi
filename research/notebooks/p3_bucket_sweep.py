"""Phenomenon 3 — Bucket-level home-buy edge across (margin × quarter).

For every in-game minute in every game, treat "buy home at the current mid" as a
one-shot bet that pays off at settlement. The per-minute PnL is
    pnl_gross_t = home_won - home_wp_t
The bucket-average PnL is then the bucket-level calibration error: positive
means the home team is systematically undervalued in that bucket.

This is a diagnostic, not a single strategy. We test (5 margin buckets) ×
(4 quarter buckets) = 20 buckets. To be honest about multiple testing, we apply
Bonferroni correction: a bucket only "passes" if its 99.75% block-bootstrap CI
lower bound is > 0 after costs (= raw 0.25% / 20 = ~0.0125% tail per bucket).

We also test the inverse (buy AWAY when bucket suggests home is overvalued).
"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from eda_kit import (
    BOOTSTRAP_N,
    COST_PER_TRADE,
    MIN_TRADES,
    block_bootstrap_mean,
    load_games,
    load_minutes,
)

MARGIN_BINS = [-100, -15, -5, 5, 15, 100]
MARGIN_LABELS = ["≤-15", "-14..-5", "-4..+4", "+5..+14", "≥+15"]
QUARTER_EDGES = [(0, 720, "Q1"), (720, 1440, "Q2"), (1440, 2160, "Q3"), (2160, 2880, "Q4")]
N_BUCKETS = len(MARGIN_LABELS) * len(QUARTER_EDGES)  # 20
BONFERRONI_CI = 1.0 - (1.0 - 0.95) / N_BUCKETS       # ~0.9975


def _wp_home(row) -> float:
    v = row.get("kalshi_home_winprob")
    if isinstance(v, (int, float)) and np.isfinite(v):
        return float(v)
    v = row.get("pm_home_winprob")
    return float(v) if isinstance(v, (int, float)) and np.isfinite(v) else np.nan


def build_minute_trades(minutes: pd.DataFrame, games: pd.DataFrame) -> pd.DataFrame:
    """One row per (game, in-game minute) with the home-buy gross PnL."""
    home_won = games.set_index("game_id")["home_won"]
    m = minutes.copy()
    m = m.dropna(subset=["elapsed_game_sec", "margin"])
    m = m[(m["elapsed_game_sec"] >= 0) & (m["elapsed_game_sec"] <= 2880)]
    # winprob: prefer kalshi, fallback pm
    wp = m["kalshi_home_winprob"]
    pm = m["pm_home_winprob"]
    wp = wp.where(wp.between(0, 1, inclusive="neither"), pm)
    m["home_wp"] = wp
    m = m[m["home_wp"].between(0, 1, inclusive="neither")]
    m["home_won"] = m["game_id"].map(home_won)
    m = m[m["home_won"].notna()]
    m["pnl_gross_home"] = m["home_won"].astype(float) - m["home_wp"]
    m["margin_bucket"] = pd.cut(m["margin"], bins=MARGIN_BINS, labels=MARGIN_LABELS)
    # Assign quarter from elapsed
    qcol = pd.Series("", index=m.index, dtype=object)
    for lo, hi, name in QUARTER_EDGES:
        qcol = qcol.mask(m["elapsed_game_sec"].between(lo, hi - 1), name)
    m["quarter"] = qcol
    m = m[m["quarter"] != ""]
    return m[["game_id", "date", "home", "away", "margin", "elapsed_game_sec",
              "home_wp", "home_won", "pnl_gross_home", "margin_bucket", "quarter"]]


def sweep(m: pd.DataFrame) -> pd.DataFrame:
    rows = []
    for q in [name for *_, name in QUARTER_EDGES]:
        for mb in MARGIN_LABELS:
            sub = m[(m["quarter"] == q) & (m["margin_bucket"] == mb)]
            if len(sub) < MIN_TRADES:
                rows.append({
                    "quarter": q, "margin_bucket": mb, "n_trades": len(sub),
                    "n_games": sub["game_id"].nunique(),
                    "mean_wp": sub["home_wp"].mean() if not sub.empty else np.nan,
                    "mean_outcome": sub["home_won"].mean() if not sub.empty else np.nan,
                    "edge_gross": (sub["pnl_gross_home"].mean()
                                    if not sub.empty else np.nan),
                    "edge_net_home": (sub["pnl_gross_home"].mean() - COST_PER_TRADE
                                      if not sub.empty else np.nan),
                    "ci_lo_bf": np.nan, "ci_hi_bf": np.nan,
                    "passes_bonferroni_home": False, "passes_bonferroni_away": False,
                    "side": "skip (too few trades)",
                })
                continue
            # Home-buy net pnl
            net = sub["pnl_gross_home"].to_numpy() - COST_PER_TRADE
            boot = block_bootstrap_mean(net, sub["game_id"].to_numpy(),
                                        n_boot=BOOTSTRAP_N, ci=BONFERRONI_CI)
            home_pass = boot["ci_lo"] > 0
            # Away-buy (inverse): if home is overvalued, sell home / buy away. Symmetric.
            net_away = (-sub["pnl_gross_home"].to_numpy()) - COST_PER_TRADE
            boot_away = block_bootstrap_mean(net_away, sub["game_id"].to_numpy(),
                                             n_boot=BOOTSTRAP_N, ci=BONFERRONI_CI)
            away_pass = boot_away["ci_lo"] > 0
            side = "home" if home_pass else ("away" if away_pass else "neither")
            rows.append({
                "quarter": q, "margin_bucket": mb,
                "n_trades": int(len(sub)),
                "n_games": int(sub["game_id"].nunique()),
                "mean_wp": float(sub["home_wp"].mean()),
                "mean_outcome": float(sub["home_won"].mean()),
                "edge_gross": float(sub["pnl_gross_home"].mean()),
                "edge_net_home": float(net.mean()),
                "edge_net_away": float(net_away.mean()),
                "ci_lo_bf_home": float(boot["ci_lo"]),
                "ci_hi_bf_home": float(boot["ci_hi"]),
                "ci_lo_bf_away": float(boot_away["ci_lo"]),
                "ci_hi_bf_away": float(boot_away["ci_hi"]),
                "passes_bonferroni_home": home_pass,
                "passes_bonferroni_away": away_pass,
                "side": side,
            })
    return pd.DataFrame(rows)


def run(verbose: bool = True) -> dict:
    minutes = load_minutes()
    games = load_games()
    m = build_minute_trades(minutes, games)
    print(f"Built {len(m):,} minute-trade rows across {m['game_id'].nunique():,} games")
    print(f"Bonferroni-corrected CI = {BONFERRONI_CI:.4f} ({N_BUCKETS} buckets)\n")
    out = sweep(m)
    if verbose:
        cols = ["quarter", "margin_bucket", "n_trades", "mean_wp", "mean_outcome",
                "edge_gross", "edge_net_home", "ci_lo_bf_home", "ci_hi_bf_home",
                "edge_net_away", "ci_lo_bf_away", "ci_hi_bf_away", "side"]
        df = out[cols].copy()
        for c in ["mean_wp", "mean_outcome", "edge_gross", "edge_net_home",
                  "ci_lo_bf_home", "ci_hi_bf_home", "edge_net_away",
                  "ci_lo_bf_away", "ci_hi_bf_away"]:
            df[c] = df[c].round(4)
        print(df.to_string(index=False))
        print()
        winners = out[(out["passes_bonferroni_home"]) | (out["passes_bonferroni_away"])]
        if winners.empty:
            print("No bucket passes the Bonferroni-corrected gate. No edge surfaces.")
        else:
            print(f"BUCKETS PASSING BONFERRONI GATE: {len(winners)}")
            print(winners[["quarter", "margin_bucket", "n_trades", "side",
                           "edge_net_home", "ci_lo_bf_home",
                           "edge_net_away", "ci_lo_bf_away"]].to_string(index=False))
    return {"sweep": out, "minute_trades": m}


if __name__ == "__main__":
    run()
