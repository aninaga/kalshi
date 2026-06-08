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
