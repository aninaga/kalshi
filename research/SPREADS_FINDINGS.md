# NBA Spread / Handicap Book — Edge Search Findings

_Date: 2026-06-08. Scope: NBA spread (point-handicap) markets only, via the
existing `nba_odds_study` pipeline (ESPN PBP + Kalshi `KXNBASPREAD` + Polymarket
`Spread:` markets), same venues/games as the certified totals edge._

## 0. Pre-registered mechanism + direction (written BEFORE running the gate)

**Market.** The NBA *spread / handicap* book — Kalshi `KXNBASPREAD-...` markets
("Team X wins by over N.5 points") and Polymarket `Spread: Team (-N.5)` markets.
From these I reconstruct, per minute, an **implied home margin** `imp_t`: the
signed point at which `P(final home margin > x) = 0.5`, interpolated across the
strike ladder (the spread analog of the certified `_implied_total`).

**Mechanism (one paragraph, mirror of the totals pace-anchoring edge).** The
pregame spread is set by the closing line. Once the game is live, the *actual
margin trajectory* is the dominant new information about the final margin. The
hypothesis: the live spread line **anchors on the pregame number and
under-reacts to the live margin trajectory** — i.e. when the projected final
margin (current margin extrapolated to a full game) is more extreme than the
implied-margin line, the line lags and the final margin tends to finish in that
projected direction more often than the line implies. This is the same
behavioral anchoring that the totals book exhibits on pace, applied to the
margin/spread book. If it exists it should persist because the spread book is
less-watched than moneyline and re-marking a live handicap ladder is slower than
re-marking a single win-prob.

**Direction (pre-registered, ONE bit): `continuation`.** When the pace-projected
final home margin `proj_t = margin_t * 2880 / elapsed_t` exceeds the implied
margin `imp_t` by at least a threshold (line too low on the home side), **BUY the
HOME side** of the at-the-money spread (bet final home margin > strike). When
`proj_t < imp_t - thresh`, BUY the AWAY side. Hold to settlement. We trade the
at-the-money contract (strike = implied margin, price ≈ 0.50), locking the strike
one bar AFTER the signal (honest +1-bar latency). One trade per game.

**Secondary candidate (only if continuation is dead): favorite-longshot / level
bias at extreme spread prices** — extreme spread strikes (deep favorite covers)
may be systematically over/under-priced. Pre-registered as a fallback only; not
fished alongside the primary.

**Falsification.** If `continuation` is not net-positive at 2c with a
block-bootstrap-by-game CI lower bound > 0 on the full population, AND the
monthly walk-forward is not majority-positive, the spread anchoring edge is DEAD.
The unconditional `P(final home margin > implied margin)` at a clean snapshot
must be ≈ 0.50 for the estimator to be unbiased; if the conditional edge is just
that baseline shifted, it is a measurement artifact, not alpha.

---

## 1. Data availability (STEP 0)

_(filled after probe — see below)_

## 2. Evaluator

`research/scripts/spread_alpha.py` (clone of `totals_alpha.py`): builds an
implied-home-margin curve from the spread ladder, honest +1-bar latency,
freshness guard, hold-to-settlement, scored by `promotion_gate.evaluate_trial`.
`research/scripts/spread_walkforward.py`: full-population gate + monthly OOS.

## 3. Results

_(filled after running)_

## 4. Adversarial checks

_(filled after running)_

## 5. Verdict

_(filled after running)_

## Coordinator preliminary result (n=267 cached, fire-4 finished by coordinator)
The fire-4 agent stalled waiting on its data build; coordinator ran the eval.
Spread continuation (anchoring) PRELIMINARY, full population (pre-registered):
- cost 0c: +6.18c/ct, CI [+0.19, +12.17]
- cost 2c: +4.18c/ct, CI [-1.81, +10.17]  (gate FAIL on CI lo + season/parity stability)
- monthly walk-forward: 2/2 months net-positive at 2c (Oct +1.03, Nov +5.21)
VERDICT (preliminary): PROMISING / NEEDS_DATA — positive point estimate, mechanism
mirrors the CERTIFIED totals anchoring edge, but under-powered at n=267 (same as
totals at n=199 pre-certification). Building the FULL season to attempt certification.

