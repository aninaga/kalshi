# NBA Cross-Contract Relative Value (Winner ↔ Total Term Structure) — Findings

_Date: 2026-06-08. Branch: claude/hedge-fund-strategy-analysis-jyXWB.
Scope: NBA only, using the already-cached winner + total ladders under
`market_data/nba_studies/_cache/` (2025-26, ~1319 games). No new adapters, no
new market types, no test split._

## Pre-registered mechanism (written BEFORE results)

**Direction assigned:** cross-contract relative value (term structure) between
the WINNER and TOTAL contracts of the SAME game — structurally different from
every prior single-contract directional bet (moneyline threshold,
substitution-latency, fair-value-gap, calibration, totals-pace).

**Mechanism (a) — JOINT mispricing of the winner leg vs the total leg.**
Both contracts depend on *remaining-game scoring*, but through different
channels: the winner contract reacts to the **margin trajectory**, the total
contract reacts to the **pace trajectory**. A standard random-walk model of the
final score ties them together: if the home lead is `M` points with remaining
implied scoring `R = imp_total − points_so_far`, then the final home margin is
approximately `M + N(0, σ²)` where the dispersion `σ` scales with the remaining
scoring `σ = k·√R` (more points left to play ⇒ a wider final-margin
distribution ⇒ a given lead is *less safe*). The model-implied home win prob is
therefore `Φ(M / (k·√R))`.

The cross-contract claim: the winner contract **does not fully use the pace
signal carried by the total contract**. Concretely, when the total leg implies
an unusually *high* remaining pace (R large) relative to what the margin alone
would suggest, a current leader is less safe than the winner price reflects —
so the winner contract **overprices the leader**; symmetrically, when remaining
pace is *low* (R small), a lead is safer and the winner contract **underprices
the leader**.

**Why it would persist:** the two contracts trade in separate order books with
separate participants. The winner book is the heavily-watched one (moneyline is
efficient per ALPHA_FINDINGS.md); the total book is thinner. There is no
mechanical arbitrage forcing the joint distribution to be coherent — keeping
them consistent requires a participant who simultaneously models both, which is
rarer than someone trading one leg on the scoreboard.

**Pre-registered trade & direction (fixed from the mechanism, ONE bit, fit on
train only as a sign confirmation, then evaluated OOS):**
- Build a random-walk model win prob `mwp = Φ(M / (k·√R))`, with `k` calibrated
  ONCE on TRAIN by log-loss against realized outcomes.
- Signal `gap = mwp − wp_market`. When `gap > +τ` the model says the leader is
  underpriced ⇒ **BUY the home/leader winner contract**; when `gap < −τ` the
  model says the leader is overpriced ⇒ **SELL it (buy the other side)**.
- Trade the REAL winner contract (binary, settles 0/1), held to settlement.
- Honest entry latency: observe the gap at bar `t`, ENTER at bar `t+1` using the
  next observable winner price; one trade per game (first qualifying bar after
  `min_elapsed`).
- PnL in prob units: `(outcome − entry_price)` for a long, `(entry_price −
  outcome)` for a short, minus round-trip cost swept over {0,1,2,3,4}c.
- Scored ONLY by `research.scorer.promotion_gate.evaluate_trial`
  (block-bootstrap-by-game CI, n≥200, cluster knockouts, season/parity
  stability, concentration). Self-reported PnL is worthless.

**Alternative mechanism (b)** — totals strike-curve BODY shape — held in reserve
if (a) is dead and the strike ladder supports it. NOT the favorite-longshot
tails (a separate agent owns that); the body = the 25–75% strikes.

(Results, cost sweep, walk-forward, and the four adversarial checks follow
below.)

---

## Evaluator (the only judge: `promotion_gate.evaluate_trial`)

