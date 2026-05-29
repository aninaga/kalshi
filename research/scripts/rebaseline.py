"""One-shot RE-BASELINE: evaluate the existing canonical specs under the
now-honest data + cost model (recovered Kalshi, calibrated-depth + 2% PM flat
fee, 300s staleness bound, fixed frames) and print a survival table.

Prior "validated"/"rejected" verdicts ran on broken data, so they must be
re-checked. Read-only: uses run_batch on train/val only (NO registry writes,
NEVER touches the test split). Overfit gap is omitted for speed — run
research/scripts/eval_spec.py on a single spec for the full cost ladder.

Usage:  ~/code/kvenv/bin/python -m research.scripts.rebaseline
"""
from __future__ import annotations

import importlib
import sqlite3
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_ROOT))

from research.harness.cost_profile import ZERO_COST  # noqa: E402
from research.harness.strategy_spec import StrategySpec  # noqa: E402
from research.scripts.eval_spec import _gate, _summ, _trades, SCREEN_2C  # noqa: E402

MANUAL = ["c_score_run_buy_hot", "p4_halftime_1to3", "p2_underdog", "deliberate_overfit"]
REGISTRY = ["halftime_v2_window_wide_v1", "claude_5ebcabb1_q4_clutch_run_hot_v1"]
NOTE = {
    "c_score_run_buy_hot": "score-run momentum (Phase-−1's only survivor)",
    "p4_halftime_1to3": "halftime deficit bucket",
    "p2_underdog": "underdog",
    "deliberate_overfit": "CONTROL — must fail",
    "halftime_v2_window_wide_v1": "prior promotion candidate (was gate-PASS, +$24)",
    "claude_5ebcabb1_q4_clutch_run_hot_v1": "top-val momentum (was +$41.8)",
}


def collect():
    specs = {}
    for n in MANUAL:
        try:
            specs[n] = importlib.import_module(f"research.experiments.manual.{n}").SPEC
        except Exception as e:  # noqa: BLE001
            print(f"  (skip manual {n}: {e})", file=sys.stderr)
    con = sqlite3.connect(str(_ROOT / "market_data" / "trials.db"))
    con.row_factory = sqlite3.Row
    for n in REGISTRY:
        r = con.execute("SELECT spec_json FROM trials WHERE spec_name=? ORDER BY id DESC LIMIT 1",
                        (n,)).fetchone()
        if r and r["spec_json"]:
            try:
                specs[n] = StrategySpec.from_json(r["spec_json"])
            except Exception as e:  # noqa: BLE001
                print(f"  (skip registry {n}: {e})", file=sys.stderr)
    con.close()
    return specs


def evaluate(spec):
    spec.validate()
    vz, _ = _trades(spec, "val", ZERO_COST)
    v2, _ = _trades(spec, "val", SCREEN_2C)
    sz, s2 = _summ(vz, None), _summ(v2, None)
    g2 = _gate(v2)
    cost_robust = bool(s2["n_trades"] >= 200 and s2["mean_per_trade"] > 0 and g2["passed"])
    return s2["n_trades"], sz["cents_per_contract"], s2["cents_per_contract"], g2["passed"], cost_robust


def main():
    specs = collect()
    print("\nRE-BASELINE — val split under the honest screen "
          "(CALIBRATED_PM real depth + 2% PM flat + 300s staleness + recovered Kalshi)\n")
    hdr = f"{'spec':<40} {'n@2c':>6} {'c/ct@0':>8} {'c/ct@2c':>8} {'gate2c':>7} {'ROBUST':>7}  note"
    print(hdr)
    print("-" * len(hdr))
    for name, spec in specs.items():
        try:
            n, c0, c2, gate, robust = evaluate(spec)
            print(f"{name[:40]:<40} {n:>6} {c0:>8.2f} {c2:>8.2f} {str(gate):>7} "
                  f"{('YES' if robust else 'no'):>7}  {NOTE.get(name, '')}")
        except Exception as e:  # noqa: BLE001
            print(f"{name[:40]:<40}  ERROR: {str(e)[:70]}  ({NOTE.get(name, '')})")
    print("\nc/ct@0 = cents/contract at ZERO cost (underlying signal); "
          "c/ct@2c = under honest costs; ROBUST = n>=200 & net>0 & gate passes at 2c.")


if __name__ == "__main__":
    main()
