# PROGRAM_STATE — NBA prediction-market research, program-level decision artifact

_Date: 2026-06-09. Branch: `claude/hedge-fund-strategy-analysis-jyXWB`.
Author: research director (Opus 4.8, max effort), READ-ONLY synthesis of the
fleet's durable verdicts. No new backtests, no new hypotheses, no gate change,
no git commit/push. Sources cited inline by run file + numbers._

## TL;DR — the program has reached its honest floor

The fleet has tested every lever DIAGNOSTIC_2 named. The single real residual
(spread continuation-anchoring) **failed the gate as a taker AND failed under
honest maker fills** — the two moves DIAGNOSTIC_2 made the PAUSE trigger explicit
about. Every other family is market-efficient (negative/zero OOS, mechanistically
falsified). The binding constraint is structural and **not relievable**: a
~3.4¢ taker round-trip floor shrinks the one genuine +4.1¢-gross residual to
+0.77¢ net, and a +0.77¢ edge against a 0.487 hold-to-settlement std needs
**n ≈ 15k (bare CI) to ≈30k (with stability sub-gates)** — 14–27× the entire,
and only-ever, 2025-26 season (markets launched this season; no prior history
exists). Maker execution lifts the point estimate to at best +2.1¢ but still
fails the CI/stability bar and is fragile to fee schedule and queue friction
(goes negative under a 1.5¢ queue penalty), because the fills are adversely
selected (filled cohort wins 0.532 vs forgone 0.622).

**Recommendation: PAUSE.** The DIAGNOSTIC_2 PAUSE trigger has fired on its own
terms. Detail and dissent below.

---

## CORRECTION (2026-06-12) — fee model audit supersedes the cost-floor numbers

The fee_model_audit lane (`runs/fee_model_audit_20260612T075325Z.md`,
`research/scripts/fee_audit_rerate.py`, ledger row `fee_model_audit_20260612`)
found this document's PM cost assumption was wrong: the **flat 2%-of-notional
Polymarket taker fee does not exist** in the official schedule
(docs.polymarket.com/trading/fees; the arb line verified this 2026-06-09 on a
branch never merged into this line). Official PM sports taker = 300 bps
parabolic ≈ **0.75¢ at ATM**, makers **$0** (+20–25% rebate); Kalshi NBA maker
= **quarter of taker** (≈0.44¢ batch), not the full taker fee charged as the
"pessimistic" contingency. Corrected levels (gate *verdicts* unchanged — the
season/parity stability failures are level-independent):

- spread taker: +0.77¢ → **+1.92¢** net (CI [−0.93, +4.75]; OOS halves now
  +2.42 → +1.41, both positive) — still gate-FAIL on CI lo + stability.
- spread maker: PM +2.10¢ already-correct; Kalshi contingency +0.12¢ →
  **+1.10¢** (1-lot) / **+1.66¢** (batch).
- totals pace: −1.00¢ → **+0.27¢** — retraction STANDS (no positive edge) but
  it was not "execution-killed", it is ~breakeven at honest costs.
- totals-extremes: high-price legs were over-charged **9–22×**; family needs a
  cache rebuild + true re-run before any claim.

**Restated cost floor:** PM-sports taker all-in ≈ **2.25¢** ATM (not ~3.4¢);
Kalshi taker ≈ 3.25¢ (unchanged, real); **maker fee floor ≈ 0** (PM $0;
Kalshi NBA 0.44¢ batch; Kalshi crypto/weather series charge makers nothing) —
the binding maker costs are fill probability and adverse selection, which the
live fill probes measure.

**Restated power arithmetic:** §2(c)'s n ≈ 15,200–30,400 was computed at
μ = +0.77¢. At the corrected μ = +1.92¢ (σ = 0.487 unchanged), a bare 95% CI
clears 0 at **n ≈ 2,500 games (~2 further seasons)**, stability sub-gates
roughly double that. The forward-paper option on the spread maker residual is
therefore far more valuable than this document's "14–27× impossible" framing
implies — certification is a 2–4 season program, not a multi-decade one. The
PAUSE recommendation for *backtest mining* stands; the capital/risk dissent
(§ honest dissent) gains weight.

---

## 1. Cross-market efficiency map

