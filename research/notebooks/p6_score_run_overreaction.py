"""Phenomenon C — Score-run overreaction / underreaction.

For each game we walk the per-minute frame forward and, in a 4-game-minute
rolling window of `pts_home` and `pts_away`, flag the FIRST minute (per game,
per run definition) where the score differential satisfies one of:

  - 6-0  : hot ≥ 6 AND cold == 0
  - 8-2  : hot ≥ 8 AND cold ≤ 2
  - 10-2 : hot ≥ 10 AND cold ≤ 2
  - 12-4 : hot ≥ 12 AND cold ≤ 4

The "hot" team is whichever side is on the scoring run; "cold" is its opponent.
At the detection bar we record both teams' mids and then look at the trailing-
team mid after 2, 3, 4, 5, 6 game-minutes (`elapsed_game_sec + 60*N`). We
evaluate two strategies per run instance:

  - **buy_hot**  : long the running team, hypothesis = market underreacts to runs
  - **buy_cold** : long the team being run on, hypothesis = market overreacts

PnL = exit_price - entry_price. Cost sweep: 0c, 1c, 2.5c, 4c.

Live-availability: only data up to and including the detection minute is used
to pick the trade. Exit price is the ffilled mid at the exit bar — knowable
only at exit time. No outcome look-back. To prevent multiple bites of the same
information, we take only the FIRST qualifying minute per (game, run-spec); a
trade taken on a 6-0 detection blocks future 6-0 detections in that game but
does not block 8-2 detections (those are scored independently).

We require runs to occur strictly in-game (`240 ≤ elapsed_game_sec ≤ 2820` —
need a 4-min lookback at the start and a 1-min forward window at the end).
Garbage-time runs at the very tail of Q4 are noise-prone; they are still
included because excluding them would be data peeking.

Cache reality: Polymarket-only mids. Same `_wp_home` priority as P1/P4 for
forward compatibility.
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

RUN_DEFS = [
    ("6-0", 6, 0),
    ("8-2", 8, 2),
    ("10-2", 10, 2),
    ("12-4", 12, 4),
]
WINDOW_GAME_SEC = 240.0          # 4-game-minute rolling window for run detection
EXIT_MINUTES = [2, 3, 4, 5, 6]
COST_LEVELS = [0.0, 0.01, 0.025, 0.04]
MIN_ELAPSED = 240.0              # need full window
MAX_ELAPSED_DETECT = 2820.0      # need at least 1 min ahead


def _wp_home(row) -> float:
    v = row.get("kalshi_home_winprob")
    if isinstance(v, (int, float)) and np.isfinite(v):
        return float(v)
    v = row.get("pm_home_winprob")
    return float(v) if isinstance(v, (int, float)) and np.isfinite(v) else np.nan


def _detect_runs_in_game(g: pd.DataFrame) -> list[dict]:
    """Return one detection per run-spec, the first minute in this game where
    each run definition fires. Uses an asof-style cumulative-points lookup."""
    g = g.dropna(subset=["elapsed_game_sec"]).sort_values("elapsed_game_sec").copy()
    if g.empty:
        return []
    g["cum_home"] = g["pts_home"].fillna(0).cumsum()
    g["cum_away"] = g["pts_away"].fillna(0).cumsum()
    elapsed = g["elapsed_game_sec"].to_numpy()
    cum_h = g["cum_home"].to_numpy()
    cum_a = g["cum_away"].to_numpy()

    detections: dict[str, dict] = {}
    for i in range(len(g)):
        e = elapsed[i]
        if e < MIN_ELAPSED or e > MAX_ELAPSED_DETECT:
            continue
        # find largest j where elapsed[j] <= e - WINDOW_GAME_SEC (lookback window edge)
        target = e - WINDOW_GAME_SEC
        # searchsorted - 1
        j = int(np.searchsorted(elapsed, target, side="right")) - 1
        if j < 0:
            # take cumulative from "start" — i.e. nothing before the window edge
            prev_h, prev_a = 0.0, 0.0
        else:
            prev_h, prev_a = float(cum_h[j]), float(cum_a[j])
        win_h = float(cum_h[i]) - prev_h
        win_a = float(cum_a[i]) - prev_a
        if win_h <= 0 and win_a <= 0:
            continue
        for spec, hi, lo in RUN_DEFS:
            if spec in detections:
                continue
            # home is running
            if win_h >= hi and win_a <= lo:
                detections[spec] = {
                    "spec": spec, "hot_side": "home", "cold_side": "away",
                    "elapsed_game_sec": float(e),
                    "win_hot_pts": win_h, "win_cold_pts": win_a,
                    "row_index": int(g.index[i]),
                }
            # away is running
            elif win_a >= hi and win_h <= lo:
                detections[spec] = {
                    "spec": spec, "hot_side": "away", "cold_side": "home",
                    "elapsed_game_sec": float(e),
                    "win_hot_pts": win_a, "win_cold_pts": win_h,
                    "row_index": int(g.index[i]),
                }
    return list(detections.values())


def build_trades(minutes: pd.DataFrame, games: pd.DataFrame) -> pd.DataFrame:
    rows = []
    for game_id, gdf in minutes.groupby("game_id", sort=False):
        g = gdf.sort_values(["elapsed_game_sec", "minute_ts"]).copy()
        dets = _detect_runs_in_game(g)
        if not dets:
            continue
        # We need to look up mids/prices on the same sorted frame
        g_sorted = g.dropna(subset=["elapsed_game_sec"]).sort_values("elapsed_game_sec")
        # Add trail prices per side at every bar
        g_sorted["price_home_long"] = g_sorted["pm_home_winprob"]
        g_sorted["price_away_long"] = 1.0 - g_sorted["pm_home_winprob"]
        for d in dets:
            det_row_mask = g_sorted.index == d["row_index"]
            if not det_row_mask.any():
                continue
            det = g_sorted[det_row_mask].iloc[0]
            wp_home = _wp_home(det)
            if not np.isfinite(wp_home):
                continue
            hot = d["hot_side"]
            cold = d["cold_side"]
            entry_hot = (wp_home if hot == "home" else (1.0 - wp_home))
            entry_cold = (wp_home if cold == "home" else (1.0 - wp_home))
            if not (0.0 < entry_hot < 1.0 and 0.0 < entry_cold < 1.0):
                continue

            base = {
                "game_id": game_id, "date": det["date"],
                "spec": d["spec"],
                "hot_side": hot, "cold_side": cold,
                "hot_team": (det["home"] if hot == "home" else det["away"]),
                "cold_team": (det["home"] if cold == "home" else det["away"]),
                "det_elapsed_sec": d["elapsed_game_sec"],
                "win_hot_pts": d["win_hot_pts"], "win_cold_pts": d["win_cold_pts"],
                "entry_price_hot": float(entry_hot),
                "entry_price_cold": float(entry_cold),
            }
            forward = g_sorted[g_sorted["elapsed_game_sec"] >= d["elapsed_game_sec"]]
            for n in EXIT_MINUTES:
                target = d["elapsed_game_sec"] + 60.0 * n
                cand = forward[forward["elapsed_game_sec"] >= target]
                if cand.empty:
                    base[f"exit_{n}min_wp_home"] = np.nan
                else:
                    exit_row = cand.iloc[0]
                    wp_h_exit = _wp_home(exit_row)
                    base[f"exit_{n}min_wp_home"] = float(wp_h_exit) if np.isfinite(wp_h_exit) else np.nan
            # PnL columns per strategy & exit
            for n in EXIT_MINUTES:
                wp_h_exit = base[f"exit_{n}min_wp_home"]
                if not np.isfinite(wp_h_exit):
                    base[f"buy_hot_{n}min_pnl"] = np.nan
                    base[f"buy_cold_{n}min_pnl"] = np.nan
                    continue
                exit_hot = wp_h_exit if hot == "home" else (1.0 - wp_h_exit)
                exit_cold = wp_h_exit if cold == "home" else (1.0 - wp_h_exit)
                base[f"buy_hot_{n}min_pnl"] = float(exit_hot) - base["entry_price_hot"]
                base[f"buy_cold_{n}min_pnl"] = float(exit_cold) - base["entry_price_cold"]
            rows.append(base)
    return pd.DataFrame(rows)


def _gate_reasons(boot: dict, knock: dict, n: int) -> list[str]:
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
    return reasons


def summarize(trades: pd.DataFrame) -> pd.DataFrame:
    rows = []
    if trades.empty:
        return pd.DataFrame()
    for spec, *_ in RUN_DEFS:
        for direction in ("buy_hot", "buy_cold"):
            sub_all = trades[trades["spec"] == spec]
            primary_team_col = "hot_team" if direction == "buy_hot" else "cold_team"
            for n in EXIT_MINUTES:
                pnl_col = f"{direction}_{n}min_pnl"
                valid = sub_all.dropna(subset=[pnl_col]).copy()
                valid["primary_team"] = valid[primary_team_col]
                if valid.empty:
                    continue
                for cost in COST_LEVELS:
                    net = (valid[pnl_col] - cost).to_numpy()
                    boot = block_bootstrap_mean(net, valid["game_id"].to_numpy(),
                                                n_boot=BOOTSTRAP_N, ci=BOOTSTRAP_CI)
                    v_net = valid.assign(pnl_net=net)
                    knock = cluster_knockout(v_net, pnl_col="pnl_net")
                    reasons = _gate_reasons(boot, knock, len(valid))
                    rows.append({
                        "spec": spec, "direction": direction, "exit_min": n,
                        "cost_per_trade": cost,
                        "n_trades": len(valid),
                        "n_games": int(valid["game_id"].nunique()),
                        "mean_entry": float(valid[f"entry_price_{direction.split('_')[1]}"].mean()),
                        "mean_gross": float(valid[pnl_col].mean()),
                        "mean_net": float(boot["mean"]),
                        "ci_lo": float(boot["ci_lo"]),
                        "ci_hi": float(boot["ci_hi"]),
                        "win_rate": float((net > 0).mean()),
                        "knockout_top_games_net": float(knock.get("drop_top_games_mean", np.nan)),
                        "knockout_top_team_net": float(knock.get("drop_top_team_mean", np.nan)),
                        "knockout_first_quarter_net": float(knock.get("drop_first_quarter_mean", np.nan)),
                        "passes_gate": not reasons,
                        "reasons": "; ".join(reasons),
                    })
    return pd.DataFrame(rows)


def plot_summary(summary: pd.DataFrame, path: Path) -> None:
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    specs = [r[0] for r in RUN_DEFS]
    fig, axes = plt.subplots(2, len(specs), figsize=(4 * len(specs), 7), sharey="row")
    for col, spec in enumerate(specs):
        for row, direction in enumerate(("buy_hot", "buy_cold")):
            ax = axes[row, col]
            sub_spec = summary[(summary["spec"] == spec) & (summary["direction"] == direction)]
            for cost in COST_LEVELS:
                s = sub_spec[sub_spec["cost_per_trade"] == cost].sort_values("exit_min")
                if s.empty:
                    continue
                ax.errorbar(s["exit_min"], s["mean_net"],
                            yerr=[(s["mean_net"] - s["ci_lo"]).values,
                                  (s["ci_hi"] - s["mean_net"]).values],
                            marker="o", capsize=3, label=f"cost={cost:.3f}")
            ax.axhline(0, color="k", linewidth=0.5)
            ax.set_title(f"{direction} on {spec}")
            ax.set_xlabel("Exit (game min later)")
            if col == 0:
                ax.set_ylabel("Mean net PnL")
            ax.grid(alpha=0.3)
            if row == 0 and col == 0:
                ax.legend(fontsize=8)
    fig.suptitle("Phenomenon C — Score-run overreaction/underreaction", fontsize=11)
    fig.tight_layout()
    fig.savefig(path, dpi=120)
    plt.close(fig)


def run(verbose: bool = True) -> dict:
    minutes = load_minutes()
    games = load_games()
    trades = build_trades(minutes, games)
    summary = summarize(trades)

    CSV_DIR.mkdir(parents=True, exist_ok=True)
    FIG_DIR.mkdir(parents=True, exist_ok=True)
    trades.to_csv(CSV_DIR / "p6_trades.csv", index=False)
    summary.to_csv(CSV_DIR / "p6_summary.csv", index=False)
    if not summary.empty:
        plot_summary(summary, FIG_DIR / "p6_run_overreaction.png")

    if verbose:
        print(f"Trades: {len(trades):,} run-detections across {trades['game_id'].nunique() if not trades.empty else 0:,} games")
        if not trades.empty:
            counts = trades["spec"].value_counts().sort_index().to_dict()
            print(f"  by spec: {counts}\n")
            best = (summary.sort_values(["spec", "direction", "exit_min", "cost_per_trade"])
                    if not summary.empty else summary)
            cols = ["spec", "direction", "exit_min", "cost_per_trade", "n_trades",
                    "mean_entry", "mean_gross", "mean_net", "ci_lo", "ci_hi",
                    "win_rate", "passes_gate"]
            disp = best[cols].copy()
            for c in ["mean_entry", "mean_gross", "mean_net", "ci_lo", "ci_hi", "win_rate"]:
                disp[c] = disp[c].round(4)
            print(disp.to_string(index=False))
            winners = summary[summary["passes_gate"]]
            print()
            if winners.empty:
                print("No (spec, direction, exit, cost) cell passes the Phase −1 gate.")
            else:
                print(f"PASSING CELLS: {len(winners)}")
                print(winners.to_string(index=False))
    return {"trades": trades, "summary": summary}


if __name__ == "__main__":
    run()
