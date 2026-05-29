"""Adversarial-review evidence generator for trial id=174 (halftime_v2_window_wide_v1).

This script produces the quantitative evidence an adversarial reviewer needs
BEFORE a human decides whether to burn a finite test-set unlock. It NEVER
touches the test split: every backtest here runs on `val` (and `train` for the
overfit-gap probe). It does NOT write to the trial registry — it calls
`run_batch` directly (pure compute), so it cannot pollute the registry's
multiple-testing N or add spurious siblings.

Probes
------
  baseline      : reproduce id=174's val result; also run train for overfit gap;
                  exit-reason distribution (sanity-checks the TP/SL bug fix).
  cost          : re-replay val under a cost ladder (zero -> live_pm -> +0.5/1/1.5/2c
                  -> pessimistic); report per-trade net & block-bootstrap CI lo at
                  each cost. Answers "does the edge survive a 1c shift?".
  concentration : per-game / per-team PnL distribution; top-10 game share;
                  median game; fraction of games profitable; entry-time histogram.
  thresholds    : perturb window bounds, run threshold, TP/SL, time-stop; map the
                  edge surface (plateau = robust, spike = curve-fit).
  all           : run every probe.

Usage
-----
  ~/code/kvenv/bin/python research/scripts/review_probe_174.py \
      --probe all --parallel 6 --out research/reports/2026-05-28_review_174
"""

from __future__ import annotations

import argparse
import copy
import json
import math
import sqlite3
import sys
import time
from dataclasses import replace
from pathlib import Path
from typing import Any

import numpy as np

_REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_REPO_ROOT))

from research.harness.cost_profile import (  # noqa: E402
    LIVE_PM,
    PESSIMISTIC,
    ZERO_COST,
    CostProfile,
    use_profile,
)
from research.harness.run_batch import run_batch  # noqa: E402
from research.harness.strategy_spec import Condition, StrategySpec  # noqa: E402
from research.scorer.promotion_gate import evaluate_trial  # noqa: E402

_DB = _REPO_ROOT / "market_data" / "trials.db"
_TRIAL_ID = 174


# --------------------------------------------------------------------------- #
# Spec loading + perturbation
# --------------------------------------------------------------------------- #


def load_spec_174() -> StrategySpec:
    conn = sqlite3.connect(str(_DB))
    try:
        row = conn.execute(
            "SELECT spec_json FROM trials WHERE id=?;", (_TRIAL_ID,)
        ).fetchone()
    finally:
        conn.close()
    if row is None:
        raise SystemExit(f"trial id={_TRIAL_ID} not found in {_DB}")
    return StrategySpec.from_json(row[0])


def build_entry_expr(win_lo: int, win_hi: int, run_thr: int) -> str:
    return (
        f"elapsed_game_sec >= {win_lo} and elapsed_game_sec <= {win_hi} "
        f"and abs(recent_run_signed) >= {run_thr} and pos_side is None"
    )


def build_exit_exprs(tp_bps: int, sl_bps: int) -> list[Condition]:
    return [
        Condition(
            expr=f"pos_side is not None and unrealized_pnl_bps >= {tp_bps}",
            description=f"TP {tp_bps}bps",
        ),
        Condition(
            expr=f"pos_side is not None and unrealized_pnl_bps <= {sl_bps}",
            description=f"SL {sl_bps}bps",
        ),
    ]


def perturb(
    base: StrategySpec,
    *,
    win_lo: int = 1320,
    win_hi: int = 1740,
    run_thr: int = 6,
    tp_bps: int = 200,
    sl_bps: int = -300,
    time_stop_sec: int = 600,
    name: str = "perturbed",
) -> StrategySpec:
    spec = replace(
        base,
        name=name,
        entry_condition=Condition(
            expr=build_entry_expr(win_lo, win_hi, run_thr),
            description="post-halftime window + run trigger (perturbed)",
        ),
        exit_conditions=build_exit_exprs(tp_bps, sl_bps),
        time_stop_sec=time_stop_sec,
    )
    spec.validate()
    return spec


