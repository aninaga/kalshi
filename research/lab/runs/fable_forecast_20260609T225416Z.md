# weather / forecast-vs-implied gap (first exogenous informational-edge test) — DEAD

_Date: 2026-06-09. Branch: `claude/hedge-fund-strategy-analysis-jyXWB`.
Agent: `fable_forecast`. Hypothesis id `3cb384c482d1c2f7` (registered + claimed
2026-06-09T22:54:16Z, BEFORE any strategy PnL was computed).
Family: **weather**, market: **temp**. Test split never read._

This is the first non-microstructure test on the family: it uses the NEW
exogenous field `features["forecast_high_f"]` (point-in-time Open-Meteo
previous-runs daily-high forecasts, 2 leads per event, conservative ISSUE-time
`known_from_ts`, as-of joined — see
`research/lab/runs/data_forecast_high_f_20260609T214420Z.md`).

## EDA (TRAIN only — what I looked at to pick threshold/window)

All EDA on the 875 train panels, before registration. Three passes:

1. **Raw gap vs settlement residual** (`gap = forecast_high_f − mid`,
   `resid = final − mid`, sampled at 5% fractions of the quote window):
   corr(gap, resid) ≈ +0.03–0.05 in the early window, rising to +0.42 at 90–95%
   elapsed. The late-window rise is the KNOWN stale-mid artifact (liquidity
   withdrawal; mid stuck below the realized high while the forecast stays put) —
   not tradeable, and the unconditional late long was already falsified
   (`3a7747b33881da01`, −2.63c). Discarded the late window by design.

2. **Price space at the ATM strike, fresh bars only** (stale ≤ 2 min): buy
   toward the forecast at the ATM-snapped strike, gross = win − quote. Raw gap is
   dominated by the field's documented cold bias (gap ≤ −1.5F at ~40% of bars
   with ~0 edge; gap ≥ +1.5F rare) — the raw gap mostly measures *forecast* bias,
   not *market* error.

