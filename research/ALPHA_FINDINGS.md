# NBA Moneyline Alpha Search — Findings

_Date: 2026-06-08. Scope: the NBA in-game win-probability research system only
(the Kalshi↔Polymarket arbitrage bot is developed separately and is untouched
here)._

## ⚠️ CORRECTION (2026-06-08, later) — the "certified totals edge" is EXECUTION-KILLED

The full-season gate PASS for the totals pace-anchoring edge reported below was
an **execution-model artifact** and is RETRACTED. The evaluator filled at the
interpolated 0.50 at-the-money strike; the *real listed* over contract on the
signal side prices ~**0.5555** (the market had already priced the pace signal).
Under realistic execution — snap to the actual listed strike at its real quoted
price, cross a ~1.5¢ half-spread, pay the Polymarket 2% taker fee — the edge goes
from +8.21¢ to **net −1.00¢/contract**, the gate FAILS at every spread, and the
walk-forward collapses 7/9 → 3/8 months (see `research/TOTALS_REFINE_FINDINGS.md`,
`research/scripts/totals_realistic.py`). The gate and methodology were sound
(DIAGNOSTIC_1: 0% false-positive); the **cost/fill model under-charged by ~7.5¢**.
This is the same failure mode as the original project's i+1 latency bug —
optimistic execution manufactures phantom edges.

**Net result: NO gate-CERTIFIED tradeable NBA edge in this data.** But the
realistic re-check split the two anchoring edges apart:
- **totals** — pure 0.50-fill artifact, net **−1.00¢/ct**, dead. RETRACTED.
- **spreads** — NOT a pure artifact. Real fill mid is 0.5187 (not 0.50), so
  **+5.22¢ of genuine residual gross survives**; realistic taker net is
  **+1.88¢/ct** (1.5¢ half-spread + 2% fee; +3.43¢ at zero spread). It still
  **fails the gate** (bootstrap CI lo < 0 at any nonzero spread; season/parity
  stability fail; walk-forward 5/8) — so PROMISING-BUT-UNCERTIFIED and marginal
  as a taker. The one real residual mispricing found; would need **maker fills**
  (to dodge the half-spread + 2% taker) or **more seasons** to certify. See
  `research/SPREADS_FINDINGS.md` → "REALISTIC EXECUTION RE-CHECK".

Treat every result that quotes a 0.50/flat-cost fill as a STATISTICAL signal
only, not a tradeable edge, until re-scored on real listed-strike prices + fees.

## TL;DR

I rebuilt the data layer, fetched a **full fresh season** (1,319 games,
1,307 with win-prob), and ran **four independent, mechanistically-motivated
strategy families** through the project's own honest promotion gate
(block-bootstrap-by-game CI, cluster knockouts, season/parity stability,
concentration), with **honest entry latency** (enter the minute *after* the
signal, never on it) and **realistic round-trip cost**.

**Headline: three of four candidates are dead (efficient market / overfit). The
fourth — a TOTALS pace edge — PASSES the project's full promotion gate at
realistic cost on the complete 2025-26 season.**

After building the **entire season** (1,297 totals games) and pre-registering the
direction (mechanism-driven), the full promotion gate (block-bootstrap CI,
n≥200, cluster knockouts, season & parity stability, concentration) **passes at
0¢/1¢/2¢ round-trip cost**:

| cost | n games | net ¢/contract | block-bootstrap 95% CI | gate |
|---|---|---|---|---|
| 0¢ | 1,297 | +8.21 | [+5.59, +10.83] | ✅ PASS |
| 1¢ | 1,297 | +7.21 | [+4.59, +9.83] | ✅ PASS |
| **2¢** | **1,297** | **+6.21** | **[+3.59, +8.83]** | **✅ PASS** |
| 3¢ | 1,297 | +5.21 | [+2.59, +7.83] | ❌ (sub-split marginal) |

Monthly **walk-forward** (each month an independent out-of-sample slice, nothing
fit): **7 of 9 months net-positive after 2¢** — every regular-season month
(Oct–Apr) positive, three independently significant; only the sparse late-playoff
months (May n=37, Jun n=2) negative.

It is **not** an artifact: broad-based (top team = 5% of |PnL|, well under the
20%/25% caps), a flat threshold plateau (thresh 4→10 all ≈+9.5¢ on train), a
calibrated estimator at clean snapshots (unconditional P(over)=0.502), a
freshness guard against stale lines, and a documented behavioral mechanism.

**This is the concrete answer to "is alpha-finding data-limited or
un-automatable": it was data-limited, and getting the data resolved it.** On one
train/val split (n=199 val) the same edge showed +6.78¢/contract but FAILED the
gate (CI lower bound −2.25¢ — the hold-to-settlement 0/1 variance is too high to
reach significance at n=199). Building the full season (n→1,297) tightened the CI
and the gate passed. The prior project's "+$24/+$41" headlines were artifacts
that vanish under correct methodology; this is the opposite — a real signal that
needed enough data to *prove*, and now has it.

