# EXECUTION lane — maker / limit-fill study on the spread anchoring residual

_Date: 2026-06-09. Branch: `claude/hedge-fund-strategy-analysis-jyXWB`.
Lane: execution / maker-fill (HONEST). Acting on DIAGNOSTIC_2's top scoped
recommendation. No git commit/push; no test split; gate never weakened.
Hypothesis id `06470cb1ee87a815` (registered/claimed/closed in the registry);
trials appended to `research/reports/alpha/ledger.jsonl`._

## TL;DR — VERDICT: DEAD (maker fills do NOT rescue it)

Maker / passive-limit fills **raise the point estimate** of the spread
continuation-anchoring residual (taker **+0.77¢/ct → maker +2.10¢/ct** at the
central config, by earning the half-spread and dodging the Polymarket 2% taker
fee) but **do NOT rescue it past the promotion gate**. At every maker config the
decisive **block-bootstrap CI lower bound stays < 0** and the **season/parity
stability sub-gates fail** — the exact sub-gates that already killed the taker
arm. The maker lift is also **fragile and contingent on the most favorable fill
assumptions**: a Kalshi maker fee (no rebate) collapses maker net to **+0.12¢**,
and a modest queue-position penalty turns it **negative (−0.46¢, below taker)**.
Adverse selection is real, endogenous, and measured: the games you fill as a
maker win **0.53**, while the games you forgo (cancelled, ran away from your
limit) win **0.62** — you systematically fill losers and miss winners.

This is a clean, publishable NEGATIVE: the spread anchoring residual is
**not a taker-killed edge that maker execution recovers** — it is a genuinely
**thin, under-powered residual** that no realistic execution path lifts above
the gate's stability bar at one season of n.

## The maker FillModel and its assumptions (honest by construction)

Built in `research/scripts/maker_fill_study.py` on the lab substrate (real
`Panel` ladders, scored by `research.lab.evaluate` with `ledger_path`), over the
**same signal/population** as the taker re-check (`spread_realistic.py`):
continuation anchoring, `thresh=6`, ≤2-min freshness guard, honest +1-bar
latency, snap to the nearest **listed** signed-home strike, hold to settlement,
non-test population (1,107 trades / 1,107 games).

Two arms on the SAME population:

* **TAKER** = `lab.execution.REALISTIC` (`FillModel`, 1.5¢ half-spread + PM 2%
  taker fee). Reproduces the established `spread_realistic` verdict via the lab
  pipeline (small ladder-blend difference: net +0.77¢ here vs +1.88¢ in
  SPREADS_FINDINGS, same sign/verdict).
* **MAKER** = post a passive BUY limit `edge_c` cents **inside** the current
  quote for the chosen side. Two costs that make it honest, NOT free money:
  1. **Fill probability (endogenous, read from data).** Walk forward up to
     `wait_min` minutes; the order fills only at the first bar where the
     contract's **real quoted price for our side trades to the limit**. If it
     never does within the window, the order is **CANCELLED — no trade**. The
     forgone games are not free: they are disproportionately the winners that
     gapped away from the limit.
  2. **Adverse selection (endogenous, measured).** No assumed number. Each made
     fill settles against the **same real final margin**; the conditional win
     rate of the filled cohort falls out of the quote paths. It comes out
     **lower** than the taker population — automatically — because the fills are
     the picked-off cohort.
  Fill price = the limit (spread earned, NO half-spread). **Maker fee**: PM 0%
  (current PM maker schedule) as the optimistic-but-real central case; Kalshi
  maker fee (no rebate) as the honest pessimistic fee case.
  A `queue_c` knob adds a pessimistic queue-position penalty (require the price
  to trade `queue_c` cents THROUGH the limit, modeling no guaranteed fill on a
  mere touch / being behind resting size).

Grounding: measured near-ATM nonzero one-step |Δprob| on the real spread ladder
= median 3.5¢, mean 6.6¢ — so a 1.5¢ taker half-spread is conservative, and a
passive limit 1–2¢ inside the quote is touched frequently (high fill rate), but
the touches are correlated with direction (the adverse selection above).

## Head-to-head gate numbers (cents/contract, 95% block-bootstrap CI, cost sweep, OOS)

All scored by `research.lab.evaluate(..., ledger_path="research/reports/alpha/ledger.jsonl")`.

| arm | n | net ¢/ct | CI (¢) | cost sweep 0/1/2/3/4¢ | OOS (H1→H2) | gate |
|---|---|---|---|---|---|---|
| TAKER realistic (PM) | 1107 | **+0.77** | [−2.08, +3.60] | +0.77 / −0.23 / −1.23 / −2.23 / −3.23 | +1.27→+0.26 **FAIL** | **FAIL** |
| MAKER edge 1.5¢ wait 10m (PM 0% fee) | 726 | **+2.10** | [−1.56, +5.61] | +2.10 / +1.10 / +0.10 / −0.90 / −1.90 | +2.82→+1.38 ok | **FAIL** |
| MAKER edge 2.0¢ wait 10m (PM, best) | 700 | **+2.13** | [−1.39, +5.66] | +2.13 / +1.13 / +0.13 / −0.87 / −1.87 | +2.95→+1.30 ok | **FAIL** |
| MAKER edge 1.5¢ wait 10m (**Kalshi fee**) | 726 | **+0.12** | [−3.55, +3.63] | +0.12 / −0.88 / −1.88 / … | +0.84→−0.60 **FAIL** | **FAIL** |

