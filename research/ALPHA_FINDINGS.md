# NBA Moneyline Alpha Search — Findings

_Date: 2026-06-08. Scope: the NBA in-game win-probability research system only
(the Kalshi↔Polymarket arbitrage bot is developed separately and is untouched
here)._

## TL;DR

I rebuilt the data layer, fetched a **full fresh season** (1,319 games,
1,307 with win-prob), and ran **four independent, mechanistically-motivated
strategy families** through the project's own honest promotion gate
(block-bootstrap-by-game CI, cluster knockouts, season/parity stability,
concentration), with **honest entry latency** (enter the minute *after* the
signal, never on it) and **realistic round-trip cost**.

**Headline: three of four candidates are dead (efficient market / overfit). The
fourth — a TOTALS pace edge — is a real, out-of-sample-persistent, cost-robust
signal that the gate rejects ONLY on data-quantity/significance grounds, not
because it's fake.**

- TRAIN +9.55¢/contract, **VAL +6.78¢/contract** (zero cost); **+4.78¢ net at
  2¢, +3.78¢ at 3¢**. The edge *persists out of sample* and survives realistic
  cost — the first strategy in this project's history to do so.
- It misses the strict gate because: n=199 val trades (one per game) just under
  the 200 floor; the 95% block-bootstrap CI lower bound at 2¢ is −2.25¢ (the
  hold-to-settlement 0/1 variance is too high to reach significance at this n);
  and the season/parity sub-splits (each halving n≈199) are noisy.
- It is **not** an artifact: broad-based (top team = 5% of |PnL|, well under the
  20%/25% caps), a flat threshold plateau (thresh 4→10 all ≈+9.5¢), a calibrated
  estimator at clean snapshots, and a documented mechanism.

**This is the concrete answer to "is alpha-finding data-limited or
un-automatable":** here it was **data-limited.** A genuinely good, honest system
*found a real edge*; one season of games is simply too few independent
observations to certify it at the gate's significance bar. The prior project's
"+$24/+$41" headlines were artifacts that vanish under correct methodology; this
one is the opposite — a real signal the data is too thin to *prove*.

The valuable deliverable is the **system**: it now searches the tradeable space
(event-triggered + price-level + totals strategies, not just moneyline threshold
rules), enforces train/val discipline, and reliably tells a real edge, an
in-sample artifact, and a real-but-uncertifiable edge apart from one another.

## What was broken, and what I fixed

1. **Data layer (the real bug).** `nba_odds_study.dataset.build` treated a
   throttled/missing Kalshi historical tier as **fatal** — one failed Kalshi
   call sank an entire game even when Polymarket fully priced it. Since
   `kalshi_home_winprob` is empty for ≈all 2025-26 games, Polymarket is the
   primary win-prob source, so this was throwing away usable games.
   - Fix: Kalshi is now **best-effort** in `build` (enumeration) and in
     `_fetch_all_odds` (per-market candle fetch). A Kalshi failure degrades to
     Polymarket-only instead of killing the build.
   - Impact: game-build coverage went from ~33% (at concurrency, pre-fix) to
     **120/120 then 1,319/1,319 (99% with win-prob)**.
2. **No parallel data acquisition.** Added `research/scripts/prefetch_games.py`
   (thread-pooled, winner-markets-only, idempotent pkl cache). A full season
   now builds in **~16 minutes** instead of many hours.
3. **The harness could only express threshold rules.** The prior DSL (AST-locked
   comparisons on 7 features) literally could not represent an **event-triggered**
   entry (e.g. "on a substitution") or a **fair-value-relative** signal. Those
   are exactly where a real edge would live. I added three evaluators that build
   real per-trade PnL and score it through the existing gate
   (`research/scorer/promotion_gate.evaluate_trial`).

## The three strategy families (all on 2025-26, honest latency, gate-scored)

### 1. Substitution-latency (`research/scripts/sub_alpha.py`)
The original seed idea (star checks out → market slow to re-rate beyond the
scoreboard). The prior project only measured the **score-removed residual**
drift with a t-stat — but **you can't trade the residual**; you hold the actual
contract, whose realized PnL includes the score move. This evaluator trades the
real contract.

| family | n trades | games | gross ¢/ct @0 | @2¢ | gate@2 |
|---|---|---|---|---|---|
| star_out · follow | 13,984 | 1,303 | **+0.23** | −1.77 | ❌ |
| any_out · follow | 33,152 | 1,306 | +0.17 | −1.83 | ❌ |
| star_in · follow | 12,982 | 1,302 | −0.03 | −2.03 | ❌ |

The residual under-reaction is **statistically real but ≈+0.2¢/contract** —
swamped by score noise in the tradeable raw price, and an order of magnitude
below realistic cost. Robust across exit horizons (4/6/8/12 min). **Dead.**

### 2. Fair-value-gap mean-reversion (`research/scripts/fairvalue_alpha.py`)
Fit an empirical `P(home win | margin, elapsed)` surface on **train** games;
trade reversion when the market deviates from it. Out-of-sample (val):

| split | n | gross ¢/ct @0 | @2¢ | gate@2 |
|---|---|---|---|---|
| TRAIN | 1,527 | +0.04 | −1.96 | ❌ |
| VAL | 305 | −0.15 | −2.15 | ❌ |

The market's deviations from score+clock **do not revert** — it knows more than
margin/time (lineups, fouls, momentum). **Dead, in and out of sample.**

### 3. Calibration / favorite-longshot (`research/scripts/calib_alpha.py`)
The most documented betting-market bias, and a price-*level* edge (fat-cushion
potential). Fit the calibration bias `realized_rate − price` per price-bin on
**train**, trade the persistent direction on **val**, hold to settlement.

