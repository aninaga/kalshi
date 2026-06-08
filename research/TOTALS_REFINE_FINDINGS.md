# TOTALS_REFINE — Realistic-execution re-score of the certified NBA totals edge

_Date: 2026-06-08. Branch: `claude/hedge-fund-strategy-analysis-jyXWB`.
Scope: NBA totals pace-anchoring (the one certified edge), existing cached
winner+total pkls. Follow-up to DIAGNOSTIC_1.md / ALPHA_FINDINGS.md. No git, no
test-split usage._

## TL;DR (the single most important thing to know before risking capital)

**The certified +6.21¢/contract totals edge does NOT survive realistic
execution. It collapses to ~+0.5¢ gross of spread and goes NEGATIVE at any
realistic bid-ask, and it FAILS the promotion gate at every modeled spread.**
The certified number was almost entirely an artifact of the flat-cost model's
assumption that you fill the at-the-money over/under at exactly 0.50. You cannot:
by the time pace says "over," the actual listed over contract already prices at
~0.556, and Polymarket's 2% taker fee takes another ~1.9¢. No pre-registered
size/cushion variant (signal-strength gap, entry window) recovers it.

**Verdict: realistic execution kills it. Do not size this.**

## What "realistic execution" means here (pre-registered before scoring)

The certified strategy BUYS an at-the-money over/under and HOLDS TO SETTLEMENT.
The flat model scored `pnl = payoff − 0.50 − flat_cost` against a *fabricated*
strike interpolated to where P(over)=0.50 exactly. Realistic execution
(`research/scripts/totals_realistic.py`) replaces that with three measured pieces:

1. **Actual listed strike (no fabricated interpolation).** You can only buy a
   *listed* strike. We snap the bet to the listed strike nearest the implied
   total (real ladder; Kalshi spacing ~3 pts, PM ~1 pt) and settle PnL vs THAT
   real strike. The fill mid is the strike's **real quoted P(over)**, taken at
   honest +1-bar latency. This is a measured cost, not an assumption.
2. **Entry half-spread (modeled).** The cached `prob` is a single mid/last per
   strike (no explicit book). Venue min tick = 1¢; measured near-ATM one-step
   |Δprob| ≈ 2¢. Pre-registered central estimate **1.5¢ half-spread**, swept
   0/1/1.5/2/2.5¢.
3. **Polymarket 2% flat taker fee** on entry notional (`0.02·price`, plus a tiny
   curve piece), mirroring `kalshi_arbitrage.mock_execution.FeeModel` /
   `research.harness.realistic_fills._polymarket_taker_fee`. Held to settlement,
   so ONE entry fee, NO exit fee. At ATM ~0.50 → ~1.9¢ here (price ~0.556).

Direction (`continuation`), threshold (6), freshness guard (≤2 min), +1-bar
latency, and the full promotion gate are UNCHANGED. **Test ids are excluded**
(population = 1,127 non-test games, 1,107 trades), so this is, if anything,
stricter than the certified full-season run.

## Result 1 — realistic-cost re-score (gate verdict)

Half-spread sensitivity, full non-test population, direction pre-registered:

| half-spread | n | net ¢/ct | block-bootstrap 95% CI | gate |
|---|---|---|---|---|
| 0.0¢ | 1,107 | **+0.53** | [−2.28, +3.43] | ❌ FAIL |
| 1.0¢ | 1,107 | −0.49 | [−3.30, +2.41] | ❌ FAIL |
| **1.5¢ (central)** | 1,107 | **−1.00** | [−3.82, +1.90] | ❌ FAIL |
| 2.0¢ | 1,107 | −1.52 | [−4.33, +1.38] | ❌ FAIL |
| 2.5¢ | 1,107 | −2.03 | [−4.84, +0.87] | ❌ FAIL |

- **Realistic net cents/contract (central 1.5¢ spread): −1.00¢.**
- **Breakeven cost: the edge survives only ~+0.53¢ of total additional drag
  beyond the real listed-strike fill — i.e. it breaks even at essentially a
  zero-spread, but-still-pay-the-2%-fee world, and is under water for any
  nonzero spread.**
- **Gate: FAIL at every half-spread, including 0¢.** (Certified was PASS @2¢
  flat, CI [+3.59,+8.83]. Under realistic fills the CI lower bound is negative
  everywhere.) At the central 1.5¢ spread the gate fails on *every* sub-check:
  main CI lo −3.82¢, both knockouts negative, season-split (early lo −2.68¢,
  late lo −7.40¢, no overlap), parity-split (even lo −4.85¢, odd lo −5.26¢).

### Monthly walk-forward under realistic execution (the primary OOS evidence)

The certified edge's strongest OOS claim was "7/9 months net-positive after 2¢."
Under realistic fills (1.5¢ spread + 2% fee) that collapses to **3/8 months
net-positive, and NO month has a CI lower bound above zero**:

| month | n | win% | net ¢/ct | CI lo |
|---|---|---|---|---|
| 2025-10 | 66 | 67% | +10.67 | −1.08 |
| 2025-11 | 216 | 59% | +1.12 | −5.55 |
| 2025-12 | 197 | 57% | −2.43 | −9.21 |
| 2026-01 | 231 | 61% | +1.70 | −4.71 |
| 2026-02 | 166 | 55% | −5.38 | −12.92 |
| 2026-03 | 224 | 55% | −3.51 | −9.97 |
| 2026-05 | 5 | 20% | −37.02 | — |
| 2026-06 | 2 | 0% | −52.81 | — |

