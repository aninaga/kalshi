"""Phenomenon A — Halftime small-deficit trailing-team buy across exit windows × cost levels.

For every game with a halftime price, we buy the trailing team at the end of Q2
(`elapsed_game_sec ≥ 1440`) at the available mid. Two exit policies are compared:

  1. **hold_to_settlement**: PnL = trailing_won - entry_price.
  2. **exit_end_q3**: PnL = q3_end_mid - entry_price (price of the trailing
     team's contract at the first row with `elapsed_game_sec ≥ 2160`).

We bucket trades by halftime deficit (|margin|) ∈ {1-3, 4-6, 7-10, 11-15, 16+},
and report per-bucket mean net PnL under cost sweeps 0c, 1c, 2.5c, 4c. The Phase
−1 gate is applied at each (bucket, exit, cost) cell.

Live-availability: the entry mid is the price ffilled up to halftime, so there
is no lookahead at entry. The Q3-end exit uses the first quote at
elapsed ≥ 2160 — that quote is by definition only knowable at or after the
exit time, so the trade is live-tradeable. Settlement is the realized outcome.

Cache reality: `kalshi_home_winprob` is empty for the entire 2025-26 cache
(0/1,310 games). All trades execute on `pm_home_winprob`; "kalshi" branches in
this script are kept for forward compatibility but are dead code today.
"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pandas as pd

NOTEBOOK_DIR = Path(__file__).resolve().parent
RESEARCH_DIR = NOTEBOOK_DIR.parent
sys.path.insert(0, str(RESEARCH_DIR))
from eda_kit import (  # noqa: E402
    BOOTSTRAP_N,
    BOOTSTRAP_CI,
    MIN_TRADES,
    block_bootstrap_mean,
    cluster_knockout,
    load_games,
    load_minutes,
)

REPORT_DIR = RESEARCH_DIR / "reports" / "phase_minus1_2026-05-27"
CSV_DIR = REPORT_DIR / "csv"
FIG_DIR = REPORT_DIR / "figures"

HALFTIME_ELAPSED = 1440.0
Q3_END_ELAPSED = 2160.0

DEFICIT_BINS = [0.5, 3.5, 6.5, 10.5, 15.5, 100.0]
DEFICIT_LABELS = ["1-3", "4-6", "7-10", "11-15", "16+"]
COST_LEVELS = [0.0, 0.01, 0.025, 0.04]


def _wp_home(row: pd.Series) -> float:
    v = row.get("kalshi_home_winprob")
    if isinstance(v, (int, float)) and np.isfinite(v):
        return float(v)
    v = row.get("pm_home_winprob")
    return float(v) if isinstance(v, (int, float)) and np.isfinite(v) else np.nan


def _trailing_price(wp_home: float, margin: float) -> tuple[str, float] | None:
    """Return (trailing_side, trailing_team_price). The price is what we *buy* —
    1-wp_home if home leads (we buy away), wp_home if away leads (we buy home).
    """
    if margin > 0:
        return "away", 1.0 - wp_home
    if margin < 0:
        return "home", wp_home
    return None


def build_trades(minutes: pd.DataFrame, games: pd.DataFrame) -> pd.DataFrame:
    home_won_map = games.set_index("game_id")["home_won"].to_dict()
    rows = []
    for game_id, gdf in minutes.groupby("game_id", sort=False):
        g = gdf.dropna(subset=["elapsed_game_sec"]).sort_values("elapsed_game_sec")
        if g.empty:
            continue

        h_mask = g["elapsed_game_sec"] >= HALFTIME_ELAPSED
        if not h_mask.any():
            continue
        h = g[h_mask].iloc[0]

        margin = h["margin"]
        if not np.isfinite(margin) or margin == 0:
            continue

        wp_home = _wp_home(h)
        if not np.isfinite(wp_home):
            continue
        leg = _trailing_price(wp_home, float(margin))
        if leg is None:
            continue
        trailing, entry_price = leg
        if not (0.0 < entry_price < 1.0):
            continue
        primary_team = h["away"] if trailing == "away" else h["home"]

        # Q3-end exit price for the trailing side
        q3_mask = g["elapsed_game_sec"] >= Q3_END_ELAPSED
        if q3_mask.any():
            q = g[q3_mask].iloc[0]
            wp_h_q3 = _wp_home(q)
            if np.isfinite(wp_h_q3):
                q3_exit_price = (1.0 - wp_h_q3) if trailing == "away" else wp_h_q3
            else:
                q3_exit_price = np.nan
        else:
            q3_exit_price = np.nan

        home_won = home_won_map.get(game_id)
        if home_won is None:
            continue
        trailing_won = (trailing == "home") == bool(home_won)
        settle_value = 1.0 if trailing_won else 0.0

        deficit = float(abs(margin))
        rows.append({
            "game_id": game_id,
            "date": h["date"],
            "primary_team": primary_team,
            "trailing_side": trailing,
            "halftime_deficit_abs": deficit,
            "entry_price": entry_price,
            "q3_exit_price": q3_exit_price,
            "settle_value": settle_value,
            "pnl_gross_settle": settle_value - entry_price,
            "pnl_gross_q3": (q3_exit_price - entry_price) if np.isfinite(q3_exit_price) else np.nan,
        })
    df = pd.DataFrame(rows)
    if df.empty:
        return df
    df["deficit_bucket"] = pd.cut(df["halftime_deficit_abs"],
                                  bins=DEFICIT_BINS, labels=DEFICIT_LABELS,
                                  include_lowest=True)
    return df


def summarize(trades: pd.DataFrame, pnl_col: str, exit_label: str) -> pd.DataFrame:
    """Per (bucket, cost) cell: mean net PnL, block-bootstrap CI, knockout stability."""
    out_rows: list[dict] = []
    if trades.empty:
        return pd.DataFrame()

    valid = trades.dropna(subset=[pnl_col]).copy()
    valid = valid[valid["deficit_bucket"].notna()]

    for bucket in DEFICIT_LABELS:
        sub = valid[valid["deficit_bucket"] == bucket]
        n = len(sub)
        for cost in COST_LEVELS:
            net = (sub[pnl_col] - cost).to_numpy() if n else np.array([])
            if n == 0:
                out_rows.append({
                    "exit": exit_label, "deficit_bucket": bucket, "cost_per_trade": cost,
                    "n_trades": 0, "n_games": 0, "mean_entry": np.nan,
                    "mean_gross": np.nan, "mean_net": np.nan,
                    "ci_lo": np.nan, "ci_hi": np.nan, "win_rate": np.nan,
                    "knockout_top_games_net": np.nan,
                    "knockout_top_team_net": np.nan,
                    "knockout_first_quarter_net": np.nan,
                    "passes_gate": False, "reasons": "no trades",
                })
                continue
            boot = block_bootstrap_mean(net, sub["game_id"].to_numpy(),
                                        n_boot=BOOTSTRAP_N, ci=BOOTSTRAP_CI)
            sub_with_net = sub.assign(pnl_net=net)
            knock = cluster_knockout(sub_with_net, pnl_col="pnl_net")

            reasons = []
            if boot["mean"] <= 0:
                reasons.append(f"mean_net {boot['mean']:+.4f}≤0")
            if boot["ci_lo"] <= 0:
                reasons.append(f"ci_lo {boot['ci_lo']:+.4f}≤0")
            if n < MIN_TRADES:
                reasons.append(f"n={n}<{MIN_TRADES}")
            for k in ("drop_top_games_mean", "drop_top_team_mean", "drop_first_quarter_mean"):
                v = knock.get(k)
                if v is not None and np.isfinite(v) and v <= 0:
                    reasons.append(f"{k}={v:+.4f}≤0")
            passes = not reasons

            out_rows.append({
                "exit": exit_label, "deficit_bucket": bucket, "cost_per_trade": cost,
                "n_trades": int(n), "n_games": int(sub["game_id"].nunique()),
                "mean_entry": float(sub["entry_price"].mean()),
                "mean_gross": float(sub[pnl_col].mean()),
                "mean_net": float(boot["mean"]),
                "ci_lo": float(boot["ci_lo"]),
                "ci_hi": float(boot["ci_hi"]),
                "win_rate": float((net > 0).mean()),
                "knockout_top_games_net": float(knock.get("drop_top_games_mean", np.nan)),
                "knockout_top_team_net": float(knock.get("drop_top_team_mean", np.nan)),
                "knockout_first_quarter_net": float(knock.get("drop_first_quarter_mean", np.nan)),
                "passes_gate": passes,
                "reasons": "; ".join(reasons),
            })
    return pd.DataFrame(out_rows)


def plot_summary(summary: pd.DataFrame, path: Path) -> None:
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    fig, axes = plt.subplots(2, 2, figsize=(11, 7), sharey=False)
    axes = axes.flatten()
    for i, cost in enumerate(COST_LEVELS):
        ax = axes[i]
        for exit_label, marker in [("hold_to_settlement", "o"), ("exit_end_q3", "s")]:
            sub = summary[(summary["cost_per_trade"] == cost)
                          & (summary["exit"] == exit_label)
                          & (summary["n_trades"] >= MIN_TRADES)]
            if sub.empty:
                continue
            ax.errorbar(sub["deficit_bucket"], sub["mean_net"],
                        yerr=[(sub["mean_net"] - sub["ci_lo"]).values,
                              (sub["ci_hi"] - sub["mean_net"]).values],
                        marker=marker, capsize=3, label=exit_label)
        ax.axhline(0, color="k", linewidth=0.5)
        ax.set_title(f"cost={cost:.3f} per trade")
        ax.set_xlabel("Halftime deficit bucket")
        ax.set_ylabel("Mean net PnL")
        ax.grid(alpha=0.3)
        ax.legend(fontsize=8)
    fig.suptitle("Phenomenon A — Halftime trailing-team buy, deficit bucket × cost", fontsize=11)
    fig.tight_layout()
    fig.savefig(path, dpi=120)
    plt.close(fig)


def run(verbose: bool = True) -> dict:
    minutes = load_minutes()
    games = load_games()
    trades = build_trades(minutes, games)
    if trades.empty:
        print("No trades built.")
        return {"trades": trades, "summary": pd.DataFrame()}

    settle_summary = summarize(trades, "pnl_gross_settle", "hold_to_settlement")
    q3_summary = summarize(trades, "pnl_gross_q3", "exit_end_q3")
    summary = pd.concat([settle_summary, q3_summary], ignore_index=True)

    CSV_DIR.mkdir(parents=True, exist_ok=True)
    FIG_DIR.mkdir(parents=True, exist_ok=True)
    trades_path = CSV_DIR / "p4_trades.csv"
    summary_path = CSV_DIR / "p4_summary.csv"
    trades.to_csv(trades_path, index=False)
    summary.to_csv(summary_path, index=False)
    plot_summary(summary, FIG_DIR / "p4_deficit_buckets.png")

    if verbose:
        print(f"Trades: {len(trades):,} (across {trades['game_id'].nunique():,} games)")
        n_with_q3 = trades["pnl_gross_q3"].notna().sum()
        print(f"  with Q3-end exit price: {n_with_q3:,}\n")
        for exit_label in ("hold_to_settlement", "exit_end_q3"):
            sub = summary[summary["exit"] == exit_label]
            print(f"=== exit = {exit_label} ===")
            cols = ["deficit_bucket", "cost_per_trade", "n_trades", "mean_entry",
                    "mean_gross", "mean_net", "ci_lo", "ci_hi", "win_rate",
                    "passes_gate"]
            disp = sub[cols].copy()
            for c in ["mean_entry", "mean_gross", "mean_net", "ci_lo", "ci_hi", "win_rate"]:
                disp[c] = disp[c].round(4)
            print(disp.to_string(index=False))
            print()
        winners = summary[summary["passes_gate"]]
        if winners.empty:
            print("No (bucket, exit, cost) cell passes the Phase −1 gate.")
        else:
            print(f"PASSING CELLS: {len(winners)}")
            print(winners.to_string(index=False))
        print(f"\nWrote:\n  {trades_path}\n  {summary_path}\n  {FIG_DIR / 'p4_deficit_buckets.png'}")
    return {"trades": trades, "summary": summary}


if __name__ == "__main__":
    run()
