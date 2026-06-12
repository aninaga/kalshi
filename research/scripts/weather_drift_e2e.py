"""Walking-skeleton end-to-end: weather family -> gate verdict.

PRE-REGISTERED hypothesis (committed before any scoring; params fixed below):

  market   : temp (Kalshi daily high temperature, 5 cities)
  mechanism: intraday under-reaction — thin weather books update sluggishly as
             morning observations arrive, so a recent move in the implied high
             CONTINUES toward settlement.
  signal   : drift = implied_temp[i] - implied_temp[i-K] over fresh quotes
  direction: continuation — drift >= +TAU buys OVER the nearest boundary,
             drift <= -TAU buys UNDER (1 bit, fixed in advance)

Execution honesty (this family's specifics):
  * Fill via ``lab.execution.FillModel(venue="kalshi")`` — snap to a listed
    boundary, fill at the REAL cumulative quote, never 0.50.
  * The cumulative ladder's interior boundaries are MULTI-LEG synthetics of
    the bucket book, so the half-spread charged is ``LEG_MULT x`` the median
    MEASURED per-bucket half-spread from the panels' candle bid/asks (train
    split only, computed before scoring) — and the gate's 0-4c cost sweep
    stresses beyond it.
  * Honest i+1 latency; freshness guard ``<= 2`` stale minutes (the lab
    default; a stale thin book is a measurement artifact, not an edge).
  * Splits from ``market_data/weather/splits.json``; TEST never read.

The deliverable is REACHING an honest GateResult on real non-NBA data through
the generic seam — the verdict itself may well be DEAD; that still proves the
skeleton (and is recorded to the ledger with ``family="weather"`` so the DSR
hurdle partitions correctly).

Run::

    python -m research.scripts.weather_drift_e2e --split train
    python -m research.scripts.weather_drift_e2e --split nontest
"""
from __future__ import annotations

import argparse
import json

import numpy as np

from research.lab import data as lab_data
from research.lab import evaluate as lab_eval
from research.lab.execution import FillModel
from research.lab.providers.weather import TEMP
from research.lab.strategy import Strategy

# ---- pre-registered parameters (fixed before any scoring) ------------------
K_BARS = 5            # drift lookback, quote-minutes
TAU_DEG = 0.75        # minimum |drift| in degrees F to fire
MIN_ELAPSED = 3600.0  # skip the first hour of the quote window (book warm-up)
LEG_MULT = 2.0        # interior cumulative boundaries are multi-leg synthetics
LEDGER = "research/reports/alpha/ledger.jsonl"

MECHANISM = ("intraday under-reaction: thin daily-high books update sluggishly "
             "as observations arrive; recent implied-temp moves continue")
SIGNAL_DESC = f"implied-temp drift over last {K_BARS} fresh bars, |drift|>={TAU_DEG}F"
DIRECTION = "continuation"


def _drift(panel) -> np.ndarray:
    mid = np.asarray(panel.mid, dtype=float)
    out = np.full_like(mid, np.nan)
    out[K_BARS:] = mid[K_BARS:] - mid[:-K_BARS]
    return out


def _entry(panel) -> np.ndarray:
    d = _drift(panel)
    return np.isfinite(d) & (np.abs(d) >= TAU_DEG)


def _side(panel, i: int) -> str:
    return "over" if _drift(panel)[i] > 0 else "under"


def measured_half_spread(panels) -> float:
    """Median measured per-bucket half-spread across panels (prob units)."""
    vals = []
    for p in panels:
        hs = np.asarray(p.features.get("half_spread", []), dtype=float)
        vals.extend(hs[np.isfinite(hs)].tolist())
    return float(np.median(vals)) if vals else 0.015


def run(split: str) -> dict:
    # Spread is CALIBRATED ON TRAIN regardless of the scored split (no peeking
    # at val through the cost model).
    train_panels = lab_data.load_panels(TEMP, split="train")
    half_bucket = measured_half_spread(train_panels)
    half_spread = max(LEG_MULT * half_bucket, 0.015)

    panels = train_panels if split == "train" else lab_data.load_panels(TEMP, split=split)
    strat = Strategy(
        name="weather.drift_continuation",
        entry=_entry, side=_side, exit="settlement",
        min_elapsed=MIN_ELAPSED, max_elapsed=float("inf"), max_stale_min=2.0,
    )
    fills = FillModel(half_spread=half_spread, venue="kalshi")
    trades = strat.run(panels, fill_model=fills)

    gate = lab_eval.evaluate(trades, ledger_path=LEDGER, family="weather")
    df = trades.df()
    return {
        "split": split,
        "n_panels": len(panels),
        "n_trades": len(trades),
        "half_spread_used": half_spread,
        "half_spread_measured_bucket_median": half_bucket,
        "by_city": (df.groupby("home_team").size().to_dict() if len(df) else {}),
        "gate": {
            "passed": gate.passed,
            "cents_per_contract": gate.cents_per_contract,
            "ci": [gate.ci_lo, gate.ci_hi],
            "n": gate.n, "n_games": gate.n_games,
            "reasons": gate.reasons,
            "cost_sweep": gate.cost_sweep,
            "walkforward": gate.walkforward,
            "adversarial": {k: v.get("passed") for k, v in gate.adversarial.items()},
            "governance": gate.governance,
        },
    }


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--split", default="train",
                    choices=["train", "val", "nontest"])
    a = ap.parse_args(argv)
    print(json.dumps(run(a.split), indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