- `research/scripts/term_structure_cache.py` — builds a one-time joined
  winner+total per-minute panel (1,304/1,319 games joined, 229,053 minute rows)
  from the cached pkls. For each game and minute: `wp` (blended Kalshi+PM home
  win prob from the winner pkl), `imp` (blended implied total from the total
  pkl's strike ladder via `analysis._implied_total`), margin, total, elapsed,
  and `home_won`. Output: `_joined_ws.parquet`.
- `research/scripts/term_structure_alpha.py` — the evaluator. Calibrates `k` on
  TRAIN by log-loss, builds the random-walk model win prob, forms the signal,
  builds one honest-latency trade per game on the REAL winner contract held to
  settlement, sweeps cost {0,1,2,3,4}c, and scores through `evaluate_trial`.
  Two signals: `gap` (full model−market) and `pace_only` (the incremental
  total-leg contribution — the genuine cross-contract test).

## Step 0 — does the premise even hold? (probe, TRAIN only, 94,928 obs)

The mechanism requires the winner contract to *lag* the total leg. The data
say the opposite:

- **The winner contract is already excellently calibrated and far better than
  the model.** Log-loss: winner market **0.440** vs random-walk model **0.516**
  (best `k=1.30`). Unconditional in-game calibration: realized home-win rate
  0.518 vs mean win price 0.517. The winner book is not naive about
  remaining-game scoring.
- **DECISIVE disentangling test** — does the TOTAL leg add winner-relevant info
  beyond the margin trajectory? Correlation with the outcome residual
  `(home_won − wp)`:
  - `corr(gap_full, resid)   = 0.041`  (margin-model gap vs market)
  - `corr(gap_margin_only, resid) = 0.038`  (SAME model with R fixed to its
    median — i.e. NO total-leg information at all)
  - `corr(pace_only, resid)  = 0.011`  (the incremental TOTAL-leg contribution,
    `gap_full − gap_margin_only`; std 0.041 vs gap std 0.186)

  Nearly all of the model–market gap comes from the margin random-walk model
  disagreeing with the market (a margin/time/calibration effect, which is the
  favorite-longshot territory another agent owns and which already inverted
  OOS in `calib_alpha`), **NOT from the total contract**. The cross-contract
  (pace) component is `corr ≈ 0.011` — essentially zero. The total leg carries
  no win-relevant information the winner book hasn't already priced.

## Step 1 — the tradeable gate test (the judge)

Trades on the REAL winner contract, honest i+1 latency, hold to settlement,
scored by `evaluate_trial`. Default config `min_elapsed=600, max_stale_min=2`.

**`pace_only` (the assigned cross-contract signal, τ=0.02):**

| set | n games | c/ct @0 | @2c | block-bootstrap 95% CI @2c | gate |
|---|---|---|---|---|---|
| TRAIN | 902 | +1.26 | −0.74 | [−3.60, +2.11] | ❌ |
| VAL | 198 | **−1.56** | −3.56 | [−8.74, +2.16] | ❌ |

**`gap` (full model gap, favorite-longshot-confounded, τ=0.05) — for reference:**

| set | n games | c/ct @0 | @2c | block-bootstrap 95% CI @2c | gate |
|---|---|---|---|---|---|
| TRAIN | 902 | +1.89 | −0.11 | [−3.00, +2.85] | ❌ |
| VAL | 199 | **−0.08** | −2.08 | [−7.66, +3.48] | ❌ |

Both signals are **below the 2c cost floor on TRAIN and NEGATIVE on VAL** — they
invert out-of-sample, the exact failure mode of the prior `calib_alpha` mirage.
Cost-robustness is moot (nothing is net-positive at 2c with CI lower bound > 0,
in or out of sample).

## Step 2 — robustness sweep (no cherry-picking)

`pace_only` over τ ∈ {0.01, 0.03, 0.05} × min_elapsed ∈ {360, 720, 1440},
gross c/ct @0 (TRAIN → VAL):

| τ | min_elapsed | TRAIN @0 | VAL @0 |
|---|---|---|---|
| 0.01 | 360 | +0.25 | +0.21 |
| 0.01 | 720 | +1.99 | **−2.31** |
| 0.01 | 1440 | −0.14 | **−3.86** |
| 0.03 | 360 | −0.63 | **−1.79** |
| 0.03 | 720 | +1.78 | +4.21 |
| 0.03 | 1440 | (running, irrelevant) | — |

The signs flip cell-to-cell with no stable pattern; the mean VAL gross over the
five completed cells is **−0.71c** (before any cost). The single positive cell
(τ=0.03, min_elapsed=720: VAL +4.21c gross) is isolated multiple-testing noise
— and when run through the gate it **FAILS decisively**: TRAIN gate=False
(block-bootstrap CI_lo −0.028, knockouts/season/parity all fail) and even the
"good" VAL slice has block-bootstrap CI @2c = [−2.48, +7.29]c (lower bound
NEGATIVE), n=197 < 200, season splits do not overlap, parity splits do not
overlap, top-game knockout negative. Selecting it would be precisely the
p-hacking the LAWS forbid.

## The four mandatory adversarial checks

1. **Staleness.** A 2-minute freshness guard on the implied-total line is built
   into the evaluator (`max_stale_min=2`) so the signal can never fire on a
   stale total quote. The result is unchanged/negative WITH the guard — staleness
   is not hiding a real edge here (and there is no edge to begin with).
2. **Estimator / measurement bias.** Checked against an unconditional baseline:
   the winner contract's unconditional in-game calibration is 0.518 realized vs
   0.517 priced — calibrated, not biased. The model's own log-loss (0.516) is
   WORSE than the market's (0.440), so the "gap" is the model being wrong, not
   the market being wrong. The cross-contract component has corr 0.011 with the
   outcome — no measurable signal.
3. **Concentration.** The gate's cluster knockouts (drop-top-games,
   drop-top-team, drop-first-quarter) are NEGATIVE in every configuration, i.e.
   there isn't even a positive base to concentrate — failure is broad, not a
   single team/game.
