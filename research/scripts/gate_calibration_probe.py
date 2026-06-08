"""Diagnostic: measure the promotion gate's false-positive (NULL) and
false-negative (known-SIGNAL) rates at realistic n, for the PnL distributions
the evaluators actually produce.

Settles the central diagnostic question: is the recurring
"positive-in-sample -> inverts/fails OOS + stability" pattern (a) genuine
overfitting the gate correctly catches, or (b) gate miscalibration producing
false negatives on real edges?

Generators match the evaluators' real trade shapes (verified empirically:
both families have per-trade std ~0.45-0.50, so the gate's power is comparable
across them):

  ATM (totals/spreads): pnl = payoff-0.5, payoff in {0,1}, P(win)=0.5+mu.
       pnl in {-0.5,+0.5}, std=0.5.
  ML  (moneyline CLV/term-structure): entry ~ U(0.15,0.85); true win prob =
       entry+mu; pnl = y-entry. std ~0.45 (matches clv_final dumps).

For each (generator, true edge mu_cents, n_games) we draw `reps` synthetic
trials (one trade/game), run a FAITHFUL re-implementation of the promotion
gate's DECISIVE criteria, and report PASS-rate. mu=0 -> false-positive rate;
mu>0 -> power (1-false-negative).

This re-implements the gate's bootstrap with a VECTORIZED exact equivalent (a
game has >=1 trade; with one-trade-per-game, the by-game block bootstrap mean
of a resample = mean of `n` iid draws with replacement from the per-game pnl),
plus the season-half and parity CI-overlap checks and the concentration caps.
It is validated against the real `evaluate_trial` on a handful of cells
(--verify) so the Monte-Carlo numbers are trustworthy.

Usage:
    python3 -m research.scripts.gate_calibration_probe --reps 400 --resamples 2000
    python3 -m research.scripts.gate_calibration_probe --verify
"""
from __future__ import annotations

import argparse
import hashlib
import sys
from pathlib import Path

import numpy as np

_ROOT = Path(__file__).resolve().parents[2]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

TEAMS = [
    "ATL", "BOS", "BKN", "CHA", "CHI", "CLE", "DAL", "DEN", "DET", "GSW",
    "HOU", "IND", "LAC", "LAL", "MEM", "MIA", "MIL", "MIN", "NOP", "NYK",
    "OKC", "ORL", "PHI", "PHX", "POR", "SAC", "SAS", "TOR", "UTA", "WAS",
]
_MIN_N = 200
_GAME_CAP = 0.20
_TEAM_CAP = 0.25


def _boot_ci(pnl, n_resamples, ci, rng):
    """Block-bootstrap-by-game CI when there is exactly ONE trade per game
    (the case for every evaluator here). Then a by-game resample is just an
    iid resample of `pnl`, and the resampled mean is the mean of n draws.
    Vectorized: draw (n_resamples, n) index matrix in one shot."""
    n = len(pnl)
    if n == 0:
        return float("nan"), float("nan")
    idx = rng.integers(0, n, size=(n_resamples, n))
    means = pnl[idx].mean(axis=1)
    a = (1.0 - ci) / 2.0
    return float(np.percentile(means, 100 * a)), float(np.percentile(means, 100 * (1 - a)))


def _cis_overlap(lo1, hi1, lo2, hi2):
    if not all(np.isfinite([lo1, hi1, lo2, hi2])):
        return False
    return max(lo1, lo2) <= min(hi1, hi2)


def _parity(teams):
    return np.array([
        int.from_bytes(hashlib.sha256(str(t).encode()).digest()[:8], "big") & 1
        for t in teams
    ])