| market | honest residual found | realistic effect size | classification | decisive evidence |
|---|---|---|---|---|
| **winner / moneyline** | none | negative at every cost | **EFFICIENT** | In-game WP-excursion fade: −4.54¢ train, −1.89¢ val, gate FAIL + OOS FAIL, monotone-negative cost sweep (`runs/winner_originate_20260609T085815Z.md`). The −0.37 EDA lens that motivated it was a whole-game-mean look-ahead (per-game corr −1.00 by construction); the causal trailing-z version is a measurement confound, not alpha. Pregame CLV and cross-contract term-structure also falsified at source (winner book log-loss 0.440 beats RV model 0.516). |
| **total / over-under** | pace-anchoring, but it is a **pure 0.50-fill artifact** | **−1.00¢ net** | **ARTIFACT (RETRACTED)** | The cleanest natural experiment in the program. Flat-cost 0.50-ATM fill: +6.21¢ @2¢, gate PASS (n=1,297). Same edge, real listed strike (~0.556) + 1.5¢ half-spread + 2% PM taker: **−1.00¢, FAIL at every spread**, walk-forward 7/9 → 3/8 months (`TOTALS_REFINE_FINDINGS.md`, `ALPHA_FINDINGS.md` correction box). The certified headline was a ~7.5¢ cost under-charge, not leakage. |
| **spread / handicap** | continuation-anchoring (pace lags a thin handicap book) | **+0.77¢ net taker** (gross +4.11¢, win 0.563 vs price 0.522) | **REAL but UNCERTIFIABLE — and now shown not taker-killed** | Survives leakage audit (40/40 dynamic clean) and the estimator-bias adversarial (gap +0.044 < 0.05 tolerance) — a *genuine* small conditional effect, not a snap/look-ahead artifact (`runs/spread_adversary_20260609T085637Z.md`). But block-bootstrap CI lo is **negative even at 0¢ (−1.90)**; season/parity stability fail with no overlap; all 30 team-knockouts have CI lo ≤ 0. Deepening only lifts the estimate by drifting into the favorite-longshot/late-decided regime (est-bias FAIL 0.069, train→val inversion +4.47→−1.59) — confirmed by a null test where "bet whoever is ahead" matches the pace strategy, so pace adds ~nothing (`runs/spread_deepen_20260609T090246Z.md`). |

**Summary:** one market efficient (winner), one residual that is a cost artifact
(total), one residual that is real-but-thin (spread). The spread residual is the
program's single honest finding — and it has now been run to ground.

---

## 2. The binding constraint (stated with numbers)

**Constraint = (b) the ~2–4¢ taker cost floor × (c) statistical power at one
season of n — a compounding interaction, neither factor a bug.**

- **Cost floor (b):** realistic round-trip = 1.5¢ half-spread + ~2% Polymarket
  taker (~1.9¢ at ATM) ≈ **3.4¢**. This is what turned +6.21¢ totals into −1.00¢
  and shrinks the spread residual's +4.11¢ gross to **+0.77¢ net**
  (`spreads_power_20260609T090521Z.md` §2; `TOTALS_REFINE_FINDINGS.md`).
- **Power (c):** observed per-trade μ = +0.00766 (prob), σ = 0.487 (the
  hold-to-settlement 0/1 variance dominates), SNR ≈ **0.0157**. A bare 95%
  block-bootstrap CI lower bound clears 0 only at **n ≈ 15,200 games**; the gate's
  season-half + parity stability sub-gates push the practical requirement to
  **n ≈ 30,400** (`spreads_power_20260609T090521Z.md` §3). Minimum detectable
  edge at the available n=1,107 is **±2.87¢** — and the real edge is +0.77¢.
- **n is maximal and not relievable.** The cached population is the complete
  season: 1,319 spread pkls total, **1,107 tradeable nontest trades** (test=192
  never read). Kalshi/Polymarket NBA markets **launched 2025-26 — no prior
  seasons exist** (verified, ALPHA_FINDINGS/DIAGNOSTIC). Required n is 14–27× a
  single, and only-ever, season. Going from the n=267 preliminary to n=1,107
  tightened the CI half-width 5.8¢→2.8¢ exactly as √n predicts but the lower
  bound stayed below 0 — **more games tightens the CI but does not move the
  verdict.**
