"""Standardized cost-robust evaluator for a single StrategySpec, under the
now-honest replay engine (symmetric entry/exit latency).

Runs the spec on the VAL split (and TRAIN for an overfit-gap read) under a cost
ladder and reports whether the edge is COST-ROBUST — i.e. still net-positive
with n>=200 at a realistic 2¢ round-trip, not just at the optimistic 1¢ live_pm
guess. Uses run_batch directly: NO registry writes (canonical DSR-N untouched),
NO Codex. Never touches the test split.

Usage:
  ~/code/kvenv/bin/python research/scripts/eval_spec.py --spec-file <path.json>
Emits one JSON object to stdout.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import numpy as np

_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_ROOT))

from research.harness.cost_profile import CALIBRATED_PM, LIVE_PM, ZERO_COST, CostProfile, use_profile
from research.harness.run_batch import run_batch
from research.harness.strategy_spec import StrategySpec
from research.scorer.promotion_gate import evaluate_trial

# Honest 2¢ round-trip screen (1¢ half-spread + small PM fees) AND real
# executed-trade depth (WS3), so the price-impact model actually bites instead
# of riding a fabricated 1000-contract book. An edge must clear THIS to be real.
SCREEN_2C = CALIBRATED_PM
CONTRACTS = 5.0  # default sizing; cents/contract = $/trade / CONTRACTS * 100


def _parse_gid(g: str):
    p = g.split("_")
    return (p[0], p[1], p[3]) if len(p) >= 4 and p[2] == "at" else (g, "", "")


def _trades(spec, split, profile):
    with use_profile(profile):
        b = run_batch(spec, split, rng_seed=0, parallel=1)
    rows = []
    for tr in b.trials:
        gid = tr.game_id
        d, away, home = _parse_gid(gid)
        for t in tr.trades:
            side = str(t.side)
            prim = home if side == "long_home" else (away if side == "long_away" else "")
            rows.append((float(t.pnl_net), gid, d, home, prim))
    return rows, b


def _summ(rows, b):
    n = len(rows)
    pnl = np.array([r[0] for r in rows], dtype=float) if rows else np.array([])
    mean = float(pnl.mean()) if n else 0.0
    return {
        "n_trades": n,
        "pnl_net": float(pnl.sum()) if n else 0.0,
        "mean_per_trade": mean,
        "cents_per_contract": round(mean / CONTRACTS * 100, 3),
        "win_rate": float((pnl > 0).mean()) if n else None,
    }


def _gate(rows):
    if len(rows) < 2:
        return {"passed": False, "ci_lo": None}
    pnl = np.array([r[0] for r in rows], dtype=float)
    g = np.array([r[1] for r in rows], dtype=object)
    dt = np.array([r[2] for r in rows], dtype=object)
    hm = np.array([r[3] for r in rows], dtype=object)
    pr = np.array([r[4] for r in rows], dtype=object)
    d = evaluate_trial(pnl, g, dt, hm, pr, n_total_trials_in_registry=260, rng_seed=42)
    return {"passed": bool(d.passed), "ci_lo": d.block_bootstrap_ci_lo,
            "ci_hi": d.block_bootstrap_ci_hi, "reasons": list(d.reasons)[:4]}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--spec-file", required=True)
    args = ap.parse_args()
    spec = StrategySpec.from_json(Path(args.spec_file).read_text())
    spec.validate()

    # VAL under three costs (same trade set; cost only changes net pnl/gate).
    val_zero, _ = _trades(spec, "val", ZERO_COST)
    val_lpm, b_lpm = _trades(spec, "val", LIVE_PM)
    val_2c, _ = _trades(spec, "val", SCREEN_2C)
    train_lpm, _ = _trades(spec, "train", LIVE_PM)

    s_zero, s_lpm, s_2c = _summ(val_zero, None), _summ(val_lpm, b_lpm), _summ(val_2c, None)
    s_train = _summ(train_lpm, None)
    gate_lpm, gate_2c = _gate(val_lpm), _gate(val_2c)

    # Cost-robust = enough trades, positive net AND gate passes at the honest 2¢.
    cost_robust = bool(s_2c["n_trades"] >= 200 and s_2c["mean_per_trade"] > 0 and gate_2c["passed"])
    overfit_gap = round(s_train["cents_per_contract"] - s_lpm["cents_per_contract"], 3)

    print(json.dumps({
        "spec_name": spec.name,
        "spec_hash": spec.spec_hash(),
        "side": spec.side,
        "val": {"zero_cost": s_zero, "live_pm_1c": s_lpm, "honest_2c": s_2c},
        "train_live_pm": s_train,
        "gate_live_pm": gate_lpm,
        "gate_honest_2c": gate_2c,
        "overfit_gap_cents_per_contract": overfit_gap,
        "COST_ROBUST": cost_robust,
        "verdict": ("COST-ROBUST EDGE (survives 2c)" if cost_robust
                    else "fails honest 2c screen" if s_2c["n_trades"] >= 200
                    else "too few trades (n<200 at 2c)"),
    }, indent=2, default=str))


if __name__ == "__main__":
    main()
