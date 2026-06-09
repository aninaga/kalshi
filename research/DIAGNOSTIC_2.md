# DIAGNOSTIC_2 — Why are the NBA edge-hunts not PROMOTE-ing? (meta / data-quality)

_Date: 2026-06-09. Branch: `claude/hedge-fund-strategy-analysis-jyXWB`.
Author: meta/diagnostic + data-quality agent (lane 5). Follow-up to DIAGNOSTIC_1
(power audit) and the realistic-execution re-score (TOTALS_REFINE_FINDINGS).
No git, no test-split, no gate change._

## TL;DR (verdict)

The binding blocker is **NOT a shared bug, NOT gate miscalibration, and NOT
search-space exhaustion.** It is a **two-factor interaction** that DIAGNOSTIC_1
already isolated and the realistic-execution re-score then sharpened:

> **(b) the ~2–4¢ cost floor × (c) statistical power at the one season of n.**

Concretely: the only genuine residual mispricing in this data is the **anchoring
family** (totals + spreads pace/margin under-reaction). Everything else
(moneyline calibration, favorite-longshot, substitution-latency, fair-value-gap,
CLV, cross-contract term-structure, totals-extremes) is **market-efficient** —
negative/zero OOS point estimates that invert train→val. For the one real
family, the edge is small enough (~3–6¢ gross at the real listed strike, not the
fabricated 0.50 fill) that **realistic round-trip cost (1.5¢ half-spread + ~2%
PM taker ≈ 3.4¢) eats most of it**, and what survives is a moderate (~2–4¢ net)
edge that the near-zero-false-positive gate **cannot certify below n≈1000–1300**.

The recommendation is **CONTINUE (scoped)** — but the scope is narrow and the
honest ceiling is real. Pausing is not yet warranted; changing approach means
changing the *execution assumption* (maker fills), not the gate or the search.

## Evidence for the ranking

### Why NOT (d) a recurring harness/evaluator bug — ruled out

- **Leakage audit (this agent, today): CLEAN.** Every shipped `lab.signals`
  live signal — `pace_projection`, `anchoring_gap`, `implied_level`,
  `staleness_min`, `rolling`, `zscore` — passes both the static outcome-field
  source check and the dynamic no-lookahead perturbation test on real panels
  from **all three markets** (winner/total/spread) and a synthetic adversarial
  fixture, across 40 panels per market. `calibration_gap` is correctly exempt
  (label-style API that takes `realized` explicitly). The auditor was confirmed
  to **have teeth**: a deliberately leaky signal (reads `final_total`) flags
  statically, and a name-free dynamic leak (reversed cumsum) flags dynamically.
  See the companion run file `research/lab/runs/meta_diagnostic_*.md`.
- **DIAGNOSTIC_1 already audited the shared cost / latency / split handling and
  found it clean** (cost applied identically as `pnl − cost_c/100`; honest +1-bar
  latency everywhere; chronological splits; test ids never touched; the one
  historical PYTHONHASHSEED parity bug already fixed via sha256). No load-bearing
  scorer bug.
- The single historical "bug" that *did* manufacture a false pass — the 0.50
  at-the-money fill in the flat-cost evaluator — is a **cost/fill-model under-
  charge, not a leakage bug**, and it is exactly factor (b). It was caught and
  the totals edge was RETRACTED (TOTALS_REFINE_FINDINGS). The substrate now
  defaults to `lab.execution.REALISTIC` precisely to prevent its recurrence. So
  the one real methodological error in the program's history was a *cost* error,
  which is why (b) ranks first among the binding factors.

### Why NOT (e) gate miscalibration — ruled out (in the dangerous direction)

- DIAGNOSTIC_1's injection experiments: **false-positive rate = 0.0% at every n**
  for both PnL distributions (ATM std 0.50 and hold-to-settlement std 0.45). The
  gate does not pass noise. The dead directions were rejected because they had
  **no positive OOS signal**, not because an over-strict gate ate a real one.
- The gate IS miscalibrated in the *conservative* direction (massive false
  negatives on real moderate edges at small n: a true +6¢ edge passes only ~3%
  at n=270, ~63% at n=1300). That is factor (c), not a defect to "fix" — it is
  the price of the 0% FP rate, and weakening it is explicitly out of bounds.

### Why (b) the cost floor is the FIRST binding factor — the decisive new evidence

The realistic-execution re-score (TOTALS_REFINE_FINDINGS) is the cleanest natural
experiment in the whole program, because it changed *only the execution
assumption* and watched the verdict flip:

| edge | fill assumption | net ¢/ct | gate |
|---|---|---|---|
| totals pace-anchoring | fabricated 0.50 ATM, flat cost | +6.21 @2¢ | PASS (n=1297) |
| **same edge** | **real listed strike (~0.556) + 1.5¢ half-spread + ~2% PM taker** | **−1.00** | **FAIL @ every spread** |
| spreads margin-anchoring | real fill mid 0.5187 (not 0.50) | +1.88 taker / +3.43 @0¢ | FAIL (CI lo<0; n=267) |

The genuine micro-biases that exist elsewhere are all sub-floor by construction:
substitution under-reaction ≈+0.2¢; moneyline favorite-longshot ≈3pp in-sample
(and inverts OOS anyway); totals-extremes ≈2–5pp but living at extreme prices
where the un-modeled half-spread is widest (breaks even ~4–5¢, bias decays
Q2→Q4 = a spread/measurement artifact). **The real edges are smaller than the
real cost of trading them.** That is factor (b), and it is what separates a
statistical signal from a tradeable one.

