# DIAGNOSTIC_1 — Why aren't the NBA edge-hunts surviving? (gate power audit)

_Date: 2026-06-08. Branch: `claude/hedge-fund-strategy-analysis-jyXWB`.
Author: diagnostic agent (broad authority — read all code/data/findings,
re-ran the certified edge, injected known-NULL and known-SIGNAL through the
gate, audited the shared cost/latency/split handling)._

## TL;DR (verdict)

- **Pattern verdict: the recurring "positive in-sample → inverts/fails OOS" is
  GENUINE OVERFITTING / market efficiency that the gate is correctly catching —
  NOT a shared bug and NOT gate miscalibration in the false-positive direction.**
  The dead directions (CLV, term-structure RV, totals favorite-longshot,
  moneyline calibration) all have **negative-or-zero OOS point estimates** and
  invert train→val. That is the signature of no edge, not of a gate eating a
  real one. The term-structure direction was even falsified *at the source*
  (the total leg adds corr≈0.011 of win-relevant info; the winner book's
  log-loss 0.440 beats the model's 0.516) — there is nothing there to reject.
- **Bug found? NO shared methodological bug.** Cost is applied identically and
  correctly across every evaluator (`pnl - cost_c/100`, flat sweep); honest
  +1-bar entry latency and the freshness guard are present and consistent;
  the train/val/test split handling is clean and the test ids are never
  touched. The one historical reproducibility bug (PYTHONHASHSEED in the parity
  split) was already fixed (sha256). I found and fixed nothing load-bearing in
  the scorer — it is sound.
- **BUT the gate is severely UNDER-POWERED at small n, and the binding
  constraint is the season-half + parity STABILITY sub-gates, not the main CI.**
  Injecting a *known real* +6¢/contract edge (the same magnitude as certified
  totals) passes the full gate only **3% of the time at n=270, 19% at n=600,
  and ~63% at n=1300**. The main block-bootstrap CI passes ~83% at n=600; the
  stability splits drag it to 18%. This is *correct conservatism* (false-
  positive rate is ~0% everywhere), but it means **a genuinely real moderate
  edge is UNCERTIFIABLE below n≈1000-1300.** That is exactly why totals
  certified only after the full season (n=1297) and why spreads at n=267 cannot
  yet — and it means "spreads uncertified at n=267" is NOT evidence the spread
  edge is fake.

## Binding constraint — ranked, with evidence

**1. (DECISIVE) Statistical power at the available n — the gate's stability
sub-gates need n≈1000-1300 to certify a real moderate edge.**
Monte-Carlo over the gate (faithful vectorized replica, validated 30/30 vs the
real `evaluate_trial`; 400 reps, 2000 resamples), at-the-money generator
(matches totals/spreads, per-trade std 0.50), pass-rate = power:

| n | μ=0¢ (FP) | μ=2¢ | μ=4¢ | μ=6¢ | μ=8¢ |
|---|---|---|---|---|---|
| 200 | 0.0% | 0.0% | 0.5% | 1.5% | 5.5% |
| 270 | 0.0% | 0.2% | 0.8% | 3.2% | 11.0% |
| 600 | 0.0% | 0.2% | 4.5% | 18.5% | 42.2% |
| 1000 | 0.0% | 0.2% | 12.2% | 42.8% | 76.8% |
| 1300 | 0.0% | 2.5% | 18.2% | 57.0% | 82.8% |

Moneyline hold-to-settlement generator (CLV/term-structure shape, std ≈0.45) —
same shape, 0% FP, power rises with n (slightly higher than ATM because the
uniform entry-price draw lowers effective variance):

| n | μ=0¢ (FP) | μ=2¢ | μ=4¢ | μ=6¢ | μ=8¢ |
|---|---|---|---|---|---|
| 200 | 0.0% | 0.0% | 0.8% | 4.5% | 13.0% |
| 270 | 0.0% | 0.0% | 2.8% | 7.8% | 18.2% |
| 600 | 0.0% | 0.0% | 5.8% | 29.5% | 60.2% |
| 1000 | 0.2% | 1.2% | 19.2% | 56.0% | 82.5% |

(ML n=1300 not run to completion — the 500s wall-clock cap hit the last cell;
the curve is identical in shape to ATM and to ML n≤1000, so it adds nothing.)

Gate-stage attribution (which sub-gate kills power), 300 reps:

| n, μ | main CI>0 | season-half | parity | all three |
|---|---|---|---|---|
| 270, 6¢ | 47% | 10% | 10% | 6% |
| 600, 6¢ | 83% | 27% | 30% | 18% |
| 1300, 6¢ | 99% | 71% | 75% | 63% |
| 1300, 8¢ | 100% | 90% | 91% | 83% |

The **season-half and parity-split stability checks are the binding gate** — each
splits the sample in half and requires *each half's* independent 95% one-sided
bootstrap CI lower bound > 0 (four independent near-significance demands). The
main CI is not the bottleneck. This is why a real edge needs ~full-season n.

**2. The ~2-4¢ cost floor relative to the size of the real micro-biases.**
The genuine in-sample effects that exist (substitution under-reaction ≈+0.2¢,
moneyline favorite-longshot ≈3pp, extreme-tail totals bias ≈2-5pp) are either
below the cost floor or live entirely at extreme prices where the un-modeled
half-spread eats them (the totals-extremes "edge" breaks even at ~4-5¢ inside a
realistic extreme-price spread, and its bias decays Q2→Q4 — a spread/measurement
artifact, not alpha). Combined with #1, only an edge that is BOTH large (≳6¢)
AND broad (so it survives the stability halving) at full-season n can certify.

**3. Market efficiency for the dead mechanisms (real, but secondary).** Moneyline
(in-game and pregame) is picked clean; cross-contract RV is mechanistically
falsified at the source. These are genuinely efficient — the gate's rejection is
correct, not a false negative.

**4. (NOT binding) Data thinness in the absolute sense.** 1,319 games is enough
to certify a ≳6¢ broad edge (totals proves it). It is *not* enough to certify a
real but smaller (~3-4¢) or narrower edge — but that is constraint #1 restated,
and prior-season data does not exist (markets launched 2025-26).

**5. (NOT binding) Search space.** The "winner+total" data has now been mined
across moneyline (calibration, sub-latency, fair-value, CLV), cross-contract RV,
and totals (pace, extremes). The one less-watched market that paid off was
totals; spreads (also less-watched, same anchoring mechanism) is the live
candidate. The search space is reasonable; the issue is power at low n.

## The injection experiments (the core diagnostic)

- **Known-NULL (μ=0):** false-positive rate is **0.0%** at every n for both PnL
  distributions. The gate does not pass noise. So the dead directions were not
  rejected by an over-strict-on-noise gate accidentally — there was no positive
  OOS signal to begin with.
- **Known-SIGNAL (μ>0):** power table above. A real +6¢ edge is rejected 97% of
  the time at n=270 — so **the gate produces massive FALSE NEGATIVES on real
  moderate edges at small n.** This is the only "miscalibration" found, and it
  is in the conservative direction (favoring efficiency, never manufacturing a
  pass). It explains every "promising but uncertified at small n" result.
- **Certified totals re-run through its own pipeline (reproduced):**
  full population n=1297, **gate PASS @0/1/2¢** (CI @2¢ [+3.59,+8.83]),
  fails @3¢ on sub-split stability; monthly walk-forward **7/9 months positive**
  (every regular-season month; only sparse May/Jun negative). Matches
  ALPHA_FINDINGS.md exactly. The certified edge is real and reproducible.
- **PnL-distribution equivalence:** the moneyline hold-to-settlement trades
  (CLV dumps: per-trade std ≈0.45) and the at-the-money totals/spread trades
  (std =0.50) have **comparable variance**, so the gate's power is comparable
  across both families. The dead-vs-alive split is driven by the true OOS edge
  sign/size, NOT by a distribution-specific gate handicap.

## Is the survivor (totals pace-anchoring) trustworthy? — Yes, with two honest caveats

- It reproduces; it is broad (top-team |PnL| ≤5%, well under caps); it passes
  every stability sub-gate at full n; the at-the-money baseline is independently
  confirmed calibrated (P(over)≈0.502 unconditional, re-verified by the
  totals-extremes study). It is not a concentration or a stale-line artifact
  (freshness guard cuts +16.7→+9.5¢ and a substantial edge survives on fresh
  quotes).
- **Caveat A (modeled, not eliminated):** the "full-season pre-registered" gate
  includes train games; its OOS-ness rests on the direction being a 1-bit
  mechanism pre-registration plus the monthly walk-forward — a real but weaker
  form of OOS than a held-out split. The walk-forward (7/9) is the strongest
  available out-of-sample evidence given one season exists.
- **Caveat B (the real risk to capital):** execution is a flat cost sweep at an
  ATM ≈0.50 fill. The edge breaks even at ~5-6¢; the actual Polymarket ~2% fee +
  ATM spread must be confirmed against live fills before sizing.

The spreads edge (PROMISING, +4.18¢@2¢ at n=267) is the **same anchoring
mechanism**; per the power table it is simply below the n needed to certify, not
demonstrated-fake. Building the full spread season is the correct next move.

## Bug audit (shared cost / latency / split handling) — clean

- **Cost:** every evaluator does `pnl_prob - cost_c/100` and sweeps {0..4}¢
  identically. The realistic-fills/cost_profile harness is NOT in the alpha path
  (the evaluators use the flat sweep), so there is no divergent cost application.
- **Latency:** all evaluators lock entry one bar after the signal (`entry_lat_min=1`,
  interpolated entry price). Consistent and honest.
- **Splits:** chronological train(919)/val(199)/test(192). The dead-direction
  "FULL ex-test" populations correctly exclude test ids; `splits.json` test ids
  are untouched. No leakage found (season-half uses the median date of fired
  trades, computed in-sample to the evaluation set — not a look-ahead into test).
- **Scorer:** block bootstrap, knockouts (abs-based, the documented Phase-1 fix),
  parity (sha256, the documented PYTHONHASHSEED fix), DSR informational-only
  (confirmed it never appends to `reasons`). No bug. **No code change made** —
  weakening the stability gates to raise power would raise the false-positive
  rate and is explicitly out of bounds.

## Recommendation: CONTINUE (scoped) — do NOT pause, do NOT weaken the gate

1. **Finish the full-season SPREADS build and re-score.** It is the highest-value
   cheap labor: same certified anchoring mechanism, and the power table predicts
   it is uncertifiable at n=267 but should certify near full n IF the +4¢ point
   estimate holds. This directly tests the "anchoring family generalizes"
   hypothesis. (Do not re-fit direction; keep the pre-registered continuation.)
2. **Refine the certified totals edge for SIZE, not for new mechanisms.** A larger
   per-trade edge (e.g. quarter-conditioning, tighter freshness, team-pace priors)
   both improves capital efficiency and — per the power table — is far easier to
   keep certified. Harden execution realism (the real ATM spread + 2% PM fee) —
   this is the binding risk to actually trading it.
3. **Stop mining moneyline / cross-contract / extreme-tail mechanisms.** They are
   efficient (negative/zero OOS, not gate artifacts); revisiting them is wasted
   labor. The ledger should mark these families exhausted.
4. **Do NOT relax the stability sub-gates.** They are the source of the low power,
   but they are also why the false-positive rate is ~0% — the project's whole
   value proposition vs the prior buggy "+$24/$41" headlines. The correct lever
   for power is MORE n (build the full spread season; future seasons) or a LARGER
   edge (refine totals), never a looser gate. If a power lever is ever wanted, the
   only defensible one is reporting the walk-forward as the primary OOS evidence
   (as totals already does) rather than loosening the CI thresholds.

**Honest ceiling:** with one season (~1300 games) and a near-zero-FP gate, only
edges that are simultaneously ≳6¢/contract and broad-across-halves can be
certified. The anchoring family (totals certified, spreads pending) is the one
that clears that bar; everything else in this data is efficient or sub-floor.

## Reproduce

```bash
python3 -m research.scripts.gate_calibration_probe --verify          # 30/30 replica agreement
python3 -m research.scripts.gate_calibration_probe --reps 400 --resamples 2000
python3 -m research.scripts.totals_walkforward --thresh 6 --max-stale-min 2  # certified edge PASS@2c
```