Decisive gate reasons at the central PM maker config: `block_bootstrap_ci_lo
−0.0156 ≤ 0`; `season_split early_ci_lo=−0.0189, late_ci_lo=−0.0378,
overlap=False`; `parity_split even_ci_lo=−0.0472, odd_ci_lo=−0.0156,
overlap=False`. Same failure signature as the taker arm — the maker just shifts
the level up ~1.3¢, not enough to clear the conservative stability bar.

The maker arm's one genuine improvement: it **passes the OOS season-half
adversarial check** (both halves positive) where the taker arm fails it — so the
residual is more *stable* under maker execution. But "more stable and slightly
larger" is still short of the gate's by-game CI and parity demands at n≈700.

## Fill / win / adverse-selection sweep (the honest core)

```
taker baseline:                 fill 1.00  win 0.563  net +0.77c
edge 0.5c wait 10m   PM 0%fee :  fill 0.736 win 0.540  net +1.97c
edge 1.0c wait 10m   PM 0%fee :  fill 0.692 win 0.534  net +1.88c
edge 1.5c wait 10m   PM 0%fee :  fill 0.656 win 0.532  net +2.10c   <- central
edge 2.0c wait 10m   PM 0%fee :  fill 0.632 win 0.527  net +2.13c   <- best
edge 3.0c wait 20m   PM 0%fee :  fill 0.680 win 0.502  net +0.72c
```
Win rate is **monotone decreasing** as the limit gets more aggressive / waits
longer — exactly the adverse-selection signature. At the most aggressive settings
the filled cohort wins ≈0.50 (pure coin flip): the signal edge is competed away
precisely on the trades you get filled.

## Bug-hunt — is the maker "win" a fill-rate fantasy? (NO)

1. **Fill rate is NOT 1.0** (0.63–0.74); 327–393 games per config are
   **forgone**, not free.
2. **Adverse selection is endogenous and large.** Filled-cohort win = **0.532**
   vs full-population taker win **0.563** (−3.1pp). The **cancelled** (forgone)
   games win **0.622** — you systematically forgo the winners. If there were
   *no* adverse selection (filled cohort won at the full-pop rate) the maker net
   would be +5.21¢; the realistic +2.10¢ already pays the 3.11¢ adverse-selection
   tax. The lift is real but small, NOT an artifact of assuming free fills.
3. **Pessimistic-fill stress (queue penalty: must trade THROUGH the limit, not
   just touch it):**
   ```
   queue 0.0c: fill 0.656 win 0.532 net +2.10c
   queue 0.5c: fill 0.632 win 0.527 net +1.63c
   queue 1.0c: fill 0.604 win 0.520 net +0.96c
   queue 1.5c: fill 0.574 win 0.506 net -0.46c   <- NEGATIVE, below taker
   ```
   The maker advantage **evaporates under any realistic queue friction** — it
   exists only under the optimistic instant-touch / full-queue-priority / PM-0%
   assumptions. Deeper fills are even more adversely selected (win → 0.51).
4. **Fee contingency.** The PM 0% maker fee is load-bearing: the Kalshi maker fee
   (no rebate) alone drops net +2.10¢ → +0.12¢.
5. **Direction / sign verified.** `cover_away` correctly treated as the short
   side (settles below the strike); side mix balanced; entry prices sit below the
   contemporaneous quote (spread earned); taker arm reproduces the established
   `spread_realistic` verdict. No inversion bug.

## Conclusion for DIAGNOSTIC_2's pause trigger

DIAGNOSTIC_2 set the test: *is the spread anchoring family a taker-killed edge
(recoverable by maker execution) or a genuinely thin/absent one?* This study
answers it: **maker execution does NOT rescue it.** The residual nets at best
~+2.1¢ under the most favorable maker assumptions, fails the gate's by-game CI
and season/parity stability at every config, and is fragile to fee schedule and
queue friction. Combined with the realistic-taker FAIL, this satisfies the
DIAGNOSTIC_2 condition that "the anchoring family still nets ≤0¢ or fails the
gate after maker-fill re-scoring" — supporting the meta-lane's PAUSE rather than
throwing more darts (each new dart only raises the DSR hurdle). The execution
lever, the one untested rescue, is now tested and closed.

## Reproduce
```bash
python3 -m research.scripts.maker_fill_study --sweep
python3 -m research.scripts.maker_fill_study --gate --edge-c 1.5 --wait-min 10
python3 -m research.scripts.maker_fill_study --gate --edge-c 2.0 --wait-min 10        # best PM
python3 -m research.scripts.maker_fill_study --gate --edge-c 1.5 --wait-min 10 --venue kalshi
# pessimistic queue stress is in build_arms(MakerConfig(..., queue_c=1.5))
```
File: `research/scripts/maker_fill_study.py` (maker FillModel + head-to-head gate,
built on the lab substrate; reuses `lab.data` / `lab.execution.FillModel` /
`lab.evaluate`; non-test only).

DEAD
