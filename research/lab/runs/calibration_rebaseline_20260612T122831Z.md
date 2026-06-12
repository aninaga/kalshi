# calibration family re-baseline — post-C3 (long_away fix), rebuilt winner cache

**2026-06-12T12:28Z.** Obligated follow-up to the H1/W1.5 lab-honesty lane
(audit defect C3): the `long_away` side label previously sat in NEITHER side
taxonomy, so WINNER-market fades through the lab `FillModel` filled LONG
(paying `p_home`) while settling as the away bet — fabricating up to
~+84c/contract of fake edge on longshot fades. `strategies/calibration.py`
emits exactly that label. Any lab-substrate number that ever exercised it was
an artifact. This run re-baselines the family on the FIXED substrate
(`research.lab.types.SHORT_SIDES` single source; unknown labels raise) over
the rebuilt winner cache (1,320 pkls, 2026-06-12 vintage; not the original
memo vintage — see `runs/fee_audit_rerun_confirm_*` on data drift).

## Setup
- `favorite_longshot(market=WINNER)` defaults, hold-to-settlement.
- `FillModel(venue="kalshi")` — Kalshi winner book, real ceil-cents taker fee.
- Splits: train + val separately (test untouched). evaluate() promotion gate,
  no walkforward/adversarial extras (re-baseline, not a registration).

## Orientation sanity (the fix, demonstrated on real panels)
Fade legs (`long_away`) now enter at the AWAY price: avg fade entry **0.920**
when the home side is the longshot (p_home ≈ 0.08) — i.e. the trade pays the
favorite-complement, as it must. Pre-fix the same trades would have "paid"
~0.08 and settled as the away winner ≈ fake +84c/contract.

## Result — DEAD, both splits, consistent sign (no train/val flip)

| split | panels | trades (fades) | net c/ct | gate |
|---|---|---|---|---|
| train | 884 | 608 (290) | **−2.91** | FAIL (bootstrap CI lo −0.052; knockouts ≤ 0) |
| val | 199 | 145 (52) | **−1.34** | FAIL (CI lo −0.055; n 145 < 200) |

The family's standing DEAD verdict (ALPHA_FINDINGS #3, calib_alpha era:
in-sample bias real, OOS sign-flip) is **re-confirmed on the fixed substrate**
— and is now cleaner than the original: both splits negative through honest
execution, no sign-flip ambiguity. The calibration module's "kept ONLY as
regression demos" warning stands. No ledger-recorded verdict required revision
(the contaminated path never produced a registered PASS); this note exists so
the re-baseline is on the record per the C3 follow-up obligation.

Related: H1 batch A integration commits (b591e0d..f78579d), audit
`docs/CODEBASE_AUDIT_2026-06-12.md` C3, AGENDA hard rules (no new
registration implied by this run; K not consumed).
