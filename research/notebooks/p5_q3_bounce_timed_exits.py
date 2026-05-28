"""Phenomenon B — Q3 bounce trade with timed exits, target/stop, cost sweep.

Buy the trailing team's contract at halftime (entry at first row with
`elapsed_game_sec ≥ 1440`). Exit using one of:

  TIMED EXITS
    - exit_3min : at elapsed_game_sec ≥ 1440 + 180  (3 min into Q3)
    - exit_6min : at elapsed_game_sec ≥ 1440 + 360  (6 min into Q3)
    - exit_9min : at elapsed_game_sec ≥ 1440 + 540  (9 min into Q3)
    - exit_12min: at elapsed_game_sec ≥ 1440 + 720  (end of Q3)

  TARGET / STOP-LOSS BRACKETS (walk forward minute-by-minute through Q3+Q4)
    - take +3c / stop -5c
    - take +5c / stop -8c
    - take +8c / stop -12c
    On the first minute whose ffilled trailing-team mid crosses the take or
    stop threshold, exit at that mid. If neither triggers by game end, settle
    on the outcome (this is the only path that can yield outcome-shaped PnL).

Cost assumption — the user's spec for Phenomenon B requires explicit bid/ask
costs. We treat each round trip as: 2¢ effective spread + 0.5¢ slippage = 2.5¢
(Plan v2.1 baseline). We additionally sweep 0c, 1c, 4c to show sensitivity. The
cost is debited once per round trip regardless of exit type.

Live-availability: entry uses the price ffilled up to the halftime bar (no
lookahead). Timed exits sample the ffilled mid at a later game-clock; the
take/stop bracket only consults prices up to and including the trigger bar.
Settlement uses the realized outcome. There is no overlap of trades within a
single game — every game contributes at most one trade per exit policy.

Cache reality: `kalshi_home_winprob` is empty in the 2025-26 cache; all entries
are Polymarket mids. Polymarket's true taker fee (~2%) means the realistic cost
for these trades is likely closer to 4¢ than 2.5¢; the 4c column models that.
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
GAME_END_ELAPSED = 2880.0
TIMED_OFFSETS_MIN = [3, 6, 9, 12]
TARGET_STOP_PAIRS = [(0.03, 0.05), (0.05, 0.08), (0.08, 0.12)]
COST_LEVELS = [0.0, 0.01, 0.025, 0.04]


def _wp_home(row) -> float:
    v = row.get("kalshi_home_winprob")
    if isinstance(v, (int, float)) and np.isfinite(v):
        return float(v)
    v = row.get("pm_home_winprob")
    return float(v) if isinstance(v, (int, float)) and np.isfinite(v) else np.nan


def _trailing_price_from_wp(wp_home: float, trailing: str) -> float:
    if not np.isfinite(wp_home):
        return np.nan
    return (1.0 - wp_home) if trailing == "away" else wp_home


def _ascending_unique(g_df: pd.DataFrame) -> pd.DataFrame:
    """Drop rows with missing elapsed, sort, and de-dup keeping last sample at
    each elapsed bin (forward fill semantics within a wall-clock minute)."""
    g = g_df.dropna(subset=["elapsed_game_sec"]).copy()
    g = g.sort_values(["elapsed_game_sec", "minute_ts"])
    return g


def build_trades(minutes: pd.DataFrame, games: pd.DataFrame) -> pd.DataFrame:
    home_won_map = games.set_index("game_id")["home_won"].to_dict()
    rows = []
    for game_id, gdf in minutes.groupby("game_id", sort=False):
        g = _ascending_unique(gdf)
        if g.empty:
            continue
        ht_mask = g["elapsed_game_sec"] >= HALFTIME_ELAPSED
        if not ht_mask.any():
            continue
        h = g[ht_mask].iloc[0]
        margin = h["margin"]
        if not np.isfinite(margin) or margin == 0:
            continue
        wp_home = _wp_home(h)
        if not np.isfinite(wp_home):
            continue
        trailing = "away" if margin > 0 else "home"
        entry_price = _trailing_price_from_wp(wp_home, trailing)
        if not (0.0 < entry_price < 1.0):
            continue
        home_won = home_won_map.get(game_id)
        if home_won is None:
            continue
        trailing_won = (trailing == "home") == bool(home_won)
        settle_value = 1.0 if trailing_won else 0.0
        primary_team = h["away"] if trailing == "away" else h["home"]
        deficit = float(abs(margin))

        # All bars at or after entry
        forward = g[g["elapsed_game_sec"] >= HALFTIME_ELAPSED].copy()
        # Trailing-team mid per bar (ffill is already applied to the source col)
        if trailing == "away":
            forward["trail_price"] = 1.0 - forward["pm_home_winprob"]
        else:
            forward["trail_price"] = forward["pm_home_winprob"]
        forward = forward.dropna(subset=["trail_price"])

        trade = {
            "game_id": game_id, "date": h["date"],
            "primary_team": primary_team, "trailing_side": trailing,
            "halftime_deficit_abs": deficit,
            "entry_price": float(entry_price),
            "settle_value": float(settle_value),
        }

        # Timed exits — first bar with elapsed ≥ halftime + N*60
        for n in TIMED_OFFSETS_MIN:
            target_elapsed = HALFTIME_ELAPSED + 60.0 * n
            cand = forward[forward["elapsed_game_sec"] >= target_elapsed]
            if cand.empty:
                trade[f"exit_{n}min_price"] = np.nan
                trade[f"exit_{n}min_pnl"] = np.nan
            else:
                exit_row = cand.iloc[0]
                exit_price = float(exit_row["trail_price"])
                trade[f"exit_{n}min_price"] = exit_price
                trade[f"exit_{n}min_pnl"] = exit_price - entry_price

        # Bracket exits — walk forward Q3+ minutes; exit on first cross
        for take, stop in TARGET_STOP_PAIRS:
            up = entry_price + take
            dn = entry_price - stop
            exit_price = np.nan
            exit_kind = "timeout_outcome"  # default: hold to settlement
            # Skip the entry bar itself (it can't trigger at t=0 unless ffill diverges).
            future = forward.iloc[1:] if len(forward) > 0 else forward
            for _, r in future.iterrows():
                p = float(r["trail_price"])
                if p >= up:
                    exit_price = p
                    exit_kind = "take"
                    break
                if p <= dn:
                    exit_price = p
                    exit_kind = "stop"
                    break
            if np.isnan(exit_price):
                exit_price = settle_value
            tag = f"bracket_t{int(take*100):02d}_s{int(stop*100):02d}"
            trade[f"{tag}_exit_price"] = exit_price
            trade[f"{tag}_exit_kind"] = exit_kind
            trade[f"{tag}_pnl"] = exit_price - entry_price

        rows.append(trade)
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

    policies = []
    for n in TIMED_OFFSETS_MIN:
        policies.append((f"exit_{n}min", f"exit_{n}min_pnl"))
    for take, stop in TARGET_STOP_PAIRS:
        tag = f"bracket_t{int(take*100):02d}_s{int(stop*100):02d}"
        policies.append((tag, f"{tag}_pnl"))

    for policy, pnl_col in policies:
        valid = trades.dropna(subset=[pnl_col]).copy()
        if valid.empty:
            continue
        # Per-policy stats: extra columns for bracket exit kinds
        exit_kind_col = pnl_col.replace("_pnl", "_exit_kind")
        for cost in COST_LEVELS:
            net = (valid[pnl_col] - cost).to_numpy()
            boot = block_bootstrap_mean(net, valid["game_id"].to_numpy(),
                                        n_boot=BOOTSTRAP_N, ci=BOOTSTRAP_CI)
            v_with_net = valid.assign(pnl_net=net)
            knock = cluster_knockout(v_with_net, pnl_col="pnl_net")
            reasons = _gate_reasons(boot, knock, len(valid))
            row = {
                "policy": policy, "cost_per_trade": cost,
                "n_trades": len(valid),
                "n_games": int(valid["game_id"].nunique()),
                "mean_entry": float(valid["entry_price"].mean()),
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
            }
            if exit_kind_col in valid.columns:
                ec = valid[exit_kind_col].value_counts(normalize=True)
                row["frac_take"] = float(ec.get("take", 0.0))
                row["frac_stop"] = float(ec.get("stop", 0.0))
                row["frac_timeout"] = float(ec.get("timeout_outcome", 0.0))
            else:
                row["frac_take"] = np.nan
                row["frac_stop"] = np.nan
                row["frac_timeout"] = np.nan
            rows.append(row)
    return pd.DataFrame(rows)


def plot_summary(summary: pd.DataFrame, path: Path) -> None:
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    fig, axes = plt.subplots(1, 2, figsize=(12, 5))
    timed = summary[summary["policy"].str.startswith("exit_")]
    brackets = summary[summary["policy"].str.startswith("bracket_")]

    for ax, sub, title in [(axes[0], timed, "Timed exits"),
                            (axes[1], brackets, "Take/Stop brackets")]:
        if sub.empty:
            continue
        for cost in COST_LEVELS:
            s = sub[sub["cost_per_trade"] == cost]
            if s.empty:
                continue
            ax.errorbar(s["policy"], s["mean_net"],
                        yerr=[(s["mean_net"] - s["ci_lo"]).values,
                              (s["ci_hi"] - s["mean_net"]).values],
                        marker="o", capsize=3, label=f"cost={cost:.3f}")
        ax.axhline(0, color="k", linewidth=0.5)
        ax.set_title(title)
        ax.set_ylabel("Mean net PnL")
        ax.grid(alpha=0.3)
        ax.tick_params(axis="x", rotation=30)
        ax.legend(fontsize=8)
    fig.suptitle("Phenomenon B — Halftime trailing-team buy with timed/bracket exits", fontsize=11)
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
    trades.to_csv(CSV_DIR / "p5_trades.csv", index=False)
    summary.to_csv(CSV_DIR / "p5_summary.csv", index=False)
    plot_summary(summary, FIG_DIR / "p5_q3_bounce_exits.png")

    if verbose:
        print(f"Trades: {len(trades):,} games\n")
        cols = ["policy", "cost_per_trade", "n_trades", "mean_entry",
                "mean_gross", "mean_net", "ci_lo", "ci_hi", "win_rate",
                "frac_take", "frac_stop", "frac_timeout", "passes_gate"]
        disp = summary[cols].copy()
        for c in ["mean_entry", "mean_gross", "mean_net", "ci_lo", "ci_hi",
                  "win_rate", "frac_take", "frac_stop", "frac_timeout"]:
            disp[c] = disp[c].round(4)
        print(disp.to_string(index=False))
        winners = summary[summary["passes_gate"]]
        print()
        if winners.empty:
            print("No (policy, cost) cell passes the Phase −1 gate.")
        else:
            print(f"PASSING CELLS: {len(winners)}")
            print(winners.to_string(index=False))
    return {"trades": trades, "summary": summary}


if __name__ == "__main__":
    run()