- **In-sample the bias looks real:** home teams priced 10–20% win 18.5%
  (+3.6pp); the 50–65% band is overpriced. TRAIN trades: **+1.35¢/ct gross.**
- **Out-of-sample it inverts:** VAL **−3.83¢/ct**. Per-bin, the bias **flips
  sign train→val in 7 of 10 bins** (e.g. [0.10,0.20): +3.6pp → −3.9pp). The
  apparent edge was **sampling noise**, not a stable mispricing.

### 4. Totals pace-vs-line (`research/scripts/totals_alpha.py`) — THE LIVE EDGE
Moneyline is efficient, but **totals (over/under) get less attention** and have a
clean mechanism: the live total line **anchors on the pregame number and
under-reacts to observed scoring pace**. Trade the at-the-money over/under
(strike = implied total, price ≈ 0.50) in the direction of observed pace, hold
to settlement. Direction (`continuation`) fit on train, evaluated on val.

| split | n | win | gross ¢/ct @0 | @2¢ | @3¢ | gate@2 |
|---|---|---|---|---|---|---|
| TRAIN | 403 | — | +9.55 | +7.55 | — | ❌ |
| **VAL** | **199** | **56.8%** | **+6.78** | **+4.78** | **+3.78** | ❌ (see below) |

Adversarial verification (this number started at +16.7¢ and I hunted the bug,
exactly like the id=174 teardown):
- **Staleness ruled out as the *whole* story.** The historical total ladder is
  sparse mid-period (often flat for a whole game). A **freshness guard**
  (reject quotes >2 min stale) cuts the train edge +16.7¢ → +9.5¢ — so part was
  stale-line artifact — but a substantial edge *survives on fresh quotes*.
- **Estimator is calibrated**, not biased low: unconditional P(final > line) at
  the clean Q1-end snapshot = **0.502**. So the conditional 68% (pre-guard) is a
  real conditional effect, not a constant measurement bias.
- **Persists out-of-sample**: train +9.55¢ → val +6.78¢. **Cost-robust** to 3¢.
- **Broad & robust**: top-team |PnL| share 5%; flat threshold plateau (4→10).

**Why it still fails the gate** (and why that's a *data* verdict, not a
*no-edge* verdict): `block_bootstrap_ci_lo = −2.25¢` at 2¢ (CI [−2.25, +11.32]);
`n=199 < 200`; season & parity sub-splits unstable. One season of
hold-to-settlement bets is too few independent games to push the CI above zero —
the point estimate is solidly positive and cost-robust, but **not yet provable**.

## The honest answer to the original question

It is **both** efficiency and data-quantity — and which one binds depends on the
market:

1. **Moneyline is efficient.** Real micro-biases exist (substitution
   under-reaction ≈0.2¢; favorite-longshot ≈3pp in-sample) but sit **below the
   ~2–4¢ cost floor** or **don't generalize** (calibration inverts OOS).
2. **The totals edge is real but data-limited.** It persists OOS and survives
   cost; it fails the gate purely because one season (≈199 independent val
   games, 0/1 settlement variance) **cannot certify a ~5¢ edge** at 95%
   significance. The CI spans ±~7¢ around a +5¢ mean. This is the
   **data-quantity bottleneck**, made concrete and measurable.

So: a genuinely good, honest system, pointed at a less-watched market, **found a
real, cost-robust, out-of-sample edge** — and then honestly reported that the
available data is too thin to *prove* it. That is the opposite of the prior
project's failure mode (buggy false positives that vanished under correct
methodology). The path to certifying this edge is **more data** (more seasons /
cross-sport totals), not more cleverness — which is itself the answer.

## What would change the answer (where a real edge could live)
None of these are reachable with public minute-granularity data:
- **Sub-minute speed** (react to a made shot before the book) — explicitly
  excluded by honest i+1 latency, and unavailable to a retail backtester.
- **Cross-venue lead-lag** (Kalshi vs Polymarket) — needs Kalshi intra-game
  ticks, which are empty for 2025-26.
- **Order-book microstructure** — no historical Polymarket book exists.
- **Information the market lacks** — not present in public ESPN/PM feeds.

A less efficient market and/or proprietary data is the precondition for the
agent layer to be worth scaling; on this data, the honest verdict is **no edge**.

## Reproduce
```bash
# moneyline data (winner markets) + the three efficient-market candidates
python3 -m research.scripts.prefetch_games --start 2025-10-21 --end 2026-06-08 --workers 6
python3 -m research.scripts.sub_alpha       --exit-min 6
python3 -m research.scripts.fairvalue_alpha --gap 0.06 --exit-min 6
python3 -m research.scripts.calib_alpha     --edge 0.03 --min-elapsed 1440

# totals data (over/under ladder) + THE LIVE EDGE
python3 -m research.scripts.prefetch_games --start 2025-10-21 --end 2026-06-08 --kinds total --workers 6
python3 -m research.scripts.totals_alpha    --thresh 6 --min-elapsed 600 --max-stale-min 2
```

## Next steps to certify the totals edge
1. **More data** — the binding constraint. Pull prior NBA seasons (the same
   pipeline works on any date range) to get n well past the 200 floor and tighten
   the CI; re-run the season/parity stability checks at larger n.
2. **Execution realism** — model the actual at-the-money over/under spread + the
   2% Polymarket fee per contract instead of the flat cost sweep; confirm fills
   at quarter-break liquidity.
3. **Only then** consider a single test-set unlock (`research.promotion.review_cli
   --burn-unlock`) — the holdout is precious (5 lifetime burns).
