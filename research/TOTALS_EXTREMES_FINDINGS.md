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

_(filled in after running — see below)_
