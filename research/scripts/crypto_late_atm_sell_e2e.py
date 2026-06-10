"""fable_crypto1 — late-window ATM premium sell (crypto/coin_price), PRE-REGISTERED.

LANE: first hypothesis of the crypto family (hourly KXBTCD/KXETHD threshold
ladders; provider research/lab/providers/crypto.py). The walking skeleton for
the family: no engineered feature, an unconditional side, one entry window —
it exercises ladder -> ATM snap -> FillModel -> settlement -> gate end to end.

Pre-registered hypothesis (frozen BEFORE any gate scoring; registry id below):
as the hourly deadline approaches, the nearest-to-money threshold binary keeps
too much probability mass on a late cross (deadline compression / longshot
premium on the just-OTM "$X or above" lottery leg). TRAIN-only EDA (bar-level
calibration of the ATM-snapped strike, fresh quotes only) showed mean(p - y)
rising monotonically into the close: +2.2c at ttc 6-10 min, +3.3c at 3-6,
+2.4c at 0-3; per-event (first fresh bar with ttc in [2,6) min) +3.09c,
se 1.36c, n=708. Strategy: at the FIRST bar with time-to-close in [2,6)
minutes, quote fresh (stale <= 2 min) and finite implied mid, SELL the
ATM-snapped threshold ("under"), entry latency 1 min (fill re-snaps ATM at
i+1 — correct for an ATM strategy, no strike pinning needed), hold to
settlement. FillModel(venue="kalshi", half_spread=max(2 x 0.0061 measured
TRAIN median, 0.015) = 0.015).

Side is UNCONDITIONAL (always "under" on whatever strike is ATM at fill), so
the i+1 ATM re-snap cannot invert exposure (the snap-flip hazard of
runs/model_bakeoff_20260609.md applies only to quote-level-conditioned sides).

Window note: all cached BTC events are even-hour ~1h windows (stride 2 skips
the 25h 17:00-EDT daily book); entry conditions on TIME-TO-CLOSE, not elapsed
fraction, per the family run note.

Run::

    PYTHONPATH=. python3 -m research.scripts.crypto_late_atm_sell_e2e --register
    PYTHONPATH=. python3 -m research.scripts.crypto_late_atm_sell_e2e --split train
    PYTHONPATH=. python3 -m research.scripts.crypto_late_atm_sell_e2e --split nontest
"""
from __future__ import annotations

import argparse
import json

import numpy as np

from research.lab import data as lab_data
from research.lab import evaluate as lab_eval
from research.lab import hypothesis as lab_hyp
from research.lab.execution import FillModel
from research.lab.providers.crypto import COIN_PX
from research.lab.strategy import Strategy
from research.lab.types import Hypothesis

HYP_ID = "f0dcfdc43c86fd58"          # registered 2026-06-10T04:57:42Z, agent fable_crypto1
AGENT = "fable_crypto1"
LEDGER = "research/reports/alpha/ledger.jsonl"
FAMILY = "crypto"

# ---- pre-registered strategy parameters (frozen before scoring) -------------
TTC_LO_MIN = 2.0    # signal-bar time-to-close window, minutes (inclusive low)
TTC_HI_MIN = 6.0    # (exclusive high) — fills land ~[1,5) after the 1-min latency
HALF_SPREAD = 0.015  # max(2 x 0.0061 measured TRAIN median half-spread, 0.015)
VENUE = "kalshi"


def hypothesis() -> Hypothesis:
    return Hypothesis(
        market=COIN_PX,
        mechanism=(
            "Deadline compression / longshot premium on near-dated threshold "
            "binaries: as the hourly settlement approaches, the remaining-move "
            "distribution narrows faster than the quoted ATM threshold "
            "converges, so the nearest-to-money '$X or above' leg keeps too "
            "much probability mass on a late cross. Sellers of the ATM "
            "threshold in the last minutes collect the premium."),
        signal_desc=(
            "First bar with time-to-close in [2,6) minutes, quote fresh "
            "(mid stale <= 2 min) and finite implied mid. No feature, no "
            "quote-level condition."),
        direction=(
            "SELL (side='under') the ATM-snapped threshold at i+1 (1-min "
            "latency, ATM re-snap at fill), hold to settlement. "
            "FillModel(venue=kalshi, half_spread=0.015)."),
    )


# --------------------------------------------------------------------------- #
# strategy
# --------------------------------------------------------------------------- #
def entry(panel) -> np.ndarray:
    n = panel.n
    dur = panel.duration_sec or float("nan")
    if not np.isfinite(dur) or dur <= 0:
        return np.zeros(n, dtype=bool)
    ttc_min = (float(dur) - np.asarray(panel.elapsed_sec, float)) / 60.0
    mid = np.asarray(panel.mid, float)
    return np.isfinite(mid) & (ttc_min >= TTC_LO_MIN) & (ttc_min < TTC_HI_MIN)
    # staleness <= 2 min enforced by Strategy(max_stale_min=2.0)


def side(panel, i: int) -> str:   # noqa: ARG001 — unconditional by design
    return "under"


def build_strategy() -> Strategy:
    return Strategy(
        name="crypto.late_atm_premium_sell",
        entry=entry, side=side, exit="settlement",
        entry_latency_min=1.0, max_stale_min=2.0,
        min_elapsed=0.0, max_elapsed=200000.0,   # window handled by ttc in entry()
    )


def run(split: str, ledger: bool = True) -> dict:
    panels = lab_data.load_panels(COIN_PX, split=split)
    strat = build_strategy()
    fills = FillModel(venue=VENUE, half_spread=HALF_SPREAD)
    trades = strat.run(panels, fill_model=fills)
    gate = lab_eval.evaluate(trades, ledger_path=(LEDGER if ledger else None),
                             family=FAMILY)
    df = trades.df()
    return {
        "hyp_id": HYP_ID, "split": split,
        "params": {"ttc_lo_min": TTC_LO_MIN, "ttc_hi_min": TTC_HI_MIN,
                   "half_spread": HALF_SPREAD, "venue": VENUE,
                   "side": "under (unconditional)"},
        "n_panels": len(panels), "n_trades": len(trades),
        "by_cluster": (df.groupby("home_team").size().to_dict() if len(df) else {}),
        "avg_fill": (float(df["entry_price"].mean()) if len(df) else None),
        "win_rate": (float((df["payoff"] > 0.5).mean()) if len(df) else None),
        "gross_c": (float((df["payoff"] - df["entry_price"]).mean() * 100)
                    if len(df) else None),
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
    ap.add_argument("--register", action="store_true",
                    help="register + claim the hypothesis (idempotent; prints id)")
    ap.add_argument("--split", default=None, choices=["train", "val", "nontest"])
    ap.add_argument("--no-ledger", action="store_true",
                    help="diagnostic rerun: do not touch the trial ledger")
    a = ap.parse_args(argv)
    if a.register:
        h = lab_hyp.register(hypothesis())
        h = lab_hyp.claim(h.id, AGENT)
        print(json.dumps({"id": h.id, "status": h.status,
                          "created": h.created}, indent=2))
    if a.split:
        print(json.dumps(run(a.split, ledger=not a.no_ledger),
                         indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
