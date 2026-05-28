"""Phenomenon C — lag robustness check.

The headline P6 result (long the team on a scoring run, exit 4 minutes later)
clears the Phase −1 gate at ≤2.5¢ cost. Before reporting it as a real edge we
have to rule out sub-minute leakage. The cache buckets every event into a
wall-clock minute and forward-fills the price — within a minute the score may
update from a play at second 50 while the latest price tick arrived at second
20 (pre-play). In that scenario the "current-minute" entry price is stale
relative to the scoring event that defined the run, and the 4-minute drift we
attribute to a momentum effect is really the market catching up to the same
information we used to detect the trade.

The check: re-run the buy_hot strategy but enter at the price snapshot one
wall-clock minute BEFORE the detection minute (`entry_price_hot_lag1`). If the
edge survives, momentum continuation is real for at least 4 game minutes after
the run. If the edge collapses, the headline result is intra-minute price-lag
arbitrage that would not be tradeable in live conditions without
sub-second execution.

Cost sweep and gate identical to p6_score_run_overreaction.py.
"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pandas as pd

NOTEBOOK_DIR = Path(__file__).resolve().parent
RESEARCH_DIR = NOTEBOOK_DIR.parent
sys.path.insert(0, str(RESEARCH_DIR))
sys.path.insert(0, str(NOTEBOOK_DIR))
from eda_kit import (  # noqa: E402
    BOOTSTRAP_N,
    BOOTSTRAP_CI,
    MIN_TRADES,
    block_bootstrap_mean,
    cluster_knockout,
    load_games,
    load_minutes,
)
from p6_score_run_overreaction import (  # noqa: E402
    RUN_DEFS,
    EXIT_MINUTES,
    COST_LEVELS,
    _detect_runs_in_game,
    _wp_home,
)

REPORT_DIR = RESEARCH_DIR / "reports" / "phase_minus1_2026-05-27"
CSV_DIR = REPORT_DIR / "csv"


def build_trades(minutes: pd.DataFrame, games: pd.DataFrame) -> pd.DataFrame:
    rows = []
    for game_id, gdf in minutes.groupby("game_id", sort=False):
        g_sorted = gdf.dropna(subset=["elapsed_game_sec"]).sort_values(
            ["elapsed_game_sec", "minute_ts"]).copy()
        if g_sorted.empty:
            continue
        dets = _detect_runs_in_game(g_sorted)
        if not dets:
            continue
        # Build a wall-clock-minute series of pm_home_winprob with lag-1
        wc_sorted = gdf.sort_values("minute_ts").reset_index(drop=True)
        wc_sorted["pm_home_lag1"] = wc_sorted["pm_home_winprob"].shift(1)
        wc_lookup = dict(zip(wc_sorted["minute_ts"].astype(int),
                              wc_sorted["pm_home_lag1"]))

        for d in dets:
            try:
                det = g_sorted.loc[d["row_index"]]
            except KeyError:
                continue
            mt = int(det["minute_ts"])
            wp_home_lag = wc_lookup.get(mt, np.nan)
            if not (isinstance(wp_home_lag, (int, float)) and np.isfinite(wp_home_lag)):
                continue
            wp_home_now = _wp_home(det)
            if not np.isfinite(wp_home_now):
                continue
            hot = d["hot_side"]
            entry_hot_lag = (1.0 - wp_home_lag) if hot == "away" else wp_home_lag
            entry_hot_now = (1.0 - wp_home_now) if hot == "away" else wp_home_now
            if not (0.0 < entry_hot_lag < 1.0):
                continue

            forward = g_sorted[g_sorted["elapsed_game_sec"] >= d["elapsed_game_sec"]]
            base = {
                "game_id": game_id, "date": det["date"],
                "spec": d["spec"],
                "hot_side": hot,
                "hot_team": (det["home"] if hot == "home" else det["away"]),
                "det_elapsed_sec": d["elapsed_game_sec"],
                "entry_price_hot": float(entry_hot_now),
                "entry_price_hot_lag1": float(entry_hot_lag),
                "entry_lag_delta": float(entry_hot_now - entry_hot_lag),
            }
            for n in EXIT_MINUTES:
                target = d["elapsed_game_sec"] + 60.0 * n
                cand = forward[forward["elapsed_game_sec"] >= target]
                if cand.empty:
                    base[f"exit_{n}min_wp_home"] = np.nan
                else:
                    exit_row = cand.iloc[0]
                    wp_h = _wp_home(exit_row)
                    base[f"exit_{n}min_wp_home"] = float(wp_h) if np.isfinite(wp_h) else np.nan
            for n in EXIT_MINUTES:
                wp_h_exit = base[f"exit_{n}min_wp_home"]
                if not np.isfinite(wp_h_exit):
                    base[f"buy_hot_lag1_{n}min_pnl"] = np.nan
                    base[f"buy_hot_now_{n}min_pnl"] = np.nan
                    continue
                exit_hot = wp_h_exit if hot == "home" else (1.0 - wp_h_exit)
                base[f"buy_hot_lag1_{n}min_pnl"] = float(exit_hot) - entry_hot_lag
                base[f"buy_hot_now_{n}min_pnl"] = float(exit_hot) - entry_hot_now
            rows.append(base)
    return pd.DataFrame(rows)


def _gate_reasons(boot, knock, n):
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
        sub = trades[trades["spec"] == spec]
        if sub.empty:
            continue
        for variant in ("lag1", "now"):
            for n in EXIT_MINUTES:
                pnl_col = f"buy_hot_{variant}_{n}min_pnl"
                if pnl_col not in sub.columns:
                    continue
                valid = sub.dropna(subset=[pnl_col]).copy()
                if valid.empty:
                    continue
                valid["primary_team"] = valid["hot_team"]
                for cost in COST_LEVELS:
                    net = (valid[pnl_col] - cost).to_numpy()
                    boot = block_bootstrap_mean(net, valid["game_id"].to_numpy(),
                                                n_boot=BOOTSTRAP_N, ci=BOOTSTRAP_CI)
                    v_net = valid.assign(pnl_net=net)
                    knock = cluster_knockout(v_net, pnl_col="pnl_net")
                    reasons = _gate_reasons(boot, knock, len(valid))
                    rows.append({
                        "spec": spec, "entry": variant, "exit_min": n,
                        "cost_per_trade": cost,
                        "n_trades": len(valid),
                        "n_games": int(valid["game_id"].nunique()),
                        "mean_entry": float(valid[f"entry_price_hot{'_lag1' if variant=='lag1' else ''}"].mean()),
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


def run(verbose: bool = True) -> dict:
    minutes = load_minutes()
    games = load_games()
    trades = build_trades(minutes, games)
    summary = summarize(trades)

    CSV_DIR.mkdir(parents=True, exist_ok=True)
    trades.to_csv(CSV_DIR / "p6_lag_trades.csv", index=False)
    summary.to_csv(CSV_DIR / "p6_lag_summary.csv", index=False)

    if verbose:
        print(f"Trades: {len(trades):,}\n")
        if not trades.empty:
            # Diagnose the entry-price lag delta — how often does the price move within the detection minute?
            print("Entry price delta (now - lag1) summary:")
            print(trades['entry_lag_delta'].describe().round(4).to_string())
            move_count = (trades['entry_lag_delta'].abs() > 0.005).sum()
            print(f"  rows with |delta| > 0.005: {move_count:,} / {len(trades):,}\n")
            cols = ["spec","entry","exit_min","cost_per_trade","n_trades","mean_entry",
                    "mean_gross","mean_net","ci_lo","ci_hi","win_rate","passes_gate"]
            disp = summary[cols].copy()
            for c in ["mean_entry","mean_gross","mean_net","ci_lo","ci_hi","win_rate"]:
                disp[c] = disp[c].round(4)
            print(disp.to_string(index=False))
            winners = summary[summary["passes_gate"]]
            print()
            if winners.empty:
                print("No (spec, entry, exit, cost) cell passes the Phase −1 gate.")
            else:
                print(f"PASSING CELLS: {len(winners)}")
                print("By (entry, spec):")
                print(winners.groupby(['entry','spec']).size().to_string())
    return {"trades": trades, "summary": summary}


if __name__ == "__main__":
    run()