## FULL-SEASON RESULT (coordinator, n=1297, direction pre-registered)
Built all ~1305 spread games and re-scored (the DIAGNOSTIC_1 power table predicted
n=267 was uncertifiable, not fake). Spread continuation (anchoring):
- cost 0c: +7.67c/ct, CI [+4.97, +10.37]
- cost 2c: +5.67c/ct, CI [+2.97, +8.37]   <- DECISIVE block-bootstrap CI lo > 0
- cost 3c: +4.67c/ct, CI [+1.97, +7.37]
- cost 4c: +3.67c/ct, CI [+0.97, +6.37]   <- still CI lo > 0 at 4c
- monthly walk-forward: 8/9 months net-positive at 2c (only June n=2 negative)
- FULL gate @2c: FAILS on ONE sub-gate only — parity_split (even_ci_lo=-0.0183 marginal,
  odd=+0.0502, no overlap). All other criteria (bootstrap CI, n>=200, knockouts,
  season-split) pass.

VERDICT: STRONG SECOND ANCHORING EDGE. Point estimate (+5.67c@2c) matches the
certified totals edge (+6.21c@2c); decisive block-bootstrap CI is positive at all
costs to 4c; 8/9 OOS months. Not FULLY gate-certified — blocked solely by the
conservative parity stability sub-gate, which DIAGNOSTIC_1 showed is low-power at
one-season n (a real +6c edge fails the full gate ~40% of the time at n~1300).
The anchoring family (line lags live game-state) GENERALIZES totals -> spreads.
Same caveats as totals: full-pop gate includes train (OOS rests on pre-registration
+ walk-forward); confirm realistic fills (the totals-refine agent is hardening this).

---

## REALISTIC EXECUTION RE-CHECK

_Date: 2026-06-08. `research/scripts/spread_realistic.py`. Same realistic-fill
template as the totals re-check (`totals_realistic.py` / TOTALS_REFINE_FINDINGS.md):
snap the bet to the ACTUAL listed signed-home spread strike nearest the implied
margin, fill at that strike's REAL quoted P(home margin > strike) (NOT 0.50),
cross a half-spread to take liquidity, pay the Polymarket 2% flat taker fee on
entry, settle vs the REAL strike, honest +1-bar latency, hold to settlement.
Population = 1,127 NON-test games (1,107 trades). Direction (`continuation`),
threshold (6), freshness guard (≤2 min) UNCHANGED; no refit; test split untouched._

### TL;DR — NOT a pure 0.50-fill artifact; survives gross but FAILS the gate

**Unlike the totals edge, the spread market did NOT price away most of the
signal.** The real listed at-the-money fill mid is only **0.5187** (vs 0.5555 for
totals), so the true tradeable gross is **+5.22¢** and survives the 2% fee + a
1.5¢ half-spread at **+1.88¢ net**. The edge is NOT a 0.50-fill artifact the way
totals was — roughly half the flat-model number is genuine residual signal. BUT
the realistic net is too small to clear the promotion gate: the block-bootstrap
CI lower bound goes negative at any nonzero half-spread, and the season/parity
stability sub-gates fail. **Real direction, real (small) residual edge, but NOT
gate-certified and NOT comfortably tradeable after costs.**

### Decomposition — where the reported +5.67¢ went

| component | ¢/contract |
|---|---|
| `payoff − 0.50` (flat-model gross, reproduces reported artifact) | **+7.09** |
| real listed fill mid you actually pay (avg **0.5187**, not 0.50) | −1.87 |
| `payoff − mid` (TRUE tradeable gross, no spread/fee) | **+5.22** |
| Polymarket 2% taker fee (avg ~1.8¢) | −1.79 |
| `payoff − mid − fee` (realistic, 0 half-spread) | **+3.43** |
| − 1.5¢ half-spread (central) | **+1.88** |

Win rate = 0.571. Decisive contrast with totals: there the real fill (0.5555) ate
**−5.55¢** and left only +2.44¢ gross; here the real fill (0.5187) eats only
**−1.87¢** and leaves **+5.22¢** gross. The spread book under-reacts MORE / prices
the live-margin signal LESS than the totals book — consistent with the
pre-registered mechanism (the handicap ladder is slower to re-mark). The
flat-model `payoff−0.50` (+7.09¢) is close to the reported +7.67¢@0¢ / +5.67¢@2¢;
the gap to my number is the test-split exclusion + the listed-strike snap.

### Realistic-cost re-score (half-spread sensitivity, full non-test population)

