# NBA Totals Favorite-Longshot / Level Bias at Extreme O/U Prices

_Date: 2026-06-08. Branch: claude/hedge-fund-strategy-analysis-jyXWB._
_Scope: NBA only, cached totals pkls under `market_data/nba_studies/_cache/` (kinds=total)._

## Pre-registered mechanism + direction (written BEFORE running)

**Market.** NBA totals (over/under). Each `(strike, prob)` row in `odds_long`
is a tradeable "over X" contract whose price is `prob = P(final_total > strike)`.
`prob` is monotonically decreasing in strike (verified: over-226.5 → 0.99,
over-229.5 → 0.01 in a game that settled 228). This is the per-contract price
ladder, distinct from the implied-total *level* used by the certified pace edge.

**Mechanism (favorite-longshot, a price LEVEL bias).** The most documented
inefficiency in betting markets: bettors overpay for longshots (lottery-ticket
preference) and underpay for near-locks (dislike risking a lot to win little).
Applied to the totals book: an "over X" trading at an **extreme** price —
a longshot `price < 0.10` (deep out-of-the-money) or a near-lock `price > 0.90`
(deep in-the-money) — should settle systematically *worse than implied* for
longshots and *better than implied* for near-locks.

**Pre-registered direction (1-bit, fixed from the mechanism, NOT fished):**
- Longshot over (`price < p_lo`): realized P(settle YES) < price → **overpriced → SELL the over** (buy the under).
- Near-lock over (`price > p_hi`): realized P(settle YES) > price → **underpriced → BUY the over**.

In both cases we **buy the cheap side of the mispricing and hold to settlement**.
PnL (prob units) = `outcome − entry_price` for a long over;
`entry_price − outcome` for a short over (= long under). Honest i+1 latency
(enter the bar AFTER the signal). One trade per game (first extreme contract
seen), block-bootstrap by game, scored ONLY by `promotion_gate.evaluate_trial`.

**Why be skeptical (pre-registered caveat).** The *moneyline* favorite-longshot
bias looked real in-sample (+1.35c) and **inverted OOS (−3.83c)** — it was
sampling noise. So real_edge defaults to FALSE until this survives the four
adversarial checks: (a) staleness, (b) estimator/measurement bias vs an
unconditional baseline (CRITICAL: extreme-price bins are exactly where a stale
or mis-snapshotted price masquerades as edge), (c) concentration, (d) OOS
persistence (train→val + monthly walk-forward).

---

## Results

Evaluator: `research/scripts/totals_extremes_alpha.py` (cache-only; cached totals
pkls under `market_data/nba_studies/_cache/`). Window: elapsed in [720, 2520]s
(mid Q2 to mid Q4), freshness guard `max_stale_min=2`, honest i+1 latency, one
trade per game (first extreme contract by timestamp), hold to settlement, scored
by `promotion_gate.evaluate_trial`. Test split game_ids excluded throughout.
n = 1062 games with a qualifying extreme contract (train 870 / val 192).

### 1. Pre-registered direction (favorite-longshot: SELL longshots, BUY locks) — DEAD

| set | n | @0c | @2c | 2c block-boot CI | gate@2 |
|---|---|---|---|---|---|
| TRAIN | 870 | −0.65 | −2.65 | [−4.70, −0.67] | ❌ |
| VAL | 192 | +0.33 | −1.67 | [−5.75, +1.94] | ❌ |
| FULL (pre-reg dir) | 1062 | −0.47 | **−2.47** | [−4.35, −0.69] | ❌ |

The pre-registered favorite-longshot mechanism is **dead and negative** at all
costs. The calibration shows *why*: it is NOT favorite-longshot.

### 2. What the data actually shows: calibration of extreme OVER contracts (full population)

`bias = realized P(settle YES) − implied price` (negative ⇒ the OVER is overpriced):

Unconditional (all fresh extreme observations, mid-game, n≈188k obs):
| bin | n obs | mean price | realized YES | bias |
|---|---|---|---|---|
| longshot over (price < 0.10) | 79,598 | 0.0575 | 0.0355 | **−0.0220** |
| near-lock over (price > 0.90) | 108,764 | 0.9507 | 0.9003 | **−0.0504** |

Both tails: the OVER settles YES *less* than its price implies. This is **not**
favorite-longshot (which needs opposite signs: longshots overpriced, locks
underpriced). It is a **directional bias — the live total prices the OVER too
high at extremes** — far stronger on the near-lock tail (−5pp) than the longshot
tail (−2pp). Equivalently the *under* longshot is underpriced (priced 0.024,
hits 0.097).

