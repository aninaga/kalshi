"""fable_forecast — PRE-REGISTERED forecast-vs-implied gap hypothesis (weather/temp).

Hypothesis id 3cb384c482d1c2f7, registered + claimed (agent "fable_forecast")
2026-06-09T22:54:16Z, BEFORE any strategy PnL was computed.

Mechanism (informational edge, first exogenous-field test in the program):
  early in the quote window (first third) the thin Kalshi daily-high market has
  not fully absorbed the numerical weather model's point-in-time daily-high
  forecast (``features["forecast_high_f"]``, Open-Meteo previous-runs, as-of
  joined on conservative ISSUE time — see
  research/lab/runs/data_forecast_high_f_20260609T214420Z.md). When the
  TRAIN-debiased forecast disagrees with the implied temperature by >= 2.0F,
  settlement resolves toward the forecast. Late window excluded by design: the
  market end-mid out-sharpens the forecast (MAE 1.53F vs 2.69F).

Pre-registered signal (all parameters frozen from TRAIN EDA before scoring):
  first bar i with elapsed_frac in [0.05, 0.30], stale_min[i] <= 2, finite mid
  and forecast, |gap_i| >= 2.0F where
      gap_i = (forecast_high_f[i] - bias_city) - mid[i]
      bias_city = TRAIN mean(last as-of forecast - realized settlement rep)
                = {AUS: -2.69, CHI: -1.71, DEN: -0.90, MIA: -2.08, NYC: -2.00}
  direction: buy TOWARD the forecast at the ATM-snapped strike
  ("over" if gap > 0 else "under"); entry i+1; hold to settlement.
  ATM-only so the FillModel strike re-snap at the i+1 fill is harmless (the
  snap-flip hazard documented in research/lab/runs/model_bakeoff_20260609.md
  only inverts exposure for band/strike-conditioned signals).

Execution honesty (family conventions):
  FillModel(venue="kalshi", half_spread=max(2 x 0.0067 measured TRAIN bucket
  median, 0.015) = 0.015); honest i+1 latency; freshness <= 2 stale minutes;
  never a 0.50 fill; TEST split never read.

Run::

    python -m research.scripts.weather_forecast_gap_e2e --split train --no-ledger
    python -m research.scripts.weather_forecast_gap_e2e --split nontest
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

HYP_ID = "3cb384c482d1c2f7"
LEDGER = "research/reports/alpha/ledger.jsonl"

# ---- pre-registered parameters (frozen 2026-06-09T22:54:16Z) ----------------
FRAC_LO, FRAC_HI = 0.05, 0.30      # entry window, fraction of quote-window duration
GAP_THRESH_F = 2.0                 # |debiased forecast - implied mid| threshold, degF
CITY_BIAS = {                      # TRAIN mean(last as-of forecast - realized), degF
    "AUS": -2.69, "CHI": -1.71, "DEN": -0.90, "MIA": -2.08, "NYC": -2.00,
}
HALF_SPREAD = 0.015                # max(2 x 0.0067 measured TRAIN bucket median, 0.015)


def debiased_gap(panel) -> np.ndarray:
    """gap_i = (forecast_high_f[i] - bias_city) - mid[i], NaN where unavailable."""
    fc = np.asarray(panel.features.get("forecast_high_f",
                                       np.full(panel.n, np.nan)), float)
    mid = np.asarray(panel.mid, float)
    b = CITY_BIAS.get(panel.home_team, 0.0)
    return (fc - b) - mid


def entry(panel) -> np.ndarray:
    n = panel.n
    dur = panel.duration_sec or float("nan")
    if not np.isfinite(dur) or dur <= 0:
        return np.zeros(n, dtype=bool)
    frac = np.asarray(panel.elapsed_sec, float) / float(dur)
    gap = debiased_gap(panel)
    return (np.isfinite(gap) & (np.abs(gap) >= GAP_THRESH_F)
            & (frac >= FRAC_LO) & (frac <= FRAC_HI))
    # staleness <= 2 min is enforced by Strategy(max_stale_min=2.0)


def side(panel, i: int) -> str:
    return "over" if debiased_gap(panel)[i] > 0 else "under"


def build_strategy() -> Strategy:
    return Strategy(
        name="weather.forecast_gap_toward",
        entry=entry, side=side, exit="settlement",
        entry_latency_min=1.0, max_stale_min=2.0,
        min_elapsed=0.0, max_elapsed=200000.0,
    )


def run(split: str, ledger: bool = True) -> dict:
    panels = lab_data.load_panels(TEMP, split=split)
    strat = build_strategy()
    fills = FillModel(venue="kalshi", half_spread=HALF_SPREAD)
    trades = strat.run(panels, fill_model=fills)
    gate = lab_eval.evaluate(trades, ledger_path=(LEDGER if ledger else None),
                             family="weather")
    df = trades.df()
    return {
        "hyp_id": HYP_ID, "split": split,
        "params": {"frac_lo": FRAC_LO, "frac_hi": FRAC_HI,
                   "gap_thresh_f": GAP_THRESH_F, "city_bias": CITY_BIAS,
                   "half_spread": HALF_SPREAD, "venue": "kalshi"},
        "n_panels": len(panels), "n_trades": len(trades),
        "by_city": (df.groupby("home_team").size().to_dict() if len(df) else {}),
        "by_side": (df.groupby("side").size().to_dict() if len(df) else {}),
        "avg_fill": (float(df["entry_price"].mean()) if len(df) else None),
        "win_rate": (float((df["payoff"] > 0.5).mean()) if len(df) else None),
        "gate": {
            "passed": gate.passed,
            "cents_per_contract": gate.cents_per_contract,
            "ci": [gate.ci_lo, gate.ci_hi],
            "n": gate.n, "n_games": gate.n_games,
            "reasons": gate.reasons,
            "cost_sweep": gate.cost_sweep,
            "walkforward": gate.walkforward,
            "adversarial": {k: v for k, v in gate.adversarial.items()},
            "governance": gate.governance,
        },
    }


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--split", default="train",
                    choices=["train", "val", "nontest"])
    ap.add_argument("--no-ledger", action="store_true",
                    help="diagnostic rerun: do not touch the trial ledger")
    a = ap.parse_args(argv)
    print(json.dumps(run(a.split, ledger=not a.no_ledger), indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
