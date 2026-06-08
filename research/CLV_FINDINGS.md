# NBA Pre-game / Closing-Line-Value (CLV) — alpha findings

_Date: 2026-06-08. Branch: `claude/hedge-fund-strategy-analysis-jyXWB`.
Direction: **pre-game / CLV** (a new TEMPORAL regime — all prior NBA work was
in-game). Verdict: **DEAD**._

## TL;DR

The pre-registered mechanism — **early-pregame drift continuation** on the
Kalshi NBA winner (moneyline) contract — is **in-sample noise that inverts
out-of-sample**. It nets a positive but sub-cost-floor +3.2c/contract gross on
TRAIN, **−2.7c/contract on the VAL holdout**, and ≈0 (+0.13c at 2c, CI lower
bound −3.05c) on the full ex-test population. It fails the promotion gate on
every cut and at every observation window tested. This is the same
calibration-mirage failure mode the methodology warned about (the prior in-game
favorite-longshot edge: +1.35c train → −3.83c val). **Not a real edge.**

A secondary, genuinely useful finding for future workers: **Polymarket CLOB
`prices-history` returns ZERO points for resolved markets**, so a wide-pregame
CLV test is impossible from Polymarket after settlement. **Kalshi
`/historical/markets/{ticker}/candlesticks` DOES persist** the full ~24–48h
pregame window, so CLV on the Kalshi winner contract was testable — and was
tested here. The data exists; the edge does not.

## Pre-registered mechanism (verbatim, fixed before running)

See `research/reports/alpha/clv_mechanism.md`. One paragraph: pregame lines open
thin/stale and incorporate information (injuries, lineups, sharp money) slowly
through the day; if the market under-reacts to early flow, the *direction* of the
early pregame drift should CONTINUE and predict the settled outcome. Tradeable
form: `p_open` = Kalshi home-winner mid at market open (earliest candle ≥3h
before tip); `p_obs` = mid at `obs_min` minutes before tip; `drift = p_obs −
p_open`; if `|drift| ≥ thresh`, **continuation**: drift>0 ⇒ buy home, drift<0 ⇒
buy away. Honest +1-bar entry latency (`enter at obs_min−1`), hold to
settlement, one trade per game. Pre-registered config: `obs_min=60`,
`thresh=0.02`. Direction = **continuation** (the reversion side is the diagnostic
complement only).

## Data layer (what I built — and a real data finding)

- **Polymarket pregame history is GONE after settlement.** The CLOB
  `prices-history` endpoint returns 0 points for every resolved 2025-26 NBA token
  (verified across multiple games, all `fidelity`/`interval` params). The cached
  `dataset.build` winner pkls only froze the last **30 minutes** before tip
  (`PRE_GAME_SEC=1800`), and that window is essentially **flat** (mean |open→close
  move| = 0.7c across 184 sampled games; only 8% move ≥2c). 30 min is NOT a real
  open→close window — too short and too settled to test CLV.
- **Kalshi historical candlesticks DO persist.**
  `/historical/markets/{ticker}/candlesticks` returns the full ~24–48h pregame
  window for settled markets (markets open ~1–2 days before tip). This is the
  real CLV data source. I built a **cloned, additive helper**
  (`research/scripts/clv_alpha.py::_fetch_pregame_candles`) that fetches the wide
  pregame Kalshi home-winner mid series into a **separate** cache
  (`market_data/nba_studies/_clv_cache/`) — it does NOT modify `dataset.py` or the
  existing pkls. Tip time + settlement are read from the existing winner pkls.
- **Operational note:** Kalshi rate-limits aggressively under concurrency. The
  fetch must run **serially** (1 worker); concurrent bursts trigger multi-minute
  IP throttles. Built all 1,118 trainval (= full ex-test) games this way; the
  cache is idempotent/resumable.

## Evaluator

`research/scripts/clv_alpha.py` (fetch + evaluate) and
`research/scripts/clv_eval_cached.py` (read-only, cache-only, used for the final
scoring so it never competes with the fetcher for the rate limit). Both build
real per-trade PnL on the **actual contract price** (`home_won − p_entry` for a
long-home, `p_entry − home_won` for a short-home), apply honest +1-bar entry
latency, hold to settlement, sweep round-trip cost {0,1,2,3,4}c, and score every
cut through `research.scorer.promotion_gate.evaluate_trial`. Direction is fit on
TRAIN (1-bit: higher 0c mean) and the **pre-registered continuation** direction
is applied to VAL and to the full ex-test population. Test split untouched.
`research/scripts/clv_diagnostics.py` runs the four adversarial checks offline on
the dumped trade frames.

## Result — pre-registered config (obs_min=60, thresh=0.02)

| set | n | @0c | @2c | block-bootstrap 95% CI @2c | gate |
|---|---|---|---|---|---|
| TRAIN (dir fit) | 627 | +3.16c | +1.16c | [−2.55, +4.74] | ❌ |
| **VAL (clean holdout)** | **134** | **−2.70c** | **−4.70c** | **[−11.94, +2.38]** | ❌ |
| FULL ex-test (pre-reg dir) | 761 | +2.13c | **+0.13c** | **[−3.05, +3.34]** | ❌ |

