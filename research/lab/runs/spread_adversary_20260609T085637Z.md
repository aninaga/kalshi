# Spread-anchoring adversary (RED TEAM) — 2026-06-09T08:56:37Z

**Lane:** spread / adversary. **Target claim:** the spread *continuation* anchoring
edge (`lab.strategies.anchoring.spread_anchoring`) is REAL-but-UNCERTIFIED,
"~+1.9c/ct net taker" (per `SPREADS_FINDINGS.md` REALISTIC RE-CHECK and
`ALPHA_FINDINGS.md`). **Job:** try to FALSIFY it with five independent checks,
all under the substrate's REALISTIC `FillModel` (listed-strike snap + half-spread +
full Polymarket taker-fee curve), honest i+1 latency, freshness guard ≤2 min,
direction pre-registered (`continuation`), test split untouched.

Hypothesis claimed: `f432bee31b1ca1fa` (adversary), agent `spread_adversary`.

## Baseline reproduction (substrate gate, nontest, n=1106 trades / 1106 games)

`lab.session.evaluate(..., ledger_path="research/reports/alpha/ledger.jsonl")`:

| cost | mean c/ct | block-bootstrap 95% CI lo | gate |
|---|---|---|---|
| 0c | **+1.09** | **−1.90** | FAIL |
| 1c | +0.09 | −2.90 | FAIL |
| 2c | −0.91 | −3.90 | FAIL |
| 3c | −1.91 | −4.90 | FAIL |
| 4c | −2.91 | −5.90 | FAIL |

Gate FAIL reasons (at 0c): `block_bootstrap_ci_lo −0.0190 ≤ 0`;
`knockout_drop_first_quarter_mean −0.0000 ≤ 0`; `season_split early_ci_lo=−0.0281,
late_ci_lo=−0.0333 (no overlap)`; `parity_split even_ci_lo=−0.0634,
odd_ci_lo=−0.0057 (no overlap)`.

> The substrate's realistic fill is **harsher** than the colleague's
> `spread_realistic.py` (+1.09c @0c here vs their reported +3.43c @0c; their
> central +1.88c was @1.5c half-spread). The substrate prices the full PM fee
> curve and snaps to the real listed strike, so the headline is even smaller —
> and the block-bootstrap CI lower bound is **negative even at zero extra cost.**

## The five falsification checks

1. **Stale-line / look-ahead artifact — `lab.audit`.** CLEAN. Static source-check of
   the live `entry` and `side` callables: no outcome-field references (`[]`, `[]`).
   Dynamic no-lookahead perturbation on the `anchoring_gap` signal: **40/40 panels
   clean** (no past bar moves when the future is truncated). The signal is honestly
   point-in-time; the edge is NOT a look-ahead artifact. → SURVIVES.

2. **Cost sweep 2–4c.** Mean falls monotonically +1.09 → −2.91c; **CI lo negative at
   every cost, including 0c (−1.90).** Breaks even ~+1c; not significantly profitable
   anywhere. → KILLS certification.

3. **OOS inversion — train vs val (clean holdout, test never touched).**
   - train n=900: **+0.99c**, CI [−2.22, +4.19], win 0.561
   - val   n=199: **+1.01c**, CI [−5.80, +7.76], win 0.573

   No dramatic sign-inversion (so it is not pure overfit), BUT **both halves are
   non-significant** (CI lo well below 0). The point estimate is real-but-tiny and
   does not reach significance out of sample. → does not certify.

4. **Cluster knockout by team (drop-one).** Base +1.09c. **All 30 single-team
   knockouts have CI lo ≤ 0;** 0/30 drive the mean ≤0 (so it is broad, not one team),
   but none is robustly positive. Dropping the most-favorable team (NYK, n=32) halves
   the mean to +0.55c (CI lo −2.44). → confirms no significant subset.

5. **Estimator-bias-vs-unconditional.** win_rate 0.564 vs avg price paid 0.520,
   gap **+0.044** (within the 0.05 tolerance). The conditional win rate genuinely
   exceeds the price paid — so this is a small GENUINE conditional effect, **not** a
   measurement/snapping artifact. → SURVIVES (the residual is real, just tiny).

## Verdict

Two checks (leakage, estimator-bias) SURVIVE — confirming the colleague's core point
that this is **not** a 0.50-fill / look-ahead / mispriced-snap artifact: the
direction is honest and a small (~+1c gross) genuine residual exists. But under the
substrate's realistic execution the edge does **NOT** survive the decisive
robustness checks: the block-bootstrap CI lower bound is **negative at every cost
including 0c**, the full promotion gate FAILS on bootstrap-CI + Q1-knockout +
season-split + parity-split, all 30 team knockouts have CI lo ≤ 0, and both train
and val are non-significant. The "+1.9c/ct" headline shrinks to **+1.09c @0c with a
negative CI lo** once the full PM fee curve and listed-strike snap are charged.

It is a real, honest, but **statistically insignificant and uncertifiable** residual
— not tradeable as a taker. Per the falsification clause in `SPREADS_FINDINGS.md`
("net-positive at 2c with a block-bootstrap CI lower bound > 0 … or it is DEAD"):
at 2c the mean is **−0.91c** with CI lo −3.90. The edge is DEAD as a taker; it would
require maker fills (to dodge the half-spread + 2% taker fee) or more seasons of data
to ever revisit.

## Reproduce
```bash
PYTHONPATH=. python3 - <<'PY'
from research.lab.session import lab
from research.lab.strategies.anchoring import spread_anchoring
L=lab(); s=spread_anchoring(continuation=True)
g=L.evaluate(s.run(L.load("spread",split="nontest")),
             ledger_path="research/reports/alpha/ledger.jsonl")
print(g.passed, g.cents_per_contract, g.ci_lo, g.cost_sweep, g.reasons)
PY
```

DEAD