4. **OOS persistence (train → val).** The signal **inverts** train → val (e.g.
   pace_only +1.26c TRAIN → −1.56c VAL gross; gap +1.89c → −0.08c). No monthly
   walk-forward is warranted because there is no net-positive, CI-clearing
   train base to walk forward; the train→val inversion already settles it.

## Verdict: DEAD

The assigned cross-contract relative-value direction (winner contract lagging
the joint winner↔total state) **does not exist in this data** at a tradeable,
cost-robust, out-of-sample level:

- **Mechanistically falsified at the source:** the total leg adds ~zero
  win-relevant information beyond the margin trajectory
  (`corr(pace_only, resid) = 0.011`); the winner book is already better
  calibrated than the joint random-walk model (log-loss 0.440 vs 0.516).
- **Gate verdict (the only judge), `pace_only` @2c:** TRAIN n=902, −0.74c/ct,
  block-bootstrap CI [−3.60, +2.11]c, **gate FAIL**; VAL n=198, −3.56c/ct,
  CI [−8.74, +2.16]c, **gate FAIL**. `real_edge = false`.
- It inverts OOS, fails every cluster knockout and stability check, and the one
  positive grid cell fails the gate too. This is the prior `calib_alpha`
  failure mode, not a new edge.

**This is NOT a NEEDS_DATA outcome.** The cached winner + total pkls fully
support a genuine cross-contract test — 1,304 games joined, ~229k minute-level
joint (wp, imp) observations, a rich ~11-strike Kalshi total ladder. The data
were sufficient to *decisively rule the direction out*. The bottleneck is not
data; the joint mispricing simply is not there.

### Note on the un-pursued alternative (mechanism b)
Mechanism (b), the totals strike-curve BODY shape, is buildable (≈99% of games
carry ≥5 simultaneous Kalshi strikes, typically 11), but it is a *within-totals*
distribution-shape test, not cross-contract relative value, and it overlaps both
the already-certified totals-pace edge and the favorite-longshot tails another
agent owns. It is left for a totals-shape-specific worker; the assigned
cross-contract direction is conclusively DEAD.

```json
{"market": "nba_winner_x_total_cross_contract",
 "mechanism_one_line": "winner contract lags the joint winner/total game state; trade the laggy leg using a random-walk model that ties margin+total-implied remaining pace to win prob",
 "gate_passed": false, "n_games": 198,
 "cents_per_contract_0c": -1.56, "cents_per_contract_2c": -3.56,
 "block_bootstrap_ci_lo_2c": -8.74, "real_edge": false,
 "artifact_checks_done": ["staleness","estimator_bias","concentration","oos"],
 "verdict": "DEAD",
 "next_step": "abandon winner<->total cross-contract RV; the total leg adds ~0 win info beyond margin (corr 0.011). A separate worker could test within-totals strike-curve BODY shape, which is buildable from the rich ~11-strike Kalshi ladder but is not cross-contract."}
```

