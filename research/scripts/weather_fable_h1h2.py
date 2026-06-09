"""fable_weather — two PRE-REGISTERED hypotheses on the weather (temp) family.

Registered + claimed (agent "fable_weather") BEFORE any scoring:

H1 id=135543e2b911f7a3  "favlong"
  mechanism: favorite-longshot miscalibration in thin daily-high tails —
             low-prob boundary outcomes are overpriced (lottery demand, lazy
             wide quoting), so the FAVORITE side of the mid-snapped boundary
             is underpriced, especially late in the window once the day's
             high is largely realized but quotes lag toward certainty.
  signal   : first fresh bar (stale<=2min, elapsed>=1h) where the favorite
             side quote p_fav at the mid-snapped boundary is in [0.85, 0.95].
  direction: buy_favorite_side (over if P(over)>=0.5 else under), hold to
             settlement.  Band fixed at registration from train CALIBRATION
             EDA only (no strategy PnL computed before registration).

H2 id=ac6c9c02b539c041  "driftfade"
  mechanism: intraday OVERREACTION — the explicitly-flagged, un-scored mirror
             of dead 8aabae5235c8ed07, scored as a NEW hypothesis: thin books
             overshoot on intraday moves; the move partially mean-reverts.
  signal   : implied-temp drift over last 5 bars, |drift| >= 0.75F, 1h
             warm-up (IDENTICAL signal to 8aabae5235c8ed07).
  direction: fade_the_drift (drift>0 -> under, drift<0 -> over).

Execution honesty (family conventions, weather_skeleton_20260609T192323Z):
  FillModel(venue="kalshi",
            half_spread=max(2 x median measured TRAIN bucket half-spread,
                            0.015))      # interior boundaries = multi-leg
  honest i+1 latency; freshness guard <= 2 stale minutes; never a 0.50 fill;
  TEST split never read.

Run::

    python -m research.scripts.weather_fable_h1h2 --hyp favlong   --split train
    python -m research.scripts.weather_fable_h1h2 --hyp favlong   --split nontest
    python -m research.scripts.weather_fable_h1h2 --hyp driftfade --split train
    python -m research.scripts.weather_fable_h1h2 --hyp driftfade --split nontest
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

LEDGER = "research/reports/alpha/ledger.jsonl"
MIN_ELAPSED = 3600.0   # 1h book warm-up (family convention)
LEG_MULT = 2.0

# ---- H1: favorite-longshot, pre-registered band ----------------------------
PFAV_LO, PFAV_HI = 0.85, 0.95
# Snap-stability guard (execution integrity, NOT signal tuning): FillModel
# re-snaps to the strike nearest mid at the i+1 fill ts. When mid sits near
# the midpoint of two boundaries, a tiny 1-minute move flips the snap and the
# side label ("under" vs the OTHER strike) executes the OPPOSITE exposure —
# a longshot buy instead of the registered favorite buy (found on train:
# 22.6% of fills landed >10c below the signal-bar favorite quote). Guard:
# only fire when mid is decisively nearest one strike, so the contract that
# fills IS the contract the hypothesis specifies. Uses bar-i info only (no
# look-ahead). Ratio fixed at 0.5 when the bug was found, before re-scoring.
SNAP_RATIO_MAX = 0.5

# ---- H2: drift fade, pre-registered params (identical to dead mirror) ------
K_BARS = 5
TAU_DEG = 0.75


def _snapped_quote(panel) -> np.ndarray:
    """P(over) at the boundary nearest panel.mid, per bar (matches FillModel snap)."""
    strikes = np.array(sorted(panel.ladder.keys()), dtype=float)
    mat = np.vstack([panel.ladder[s] for s in strikes])
    mid = np.asarray(panel.mid, dtype=float)
    out = np.full(panel.n, np.nan)
    ok = np.isfinite(mid)
    idx = np.flatnonzero(ok)
    if idx.size:
        si = np.argmin(np.abs(strikes[:, None] - mid[None, idx]), axis=0)
        out[idx] = mat[si, idx]
    return out


def _snap_stable(panel) -> np.ndarray:
    """True where mid is decisively nearest ONE strike (bar-i info only)."""
    strikes = np.array(sorted(panel.ladder.keys()), dtype=float)
    mid = np.asarray(panel.mid, dtype=float)
    out = np.zeros(panel.n, dtype=bool)
    ok = np.isfinite(mid)
    if not ok.any() or strikes.size < 2:
        return out
    d = np.abs(strikes[:, None] - mid[None, ok])      # (k, m)
    d.sort(axis=0)
    out[ok] = d[0] <= SNAP_RATIO_MAX * d[1]
    return out


def h1_entry(panel) -> np.ndarray:
    q = _snapped_quote(panel)
    pfav = np.where(q >= 0.5, q, 1.0 - q)
    band = np.isfinite(pfav) & (pfav >= PFAV_LO) & (pfav <= PFAV_HI)
    return band & _snap_stable(panel)


def h1_side(panel, i: int) -> str:
    return "over" if _snapped_quote(panel)[i] >= 0.5 else "under"


def _drift(panel) -> np.ndarray:
    mid = np.asarray(panel.mid, dtype=float)
    out = np.full_like(mid, np.nan)
    out[K_BARS:] = mid[K_BARS:] - mid[:-K_BARS]
    return out


def h2_entry(panel) -> np.ndarray:
    d = _drift(panel)
    return np.isfinite(d) & (np.abs(d) >= TAU_DEG)


def h2_side(panel, i: int) -> str:
    # FADE: positive drift -> under, negative drift -> over.
    return "under" if _drift(panel)[i] > 0 else "over"


HYPS = {
    "favlong": dict(hyp_id="135543e2b911f7a3", entry=h1_entry, side=h1_side,
                    name="weather.favlong_buy_favorite"),
    "driftfade": dict(hyp_id="ac6c9c02b539c041", entry=h2_entry, side=h2_side,
                      name="weather.drift_fade"),
}


def measured_half_spread(panels) -> float:
    vals = []
    for p in panels:
        hs = np.asarray(p.features.get("half_spread", []), dtype=float)
        vals.extend(hs[np.isfinite(hs)].tolist())
    return float(np.median(vals)) if vals else 0.015


def run(hyp: str, split: str, ledger: bool = True) -> dict:
    cfg = HYPS[hyp]
    train_panels = lab_data.load_panels(TEMP, split="train")
    half_bucket = measured_half_spread(train_panels)        # TRAIN only
    half_spread = max(LEG_MULT * half_bucket, 0.015)

    panels = train_panels if split == "train" else lab_data.load_panels(TEMP, split=split)
    strat = Strategy(
        name=cfg["name"], entry=cfg["entry"], side=cfg["side"], exit="settlement",
        min_elapsed=MIN_ELAPSED, max_elapsed=float("inf"), max_stale_min=2.0,
    )
    fills = FillModel(half_spread=half_spread, venue="kalshi")
    trades = strat.run(panels, fill_model=fills)

    gate = lab_eval.evaluate(trades, ledger_path=(LEDGER if ledger else None),
                             family="weather")
    df = trades.df()
    out = {
        "hyp": hyp, "hyp_id": cfg["hyp_id"], "split": split,
        "n_panels": len(panels), "n_trades": len(trades),
        "half_spread_used": half_spread,
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
            "adversarial": {k: v.get("passed") for k, v in gate.adversarial.items()},
            "governance": gate.governance,
        },
    }
    return out


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--hyp", required=True, choices=sorted(HYPS))
    ap.add_argument("--split", default="train", choices=["train", "val", "nontest"])
    ap.add_argument("--no-ledger", action="store_true",
                    help="diagnostic rerun: do not append to the trial ledger")
    a = ap.parse_args(argv)
    print(json.dumps(run(a.hyp, a.split, ledger=not a.no_ledger), indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