### 3. Data-driven variant (SELL the over at BOTH extremes) — post-hoc, still fails the gate

(Reported for honesty; this is a fished direction chosen AFTER seeing the
calibration, so it does not count as a pre-registered edge.)

| set | n | @0c | @1c | @2c | @3c | @4c | @5c | gate@2 |
|---|---|---|---|---|---|---|---|---|
| FULL | 1062 | +4.33 | +3.33 | **+2.33** | +1.33 | +0.33 | −0.67 | ❌ |

2c block-bootstrap CI = [+0.54, +4.08] (lo > 0), but the **full gate FAILS** on
season-half stability (early-half CI lo ≈ 0/negative, halves don't overlap) and
home/away parity stability. The cleanest OOS slice (val, n=192) is **+1.85c@2c
with CI lo −1.72** — not significant. Full-population gate@2 reasons:
`season_split early_ci_lo≈−0.001, late_ci_lo≈−0.001, overlap=False;
parity_split odd_ci_lo=−0.0037`.

Monthly walk-forward (SELL-over-both, @2c): **5/6 months positive** (Oct n=56
−5.6; Nov +3.2; Dec +2.5; Jan +2.8; Feb +2.1; Mar +3.0) but **no single month is
independently significant** (every monthly CI lo < 0).

## The four adversarial checks (on the data-driven +2.33c@2c signal)

1. **Staleness — RULED OUT as the cause.** The bias persists on perfectly fresh
   quotes (`stale=0`): longshot −2.1pp, lock −5.2pp (n=51k/69k). Tightening
   freshness does not remove it. So it is not a stale-line artifact (unlike the
   pace edge, which was partly staleness).
2. **Estimator / measurement bias — THIS IS LIKELY THE WHOLE STORY.** The bias is
   **strongly elapsed-dependent and decays toward settlement**: lock-over bias
   Q2 −7.3pp → Q3 −4.9pp → Q4 −2.6pp. A genuine hold-to-settlement mispricing
   would not depend on entry quarter (you hold to the same 0/1 outcome either
   way). The decay matches **wider quoted extreme spreads earlier in the game**:
   the recorded `prob` is a mid/last, but to SELL a 0.95–0.98 over you hit the
   bid 3–5c below mid. The −5pp lock "bias" is plausibly the un-capturable
   half-spread at the extreme, not alpha. The unconditional at-the-money baseline
   is calibrated (P(over)≈0.50 per the certified pace study), so the bias is
   specific to the extreme-price tails — exactly where spreads are widest.
3. **Concentration — clean.** Cluster knockouts show no single game/team drives
   it (drop-top-team / drop-top-games means stay positive at 0c); concentration
   shares are within caps. Not a concentration artifact.
4. **OOS persistence — FAILS the bar.** Val slice not significant (CI lo −1.72);
   no monthly walk-forward window independently significant; season-half and
   parity-split stability both fail on the full population. The pre-registered
   direction is negative OOS. Echoes the moneyline favorite-longshot, which also
   looked plausible in aggregate and did not hold up.

## Execution realism (the killer)

The signal lives entirely at **extreme prices (0.92–0.98 / 0.02–0.08)**, where
the Polymarket/Kalshi spread is widest. The flat cost sweep breaks even at
**~4–5c**, which is well inside a realistic extreme-price round-trip half-spread.
The recorded `prob` is a mid, not an executable bid — selling a 0.976 over at its
"price" is not achievable; the realistic fill is several cents worse, which the
Q2→Q4 decay independently corroborates. After a realistic extreme-tail spread the
edge is ≤ 0.

## VERDICT: DEAD (real_edge = false)

- The **pre-registered favorite-longshot direction is dead and negative**
  (−2.47c@2c, gate fail) — the mechanism does not exist in the totals book; the
  observed bias is directional ("over overpriced at extremes"), not the
  asymmetric FL pattern.
- The **post-hoc "sell the over at both extremes" variant** has a positive gross
  number (+2.33c@2c, block-boot CI lo +0.54) but **FAILS the promotion gate** on
  season-half and parity stability, is **not significant out-of-sample** (val CI
  lo −1.72), is a **fished direction**, and is **explained by extreme-tail spread
  / a measurement artifact** (elapsed-decaying bias, breaks even at ~4–5c inside
  the real spread).
- All four adversarial checks: staleness ruled out, but **estimator/spread bias
  and OOS failure both fire** → default `real_edge=false` stands.

**No tradeable, cost-robust, gate-certified totals favorite-longshot / level edge
exists in the cached data.** This direction is exhausted; do not re-open it.

