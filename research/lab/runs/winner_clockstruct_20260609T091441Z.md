# winner / STRUCTURAL clock-discontinuity — run verdict

_Date: 2026-06-09. Branch: `claude/hedge-fund-strategy-analysis-jyXWB`.
Lane: winner / structural clock-discontinuity (an untried mechanism).
Agent: winner_clockstruct. Hypothesis id `42474e4e37d7e3ff`._

## TL;DR

The structural clock-discontinuity hypothesis on the NBA in-game win-prob
(moneyline) market is **DEAD** in both pre-registered flavors. The
quarter-break-overreaction fade loses **−32.7c/contract on train and −44.4c on
val** (the market does the opposite of overreacting into a break). The
last-minutes-contested "edge" (+3.5c train / +13.4c val) is a **textbook
decided-game / leader-follow artifact**: a control with the SAME contested-leader
logic but NO clock discontinuity (mid-game, elapsed 1200–2000s) produces an
**identical +4.27c residual**. Proximity to a quarter boundary or to the final
minutes adds **zero structural information**. Both variants fail the promotion
gate (CI lower bound < 0, estimator-bias check fails, OOS decays H1→H2). This
rules out another structural family: NBA win-prob is efficient *across* its clock
discontinuities, not just along smooth game time.

## Structural regime definition (pre-registered, fixed before scoring)

NBA game time is not smooth. Scoring pauses at quarter boundaries (elapsed ≈
720 / 1440 / 2160 s, each followed by a ~2–4 min break with no possible scoring),
and the final ~3 minutes are stoppage-heavy and high-variance. Two structural
regimes, keyed purely on `elapsed_sec` proximity to a discontinuity:

- **A — quarter-break.** A bar within ±75 s of a quarter boundary. Mechanism: if
  the win-prob was set on intra-quarter scoring pace, the extreme it reached is
  stale during the break (no scoring can occur), so it should revert toward 0.5.
- **B — last-minutes.** elapsed 2600–2820 s, contested (`|mid−0.5| ≤ 0.20`).
  Mechanism: a structural variance-regime mispricing in the stoppage-heavy
  endgame.

This is calendar/structural, distinct from the DEAD families: momentum
(follow-a-run), excursion mean-reversion (own-trailing-z), and favorite-longshot
(absolute price level). It is keyed on clock structure, not on the path or level.

## Pre-registered directions

- **A:** `break_overreaction_fade` — at a quarter boundary with `|mid−0.5| ≥ 0.18`,
  FADE toward 0.5 (if home favored → buy AWAY, else buy HOME).
- **B:** `last_minutes_contested_follow` — in the final-minutes contested band,
  FOLLOW the slight leader.

Honest i+1 entry latency and `lab.execution.REALISTIC` fills (real winner mid +
1.5c half-spread + PM 2% taker) throughout; one trade/game; hold to settlement.

## Gate numbers (REALISTIC fills, `ledger_path` = research/reports/alpha/ledger.jsonl)

### Variant A — quarter-break fade (DEAD, catastrophic)

| split | n | win | avg fill | gross (payoff−fill) | net | gate | CI |
|---|---|---|---|---|---|---|---|
| TRAIN | 862 | 0.244 | 0.541 | −29.73c | **−32.72c** | ❌ | [−36.22, −29.20] |
| VAL   | 198 | 0.172 | 0.585 | −41.34c | **−44.39c** | ❌ | [−50.85, −37.41] |

Cost sweep (train, extra-c → net): 0c −32.72 / 1c −33.72 / 2c −34.72 / 3c −35.72
/ 4c −36.72 (monotone, as expected). OOS: H1 −31.27c vs H2 −34.17c (gets worse).
The market does **not** overreact into quarter breaks — fading the leader at a
boundary is a disaster, and it is *worse* out-of-sample. Mechanism falsified.

### Variant B — last-minutes contested follow (DEAD, decided-game artifact)

| split | n | win | avg fill | gross | net | gate | CI |
|---|---|---|---|---|---|---|---|
| TRAIN | 247 | 0.575 | 0.508 | +6.70c | +3.50c | ❌ | [−2.95, +9.73] |
| VAL   |  46 | 0.674 | 0.508 | +16.57c | +13.40c | ❌ (n<200) | — |