- **The one honest residual survives neither lever.**
  - *More data:* impossible (n maximal; no prior history; not fetchable in-container).
  - *Honest maker fills:* TESTED and FAILED (`exec_makerfill_20260609T091134Z.md`).
    Earning the half-spread and dodging the PM taker lifts +0.77¢ → **+2.10¢**
    (best PM config +2.13¢) but the **CI lo stays < 0 (−0.0156)** and
    season/parity stability still fail — the same failure signature as the taker
    arm. The lift is fragile and contingent: a Kalshi maker fee (no rebate)
    collapses it to **+0.12¢**, and a 1.5¢ queue penalty drives it **−0.46¢
    (below taker)**. Crucially the lift is not free money — it already pays a
    measured **3.11pp adverse-selection tax**: the games you fill as a maker win
    **0.532** while the games you forgo (gapped away from your limit) win
    **0.622**. You systematically fill losers and miss winners. Win rate is
    monotone-decreasing as the limit gets more aggressive — the textbook
    adverse-selection signature.

So the binding constraint is not a harness bug (leakage audit 40/40 clean across
all three markets; DIAGNOSTIC_1 cost/latency/split audit clean), not gate
miscalibration in the dangerous direction (false-positive rate 0.0% at every n),
and not search-space exhaustion. It is that **the real edges are smaller than the
real cost of trading them, and what survives cost is too small to certify at any
n this universe can ever provide.**

---

## 3. PAUSE vs CONTINUE recommendation — **PAUSE**