**Note on prior seasons:** Kalshi and Polymarket launched NBA single-game markets
in 2025-26 — verified directly, both APIs return zero markets/events for 2024-25
and earlier. The 2025-26 season is the entire history of this market, so "more
data" meant building all 1,319 of its games, which is what crossed the bar.

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
to settlement. Direction (`continuation`) is mechanism-pre-registered, so every
game is a valid out-of-sample observation; the full-season gate and a monthly
walk-forward are the certification.

| set | n | win | gross ¢/ct @0 | @2¢ | gate@2 |
|---|---|---|---|---|---|
| TRAIN (direction fit) | 403 | — | +9.55 | +7.55 | — |
| VAL (clean holdout) | 199 | 56.8% | +6.78 | +4.78 | ❌ (n/CI too small) |
| **FULL SEASON (pre-reg dir)** | **1,297** | **~58%** | **+8.21** | **+6.21** | **✅ PASS, CI [+3.59,+8.83]** |

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

**On val alone it failed the gate** (`ci_lo=−2.25¢`, `n=199<200`, sub-splits
unstable) — purely a data-quantity problem. **Building the full season fixed it:**
at n=1,297 the block-bootstrap CI lower bound at 2¢ is **+3.59¢** and every gate
criterion passes. The walk-forward confirms it isn't a single-window fluke —
7/7 regular-season months positive. This is the certified edge.

## The honest answer to the original question

It is **both** efficiency and data-quantity — and which one binds depends on the
market:

1. **Moneyline is efficient.** Real micro-biases exist (substitution
   under-reaction ≈0.2¢; favorite-longshot ≈3pp in-sample) but sit **below the
   ~2–4¢ cost floor** or **don't generalize** (calibration inverts OOS).
2. **The totals edge is real, cost-robust, and now gate-certified.** On the full
   season it nets **+6.21¢/contract at 2¢ cost with a block-bootstrap CI lower
   bound of +3.59¢** and passes every gate criterion, with 7/7 regular-season
   months positive in walk-forward. The data-quantity bottleneck that sank the
   val-only test was resolved by building all 1,297 games — not by more
   cleverness.

So: a genuinely good, honest system, pointed at a less-watched market, **found a
real, cost-robust, out-of-sample, gate-certified edge.** That is the opposite of
the prior project's failure mode (buggy false positives that vanished under
correct methodology). The binding constraint was **data quantity**, and getting
the data crossed the bar — which is itself the answer to the original question.

**Caveats before risking capital** (this is research, not a green light):
- Execution is modeled as an at-the-money fill at ≈0.50 with a flat cost sweep.
  Real Polymarket charges a ~2% fee and a spread; the edge survives to a ~5–6¢
  breakeven, but live fills at the implied-total mid must be confirmed.
- The direction (continuation) is a 1-bit, mechanism-justified pre-registration
  and the threshold sits on a flat plateau, but the full-population test includes
  train games. The cleanest final confirmation is a single test-set unlock
  (`research.promotion.review_cli --burn-unlock`) — a human-gated decision.
- Late-playoff months (small n) are negative; scope the edge to the regular
  season until more playoff data exists.

## Where the edge is — and isn't
The totals pace edge is real and certified. The *moneyline* edges that would
need other data are still out of reach:
- **Sub-minute speed** (react to a made shot before the book) — excluded by
  honest i+1 latency, and unavailable to a retail backtester.
- **Cross-venue lead-lag** (Kalshi vs Polymarket) — needs Kalshi intra-game
  ticks, which are empty for 2025-26.
- **Order-book microstructure** — no historical Polymarket book exists.

The lesson: efficiency varies by *market*, not by sport. Moneyline is picked
clean; the less-watched totals line carries an exploitable anchoring bias the
same games and the same data layer reveal once you look there.

## Reproduce
```bash
# moneyline data (winner markets) + the three efficient-market candidates
python3 -m research.scripts.prefetch_games --start 2025-10-21 --end 2026-06-08 --workers 6
python3 -m research.scripts.sub_alpha       --exit-min 6
python3 -m research.scripts.fairvalue_alpha --gap 0.06 --exit-min 6
python3 -m research.scripts.calib_alpha     --edge 0.03 --min-elapsed 1440

# totals data (over/under ladder) + THE CERTIFIED EDGE
python3 -m research.scripts.prefetch_games  --start 2025-10-21 --end 2026-06-08 --kinds total --workers 6
python3 -m research.scripts.totals_alpha     --thresh 6 --min-elapsed 600 --max-stale-min 2  # train/val view
python3 -m research.scripts.totals_walkforward --thresh 6 --max-stale-min 2                  # full-season gate + monthly
```

## Next steps (the edge is certified; these harden it for capital)
1. **Execution realism** — model the actual at-the-money over/under spread + the
   ~2% Polymarket fee per contract instead of the flat cost sweep; confirm live
   fills at the implied-total mid. The edge breaks even around ~5–6¢, so this is
   the main risk to size.
2. **Scope to the regular season** — late-playoff months (small n) are negative;
   keep playoffs out until more playoff data accrues.
3. **Optional pristine confirmation** — a single human-gated test-set unlock
   (`research.promotion.review_cli --burn-unlock`). Not required (the direction is
   pre-registered and the walk-forward is OOS), but it's the cleanest final stamp.
4. **Prior seasons are not available** — verified: Kalshi/Polymarket NBA markets
   start in 2025-26. More history will only come as future seasons trade.
