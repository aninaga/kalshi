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
</content>
</invoke>