# --------------------------------------------------------------------------- #
# Backtest -> per-trade arrays
# --------------------------------------------------------------------------- #


def _parse_game_id(game_id: str) -> tuple[str, str, str]:
    parts = game_id.split("_")
    if len(parts) >= 4 and parts[2] == "at":
        return parts[0], parts[1], parts[3]
    return game_id, "", ""


def run_and_extract(
    spec: StrategySpec, split: str, profile: CostProfile, parallel: int
) -> dict[str, Any]:
    """Run a backtest and return summary + per-trade rows.

    IMPORTANT: forced to parallel=1. On macOS the multiprocessing start method
    is `spawn`, so worker processes re-import `cost_profile` fresh and reset the
    module-global active profile to PESSIMISTIC — the parent's `use_profile`
    does NOT propagate. Running serially keeps the active profile in effect.
    """
    with use_profile(profile):
        batch = run_batch(spec, split, rng_seed=0, parallel=1)

    rows: list[dict[str, Any]] = []
    for trial in batch.trials:
        gid = trial.game_id
        date, away, home = _parse_game_id(gid)
        for t in trial.trades:
            side = str(t.side)
            primary = home if side == "long_home" else (away if side == "long_away" else "")
            rows.append(
                {
                    "game_id": gid,
                    "date": date,
                    "home": home,
                    "away": away,
                    "side": side,
                    "primary": primary,
                    "pnl_net": float(t.pnl_net),
                    "pnl_gross": float(t.pnl_gross),
                    "entry_sec": float(t.entry_ts_game_sec),
                    "exit_sec": float(t.exit_ts_game_sec),
                    "exit_reason": str(t.exit_reason),
                    "entry_price": float(t.entry_price),
                    "exit_price": float(t.exit_price),
                }
            )
    return {
        "summary": {
            "split": split,
            "profile": profile.name,
            "n_games": batch.n_games,
            "n_games_with_trades": batch.n_games_with_trades,
            "n_trades": batch.n_trades_total,
            "pnl_gross": batch.pnl_gross,
            "pnl_net": batch.pnl_net,
            "mean_pnl_per_trade_net": batch.mean_pnl_per_trade_net,
            "sharpe_net": batch.sharpe_per_trade_net,
            "win_rate": batch.win_rate,
            "avg_holding_sec": batch.avg_holding_sec,
            "n_games_skipped": batch.n_games_skipped,
        },
        "trades": rows,
    }


