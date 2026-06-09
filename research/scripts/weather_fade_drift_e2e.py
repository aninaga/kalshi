"""weather_fade_drift_e2e — falsify the drift-REVERSION (fade) mirror on temp.

Pre-registered hypothesis af866e8ac7d19284 (opus_weather): thin daily-high
temperature ladders OVER-react to a recent intraday implied-temp move and
revert by settlement. Side = OPPOSITE the drift sign at the ATM boundary.

This is the explicitly-flagged un-scored mirror of the DEAD continuation
hypothesis 8aabae5235c8ed07 (-6.3c/ct). Params are fixed (pre-registered):
  drift window  = 5 fresh bars of panel.mid
  |drift| thresh = 0.75 F
  warmup        = 1h (min_elapsed = 3600)
  freshness     = <= 2 stale minutes (Strategy default)
  fill          = FillModel(venue="kalshi", half_spread=0.015)
                  (= max(2 * measured train bucket median 0.0075, 0.015))
  exit          = settlement, honest i+1 latency

Run:
    python -m research.scripts.weather_fade_drift_e2e --split train
    python -m research.scripts.weather_fade_drift_e2e --split nontest
"""
from __future__ import annotations

import argparse
import json

import numpy as np

from research.lab import data
from research.lab.execution import FillModel
from research.lab.strategy import Strategy, staleness_min
from research.lab.evaluate import evaluate

DRIFT_BARS = 5
DRIFT_THRESH = 0.75
WARMUP_SEC = 3600.0
HALF_SPREAD = 0.015  # max(2 * measured train bucket median 0.0075, 0.015)


def _drift(panel) -> np.ndarray:
    """Implied-temp change over the last DRIFT_BARS bars (NaN-safe)."""
    mid = np.asarray(panel.mid, dtype=float)
    n = len(mid)
    out = np.full(n, np.nan)
    if n > DRIFT_BARS:
        out[DRIFT_BARS:] = mid[DRIFT_BARS:] - mid[:-DRIFT_BARS]
    return out


def entry(panel):
    d = _drift(panel)
    return np.isfinite(d) & (np.abs(d) >= DRIFT_THRESH)


def side(panel, i):
    # FADE: opposite the drift sign. UP drift -> bet under; DOWN drift -> over.
    return "under" if _drift(panel)[i] > 0 else "over"


def build_strategy() -> Strategy:
    return Strategy(
        name="weather_fade_drift",
        entry=entry,
        side=side,
        exit="settlement",
        entry_latency_min=1.0,
        max_stale_min=2.0,
        min_elapsed=WARMUP_SEC,
        max_elapsed=1.0e9,  # weather events run ~140k sec; do not clip
    )


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--split", default="train",
                    choices=["train", "val", "nontest"])
    a = ap.parse_args(argv)

    panels = data.load_panels("temp", split=a.split)
    fm = FillModel(venue="kalshi", half_spread=HALF_SPREAD)
    strat = build_strategy()
    trades = strat.run(panels, fill_model=fm)

    gate = evaluate(trades,
                    ledger_path="research/reports/alpha/ledger.jsonl",
                    family="weather")
    print(json.dumps({
        "split": a.split,
        "n_panels": len(panels),
        "n_trades": len(trades.rows),
        "cents_per_contract": round(gate.cents_per_contract, 4),
        "ci_lo": round(gate.ci_lo, 4),
        "ci_hi": round(gate.ci_hi, 4),
        "passed": bool(gate.passed),
        "cost_sweep": [round(x, 4) for x in gate.cost_sweep],
        "walkforward": getattr(gate, "walkforward", None),
        "adversarial": getattr(gate, "adversarial", None),
    }, indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
