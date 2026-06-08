# NBA Moneyline Alpha Search — Findings

_Date: 2026-06-08. Scope: the NBA in-game win-probability research system only
(the Kalshi↔Polymarket arbitrage bot is developed separately and is untouched
here)._

## TL;DR

I rebuilt the data layer, fetched a **full fresh season** (1,319 games,
1,307 with win-prob), and ran **three independent, mechanistically-motivated
strategy families** through the project's own honest promotion gate
(block-bootstrap-by-game CI, cluster knockouts, season/parity stability,
concentration), with **honest entry latency** (enter the minute *after* the
signal, never on it) and **realistic round-trip cost**.

**Result: no robustly tradeable alpha.** Every candidate either nets ≈0¢/contract
at *zero* cost (so cost kills it), or looks positive in-sample and **inverts
out-of-sample** (overfit). This is the *correct* answer reached with the *right*
methodology — not the prior project's buggy false positives.

The valuable deliverable is the **system**: it now searches the tradeable space
(event-triggered + price-level strategies, not just threshold rules), enforces
train/val discipline, and reliably tells a real-but-untradeable phenomenon apart
from an in-sample artifact.

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

## Why there's no tradeable alpha here (the honest answer)

It is **both** of the things the original question asked about:

1. **Market efficiency.** NBA moneyline on Kalshi/Polymarket is priced to within
   the noise floor. Real micro-biases exist (substitution under-reaction ≈0.2¢;
   favorite-longshot ≈3pp in-sample) but sit **below the ~2–4¢ cost floor**.
2. **Data quantity.** One season ≈ 1,300 games. The block is the *game*, so a
   hold-to-settlement bet's per-trade variance (≈0.5 on a 0/1 outcome) means the
   95% CI spans **±5–13¢** at val sample sizes — a real 3¢ edge is **statistically
   undetectable** at this scale. Val can't even reach the n≥200 floor for the
   level strategies.

Both factors point the same way: a genuinely good system, run honestly on the
best available data, finds no edge that survives cost **and** generalizes. The
prior "+$24 / +$41" headline edges were artifacts of an execution-latency bug
and broken data — when the methodology is correct, they're gone, and nothing
real replaces them.

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
python3 -m research.scripts.prefetch_games --start 2025-10-21 --end 2026-06-08 --workers 6
python3 -m research.scripts.sub_alpha       --exit-min 6
python3 -m research.scripts.fairvalue_alpha --gap 0.06 --exit-min 6
python3 -m research.scripts.calib_alpha     --edge 0.03 --min-elapsed 1440
```