def gate_from_rows(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """Run the promotion gate on per-trade rows; return the salient fields."""
    if not rows:
        return {"passed": False, "reasons": ["no trades"], "ci_lo": None, "ci_hi": None}
    pnl = np.array([r["pnl_net"] for r in rows], dtype=float)
    games = np.array([r["game_id"] for r in rows], dtype=object)
    dates = np.array([r["date"] for r in rows], dtype=object)
    homes = np.array([r["home"] for r in rows], dtype=object)
    prims = np.array([r["primary"] for r in rows], dtype=object)
    d = evaluate_trial(
        val_pnl_per_trade=pnl,
        val_game_id_per_trade=games,
        val_date_per_trade=dates,
        val_home_team_per_trade=homes,
        val_primary_team_per_trade=prims,
        n_total_trials_in_registry=214,
        rng_seed=42,
    )
    return {
        "passed": bool(d.passed),
        "reasons": list(d.reasons),
        "ci_lo": d.block_bootstrap_ci_lo,
        "ci_hi": d.block_bootstrap_ci_hi,
        "mean": d.block_bootstrap_mean,
        "n_trades": d.n_trades_val,
        "n_games": d.n_games_val,
        "knockouts": {k: float(v) for k, v in d.cluster_knockouts.items() if isinstance(v, (int, float))},
        "season_overlap": d.season_split.get("ci_overlap"),
        "parity_overlap": d.parity_split.get("ci_overlap"),
    }


# --------------------------------------------------------------------------- #
# Probes
# --------------------------------------------------------------------------- #


def cost_ladder() -> list[CostProfile]:
    """live_pm has spread_bps=50 (1c round-trip). Each +25bps half-spread ~= +0.5c
    round-trip. Hold fees at live_pm levels so we isolate the spread sensitivity."""
    return [
        ZERO_COST,
        LIVE_PM,
        CostProfile("live_pm+0.5c", 75, 200, 0.005),
        CostProfile("live_pm+1.0c", 100, 200, 0.005),
        CostProfile("live_pm+1.5c", 125, 200, 0.005),
        CostProfile("live_pm+2.0c", 150, 200, 0.005),
        PESSIMISTIC,
    ]


def probe_baseline(spec: StrategySpec, parallel: int) -> dict[str, Any]:
    val = run_and_extract(spec, "val", LIVE_PM, parallel)
    train = run_and_extract(spec, "train", LIVE_PM, parallel)
    # exit-reason histogram (sanity check the TP/SL bug fix actually fires)
    reasons: dict[str, int] = {}
    for r in val["trades"]:
        reasons[r["exit_reason"]] = reasons.get(r["exit_reason"], 0) + 1
    val_gate = gate_from_rows(val["trades"])
    return {
        "val_summary": val["summary"],
        "train_summary": train["summary"],
        "val_gate": val_gate,
        "exit_reason_hist_val": reasons,
        "overfit_gap": {
            "train_mean_per_trade": train["summary"]["mean_pnl_per_trade_net"],
            "val_mean_per_trade": val["summary"]["mean_pnl_per_trade_net"],
            "train_sharpe": train["summary"]["sharpe_net"],
            "val_sharpe": val["summary"]["sharpe_net"],
        },
    }


def probe_cost(spec: StrategySpec, parallel: int) -> dict[str, Any]:
    out = []
    for prof in cost_ladder():
        res = run_and_extract(spec, "val", prof, parallel)
        gate = gate_from_rows(res["trades"])
        s = res["summary"]
        out.append(
            {
                "profile": prof.name,
                "spread_bps": prof.spread_bps,
                "n_trades": s["n_trades"],
                "pnl_net": s["pnl_net"],
                "mean_pnl_per_trade_net": s["mean_pnl_per_trade_net"],
                "sharpe_net": s["sharpe_net"],
                "win_rate": s["win_rate"],
                "gate_ci_lo": gate["ci_lo"],
                "gate_passed": gate["passed"],
            }
        )
    return {"ladder": out}


def probe_concentration(spec: StrategySpec, parallel: int) -> dict[str, Any]:
    res = run_and_extract(spec, "val", LIVE_PM, parallel)
    rows = res["trades"]
    # per game
    by_game: dict[str, float] = {}
    for r in rows:
        by_game[r["game_id"]] = by_game.get(r["game_id"], 0.0) + r["pnl_net"]
    game_pnls = sorted(by_game.items(), key=lambda kv: kv[1])
    total = sum(by_game.values())
    pos_total = sum(v for v in by_game.values() if v > 0)
    games_arr = np.array([v for _, v in game_pnls], dtype=float)
    top10 = sorted(by_game.values(), reverse=True)[:10]
    # per team
    by_team: dict[str, float] = {}
    for r in rows:
        by_team[r["primary"]] = by_team.get(r["primary"], 0.0) + r["pnl_net"]
    team_sorted = sorted(by_team.items(), key=lambda kv: kv[1], reverse=True)
    # entry-time histogram (which minute of the window fires)
    entry_min: dict[int, int] = {}
    for r in rows:
        m = int(r["entry_sec"] // 60)
        entry_min[m] = entry_min.get(m, 0) + 1
    return {
        "n_trades": len(rows),
        "n_games_with_trades": len(by_game),
        "total_pnl_net": total,
        "median_game_pnl": float(np.median(games_arr)) if len(games_arr) else None,
        "frac_games_profitable": float(np.mean(games_arr > 0)) if len(games_arr) else None,
        "top10_game_share_of_total": (sum(top10) / total) if total else None,
        "top10_game_share_of_positive": (sum(top10) / pos_total) if pos_total else None,
        "top5_games": game_pnls[-5:][::-1],
        "bottom5_games": game_pnls[:5],
        "top5_teams": team_sorted[:5],
        "n_teams": len(by_team),
        "entry_minute_hist": dict(sorted(entry_min.items())),
        "gate": gate_from_rows(rows),
    }


def probe_thresholds(spec: StrategySpec, parallel: int) -> dict[str, Any]:
    variants: list[tuple[str, dict[str, int]]] = [
        ("baseline", {}),
        # window lower bound (baseline 1320)
        ("win_lo_1200", {"win_lo": 1200}),
        ("win_lo_1260", {"win_lo": 1260}),
        ("win_lo_1380", {"win_lo": 1380}),
        ("win_lo_1440", {"win_lo": 1440}),
        # window upper bound (baseline 1740)
        ("win_hi_1620", {"win_hi": 1620}),
        ("win_hi_1680", {"win_hi": 1680}),
        ("win_hi_1800", {"win_hi": 1800}),
        ("win_hi_1860", {"win_hi": 1860}),
        # run threshold (baseline 6)
        ("run_thr_4", {"run_thr": 4}),
        ("run_thr_5", {"run_thr": 5}),
        ("run_thr_7", {"run_thr": 7}),
        ("run_thr_8", {"run_thr": 8}),
        # take-profit (baseline 200)
        ("tp_150", {"tp_bps": 150}),
        ("tp_250", {"tp_bps": 250}),
        ("tp_300", {"tp_bps": 300}),
        # stop-loss (baseline -300)
        ("sl_-200", {"sl_bps": -200}),
        ("sl_-250", {"sl_bps": -250}),
        ("sl_-350", {"sl_bps": -350}),
        # time stop (baseline 600)
        ("ts_300", {"time_stop_sec": 300}),
        ("ts_450", {"time_stop_sec": 450}),
        ("ts_900", {"time_stop_sec": 900}),
    ]
    out = []
    for label, kw in variants:
        sp = perturb(spec, name=f"probe_{label}", **kw)
        res = run_and_extract(sp, "val", LIVE_PM, parallel)
        gate = gate_from_rows(res["trades"])
        s = res["summary"]
        out.append(
            {
                "variant": label,
                "params": kw,
                "n_trades": s["n_trades"],
                "pnl_net": s["pnl_net"],
                "mean_pnl_per_trade_net": s["mean_pnl_per_trade_net"],
                "sharpe_net": s["sharpe_net"],
                "win_rate": s["win_rate"],
                "gate_ci_lo": gate["ci_lo"],
                "gate_passed": gate["passed"],
            }
        )
    return {"variants": out}


# --------------------------------------------------------------------------- #
# Main
# --------------------------------------------------------------------------- #


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument(
        "--probe",
        choices=("baseline", "cost", "concentration", "thresholds", "all"),
        required=True,
    )
    p.add_argument("--parallel", type=int, default=6)
    p.add_argument("--out", default="research/reports/2026-05-28_review_174")
    args = p.parse_args()

    out_dir = _REPO_ROOT / args.out
    out_dir.mkdir(parents=True, exist_ok=True)

    spec = load_spec_174()
    probes = (
        ["baseline", "cost", "concentration", "thresholds"]
        if args.probe == "all"
        else [args.probe]
    )

    fns = {
        "baseline": probe_baseline,
        "cost": probe_cost,
        "concentration": probe_concentration,
        "thresholds": probe_thresholds,
    }
    for name in probes:
        t0 = time.time()
        result = fns[name](spec, args.parallel)
        result["_elapsed_sec"] = round(time.time() - t0, 1)
        result["_spec_hash"] = spec.spec_hash()
        dest = out_dir / f"{name}.json"
        dest.write_text(json.dumps(result, indent=2, default=str))
        print(f"[{name}] wrote {dest} in {result['_elapsed_sec']}s")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
