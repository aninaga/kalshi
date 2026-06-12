# Cross-market internal consistency (winner ↔ spread) — 2026-06-09T09:05:19Z

**Lane:** cross-market INTERNAL CONSISTENCY (a genuinely untried mechanism, distinct
from the spreads/totals anchoring family the other agents work).
**Hypothesis:** `8b8ca06b47c9a88f` (market=spread), agent `xmarket_consistency`,
claimed → done.
**Substrate:** `research/lab/*` only. Realistic `FillModel` (listed-strike snap +
1.5c half-spread + full Polymarket taker-fee curve), honest i+1 latency, freshness
guard ≤2 min, test split untouched (`nontest`/`train` only), gate never weakened.
New file only: `research/lab/strategies/xmarket.py`.

## Mechanism & the margin→winprob mapping assumption

For the SAME game and minute, the **spread** ladder maps `strike → P(home_margin >
strike)`. Evaluated at **strike = 0** that IS `P(home_margin > 0) = P(home win)`
(ties are impossible in the NBA). So the spread book carries its own implied
home-win probability that should equal the **winner** market mid (a direct
`P(home win)`).

**Mapping assumption (deliberately parameter-free):** I do NOT fit any
margin→winprob curve. `spread_pwin(t)` is read directly off the ladder by linearly
interpolating the per-minute `strike → P(margin>strike)` points to `strike = 0`,
using only that minute's finite, bracketing quotes. This is the whole point of the
lane — a fitted margin→winprob calibration is exactly where a cross-market "edge"
acquires **look-ahead** (fit on whole-game / future data). By reading the identity
`P(margin>0)` off the ladder, there is no fitted parameter and therefore no
calibration look-ahead to hunt: the map is point-in-time by construction.

`winner_pwin(t)` is the winner mid joined onto the spread panel by nearest
`elapsed_sec` (≤90s tolerance). **Divergence signal:** `xmkt_div = spread_pwin −
winner_pwin`.

## Pre-registered direction