The edge **inverts sign** train→val (+3.16c → −2.70c at 0c). On the only
gate-eligible cut (full ex-test, n=761 ≥ 200) the point estimate at 2c is
**+0.13c with a CI lower bound of −3.05c** — indistinguishable from zero and far
from the gate's CI>0 requirement. Val also fails the n≥200 floor (only 134 of
199 val games fire the signal), exactly the small-n problem from the totals work
— but unlike totals, the point estimate is **negative**, so more data would not
rescue it.

## Robustness across observation window (NOT a fishing search — a stability check)

Every `obs_min` shows the same train-positive / val-negative inversion:

| obs_min | TRAIN @0c | VAL @0c | VAL @2c | FULL ex-test @0c |
|---|---|---|---|---|
| 30  | +3.88c | −1.22c | −3.22c | +2.97c |
| 60  | +3.16c | −2.70c | −4.70c | +2.13c |
| 120 | +4.05c | −4.35c | −6.35c | +2.58c |

The train edge is a robust in-sample mirage; the val inversion is robust too. No
window survives. (The single reported direction stays `continuation`; the
reversion complement is symmetric-negative on train and so is never promoted.)

## The four adversarial checks (full ex-test, n=761)

1. **Staleness:** signals require the line to have actually moved between open and
   obs (`require_open_move`), and `p_open`/`p_obs`/`p_entry` are read from real
   candles with honest +1-bar entry. Not a stale-quote artifact.
2. **Estimator/measurement bias:** the UNCONDITIONAL "buy home at entry, hold"
   nets **−0.98c/contract** (≈calibrated; per-bucket realized−price within ±6pp).
   So the market is fair at the entry price and the train +2.13c was a genuine
   *conditional* drift effect, NOT a constant level-measurement bias. (It just
   doesn't generalize.)
3. **Concentration:** top single game = **0.3%** of |PnL|, top team = **5.0%** —
   far under the 20%/25% caps. Broad-based, not one game/team.
4. **Out-of-sample:** the decisive failure — train +3.16c → **val −2.70c** (sign
   flip), robust across obs windows. Drift-magnitude shows no clean dose-response
   (+2.05 / +2.68 / +1.56c across |drift| terciles).

Checks 1–3 pass (it is a real, broad, calibrated *in-sample* conditional effect),
but check 4 kills it: the sign does not persist out of sample. Default
`real_edge=false` stands.

## Verdict: **DEAD**

The pre-game CLV / early-drift-continuation mechanism on the NBA Kalshi winner
contract is not a real, tradeable edge. It is positive in-sample, **inverts
out-of-sample**, sits at ≈0 (+0.13c at 2c, CI lo −3.05c) on the full population,
and fails the promotion gate on every cut and observation window. The pregame
moneyline market is efficient in the open→close→settlement direction; the early
pregame drift carries no persistent, capturable information about the settlement
beyond what is already in the price (which is calibrated). This mirrors the
in-game moneyline result: micro-structure is picked clean. The certified edge
remains the in-game **totals** pace-anchoring line; **moneyline — in-game OR
pregame — is efficient.**

This was a genuine test, not a NEEDS_DATA: Kalshi historical candlesticks supply
the full pregame window. (NEEDS_DATA *would* apply to a Polymarket-only CLV test,
since its resolved-market history is unavailable — noted for future workers.)

## Reproduce

```bash
# build the wide-pregame Kalshi winner candle cache (serial — Kalshi throttles)
python3 -m research.scripts.clv_alpha --split trainval --obs-min 60 --thresh 0.02 --workers 1 \
    --dump-trades research/reports/alpha/clv_trainval.parquet
# final scoring (cache-only): train dir-fit + pre-registered continuation on val + full ex-test
python3 -m research.scripts.clv_eval_cached --obs-min 60 --thresh 0.02 \
    --dump-prefix research/reports/alpha/clv_final
# adversarial diagnostics
python3 -m research.scripts.clv_diagnostics research/reports/alpha/clv_final_full.parquet
```

```json
{"market": "nba_winner_pregame_kalshi",
 "mechanism_one_line": "early-pregame drift (p_obs - p_open) continuation predicts settlement",
 "gate_passed": false, "n_games": 761,
 "cents_per_contract_0c": 2.13, "cents_per_contract_2c": 0.13,
 "block_bootstrap_ci_lo_2c": -3.05, "real_edge": false,
 "val_cents_per_contract_0c": -2.70, "val_cents_per_contract_2c": -4.70,
 "artifact_checks_done": ["staleness","estimator_bias","concentration","oos"],
 "verdict": "DEAD",
 "next_step": "pregame moneyline is efficient; do not revisit. CLV on the totals/spread book is the only untested pregame angle, but expect the same anchoring already captured in-game."}
```
