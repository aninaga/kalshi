# weather / HYPOTHESIS PAIR — sonnet_weather analyst

_Date: 2026-06-09. Branch: `claude/hedge-fund-strategy-analysis-jyXWB`._
_Hypotheses: `6c465278b7f498bb` (H1) and `3a7747b33881da01` (H2)._
_Family: **weather** (per-family DSR partition). Test split never read._
_Agent: sonnet_weather_

---

## Background: what was already tried

The walking skeleton (`8aabae5235c8ed07`) tested **intraday drift continuation** → DEAD (−6.21c/ct). Sibling agents tested the **drift fade / overreaction mirror** (`ac6c9c02` and `af866e8a`) → both DEAD (−0.27c gross 3c eaten by 3.24c costs) and **favorite-longshot tail miscalibration** (`135543e2`) → DEAD (−1.6c, gross 1c < 2.6c all-in cost). All short-horizon drift and tail-calibration directions are closed.

---

## EDA that motivated these hypotheses

EDA scan over all 875 train panels (589 after excluding NaN-mid panels from the earliest data where the book hadn't opened):

1. **Market open structure**: The initial book is degenerate — the ATM bucket starts at P=1.0 (cumulative sum artifact), normalizing within 5–10 minutes. The implied temperature (mid) starts at the NWS 48-h forecast level.

2. **Temporal calibration**: Error `mid − realized` at each fraction of event duration:
   - t=0–20%: mean error ≈ −0.2F (well calibrated)
   - t=40–60%: mean error ≈ −0.05F (best calibrated period)
   - t=70–90%: mean error ≈ −0.35 to −0.65F (market falls below eventual outcome)
   - High staleness at 5h before event end (87% stale > 10 min) confirms the late drop is partly liquidity withdrawal.

3. **Large-deviation conditional win rates (raw, no costs)**: When `initial_mid − current_mid ≥ 2F` at mid-event (20–70%), buying the ATM strike wins 58.1% of time vs 50% baseline. Gross edge ≈ +8c before costs.

4. **Late-window calibration**: When entries are taken in the 70–85% window, avg fill = 0.540, win rate = 0.538 → nearly perfect calibration. No systematic settlement tilt.

5. **Cross-day signal ruled out**: Day-D event for city opens at ~14:00 UTC on day D−1 and closes at ~05:00 UTC on day D+1. Day-D's actual high temperature occurs ~21:00 UTC on day D (3 pm local CT), which is 7 hours AFTER day D+1's event has opened. Using prev_day realized final_total as a signal for the current day's entry (30 min after open) is unambiguous look-ahead. Correlation(prev_day_surprise, today_initial_error) = −0.258 is spurious.

---

## PRE-REGISTERED HYPOTHESES

Both hypotheses registered **before any gate scoring**.

### H1: Initial-Anchor Fade (LONG)

**Mechanism**: The market's opening implied temperature anchors at the NWS 48-h forecast. When the market later moves more than 2°F BELOW this anchor during the core trading window (20–70% of event duration), the correction may overshoot. Going LONG at the ATM strike at that point should profit from mean reversion toward the original anchor (which was typically close to the eventual settlement).

**Signal**: First bar in [20%, 70%] of event where `stale ≤ 2 min` AND `initial_mid − current_mid ≥ 2.0°F`.

**Pre-registered direction**: `long_when_fallen_from_anchor`

**Entry**: i+1 bar after signal fires (honest latency). Exit at settlement. Freshness guard: max_stale_min=2.0.

### H2: Late-Market Settlement Tilt (LONG)

**Mechanism**: Kalshi daily-high temperature books go stale before the settlement outcome is finalized. The last actively-traded price before liquidity dries up (70–85% of quote window, roughly during the afternoon when the actual high is being observed) should be systematically below the eventual settlement because market makers exit before the high has been confirmed.

**Signal**: The **last** fresh bar (stale ≤ 2 min) in [70%, 85%] of event duration. Unconditional LONG at ATM strike.

**Pre-registered direction**: `long_at_last_fresh_in_late_window`

**Entry**: i+1 bar (honest latency). Exit at settlement. Freshness guard: max_stale_min=2.0.

---

## Execution parameters

- **Fill model**: `FillModel(venue="kalshi", half_spread=max(2×median_measured, 0.015))`.
- Measured median half-spread on TRAIN panels: **0.0067** (0.67c/bucket).
- `2 × 0.0067 = 0.0133 < 0.015` → fill half_spread = **0.015** (1.5c).
- Kalshi fee schedule applied (non-linear taker formula). Effective fee ~1.2–1.5c at 50c fill.
- Strategy parameters: `min_elapsed=0`, `max_elapsed=200000` (overriding the NBA-inherited defaults which would cut off weather events at 42 minutes into a 38-hour window).

---

## Gate numbers

### H1: Initial-Anchor Fade

**TRAIN** (875 panels → 186 trades):

| Metric | Value |
|--------|-------|
| Cents/contract | **+5.02c** |
| Block-bootstrap CI | [−1.85, +11.68] |
| n | 186 |
| Avg fill | 0.501 |
| Win rate | 0.586 |
| Gate passed | **False** (CI straddles zero) |

Cost sweep:

| Extra half-spread | Cents |
|---|---|
| 0c | +5.02c |
| 1c | +4.02c |
| 2c | +3.02c |
| 3c | +2.02c |
| 4c | +1.02c |

Walk-forward (TRAIN months):

| Month | N | Cents |
|-------|---|-------|
| 2025-10 | 3 | −52.2c |
| 2025-11 | 9 | +28.2c |
| 2025-12 | 38 | +4.9c |
| 2026-01 | 55 | +10.5c |
| 2026-02 | 34 | −2.4c |
| 2026-03 | 47 | +3.2c |

**NONTEST** (1,062 panels → 287 trades):

| Metric | Value |
|--------|-------|
| Cents/contract | **+1.90c** |
| Block-bootstrap CI | [−3.67, +7.30] |
| n | 287 |
| Avg fill | 0.504 |
| Win rate | 0.557 |
| Gate passed | **False** |

Cost sweep @2c extra = −0.10c (breakeven, no margin for error).

Walk-forward adds Apr 2026: n=78, −4.47c.

Adversarial checks (nontest):
- staleness: PASS (freshness enforced upstream)
- estimator_bias_vs_unconditional: **FAIL** (win_rate=0.557 vs price=0.504, gap=0.053)
- concentration: PASS (CHI 23.7%)
- OOS (train-H1 vs val-H2 period): **FAIL** (H1=+5.24c but val period −1.43c)

### H2: Late-Market Settlement Tilt

**TRAIN** (875 panels → 826 trades):

| Metric | Value |
|--------|-------|
| Cents/contract | **−2.91c** |
| Block-bootstrap CI | [−4.40, −1.34] |
| n | 826 |
| Avg fill | 0.540 |
| Win rate | 0.538 |
| Gate passed | **False** (CI entirely negative) |

Walk-forward: all 6 months negative (−6.47c, −3.92c, −0.55c, −1.57c, −3.06c, −1.54c).

**NONTEST** (1,062 panels → 1,005 trades):

| Metric | Value |
|--------|-------|
| Cents/contract | **−2.63c** |
| Block-bootstrap CI | [−4.00, −1.19] |
| n | 1,005 |
| Avg fill | 0.543 |
| Win rate | 0.543 |
| Gate passed | **False** |

All 8 nontest months negative. CI entirely below zero.

Adversarial checks (nontest):
- staleness: PASS
- estimator_bias_vs_unconditional: PASS (gap=0.001 ≤ 0.05, perfectly calibrated)
- concentration: FAIL (DEN 27.2% > 25% threshold, borderline)
- OOS: FAIL

---

## Bug hunt

**H1 specific**:
- Look-ahead check: `p.mid[0]` is the opening bar, available before any signal fires. The signal fires at i ∈ [20%,70%] of the panel, using `p.mid[i]` at that bar. No future information is used. CLEAN.
- Settlement orientation: side='over' maps to buy P(high>strike). Payoff=1.0 if `final_total > strike`, 0.0 otherwise. `final_total` is the bucket midpoint of the settled YES bucket, so a boundary of 53.5F settles YES when bucket e.g. '53–55' wins (midpoint 54 > 53.5). Correct orientation. CLEAN.
- Initial book degenerate structure: at bar 0 the ATM bucket shows P=1.0 (cumulative sum artifact). Using bar 0 as "initial_mid" is safe because we only READ p.mid[0] (the 0.5-crossing inversion), which correctly reflects the market's opening consensus, not the raw bucket probability. Verified: p.mid[0]=51.5 in the example where P(high>51.5)=1.0 confirms the mid is computed separately. CLEAN.
- Elapsed time params: the Strategy default max_elapsed=2520s (42 min) would kill all weather entries (which occur at 8–25h elapsed). Explicitly set to 200000s. Confirmed trades fire at correct bars. CLEAN.
- Reproducibility: running the strategy twice gives identical 186 trades, 5.02c.

**H2 specific**:
- The strategy finds the LAST fresh bar before staleness; using i+1 latency means entry is the first bar AFTER the last fresh quote. If the last fresh bar is at i and bar i+1 is stale, the fill_model.fill() at ts=minute_ts[i]+60 would fail or extrapolate stale. Verified: the gate produced 826 trades without fill failures, and win_rate=0.538 matches fill=0.540 → fill is working correctly from fresh quotes. CLEAN.
- The fill price at 0.540 (above ATM=0.5) is correct: in the 70–85% window, the market has partially resolved toward the settlement direction, so the ATM is above 0.5 on average. No artificially low fill. CLEAN.

**Both hypotheses**: No test split was read. `data.load_panels('temp', split='train')` and `split='nontest'` only.

---

## Final Verdicts

### H1: Initial-Anchor Fade — DEAD

The pre-registered direction (LONG when market has fallen ≥2°F from initial open) shows raw conditional win rate of 55.7% on nontest, which is genuinely above the 50.4% fill price (53% gap). However:

1. The CI on nontest [−3.67, +7.30] straddles zero. The gate requires the **lower** confidence bound to be positive.
2. The estimator-bias adversarial check flags the win_rate/fill_price gap as concerning (>5%). This is a sign the signal selects into panels where the market has already partially moved toward the outcome — the "edge" is partially a selection artifact, not exploitable future alpha.
3. Walk-forward is highly inconsistent: Oct 2025 −52c (n=3), Jan 2026 +10.5c, Apr 2026 −4.5c. No persistent positive direction.
4. After cost sweep: at 2c extra half-spread the signal is −0.10c. There is essentially no margin.
5. OOS (adversarial): the val half of nontest shows −1.43c.

**VERDICT: DEAD**

### H2: Late-Market Settlement Tilt — DEAD

The CI is entirely negative on both train [−4.40, −1.34] and nontest [−4.00, −1.19]. All 8 monthly walk-forward buckets are negative. The win_rate=0.543 matches fill_price=0.543 almost exactly — the market is well-calibrated in the 70–85% window. There is no underpricing tilt at market close. The mechanism was falsified cleanly.

**VERDICT: DEAD**

---

## What these runs add to the family record

Weather 'temp' hypotheses to date (N=6 total after this pair):

| Mechanism | Result |
|-----------|--------|
| Drift continuation | DEAD (−6.21c) |
| Drift fade / overreaction mirror | DEAD (−0.27c, 2 independent agents) |
| Favorite-longshot tail miscalibration | DEAD (−1.6c) |
| Initial-anchor fade (this run) | DEAD (+1.9c but CI straddles zero, OOS negative) |
| Late-market settlement tilt (this run) | DEAD (−2.63c, CI entirely negative) |

Pattern: within-event directional signals and microstructure premia are consistently eaten by the 3c all-in cost floor on thin Kalshi weather books. The realized median half-spread (0.67c/bucket) is low, but interior cumulative boundaries are multi-leg synthetics (2× charge = 1.34c), plus 1.2–1.5c Kalshi fee = 2.5–3c minimum round-trip. Any signal with < 3c gross edge is structurally dead on this venue.

---

## Reproduce

```bash
# H1: Initial-Anchor Fade
python -c "
import numpy as np, warnings; warnings.filterwarnings('ignore')
from research.lab import data, evaluate
from research.lab.strategy import Strategy
from research.lab.execution import FillModel

fm = FillModel(venue='kalshi', half_spread=0.015)

def entry_h1(p):
    n = p.n; mask = np.zeros(n, dtype=bool)
    if not np.isfinite(p.mid[0]): return mask
    lo, hi = int(0.20*(n-1)), int(0.70*(n-1))
    for i in range(lo, hi+1):
        if p.features['stale_min'][i] > 2: continue
        if not np.isfinite(p.mid[i]): continue
        if p.mid[0] - p.mid[i] >= 2.0:
            mask[i] = True; break
    return mask

strat = Strategy(name='anchor_fade_long', entry=entry_h1,
    side=lambda p, i: 'over', exit='settlement',
    entry_latency_min=1.0, max_stale_min=2.0,
    min_elapsed=0.0, max_elapsed=200000.0)

for split in ['train', 'nontest']:
    panels = data.load_panels('temp', split=split)
    trades = strat.run(panels, fill_model=fm)
    gate = evaluate.evaluate(trades, ledger_path='research/reports/alpha/ledger.jsonl', family='weather')
    print(f'{split}: n={gate.n}, cents={gate.cents_per_contract:.2f}, CI=[{gate.ci_lo:.2f},{gate.ci_hi:.2f}]')
"

# H2: Late-Market Settlement Tilt
python -c "
import numpy as np, warnings; warnings.filterwarnings('ignore')
from research.lab import data, evaluate
from research.lab.strategy import Strategy
from research.lab.execution import FillModel

fm = FillModel(venue='kalshi', half_spread=0.015)

def entry_h2(p):
    n = p.n; mask = np.zeros(n, dtype=bool)
    lo, hi = int(0.70*(n-1)), int(0.85*(n-1))
    for i in range(hi, lo-1, -1):
        if p.features['stale_min'][i] <= 2:
            mask[i] = True; break
    return mask

strat = Strategy(name='late_settlement_tilt', entry=entry_h2,
    side=lambda p, i: 'over', exit='settlement',
    entry_latency_min=1.0, max_stale_min=2.0,
    min_elapsed=0.0, max_elapsed=200000.0)

for split in ['train', 'nontest']:
    panels = data.load_panels('temp', split=split)
    trades = strat.run(panels, fill_model=fm)
    gate = evaluate.evaluate(trades, ledger_path='research/reports/alpha/ledger.jsonl', family='weather')
    print(f'{split}: n={gate.n}, cents={gate.cents_per_contract:.2f}, CI=[{gate.ci_lo:.2f},{gate.ci_hi:.2f}]')
"
```

DEAD / DEAD