**`fade_spread_toward_winner`.** EDA (halftime snapshot, n=19 dense games) showed the
winner book is the better-calibrated/leading book (Brier 0.217 vs spread-implied
0.260). So when `xmkt_div < 0` (spread under-prices home's win chance) take
`cover_home` (the strike-near-0 contract paying if `margin>0`); when `xmkt_div > 0`
take `cover_away`. Entry fires at the first fresh in-window minute where `|xmkt_div|
≥ threshold`. **Single realistic fill on the SPREAD leg only** — the winner mid is
the *reference* the spread is faded toward, NEVER a second fill (the two-leg-at-mid
trap is structurally impossible: there is exactly one real fill).

## Result: the pre-registered direction LOSES on train → DEAD

Train split (n=906 spread × 909 winner → 906 joined panels), realistic fills:

| fill model | thr | n | cents/ct | 95% CI |
|---|---|---|---|---|
| default (snap to implied-margin strike) | 0.06 | 159 | **−5.53** | [−13.10, +2.07] |
| default | 0.08 | 158 | −4.12 | [−11.67, +3.51] |
| default | 0.10 | 158 | −4.36 | [−11.91, +3.31] |
| default | 0.12 | 157 | −3.42 | [−11.23, +4.42] |
| strike-0 snap (contract = signal) | 0.06 | 159 | **−6.71** | [−13.03, −0.31] |
| strike-0 snap | 0.08 | 158 | −5.92 | [−12.33, +0.64] |
| strike-0 snap | 0.10 | 158 | −4.25 | [−10.55, +2.11] |
| strike-0 snap | 0.12 | 157 | −3.67 | [−9.98, +2.84] |

The pre-registered "fade toward the winner book" direction is **net-negative at every
threshold under both fill models**, with CI upper bounds barely above zero. The
hypothesis is falsified on train; I did not threshold-mine the full population for a
pass (that would be fishing on a pre-registered direction that already lost).

### Final nontest gate (pre-registered config, thr=0.08), for completeness

Full `nontest` population (1112 joined panels, n=243 trades),
`L.evaluate(..., ledger_path="research/reports/alpha/ledger.jsonl")`:

| fill model | n | cents @0c | 95% CI | gate |
|---|---|---|---|---|
| default (implied-margin snap) | 243 | **−5.89** | [−11.97, +0.35] | FAIL |
| strike-0 snap (contract = signal) | 243 | **−4.44** | [−9.47, +0.69] | FAIL |

Cost sweep is monotone-worse (−5.89 → −9.89 default; −4.44 → −8.44 strike-0 across
0→4c). Gate FAIL reasons (both fills): `block_bootstrap_ci_lo ≤ 0`,
`knockout_drop_top_games/top_team/first_quarter ≤ 0`. **OOS both halves negative**
(default H1 −4.12c / H2 −7.65c; strike-0 H1 −5.69c / H2 −3.20c) — not a single-window
fluke, the direction is wrong everywhere. This is fully consistent with the train
falsification; no threshold or split rescues it.

## Bug-hunt (the three traps this lane is prone to)

1. **Calibration look-ahead — ELIMINATED BY DESIGN.** There is no fitted
   margin→winprob model; `spread_pwin` is the identity read of the ladder at
   strike 0, point-in-time per minute. So the classic cross-market look-ahead
   (fitting margin→winprob on whole-game data) cannot occur. `xmkt_div` reads only
   the current minute's quotes from both books, joined by elapsed time — no future
   information. (The leakage auditor would have nothing fitted to flag.)

2. **Two-leg fill realism — STRUCTURALLY AVOIDED.** I trade ONE leg (the spread
   contract), filled by the substrate's REALISTIC `FillModel`. The winner mid is a
   reference, never a fill, so the "assume you trade both legs at mid" artifact is
   impossible. I additionally built a strike-0 fill so the *traded contract*
   (`margin>strike≈0`) matches the *signal* (`P(margin>0)`); without it the default
   snaps to the strike nearest the implied margin, settling on the wrong threshold —
   I report both and the conclusion is the same (negative).

3. **Divergence inside the bid/ask / measurement noise — THIS IS THE KILLER.** The
   "divergence" is dominated by noise in deriving `P(margin>0)` from a **sparse,
   non-monotone** spread ladder (13.7% adjacent-strike monotonicity violations; the
   blended Kalshi/PM quotes are noisy near 0). Aggregated across all minutes the mean
   `spread_pwin − winner_pwin` is a large structural −0.148 (early-game/sparse-ladder
   bias), NOT a mean-zero gap with tradeable tails — the signature of a **mapping
   artifact**, not a real inefficiency. Only ~158 of 906 games ever fire (one
   trade/game at the first fresh, in-window, |div|≥thr bar; the freshness guard
   removes just 20% of in-window bars, so the bottleneck is that large divergences
   cluster where the strike-0 interpolation is least reliable). Where the divergence
   is real and fresh, it does not predict convergence in the profitable direction.

4. **Direction-flip check (is there ANY exploitable structure?).** Trusting the
   spread book instead (flipped side) is also non-tradeable: best +0.55c @thr=0.06
   (CI lo −5.86, insignificant) decaying to −1.88c @thr=0.10. Neither direction
   carries a significant edge — consistent with "no real cross-market inefficiency,"
   not "right idea, wrong sign."

## Verdict

The two books are **internally consistent enough that the residual divergence is
not tradeable**: it is dominated by the noise of reading `P(margin>0)` off a sparse,
non-monotone spread ladder, the pre-registered fade-toward-winner direction is
net-negative across all thresholds and both fill models on train, the opposite
direction is also insignificant, and the gate is not met. This is an honest negative
on a genuinely new mechanism — the cross-market consistency edge does not exist in
2025-26 NBA winner/spread data after realistic fills. No calibration look-ahead and
no two-leg-mid artifact (both designed out), so the negative is clean. Marked DEAD in
the registry; trial appended to the ledger.

## Reproduce
```bash
PYTHONPATH=. python3 - <<'PY'
from research.lab import data
from research.lab.strategies.xmarket import attach_winner_mid, xmarket_consistency
from research.lab.session import lab
sp=data.load_panels("spread","train"); wn=data.load_panels("winner","train")
panels=attach_winner_mid(sp,wn); L=lab()
for thr in (0.06,0.08,0.10,0.12):
    g=L.evaluate(xmarket_consistency(threshold=thr).run(panels))
    print(thr, round(g.cents_per_contract,2), round(g.ci_lo,2), round(g.ci_hi,2))
PY
```

DEAD
