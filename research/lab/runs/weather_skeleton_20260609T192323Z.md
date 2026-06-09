# weather / WALKING SKELETON — first non-NBA vertical, end-to-end through the gate

_Date: 2026-06-09. Branch: `claude/hedge-fund-strategy-analysis-jyXWB`.
Operator-run (coordinator session). Hypothesis id `8aabae5235c8ed07`.
Family: **weather** (per-family DSR partition). Test split never read._

## What this run is

The proof-of-seam for the beyond-NBA expansion: one genuinely new event-class
family onboarded through the generic `MarketDataProvider` seam and driven to an
honest gate verdict on real data — fetch → cache → Panel → signals → realistic
execution → `lab.evaluate` → verdict → family-stamped ledger. The deliverable
is the REACHED verdict, not its sign.

## The vertical

Kalshi daily high temperature (`KXHIGH*`), 5 cities (NYC/CHI/MIA/AUS/DEN),
newest 250 settled events per city → **1,250 cached events**
(`market_data/weather/_cache/`, gitignored; splits locked in
`market_data/weather/splits.json`: 875 train / 187 val / 188 test,
chronological, test = 2026-05-02 onward).

Each event's ~6 bucket binaries become a Panel: cumulative boundary ladder
(fewest-legs side of the bucket book), implied-temperature `mid` (0.5-crossing
inversion), exact boundary settlement from the YES bucket, timed
`duration_sec` (quote windows ~24–40h), and — new for this program — a
**measured** per-minute half-spread from the candles' real two-sided
`yes_bid`/`yes_ask` (median **0.75¢**/bucket on train; the NBA program had to
*assume* 1.5¢).

## Pre-registered hypothesis (params fixed before any scoring)

Intraday under-reaction / drift **continuation**: implied-temp drift over the
last 5 fresh bars, fire at |drift| ≥ 0.75°F, side = drift sign, hold to
settlement. Freshness ≤ 2 stale minutes, 1h book warm-up, honest i+1 latency,
`FillModel(venue="kalshi", half_spread=max(2×measured bucket median, 1.5¢))`
(interior cumulative boundaries are multi-leg synthetics — charged 2 legs,
cost sweep stresses beyond).

## Gate numbers — REALISTIC fills

**TRAIN** (875 panels → 804 trades, cities balanced 154–168):
**−6.28¢/ct**, CI [−9.20, −3.43], monotone cost sweep −6.28 → −10.28 @4¢.

**FULL NON-TEST** (1,062 panels → 976 trades):
**−6.21¢/ct**, CI [−8.83, −3.62], n=975. Walk-forward 7/8 months negative
(only 2026-03 positive, +3.2¢). Adversarial: staleness ✓,
estimator-bias ✓ (win rate matches price paid — the fills are honest),
concentration ✓ (5 cities, max share ~20%), OOS ✗ (negative everywhere).

## VERDICT: DEAD

The pre-registered continuation direction is decisively wrong: the CI is
entirely below zero. Thin daily-high books do NOT under-react to intraday
moves at this horizon — if anything the signal inverts. The mirror (fade the
drift) is a NEW hypothesis: it was NOT scored here (post-hoc sign-flipping is
the textbook overfit move), and anyone scoring it must pre-register it and
face the weather family's now-incremented DSR N.

## What the run PROVES (the actual deliverable)

1. **The seam works.** A non-NBA family flowed through the unchanged
   substrate: `data.load_panels("temp", split=...)` resolved the weather
   provider; `Strategy`/`FillModel`/`evaluate` ran without modification.
2. **Per-family governance works on the live ledger**:
   `governance_params(family="weather")` → N=1 (this trial);
   `family="nba"` → N=17, V[SR]=0.287 (the program's history) — independent
   hurdles, conservatively partitioned (family-less legacy rows count toward
   every family).
3. **Honest-execution toolchain generalizes**: real ladder quotes, measured
   spreads, venue-correct (Kalshi) fees, i+1 latency, never a 0.50 fill.
4. **Power ceiling lifts**: 1,250 events from 8 months × 5 cities of ONE
   series family; the full catalog (more cities, more series, deeper history
   to 2024-03) reaches the n≈15k–30k regime the NBA universe structurally
   could not.

## Reproduce

```bash
python -m research.lab.providers.weather --build --cities NYC,CHI,MIA,AUS,DEN --max-events 250
python -m research.lab.providers.weather --make-splits   # refuses to re-lock
python -m research.scripts.weather_drift_e2e --split train
python -m research.scripts.weather_drift_e2e --split nontest
```

DEAD