DIAGNOSTIC_2 defined the PAUSE trigger precisely (its recommendation #5):

> "if, after maker-fill re-scoring (#1) AND the full spread season (#2), the
> anchoring family still nets ≤0¢ or fails the gate, then the program has
> demonstrated the one real family is taker-killed and the rest are efficient …
> the fleet should PAUSE rather than keep throwing darts."

**Both conditions are now satisfied:**

1. **Full spread population built and scored** — maximal nontest n=1,107, net
   +0.77¢, gate FAIL, and the power study proves more games cannot close the CI
   (`spreads_power_20260609T090521Z.md`).
2. **Maker-fill re-scoring done** — best honest case +2.10¢, gate FAIL on CI +
   stability, fragile to fee/queue, with a measured adverse-selection tax
   (`exec_makerfill_20260609T091134Z.md`). The execution lever — the one untested
   rescue DIAGNOSTIC_2 hinged CONTINUE on — is now tested and closed.

The anchoring family **fails the gate after both moves**, exactly the trigger
condition. The trigger does not require a literal ≤0¢ point estimate (it is
"nets ≤0¢ **OR** fails the gate") — and it fails the gate decisively, while also
going ≤0¢ under the honest fee/queue stress cases.

**Diminishing returns vs compute — the decisive argument for PAUSE:**

- **The search space relevant to retail edge is exhausted.** Three markets ×
  multiple mechanism families, all run through a 0% false-positive gate: one
  efficient, one cost-artifact, one real-but-uncertifiable-and-not-taker-killed.
  There is no untested *execution* lever left (taker and maker both done) and no
  untested *data* lever (n maximal, no history).
- **Every additional dart makes certification HARDER, not easier.** The gate is
  DSR-governed against the shared ledger (`lab.governance`, currently N=3,
  V[SR]=1.0). Each new hypothesis raises the multiple-testing hurdle. Spending
  compute to hunt in efficient markets actively *raises the bar* on the one real
  residual it cannot clear anyway.
- **The honest ceiling is known.** With one season (~1,319 games), a near-zero-FP
  gate, and realistic execution, only an edge simultaneously ≳6¢ net-of-cost AND
  broad-across-halves at full-season n can certify. After the realistic re-score
  and the maker study, **nothing in this data is both.** Continuing to hunt is
  buying lottery tickets against a structural impossibility.

**Honest dissent (so the operator decides with both sides):** the spread residual
is *real* and *directionally stable under maker execution* (it newly passes the
OOS season-half check that the taker arm fails). It is not noise; it is a true
+2¢-ish maker edge that the conservative gate cannot certify at this n. An
operator with a different risk tolerance than a 0% false-positive gate could
choose to paper-trade it forward into 2026-27 to accumulate the out-of-sample n
the gate demands. That is a *capital/risk* decision, explicitly human-gated, not
a research decision — and it is a reason to keep the finding warm, not a reason
to keep 5 agents hunting.

**Decision: PAUSE the falsification fleet.** Stand down to 0 hunting agents.
Preserve the durable record. Re-arm only when the binding constraint changes —
i.e., when a *new season of live data* exists (relieves c) — at which point the
single highest-value action is to re-score the existing spread anchoring residual
on the new out-of-sample games, not to originate new hypotheses.

---

## 4. If the operator chooses CONTINUE / SCALE-DOWN instead of PAUSE

If the operator overrides PAUSE, do **not** keep 5 agents — that is the worst
option (max compute, max DSR-hurdle inflation, near-zero marginal information).
**Scale to 1 focused agent** on, at most, these remaining questions, in priority
order:

1. **Apply the validated honest-maker model to any *totals* residual** (1 agent).
   The maker FillModel (`research/scripts/maker_fill_study.py`) is now built,
   adverse-selection-aware, and validated on spreads. Totals was killed purely on
   the 0.50-fill cost artifact; it has never been examined under maker fills with
   the listed-strike snap. *Honest prior:* near-zero — totals' real residual was a
   pure artifact (−1.00¢), so there is likely nothing for maker fills to rescue,
   and adverse selection on a thin O/U ladder will be at least as bad as on
   spreads. This is a *confirmatory* run to close the totals book honestly under
   the new model, not an expected-positive hunt. Decisive either way; cheap.

2. **Cross-market consistency, only if it shows signal first** (1 agent, gated on
   a quick EDA pre-check). Does the spread anchoring residual co-move with a
   *contemporaneous* totals or winner mispricing on the same game (a tradeable
   joint signal, e.g. a basket that nets the half-spread across legs)? This is the
   only genuinely *unexplored* direction — every prior hunt was single-market.
   Gate it hard: run `lab.eda` cross-market correlation FIRST; if there is no
   raw signal, do not spend the agent. Term-structure already falsified the
   winner↔total info link (corr ≈ 0.011), so the prior here is also low, but it
   is the one stone unturned.

**Families that are EXHAUSTED and MUST NOT be re-hunted** (efficient: negative/
zero OOS or mechanistically falsified — re-mining wastes capacity AND raises the
DSR hurdle for everyone):

- Moneyline calibration / favorite-longshot / substitution-latency / fair-value-gap
- In-game moneyline momentum / continuation AND its mirror (excursion fade) — both DEAD
- Pregame CLV
- Cross-contract / winner↔total term-structure (falsified at source: corr ≈ 0.011)
- Totals-extremes (≈2–5pp bias is a half-spread/measurement artifact at extreme prices; decays Q2→Q4)
- **Spread continuation-anchoring DEEPENING via entry-timing / clean-anchoring
  filters** — every lift is the favorite-longshot regime, inverts train→val. The
  *plain* spread residual is the only honest expression and it is fully
  characterized (taker + maker both done). Do not re-deepen it; only re-score it
  on genuinely new (future-season) data.

---

## Provenance / fleet state

All five active lanes have landed durable verdicts; the execution lane (the
DIAGNOSTIC_2 follow-up) is the decisive new one. No lanes PENDING at write time.

| run file | lane | verdict |
|---|---|---|
| `runs/meta_diagnostic_20260609T085429Z.md` | meta / diagnostic | leakage CLEAN; wrote DIAGNOSTIC_2 |
| `runs/winner_originate_20260609T085815Z.md` | winner / originate | DEAD (efficient) |
| `runs/spread_adversary_20260609T085637Z.md` | spread / adversary | DEAD as taker (residual real but insignificant) |
| `runs/spread_deepen_20260609T090246Z.md` | spread / deepen | DEAD (deepening = favorite-longshot drift) |
| `runs/spreads_power_20260609T090521Z.md` | data / power | NEEDS_DATA (n maximal, ~14–27× too small, not relievable) |
| `runs/exec_makerfill_20260609T091134Z.md` | execution / maker-fill | DEAD (maker does not rescue; PAUSE trigger satisfied) |

Program tally: **0 PROMOTE, 1 real-but-uncertifiable (spread, now fully run to
ground), 1 RETRACTED-on-cost (totals), the rest DEAD/efficient.**