Cost sweep (train): 0c +3.50 / 1c +2.50 / 2c +1.50 / 3c +0.50 / 4c −0.50 (breaks
even by ~3.5c). Adversarial: staleness PASS, concentration PASS,
**estimator_bias FAIL**, **OOS FAIL** (H1 +6.85c → H2 +0.18c — decays to zero).

## The bug-hunt (the decisive work)

The brief flagged that a "last-minutes" effect is the classic place a
decided-game / favorite-longshot artifact hides. It does — and the control proves
it.

1. **Decided-game profile (why raw PnL is positive at all).** Measured at the
   panel level: by the last 3 min, `|mid−0.5| = 0.454` and **the leader wins
   97.0%** of the time, with prices already at ~0/1. Any last-minutes
   leader-follow rule is mechanically near-profitable in *raw* terms because it
   bets on near-certainties — but you pay ~0.95 for a 0.97 contract, so the
   genuine residual is tiny and lives entirely in the *contested* band.

2. **net = (win − fill) decomposition by fill extremity.** Variant B's PnL is
   concentrated in the wider-priced sub-buckets (`fill|.5|[0.30,0.50)`: +19.7c,
   n=19; `[0.15,0.30)`: +6.3c) and ~0 at the near-coinflip core (`[0,0.15)`:
   −0.35c) — i.e. the "edge" is just betting the **clearer** leader. That is
   favorite-longshot/leader-persistence, not a clock effect.

3. **The control that kills it — same logic, NO discontinuity.** Contested-leader
   follow with identical band/side logic, scored in three regimes (train):
   - mid-game, elapsed **1200–2000 s** (no clock discontinuity): **+4.27c** net
   - at **quarter breaks** (±75 s of a boundary): **+5.33c** net
   - **last 3 min** (Variant B): **+3.50c** net

   The residual is **statistically the same regardless of proximity to a clock
   discontinuity**. The structural framing contributes nothing; B is pure
   leader-following that happens to be evaluated near the clock.

4. **OOS train→val.** Variant A inverts further negative (worse). Variant B's
   point estimate is positive on val but its train OOS sub-check decays H1
   +6.85c → H2 +0.18c (the classic decay-to-zero signature), it fails the
   estimator-bias check, val n=46 is far below the n≥200 floor, and the gate
   fails on train regardless.

5. **Direct structural calibration test (no trading).** Calibration residual
   `home_won − mid` measured just before vs just after each boundary, and the
   drift *across* the break: residuals are ≤1.5pp at every boundary and the
   across-break drift is ≤0.4pp with std 5–8pp — i.e. **no systematic over- or
   under-reaction across any quarter boundary**. The market is calibrated through
   its own clock discontinuities. Conditional-on-extremity residuals flip sign
   between adjacent extremity buckets within the same quarter (Q3 [0,0.15)
   +7.3pp vs [0.15,0.30) −7.7pp) — small-n noise, not a stable mechanism.

## Verdict rationale

- **A** (break overreaction): mechanism is *anti-confirmed* — the market trends
  through breaks rather than reverting; fading loses big and worse OOS.
- **B** (last-minutes): the positive raw number is fully explained by a
  decided-game / leader-follow confound that is present identically away from any
  clock discontinuity; fails the gate (CI_lo<0), estimator-bias, and OOS.

No structural clock-discontinuity mispricing exists in the win-prob market. This
is consistent with the program-wide finding that NBA moneyline (in-game and
pregame) is efficient; it additionally rules out the *structural/calendar* angle
on that market.

## Reproduce

```bash
# strategy module + runner are scratch (not committed); core logic:
#   A entry: |elapsed-{720,1440,2160}| <= 75 & |mid-0.5| >= 0.18 ; side = fade toward 0.5
#   B entry: 2600 <= elapsed <= 2820 & |mid-0.5| <= 0.20 ; side = follow leader
#   control: 1200 <= elapsed <= 2000 & |mid-0.5| <= 0.20 ; side = follow leader
# via research.lab.session.lab(), research.lab.strategy.Strategy, REALISTIC fills,
# research.lab.evaluate.evaluate(..., ledger_path="research/reports/alpha/ledger.jsonl")
# winner panels: research.lab.data.load_panels("winner", split="train"|"val")
```

DEAD