### Why (c) power is the SECOND binding factor — and inseparable from (b)

What survives the cost floor (the spreads anchoring residual, ~+1.9¢ taker /
+3.4¢ gross) is a *moderate* edge, and DIAGNOSTIC_1's power table shows a moderate
edge is **uncertifiable at the available n**: the season-half × parity stability
sub-gates (four independent near-significance demands) need n≈1000–1300 to give a
real +6¢ edge even ~60% power, and far more for a +3¢ edge. Spreads at n=267 is
below that floor — "uncertified" there is NOT "fake." Totals certified only
because it was *both* (at the 0.50 fill) large AND broad AND at full-season n;
remove the 0.50 fill and even that large/broad/full-n edge fails on cost (b).

So (b) and (c) compound: cost shrinks every real edge toward the moderate band,
and the gate can only certify a moderate edge with n the single season does not
provide. Prior-season data does not exist (Kalshi/PM NBA markets launched
2025-26 — verified), so (c) cannot be relieved by "more history" this season.

### (a) Market efficiency — real, and the correct explanation for the dead majority

Moneyline (in-game and pregame/CLV) is picked clean; cross-contract term-structure
was falsified *at the source* (the total leg adds corr≈0.011 of win-relevant
info; the winner book's log-loss 0.440 beats the RV model's 0.516 — nothing to
reject). For these families the gate's DEAD verdict is correct. Efficiency
explains the *count* of dead hunts; (b)×(c) explain why the *one real family*
still hasn't PROMOTE'd.

## Note on fleet state at time of writing

There is **no `research/reports/alpha/ledger.jsonl` yet** and **no peer
`research/lab/runs/<lane>_*.md` verdicts have landed** (only README.md). This
diagnostic is therefore grounded in the durable historical record (ALPHA_FINDINGS,
DIAGNOSTIC_1, and the six FINDINGS docs: CLV, SPREADS, TERM_STRUCTURE,
TOTALS_EXTREMES, TOTALS_REFINE) rather than fresh ledger rows. Across that record
the tally is: **0 PROMOTE, 1 PROMISING-uncertified (spreads), 1 RETRACTED
(totals, on cost), the rest DEAD.** That is ≥3 consecutive non-PROMOTE outcomes,
which is the trigger condition for this diagnostic. When the ledger/peer runs
populate, re-confirm the ranking against them; the prediction is that new hunts
will keep landing DEAD (efficiency) unless they target the anchoring family with
**maker-fill execution**, which is the one untested lever.

## Recommendation: CONTINUE (scoped) — do NOT pause, do NOT weaken the gate

1. **Change the EXECUTION assumption, not the gate or the search.** The single
   highest-value move is to re-score the spreads (and totals) anchoring residual
   under **maker / limit-fill** execution — resting a limit at or inside the
   line to *earn* rather than *pay* the half-spread, and dodging the PM 2% taker
   where a maker rebate/zero-fee path exists. The +3.4¢ gross spreads residual is
   tradeable only if the ~3.4¢ taker round-trip is avoided. This directly tests
   whether the one real family is a *taker-killed* edge (b) vs a genuinely
   absent one (a). It is cheap labor and decisive either way.
2. **Build the full-season SPREADS population and re-score (no re-fit of the
   pre-registered `continuation` direction).** Relieves factor (c) for the one
   live candidate exactly as the full totals season did — predicted to certify
   near full n *iff* the residual point estimate holds AND maker fills keep it
   above the cost floor. Both #1 and #2 must hold; either alone is insufficient.
3. **Mark these families EXHAUSTED in the ledger:** moneyline calibration /
   favorite-longshot / substitution-latency / fair-value-gap, pregame CLV,
   cross-contract term-structure, totals-extremes. They are efficient
   (negative/zero OOS, mechanistically falsified) — re-mining them is wasted
   fleet capacity. Originate-lane agents (3,4) should be steered off them.
4. **Do NOT relax the season-half / parity stability sub-gates.** They are the
   source of the low power AND the source of the 0% false-positive rate — the
   project's entire value proposition versus the prior buggy "+$24/$41"
   headlines. The only defensible power levers are MORE n (build the spread
   season; future seasons) or a LARGER per-trade edge (maker fills; tighter
   anchoring conditioning), never a looser gate.
5. **PAUSE trigger (define it now, do not pull it yet):** if, after maker-fill
   re-scoring (#1) AND the full spread season (#2), the anchoring family still
   nets ≤0¢ or fails the gate, then the program has demonstrated the one real
   family is taker-killed and the rest are efficient — at which point the honest
   verdict is "no tradeable retail edge in 2025-26 NBA winner/total/spread data,"
   and the fleet should PAUSE rather than keep throwing darts (each new dart only
   raises the DSR multiple-testing hurdle via `lab.governance`, making
   certification *harder*, not easier).

**Honest ceiling (unchanged from DIAGNOSTIC_1, now cost-corrected):** with one
season (~1,319 games), a near-zero-FP gate, and realistic taker execution, only
an edge that is simultaneously **≳6¢ net-of-cost** and **broad-across-halves** at
full-season n can certify. After the realistic re-score, *nothing in this data is
both* — the anchoring family is the closest, and its fate now rests on maker
execution, not on more cleverness or more in-game mechanisms.