3. **Per-city debiasing on TRAIN**: `bias_city = mean(last as-of forecast −
   realized settlement rep)` over train events:
   AUS −2.69, CHI −1.71, DEN −0.90, MIA −2.08, NYC −2.00 (matches the data
   note's hourly-max-vs-instrument cold bias). With
   `gap = (forecast − bias_city) − mid`, the early window (5–30% elapsed)
   shows +2 to +6.6c gross at thresholds 1.0–3.0F with BOTH signs contributing;
   thr 1.5/2.0/2.5 all similar (the choice is not a grid peak). Mid window decays,
   65–95% goes to ~0/negative — exactly the "market out-sharpens the forecast
   later" shape (forecast MAE 2.69F vs market end-mid MAE 1.53F).

## PRE-REGISTERED hypothesis (frozen at 22:54:16Z, before any PnL)

- **Mechanism**: early in the quote window the thin market has not fully
  absorbed the model forecast; a ≥2F disagreement between the TRAIN-debiased
  forecast and the implied temperature resolves toward the forecast. Late
  window excluded by design (market is sharper than the forecast there).
- **Signal**: first bar `i` with `elapsed_frac ∈ [0.05, 0.30]`,
  `stale_min[i] ≤ 2`, finite mid & forecast, and `|gap_i| ≥ 2.0F` where
  `gap_i = (forecast_high_f[i] − bias_city) − mid[i]`; `bias_city` frozen at the
  TRAIN values above.
- **Direction**: `buy_toward_forecast` — "over" if gap > 0 else "under", at the
  ATM-snapped strike, hold to settlement.
- **Execution**: honest i+1 latency (1 min), freshness ≤ 2 stale min,
  `FillModel(venue="kalshi", half_spread=max(2×0.0067 train median, 0.015)=0.015)`,
  one trade per event, `min_elapsed=0`, `max_elapsed=200000` (weather override).
- **Snap-flip guard**: traded ATM only, where the i+1 re-snap is harmless (the
  side is conditioned on the gap sign, not on a specific strike's quote; bucket
  boundaries are ~2F apart so a ≥2F gap stays on the same side of any 1-minute
  re-snap). No strike-band conditioning anywhere.

Parameters reported here are EXACTLY as registered; nothing was retuned after
seeing val/nontest.

## Gate numbers — REALISTIC fills

**TRAIN** (875 panels → 516 trades): **−0.35c/ct**, CI [−4.56, +3.72],
avg fill 0.501, win 0.533 (gross ≈ +3.2c). Gate failed (CI straddles zero).

**NONTEST** (1,062 panels → 616 trades): **−0.08c/ct**, CI [−3.81, +3.67].
avg fill 0.502, win rate 0.536. Gate **failed**.

- Decomposition (arithmetic closes): gross **+3.41c** − spread 1.50c − Kalshi
  fee 2.00c (ceil formula at p≈0.5) = **−0.08c**.
- Cost sweep (monotone, honest): 0c → −0.08, 1c → −1.08, 2c → −2.08,
  3c → −3.08, 4c → −4.08.
- Walk-forward (nontest): 2025-10 −3.92 (102), 2025-11 −1.03 (80),
  2025-12 −4.74 (88), 2026-01 +5.88 (89), 2026-02 −1.23 (79), 2026-03 +2.27 (97),
  2026-04 +1.83 (78), 2026-05 +20.33 (3). Sign-unstable.
- Adversarial (nontest): staleness ✓ (guard enforced upstream),
  estimator-bias ✓ (win 0.536 vs fill 0.502, gap 3.4c ≤ 5c — the fills are
  honest ATM quotes and the gap IS the claimed gross edge), concentration ✓
  (AUS 24.2% < 25%), OOS ✗ (H1 −2.37c vs H2 +2.21c — early half negative).
- Slices (diagnostic, not gates): sides over n=433 gross +4.42c / under n=183
  gross +1.03c; cities MIA +13.0c, AUS +8.5c gross vs CHI −4.1c; val-only
  events n=100 net +1.28c (gross +4.77c). Median entry at 5.0% elapsed — the
  signal mostly trades the market open against the lead-2 forecast vintage.

## Point-in-time hand-verification (3 events, at the ACTUAL entry bars)

For each, the panel's forecast value at the signal bar equals an independent
recomputation from `market_data/weather/features/forecast_high_f.parquet`
(`value of latest row with known_from_ts ≤ bar_ts`), and the contributing row
was ISSUED before the bar while the later (lead-1) row was issued after and was
NOT used:

| event | signal bar (UTC) | row used: issued, value | next row: issued (after bar) | match |
|---|---|---|---|---|
| KXHIGHAUS-25DEC01 | 11-30 16:56 | 11-30 05:00, 51.3F | 12-01 05:00, 44.8F | exact |
| KXHIGHDEN-25DEC30 | 12-29 17:17 | 12-29 06:00, 49.9F | 12-30 06:00, 52.3F | exact |
| KXHIGHNY-26MAR30 | 03-29 16:00 | 03-29 03:00, 59.9F | 03-30 03:00, 68.6F | exact |

## Bug hunt

- **As-of leakage**: hand-verified above; a future-issued forecast structurally
  cannot reach an earlier bar (`feature_store._asof_series` searchsorted-right).
- **Estimator bias / fill honesty**: PASS (win 0.536 ≈ fill 0.502 + gross).
  Fills average 0.502 at ATM — no snapping to unfairly far strikes.
- **Look-ahead in the signal**: entry mask uses only bar-i `mid`, bar-i
  forecast (as-of), and constants frozen from TRAIN. `bias_city` uses TRAIN
  outcomes only (allowed: train EDA), applied as frozen constants to nontest.
- **Snap-flip**: ATM-only with gap-sign side ⇒ no band-conditioned inversion
  (the fable_weather hazard does not apply); fill distribution (avg 0.502,
  symmetric) confirms.
- **Degenerate open book**: entry window starts at 5% elapsed (~2h after open),
  past the documented 5–10 min cumulative-sum artifact; freshness guard active.
- **Reproducibility**: two independent runs produced identical n=616 trades.
- **Too-good check**: nothing too good to hunt — net is ~0; the gross +3.41c
  matches the TRAIN EDA magnitude (+3.2c) out-of-sample, so it is not an
  artifact of first-hit selection.

## Why it's dead (the economics)

The informational edge is REAL but small: buying toward the debiased model
forecast early in the window carries ~+3.4c gross at the ATM strike
(out-of-sample, estimator-bias-clean — the largest gross found on this family
at the ATM). But the ATM is exactly where the Kalshi taker fee maxes out
(`ceil(7·p(1−p))` = 2.0c at p≈0.5), so the all-in cost is ~3.5c and the net is
−0.08c with a CI straddling zero and a failing season-half OOS (the edge was
absent Oct–Dec 2025, present Jan–May 2026 — consistent with noise, or at best
non-stationary). Per the family record this now extends the pattern from
microstructure to the first exogenous signal: gross inefficiencies on this
venue live at or below the ~3–3.5c taker cost floor. A maker-fill execution
study (fee 0, earn the spread: would flip −0.08c to roughly +3.4c+ if fills
arrive) is the natural follow-up, but that is an execution-model question, not
this hypothesis, and is not claimed.

## Governance / ledger

Registry `3cb384c482d1c2f7` → done/DEAD; ledger row appended
(`agent="fable_forecast"`, `family="weather"`, `market="temp"`,
net_cents_0c −0.08, CI [−3.81, +3.67], n=616). Weather-family DSR N=9 at
scoring time (real-ledger governance).

## Reproduce

```bash
PYTHONPATH=/home/user/kalshi python3 -m research.scripts.weather_forecast_gap_e2e --split train --no-ledger
PYTHONPATH=/home/user/kalshi python3 -m research.scripts.weather_forecast_gap_e2e --split nontest --no-ledger
```

## VERDICT: DEAD
