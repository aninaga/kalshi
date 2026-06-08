"""Guard test for the diagnostic gate-calibration probe.

The probe (`research.scripts.gate_calibration_probe`) measures the promotion
gate's false-positive / power curve via a VECTORIZED re-implementation of the
gate's decisive criteria (for speed in a Monte-Carlo with thousands of evals).
This test pins that fast replica to the REAL `evaluate_trial` so it can never
silently drift — if someone changes the gate, this fails until the probe is
updated, keeping DIAGNOSTIC_1.md's power numbers honest.
"""
from __future__ import annotations

import numpy as np

import research.scorer.bootstrap as _bs
from research.scorer.promotion_gate import evaluate_trial
from research.scripts.gate_calibration_probe import _make, gate_pass


def test_fast_replica_matches_real_gate():
    # Match the real gate's resample count for an apples-to-apples comparison.
    orig = _bs.block_bootstrap_by_game.__defaults__
    _bs.block_bootstrap_by_game.__defaults__ = (2000, orig[1], orig[2])
    try:
        cases = [
            ("atm", 200, 0.0), ("atm", 270, 6.0), ("atm", 600, 4.0),
            ("atm", 1300, 2.0), ("atm", 600, 0.0),
            ("ml", 270, 6.0), ("ml", 600, 4.0), ("ml", 1300, 2.0),
        ]
        agree = 0
        total = 0
        for gen, n, mu in cases:
            for r in range(3):
                seed = 90_000 + total
                rng_fast = np.random.default_rng(seed)
                pnl, dates, home, prim = _make(n, gen, mu, rng_fast)
                # Re-draw the SAME synthetic trial for the real gate.
                rng_real = np.random.default_rng(seed)
                pnl2, dates2, home2, prim2 = _make(n, gen, mu, rng_real)
                # gate_pass consumes additional rng draws for its bootstraps;
                # give it a fresh stream seeded identically to the probe's loop.
                rng_boot = np.random.default_rng(seed)
                _make(n, gen, mu, rng_boot)  # advance past the data draws
                fast = gate_pass(pnl, dates, home, prim, 2000, rng_boot)
                dec = evaluate_trial(
                    pnl2, np.array([f"g{i}" for i in range(n)]),
                    dates2, home2, prim2, 1, rng_seed=42,
                )
                total += 1
                agree += int(fast == dec.passed)
        # Bootstrap noise can flip at most a borderline cell; require near-total
        # agreement (the manual --verify run was 30/30).
        assert agree >= total - 1, f"replica drifted from gate: {agree}/{total}"
    finally:
        _bs.block_bootstrap_by_game.__defaults__ = orig
