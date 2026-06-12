# winner / ORIGINATE — intra-game win-probability EXCURSION mean-reversion

_Date: 2026-06-09. Branch: `claude/hedge-fund-strategy-analysis-jyXWB`.
Lane: winner (moneyline) / originate. Hypothesis id: `8a6bdf0d6dd8e656`.
Verdict below._

## The EDA observation (what I grounded it in)

`lab.eda.scan_market("winner", split="train")` (n=390 games, 76k obs) ranks
derived lenses by `|corr|` with the per-minute calibration residual
`home_won − mid`. The single strongest lens, by a wide margin, is **`mid_zscore`:
corr −0.3715** (next is `margin_zscore` −0.28; everything else < 0.06). Its decile
split: when in-game `mid` sits in its **top decile** vs the game's own mean, the
residual is **−0.178** (price too high); in the **bottom decile**, **+0.203**
(price too low). Read naively that says: when the live home win-prob has swung
above its own game-average it is overpriced, below it is underpriced.

## The mechanism (and why it is NOT a dead family)

**Intra-game win-probability excursion mean-reversion.** When the live home
win-prob spikes above its own recent path, the market has over-extrapolated a
scoring run; the price should revert. This is deliberately distinct from the two
known-dead winner families:

- **NOT moneyline momentum/continuation** — momentum *follows* the move; this
  *fades* it. Opposite sign, opposite trade.
- **NOT favorite-longshot calibration** — that conditions on the *absolute* price
  level. This conditions on price *relative to its own recent trajectory* (a
  trailing z-score), so a 0.65 price can be a "high" or a "low" depending on where
  it came from. A different conditioning variable entirely.

It is also not the totals/spread anchoring-gap family (winner has no pace
projection; `lab.eda._market_gap` returns `None` for WINNER).

## Pre-registered direction (fixed BEFORE scoring)

**fade_the_swing:** high trailing `zscore(mid, window=10)` (mid spiked up) →
**buy AWAY / short home**; low trailing z (mid dipped) → **buy HOME**.
Entry on the first fresh (`staleness ≤ 2 min`), in-window
(`600 ≤ elapsed ≤ 2520 s`) bar with `|z| ≥ 2`. Honest i+1 latency, hold to
settlement, REALISTIC fills (winner: fill at the real interpolated win-prob,
never 0.50; +1.5c half-spread; PM 2% taker fee).

## The bug-hunt (the decisive part)

A −0.37 lens is "too good" for an efficient moneyline market — hunt the bug. The
EDA lens computes `mid_zscore` with `_zscore_in_game`, which uses the **whole-game
mean** — i.e. it peeks at future minutes. I measured the per-game correlation of
the z-score with the residual two ways:

| z-score | per-game corr(z, `home_won − mid`) | tradeable? |
|---|---|---|
| **whole-game** (the EDA lens) | **−1.00** (mechanical look-ahead) | NO |
| **trailing** (`signals.zscore`, causal) | −0.31 | only causal one |

The −1.00 is structural: within a game `home_won` is constant, so a z-score built
from the game's eventual mean is, by construction, perfectly anti-correlated with
`home_won − mid`. The aggregate −0.37 in the scan was that leakage diluted across
games. The only honest signal is the **trailing** z; its −0.31 residual corr is
itself a measurement confound (the residual `home_won − mid` mechanically shrinks
as `mid` rises, even for a perfectly calibrated book), not capturable alpha.

Decomposition on TRAIN (n=903 trades) confirms the trade, not the corr, is the
truth:

| direction | @0c (+fee only) | @1.5c half-spread | mean fill mid |
|---|---|---|---|
| **FADE (pre-registered)** | **−3.01c** | **−4.54c** | 0.450 |
| FOLLOW (opposite) | +0.04c | −1.49c | 0.550 |

Fade loses ~3c even gross, despite buying the *cheaper* side (fill mid 0.45). The
opposite (follow) is ≈0 gross — and follow is just moneyline momentum, already a
dead family and sub-cost-floor anyway. No post-hoc direction flip is taken.

## Gate numbers (pre-registered fade, `ledger_path` passed)

| split | n_games | cents/ct (0c) | block-bootstrap 95% CI | gate | OOS |
|---|---|---|---|---|---|
| TRAIN | 903 | **−4.54** | [−7.34, −1.75] | ❌ (sig. negative) | H1 −5.81 / H2 −3.27 ❌ |
| **VAL (OOS)** | 199 | **−1.89** | [−7.35, +3.52] | ❌ (also n<200) | H1 +1.38 / H2 −5.13 ❌ |

Cost sweep (TRAIN, half-spread 0→4c): −4.54 → −5.54 → −6.54 → −7.54 → −8.54c —
monotone negative, never crosses zero. VAL point estimate is also negative, so
more data would not rescue it (unlike the totals data-quantity story). Gate
reasons on train: `block_bootstrap_ci_lo ≤ 0`, all knockouts ≤ 0.

## Verdict

The EDA lens that motivated this was a whole-game-mean look-ahead artifact
(per-game corr −1.0 by construction). The causal, tradeable version of the
mechanism — fade an intra-game win-prob excursion measured by a trailing
z-score — is **significantly unprofitable** (−4.54c train, −1.89c val, gate fails
at every cost and OOS). The winner book is calibrated against its own path:
elevated prices are elevated for a reason, and fading them just pays the
calibration-fair price plus the spread/fee. Confirms the standing finding that
in-game moneyline is efficient. Registry + ledger updated.

DEAD