def gate_pass(pnl, dates, home, prim, n_resamples, rng):
    """Faithful re-implementation of the DECISIVE gate criteria."""
    n = len(pnl)
    # Gate 1+2: main CI > 0 and n >= 200
    lo, hi = _boot_ci(pnl, n_resamples, 0.95, rng)
    if not (np.isfinite(lo) and lo > 0):
        return False
    if n < _MIN_N:
        return False
    # Gate 4: season split (median date) — both 95% CI lo > 0 and 80% overlap
    med = np.median(dates.astype("datetime64[s]").astype("int64"))
    em = dates <= np.datetime64(int(med), "s")
    e_lo, e_hi = _boot_ci(pnl[em], n_resamples, 0.95, rng)
    l_lo, l_hi = _boot_ci(pnl[~em], n_resamples, 0.95, rng)
    e80lo, e80hi = _boot_ci(pnl[em], n_resamples, 0.80, rng)
    l80lo, l80hi = _boot_ci(pnl[~em], n_resamples, 0.80, rng)
    if not (np.isfinite(e_lo) and np.isfinite(l_lo) and e_lo > 0 and l_lo > 0
            and _cis_overlap(e80lo, e80hi, l80lo, l80hi)):
        return False
    # Gate 5: parity split
    par = _parity(home)
    pm = par == 0
    p_lo, _ = _boot_ci(pnl[pm], n_resamples, 0.95, rng)
    q_lo, _ = _boot_ci(pnl[~pm], n_resamples, 0.95, rng)
    p80lo, p80hi = _boot_ci(pnl[pm], n_resamples, 0.80, rng)
    q80lo, q80hi = _boot_ci(pnl[~pm], n_resamples, 0.80, rng)
    if not (np.isfinite(p_lo) and np.isfinite(q_lo) and p_lo > 0 and q_lo > 0
            and _cis_overlap(p80lo, p80hi, q80lo, q80hi)):
        return False
    # Gate 3: cluster knockouts. One trade/game -> drop-top-games removes the
    # ceil(5%) games by |pnl|; drop-top-team removes the |sum|-max team; drop
    # first-quarter removes earliest 25% by date. Each must keep mean > 0.
    n_drop = max(1, int(np.ceil(n * 0.05)))
    order = np.argsort(-np.abs(pnl))
    keep = np.ones(n, bool); keep[order[:n_drop]] = False
    if not (keep.any() and pnl[keep].mean() > 0):
        return False
    ut, inv = np.unique(prim, return_inverse=True)
    tsum = np.zeros(len(ut)); np.add.at(tsum, inv, pnl)
    topt = int(np.argmax(np.abs(tsum)))
    kt = inv != topt
    if not (kt.any() and pnl[kt].mean() > 0):
        return False
    di = dates.astype("datetime64[s]").astype("int64")
    cut = np.percentile(di, 25.0)
    kl = di > cut
    if not (kl.any() and pnl[kl].mean() > 0):
        return False
    # Gate 7: concentration
    tot = np.abs(pnl).sum()
    if tot > 0:
        ug, gi = np.unique(np.arange(n), return_inverse=True)  # one trade/game
        if np.abs(pnl).max() / tot > _GAME_CAP:
            return False
        tabs = np.zeros(len(ut)); np.add.at(tabs, inv, np.abs(pnl))
        if tabs.max() / tot > _TEAM_CAP:
            return False
    return True


def _make(n, gen, mu, rng):
    if gen == "atm":
        p = 0.5 + mu / 100.0
        pnl = np.where(rng.random(n) < p, 0.5, -0.5)
    else:  # ml
        entry = rng.uniform(0.15, 0.85, size=n)
        pwin = np.clip(entry + mu / 100.0, 0.001, 0.999)
        pnl = (rng.random(n) < pwin).astype(float) - entry
    dates = np.datetime64("2025-10-21") + np.sort(rng.integers(0, 176, n)).astype("timedelta64[D]")
    home = rng.choice(TEAMS, n)
    prim = rng.choice(TEAMS, n)
    return pnl, dates, home, prim


def pass_rate(gen, n, mu, reps, resamples, seed0):
    passes = 0
    for r in range(reps):
        rng = np.random.default_rng(seed0 + r * 7919)
        pnl, dates, home, prim = _make(n, gen, mu, rng)
        passes += int(gate_pass(pnl, dates, home, prim, resamples, rng))
    return passes / reps


def verify():
    """Cross-check the fast gate replica against the real evaluate_trial."""
    import research.scorer.bootstrap as bs
    bs.block_bootstrap_by_game.__defaults__ = (2000, 0.95, 42)
    from research.scorer.promotion_gate import evaluate_trial
    print("VERIFY: fast-replica vs real evaluate_trial (10 cells)")
    agree = 0; tot = 0
    for gen in ("atm", "ml"):
        for n, mu in [(200, 0), (270, 6), (600, 4), (1300, 2), (600, 0)]:
            for r in range(3):
                rng = np.random.default_rng(50_000 + tot)
                pnl, dates, home, prim = _make(n, gen, mu, rng)
                rng2 = np.random.default_rng(50_000 + tot)  # same draws into replica
                fast = gate_pass(pnl, dates, home, prim, 2000, rng2)
                dec = evaluate_trial(pnl, np.array([f"g{i}" for i in range(n)]),
                                     dates, home, prim, 1, rng_seed=42)
                real = dec.passed
                tot += 1; agree += int(fast == real)
                if fast != real:
                    print(f"  MISMATCH gen={gen} n={n} mu={mu} r={r}: fast={fast} real={real}")
    print(f"  agreement: {agree}/{tot}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--reps", type=int, default=400)
    ap.add_argument("--resamples", type=int, default=2000)
    ap.add_argument("--seed0", type=int, default=1000)
    ap.add_argument("--verify", action="store_true")
    a = ap.parse_args()
    if a.verify:
        verify(); return

    print(f"reps={a.reps} resamples={a.resamples} "
          f"(mu=0 -> false-positive rate; mu>0 -> power=1-FN)\n", flush=True)
    ns = [200, 270, 600, 1000, 1300]
    mus = [0.0, 2.0, 4.0, 6.0, 8.0]
    for gen in ("atm", "ml"):
        print(f"===== generator={gen} =====", flush=True)
        print(f"{'n':>6} | " + " | ".join(f"mu={m:.0f}c" for m in mus), flush=True)
        for n in ns:
            cells = [f"{pass_rate(gen,n,mu,a.reps,a.resamples,a.seed0)*100:5.1f}%" for mu in mus]
            print(f"{n:>6} | " + " | ".join(cells), flush=True)
        print(flush=True)


if __name__ == "__main__":
    main()