Only the small-n opening month is meaningfully positive; the bulk regular-season
months (Nov–Mar) are flat-to-negative.

### Decomposition — where the certified +8¢ went (the decisive evidence)

| component | ¢/contract |
|---|---|
| `payoff − 0.50` (flat-model gross, reproduces certified +8.21¢) | **+8.00** |
| real listed fill price you actually pay (avg **0.5555**, not 0.50) | −5.55 |
| `payoff − mid` (true tradeable gross, no spread/fee) | **+2.44** |
| Polymarket 2% taker fee (~1.9¢ at ATM) | −1.91 |
| `payoff − mid − fee` (realistic, 0 spread) | **+0.53** |
| − 1.5¢ half-spread | **−1.00** |

The win rate (0.580) confirms the *direction* is real — pace does predict the
over. But the market price already embeds almost all of it: the at-the-money
over contract on the signal side prices at ~0.556, leaving only **+2.44¢** of
residual gross signal, which the 2% fee alone erases. **~5.55¢ of the certified
"edge" was the gap between a fabricated 0.50 fill and the real contract price** —
the exact "execution is a flat 0.50 fill" caveat (DIAGNOSTIC_1 Caveat B) was
load-bearing, and it was fatal.

## Result 2 — size / cushion variants (all pre-registered, none recover it)

All scored at the central realistic 1.5¢ half-spread + 2% fee, full non-test
population, direction unchanged (no refit):

**(a) Signal-strength conditioning** (larger |proj−line| gap):

| min gap | n | net ¢/ct | gate |
|---|---|---|---|
| ≥6 (base) | 1,107 | −1.00 | ❌ |
| ≥8 | 1,107 | −0.90 | ❌ |
| ≥10 | 1,107 | −1.53 | ❌ |
| ≥12 | 1,101 | −0.81 | ❌ |

Stronger signal does **not** help — and the gross `payoff−mid` is flat/declining
across gaps (+2.44/+2.55/+1.92¢) while the avg fill mid *rises* (0.5555→0.5581).
The stronger the pace signal, the more adversely the over already prices. There
is no cushion to buy by waiting for a bigger gap.

**(b) Entry-window cap** (enter only before Q3/Q4): **no effect** (n unchanged at
1,107 for caps at 1800s and 2160s) — the first qualifying signal almost always
fires early (the |proj−line| gap is largest when `elapsed` is small and the pace
projection is noisiest), so capping later entry changes nothing.

**No variant produces a positive net ¢/ct at realistic cost, and none passes the
gate.**

## Adversarial checks (4) — the realistic result is honest, not pessimism

1. **Estimator bias / unfair strike-snap?** No. The avg fill mid (0.5555) is
   close to the realized win rate (0.580); the contract is near-fairly priced
   with only +2.44¢ residual. We are not snapping to a too-far strike — the
   true gross edge is genuinely ~2.4¢.
2. **Concentration?** No. Top-team |PnL| share = 1.6% (well under caps).
3. **Staleness?** Controlled — the ≤2-min freshness guard is applied; entries are
   on fresh, recently-changed lines.
4. **OOS / season-half stability of the surviving +2.44¢ gross?** FAILS. H1
   +4.80¢ vs H2 +0.08¢ — the residual gross edge decays to zero in the second
   half. Even *gross of fees*, the leftover signal is unstable and consistent
   with market efficiency, not a durable edge.

## Bottom line (honest)

The pace-anchoring *direction* is real (58% hit on the over), but it is **already
priced in**: against the actual tradeable contract the residual is only ~+2.4¢
gross, the Polymarket 2% fee alone consumes it, and any bid-ask makes it
negative. The certified +6.21¢ was an execution-model artifact (a 0.50 fill that
does not exist). **No size variant rescues it; the surviving gross sliver is not
even season-half stable.** This is the same "positive in-sample → efficient under
honest conditions" pattern DIAGNOSTIC_1 documents for the dead directions — it
was hidden here only because the cost model under-charged execution by ~7.5¢.

**Recommendation: do not trade or size the totals edge. Mark it
execution-killed.** The honest project status is that the one "certified" edge
does not survive realistic fills, so there is currently NO tradeable certified
NBA edge in this data. (DIAGNOSTIC_1's gate is still sound — it was never the
problem; the cost model was.)

## Reproduce

```bash
# realistic-execution re-score + half-spread sensitivity (the gate verdict)
python3 -m research.scripts.totals_realistic --thresh 6 --min-elapsed 600 \
    --max-stale-min 2 --sweep-spread
# central case + monthly walk-forward + breakeven probe
python3 -m research.scripts.totals_realistic --thresh 6 --min-elapsed 600 \
    --max-stale-min 2 --half-spread 1.5 --walkforward
# size variants
python3 -m research.scripts.totals_realistic --thresh 8  --half-spread 1.5   # gap conditioning
python3 -m research.scripts.totals_realistic --max-entry-elapsed 1800 --half-spread 1.5
```

Files:
- `research/scripts/totals_realistic.py` — realistic-execution evaluator
  (listed-strike fill + half-spread + PM 2% fee; `reprice()` for fast spread
  sweeps; gap/entry-window size variants). Reuses the certified data loader and
  gate; never touches the test split.
