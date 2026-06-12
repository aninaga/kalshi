# total / ORIGINATE — Asymmetric pace under-reaction (the UNDER side)

_Date: 2026-06-09. Branch: claude/hedge-fund-strategy-analysis-jyXWB.
Agent: total_originate_opus. Lane 3 (total/originate). Hypothesis id `1bbab98218dbbe5c`.
Fills: REALISTIC throughout (listed strike, 1.5c half-spread, Polymarket 2% taker fee).
Splits: train/val/nontest only. Test split never read._

## The EDA observation it is grounded in (`lab.eda.scan_market('total','train')`)

The idea-agnostic scan surfaced a **sign asymmetry in the realized-vs-line residual**
across the anchoring-gap distribution that the certified pace study never split out.
Binning fresh (≤2 min) in-window quotes by `anchoring_gap = pace_projection − mid`:

| gap bucket | n | mean gap | P(over) | residual vs ATM 0.5 |
|---|---|---|---|---|
| gap < −20 | 4159 | −35.1 | 0.185 | **−0.315** |
| −20..−10 | 4182 | −14.4 | 0.302 | −0.198 |
| −10..0 | 7514 | −4.7 | 0.387 | −0.113 |
| 0..10 | 7226 | +4.8 | 0.539 | +0.039 |
| 10..20 | 4478 | +14.3 | 0.633 | +0.133 |
| gap > 20 | 4775 | +32.7 | 0.703 | +0.203 |

The residual is **far larger in magnitude on the negative (under) side**
(gap<−20 → −0.315) than on the positive (over) side (gap>20 → +0.203). The
`eda.regime_scan` independently flagged the same thing: its most-mispriced cluster
(regime 3: ~9.5 min elapsed, |margin|~4.7, mean_gap −21.8) has mean residual
**−0.104**. The certified pace edge studied the **over** (continuation) side and
the TOTALS_REFINE teardown found it priced in (fill ~0.556 ≈ win rate 0.580,
only +2.4c residual eaten by the 2% fee). **The under side had never been
isolated** — and EDA says it carries the bigger residual.

## Mechanism

**Asymmetric pace under-reaction: the live total line is sticky on the downside.**
Books anchor on the pregame total; over-bettor flow and reluctance to mark a total
DOWN on slow play keep the line propped, so when live pace runs far below the line
the **under is systematically underpriced**, more than the over is overpriced when
pace runs hot. Distinct from (a) certified pace continuation (over side, killed by
execution), (b) extreme-tail favorite-longshot (a level bias at 0.02/0.98, killed
by spread), (c) term-structure (no info). This is a *signed* pace mechanism on the
ATM contract, not a tail/level bias.

## Pre-registered direction (registered + claimed BEFORE scoring)

`under_on_slow_pace`: fresh (≤2 min stale) quote, elapsed 600..2520s,
`anchoring_gap ≤ −12` → **BUY UNDER (short the over), hold to settlement.**

## Gate numbers — REALISTIC fills (the falsification)

**TRAIN** (n=547): +7.45c/ct, block-bootstrap CI [+3.57, +11.36], cost-robust to 3c. `passed=False`.

**FULL NON-TEST** (n=654, thr=−12; flat across thr −8/−12/−20):

| cost | net c/ct |
|---|---|
| 0c | +6.02 (CI [+2.48, +9.76]) |
| 1c | +5.02 |
| 2c | **+4.02** |
| 3c | +3.02 |
| 4c | +2.02 |

**VAL (OOS, n=106, all 2026-03):** **−1.20c**, CI [−11.0, +7.99] — collapses.

This is materially different from the over side: under REALISTIC fills (not a 0.50
fill) the under side nets **+4.02c@2c**, whereas the certified over side was
**−1.00c@1.5c**. The win-vs-fill decomposition is the reason: realized under win
rate **0.656** vs all-in fill price paid **0.561** → a **+9.5c residual gap that
survives the half-spread and the 2% fee.**

## Why it still FAILS the gate (the disqualifiers)

1. **`estimator_bias_vs_unconditional` FAILS:** win_rate 0.656 vs price 0.561,
   |gap| 0.095 > 0.05 threshold. The gate treats a 9.5c gap between realized rate
   and price-paid as a measurement/estimator bias, not a tradeable edge.
2. **Season-half OOS decay:** H1 +10.4c (CIlo +5.4) → H2 +1.6c (CIlo −3.4); no
   overlap. A durable mispricing would not halve across the season.
3. **Parity split** no overlap; **val month** outright negative.

## Bug-hunt (decompose payoff − 0.50 → payoff − real_mid − fee − spread)

- **NOT a 0.50-fill artifact** — scored entirely on the real listed under price
  (fill 0.561 all-in), the cautionary-tale fill is not in play.
- **NOT a strike-snap artifact** — avg snapped strike − implied mid = **−0.64**
  (essentially ATM); the +9.5c residual concentrates at strike≈mid (n=398), not at
  far-OTM strikes.
- **NOT a stale-quote artifact** — tightening the freshness guard to **0 min**
  (enter only on a bar where mid just changed) leaves the gap at **+9.6c**.
  (Contrast: the original pace over edge was partly stale-line.)
- **NOT early-pace noise** — restricting entry to elapsed ≥1500s (where pace
  projection is reliable; n=400) leaves the gap at **+10.1c**.
- **Concentration clean** (top game 0.3%, top team HOU 4.9%, within caps).

So the +9.5c gross residual is robust to the usual artifacts — yet it is **OOS-
unstable** (H1→H2 halving, val negative) and **flagged by the estimator-bias
guard**. The most likely remaining explanation, consistent with TOTALS_EXTREMES,
is that the recorded `prob` mid on the *under* side is not cleanly executable: the
true bid/ask on the under is wider than the modeled 1.5c half-spread, so part of
the 9.5c is un-capturable spread rather than alpha. One-season power is too low to
separate "real durable under-side mispricing" from "wider-than-modeled under
spread" — the H1→H2 decay points to the latter.

## Verdict rationale

Not PROMOTE (gate fails on estimator-bias + season/parity stability + negative val
OOS). Not DEAD (unlike the over side, it nets **positive realistic net cents to 4c**
and the +9.5c residual survives strike-snap, freshness, and late-entry checks — a
genuinely larger, distinct under-side residual). It is a real lead gated on (a) a
true under-side bid/ask quote to confirm the 9.5c is capturable, and (b) more
seasons / maker fills to settle the H1→H2 instability — the DIAGNOSTIC_1 "real but
under-powered at one-season n" pattern.

Hypothesis `1bbab98218dbbe5c` updated → PROMISING; trial appended to
`research/reports/alpha/ledger.jsonl`.

PROMISING
