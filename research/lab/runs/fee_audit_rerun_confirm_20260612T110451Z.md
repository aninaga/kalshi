# fee_audit re-run — end-to-end confirmation + a reproducibility discrepancy

**2026-06-12T11:04Z.** Companion to `runs/fee_model_audit_20260612T075325Z.md`
(which re-rated verdicts ANALYTICALLY because the per-market NBA caches were
gone). The caches were rebuilt (`prefetch_games --kinds spread,total`,
1320/1320 each, 0 failures) and the evaluators re-run END TO END, train/val
only. Two clean results and one finding that matters more than either.

## 1. Fee correction — CONFIRMED exactly

Isolating the fee by running each evaluator at `--legacy-fees` (the retired
flat-2%-of-notional + curve) vs default (official PM **sports** 300bps
parabolic), on the SAME rebuilt data:

| evaluator | n | legacy net | official net | Δ (fee) | analytical Δ |
|---|---|---|---|---|---|
| spread_realistic | 1064 | +4.99c | +6.14c | **+1.15c** | +1.15c ✓ |
| totals_realistic | 1085 | +0.72c | +1.94c | **+1.22c** | +1.27c ✓ |

avg PM fee per contract: **1.87–1.93c (legacy) → 0.72c (official)** — the
~1.15–1.21c reduction that drives Δ. The `venue_fees` module is correct on
real data; the analytical re-rate's fee arithmetic holds.

## 2. Gate verdicts — UNCHANGED (every run still FAILS)

Exactly as predicted (stability sub-gates are level-independent):
- **spread_realistic** (official +6.14c, CI[+3.20,+8.95]): FAIL — `parity_split overlap=False`.
- **totals_realistic** (official +1.94c, CI[-0.89,+4.89]): FAIL — bootstrap CI lo <0, season+parity overlap=False.
- **maker_fill_study** taker +5.37c PM / +4.11c Kalshi, maker +4.83c PM / **+3.83c Kalshi**: ALL FAIL on parity/season overlap. (Kalshi maker now charges the official quarter-of-taker fee, not the full taker the memo used.)
- **totals_extremes** (mechanism): DEAD — negative even at flat 0c on train (−0.94c) and full (−0.74c); calibration bias real (longshot −0.045, lock −0.053) but the strategy is unprofitable; fee correction does not enter this script (flat cost sweep). Verdict holds.

No verdict flipped. The PROGRAM_STATE conclusion (edge fails on stability, not
level) is robust to the fee correction end-to-end.

## 3. DISCREPANCY — the rebuilt cache is systematically MORE favorable than the original memos, on identical code

This is the finding to act on, and it is the opposite of reassuring.

| metric | original memo | rebuilt (legacy fee, same code) | shift |
|---|---|---|---|
| totals_realistic net | **−1.00c** (TOTALS_REFINE_FINDINGS) | **+0.72c** | **+1.72c** |
| spread maker-study taker win rate | 0.563 (exec_makerfill) | **0.608** | +4.5pp |
| spread maker-study taker net | +0.77c | **+5.37c** | +4.60c |
| spread taker n | 1107 | 1064–1065 | −42 games |
| adverse-selection gap (taker−maker win) | 0.532 vs 0.622 = −9.0pp | 0.608 vs 0.571 = +3.7pp | reversed |

`totals_realistic.py` is the SAME script that produced −1.00c; same fee
(legacy); only the data was re-fetched → +1.72c. This is pure data drift.

**Ruled out:** cache-availability survivorship. All 1320 completed games have
a spread pkl present (probe: 1320/1320, 0 absent); the population shrinks only
at the signal-filter stage. So the favorable shift is in the DATA CONTENT of
the re-fetched ESPN PBP / Kalshi-historical / Polymarket-history series, not
in which games survived.

**Cannot fully attribute** the root cause: the original cache that generated
the memos is gone (that is why we rebuilt), so there is no pkl-level diff to
run. Live hypotheses, in order:
1. **Historical-data vintage drift** — venue history / PBP endpoints return
   different quotes or alignment weeks later (revisions, different snapshot
   resolution). Re-fetching historical data is evidently NON-deterministic.
2. **Interaction with the known `_interp_at` future-quote look-ahead** (audit
   MAJOR, `research/lab/execution.py` / `totals_realistic._load`): if the
   rebuilt series have different quote density, the gap-bridging interpolation
   (which can borrow later quotes) shifts which strike is snapped and the fill
   price — plausibly moving win rate. Not proven here.

**Consequence:** the rebuilt "better" numbers are NOT a trustworthy edge
improvement — they are a reproducibility failure. Any "certify by re-running"
workflow is undermined if the same code on re-fetched data moves a verdict by
1.7c. This elevates two H1 data-quality items: (a) FREEZE/snapshot caches with
a content hash so verdicts are reproducible (the lake/`.expected_sha256`
discipline should extend to the study cache), and (b) fix the `_interp_at`
future-quote bridge and re-measure.

## What stands

- Fee module: validated end-to-end. Δ matches the analytical re-rate.
- All NBA verdicts: still gate-FAIL; no flip. Extremes still DEAD.
- The spread continuation residual remains REAL-but-uncertifiable; on rebuilt
  data its point estimate is higher, but (i) it still fails parity/season
  stability and (ii) the rebuilt level itself is not reproducible, so the
  honest carry-forward is unchanged: forward-paper, do not certify on backtest.

## Queued follow-ups

1. **Cache reproducibility** (H1 data-quality): content-hash + freeze the study
   cache; record vintage; make re-fetch diff-able. Until then, treat any
   re-run delta < ~2c as within data-vintage noise.
2. **`_interp_at` future-quote fix** (audit MAJOR) + re-measure spreads/totals.
3. **winner-kind cache** rebuilding now (`--kinds winner`) so the winner-market
   panels the merge lost are restored (lab `available('winner')` currently 0).
4. One-way-vs-round-trip hold-fee re-grade (original fee_model_audit remainder)
   — now runnable on the rebuilt cache, but gated on (1) so it is reproducible.