| half-spread | n | net ¢/ct | block-bootstrap 95% CI | gate |
|---|---|---|---|---|
| 0.0¢ | 1,107 | **+3.43** | [+0.59, +6.25] | ❌ FAIL |
| 1.0¢ | 1,107 | +2.39 | [−0.44, +5.22] | ❌ FAIL |
| **1.5¢ (central)** | 1,107 | **+1.88** | [−0.95, +4.70] | ❌ FAIL |
| 2.0¢ | 1,107 | +1.36 | [−1.47, +4.19] | ❌ FAIL |
| 2.5¢ | 1,107 | +0.85 | [−1.98, +3.67] | ❌ FAIL |

- **Realistic net (central 1.5¢ spread): +1.88¢/contract** (still positive — unlike
  totals' −1.00¢).
- **Breakeven: survives ~+1.88¢ of ADDITIONAL drag** on top of the realistic
  listed-strike fill + 2% fee (net hits 0 at ~2¢ extra cost).
- **Gate: FAIL at every half-spread, including 0¢.** Even at 0¢ where the main CI
  lo is +0.59¢, the gate fails on the stability sub-gates. At the central 1.5¢:
  `block_bootstrap_ci_lo −0.0095 ≤ 0`; `season_split early_ci_lo=−0.0226,
  late_ci_lo=−0.0209 (no overlap)`; `parity_split even_ci_lo=−0.0440,
  odd_ci_lo=−0.0036 (no overlap)`. The realistic residual is too small/noisy to be
  significant by-game.

### Monthly walk-forward under realistic execution (1.5¢ spread + 2% fee)

| month | n | win% | net ¢/ct | CI lo |
|---|---|---|---|---|
| 2025-10 | 66 | 52% | −2.97 | −14.84 |
| 2025-11 | 215 | 57% | +1.57 | −5.02 |
| 2025-12 | 197 | 60% | +4.01 | −2.85 |
| 2026-01 | 232 | 56% | +0.97 | −5.35 |
| 2026-02 | 166 | 61% | +6.80 | −0.57 |
| 2026-03 | 224 | 54% | −0.90 | −7.30 |
| 2026-05 | 5 | 80% | +29.27 | −4.59 |
| 2026-06 | 2 | 0% | −74.14 | −94.96 |

**5/8 months net-positive** at realistic cost (was 8/9 under the flat 0.50/2¢
model). The bulk regular-season months (Nov–Feb) are positive but no single
month has a CI lower bound above zero. Better than the totals walk-forward (3/8,
all flat-to-negative) but not a clean OOS pass.

### Bottom line — is the spread edge real & tradeable, or another 0.50-fill artifact?

**It is NOT another pure 0.50-fill artifact** — that is the key finding and the
clear contrast with totals. The spread book genuinely under-reacts to the live
margin trajectory: the at-the-money contract on the signal side prices only
~0.519, leaving **+5.22¢ of real residual gross signal** that survives the 2%
fee and a central 1.5¢ spread at **+1.88¢ net positive**. The direction is real
(57% hit) and the residual is roughly half the flat-model number, not ~0.

**BUT it is NOT gate-certified and NOT comfortably tradeable.** At realistic cost
the by-game block-bootstrap CI lower bound is negative for any nonzero spread,
the season/parity stability sub-gates fail, breakeven is only ~+1.9¢ of extra
drag, and only 5/8 OOS months are positive (none with CI lo > 0). The reported
"+5.67¢@2c, STRONG" overstated it: ~3.8¢ of that was the 0.50-fill gap + missing
fee. The honest realistic figure is **~+1.9¢/contract, gate FAIL** — a marginal,
under-powered, real-but-thin edge that needs either a tighter execution path
(maker fills to avoid the half-spread + the 2% taker fee) or more data to
certify. Mark it PROMISING-BUT-UNCERTIFIED, not STRONG, and not "dead."

### Reproduce

```bash
python3 -m research.scripts.spread_realistic --thresh 6 --min-elapsed 600 --max-stale-min 2 --decompose
python3 -m research.scripts.spread_realistic --thresh 6 --min-elapsed 600 --max-stale-min 2 --sweep-spread
python3 -m research.scripts.spread_realistic --thresh 6 --min-elapsed 600 --max-stale-min 2 --half-spread 1.5 --walkforward
```

Files: `research/scripts/spread_realistic.py` — spread analog of `totals_realistic.py`
(listed signed-home-strike fill + half-spread + PM 2% fee; `reprice()` for fast
sweeps). Reuses `spread_alpha._kalshi_team` / `_implied_home_margin` and the
promotion gate; excludes the test split.
